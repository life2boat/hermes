"""Exhaustive Test Suite for Task 7.8: Controlled Autonomous PR Lifecycle.

Tests:
1. Protocol & Contract Tests
2. FakeGitHubPullRequestBackend Unit Tests
3. GitHubPullRequestProvider Secret Safety & No-Admin Tests
4. Pre-PR & VerifiedResult Gate Tests
5. PolicyEngine Gate for PR Creation
6. Idempotent PR Creation & Recovery
7. WAIT_FOR_CI Head-Binding & Head Change Detection
8. PolicyEngine Gate for PR Merge
9. Fresh Merge Gate Revalidation
10. Atomic Squash Merge & Reconciliation
11. Crash & Restart Rehydration
12. Full Autonomous Run E2E Scenarios
"""

import asyncio
import datetime
import hashlib
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
import pytest

from ai_engineering.contracts import EffectClass, GateResult, Status, StopBoundary, TaskClass
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)
from ai_engineering.supervisor.autonomous_run import (
    AstraNextActionProposal,
    AutonomousRunCoordinator,
    BudgetConfig,
    NextActionType,
    ScriptedAstraProposalProvider,
)
from ai_engineering.supervisor.ci_provider import (
    CICheckResult,
    CIStatusSnapshot,
    REQUIRED_TECHNICAL_CHECKS,
)
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.events import SupervisorEventType
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.policy.contracts import (
    AutonomyLevel,
    BudgetLimits,
    ExecutionTarget,
    PromotionThresholds,
    WorkProfile,
)
from ai_engineering.supervisor.pr_provider import (
    FakeGitHubPullRequestBackend,
    GitHubPullRequestProvider,
    MergeReceipt,
    PRConflictError,
    PRHeadMismatchError,
    PRMergeFailedError,
    PRNotFoundError,
    PRProviderError,
    PRProviderUnavailableError,
    PullRequestIdentity,
    PullRequestProvider,
    PullRequestReceipt,
    PULL_REQUEST_IDENTITY_SCHEMA_VERSION,
    PULL_REQUEST_RECEIPT_SCHEMA_VERSION,
    MERGE_RECEIPT_SCHEMA_VERSION,
    compute_merge_receipt_digest,
    compute_pr_receipt_digest,
    scrub_secrets,
)
from ai_engineering.supervisor.router.adapters import CodexAdapter
from ai_engineering.supervisor.router.envelope import MessageType
from ai_engineering.supervisor.router.registry import AgentDefinition, AgentRegistry
from ai_engineering.supervisor.router.router import AuthorityResolver, CrossAgentRouter, PersistentStore
from ai_engineering.supervisor.router.transports import FakeAgentTransport
from ai_engineering.supervisor.state import SupervisorPhase, SupervisorState
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.validator import (
    VerifiedResult,
    canonical_serialize_verified_result,
    deserialize_verified_result,
)
from ai_engineering.task_intent import IntentStatus, TaskIntent, intent_digest


# ── Test Fixtures & Helpers ───────────────────────────────────────────────────

def _create_intent(
    task_id: str = "t-pr-1",
    run_id: str = "run-pr-1",
    allowed_mutations: tuple[str, ...] = ("code", "REPOSITORY_WRITE", "PR_MUTATION", "PR_MERGE"),
    stop_boundary: StopBoundary = StopBoundary.MERGE,
) -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id=task_id,
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Test controlled PR lifecycle",
        source_repository="life2boat/hermes",
        source_main_ref="refs/remotes/github/main",
        source_base_sha="a" * 40,
        constraints=(),
        allowed_mutations=allowed_mutations,
        forbidden_mutations=(),
        stop_boundary=stop_boundary,
        acceptance_criteria=(),
        unknowns=(),
        applicable_invariants=(),
        required_gates=(),
        parent_intent_digest=None,
    )


def _create_work_profile(
    profile_id: str = "test-pr-profile",
    max_level: AutonomyLevel = AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION,
    allowed_effects: tuple[EffectClass, ...] = (
        EffectClass.READ_ONLY,
        EffectClass.REPOSITORY_WRITE,
        EffectClass.PR_MUTATION,
        EffectClass.PR_MERGE,
    ),
) -> WorkProfile:
    from ai_engineering.supervisor.policy.work_profile import validate_work_profile

    raw = {
        "schema_version": "hermes.work-profile.v1",
        "profile_id": profile_id,
        "profile_version": 1,
        "preferred_worker_model": None,
        "preferred_verifier_model": None,
        "preferred_supervisor_model": None,
        "escalation_model": None,
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION"],
        "maximum_autonomy_level": max_level.value,
        "allowed_effect_classes": [e.value for e in allowed_effects],
        "forbidden_effect_classes": [],
        "allowed_targets": ["DEV", "LOCAL"],
        "required_validators": [],
        "promotion_thresholds": {
            "required_successful_runs": 1,
            "allowed_critical_failures": 0,
            "require_rollback_verified": False,
        },
        "budget_limits": {
            "max_supervisor_decisions": 20,
            "max_child_tasks": 10,
            "max_retries_per_task": 3,
            "max_fix_cycles_per_task": 3,
            "max_consecutive_failures": 2,
            "max_provider_calls": 50,
            "max_policy_denials": 10,
        },
        "production_execution_allowed": False,
        "vector_mutation_allowed": False,
        "secret_mutation_allowed": False,
        "external_send_allowed": False,
    }
    return validate_work_profile(raw)


def _create_effective_policy(intent: TaskIntent) -> EffectivePolicyReport:
    tpa = TaskPolicyAttribution(
        task_id=intent.task_id,
        intent_revision=intent.intent_revision,
        intent_digest=intent_digest(intent),
        source_base_sha=intent.source_base_sha,
        constraints=intent.constraints,
        allowed_mutations=intent.allowed_mutations,
        forbidden_mutations=intent.forbidden_mutations,
        stop_boundary=intent.stop_boundary.value,
        source_id="0" * 64,
    )
    return EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="0" * 64,
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        source_base_sha=intent.source_base_sha,
        subject_sha=intent.source_base_sha,
        status=EffectivePolicyStatus.COMPLETE,
        policy_sources=(),
        task_policy=tpa,
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="0" * 64,
        authority_expansion=False,
    )


def _create_passing_vr(
    task_id: str = "t-pr-1",
    run_id: str = "run-pr-1",
    head_sha: str = "b" * 40,
    intent_dg: str = "c" * 64,
) -> VerifiedResult:
    fixtures_dir = Path(__file__).parent / "fixtures" / "supervisor" / "task02"
    vr_text = (fixtures_dir / "supervisor_verified_result_pass.json").read_text(encoding="utf-8")
    vr_base = deserialize_verified_result(vr_text)
    return replace(
        vr_base,
        task_id=task_id,
        attempt_id=f"{task_id}-attempt-1",
        intent_digest=intent_dg,
        head_sha=head_sha,
        status=Status.PASS,
    )


class DeterministicCIProvider:
    def __init__(self, overall_status: str = "SUCCESS") -> None:
        self.overall_status = overall_status
        self.call_count = 0
        self.latest_receipt = None

    async def wait_for_ci(self, run_id: str, sha: str) -> bool:
        self.call_count += 1
        return self.overall_status == "SUCCESS"

    async def check_ci(self, run_id: str, sha: str) -> CIStatusSnapshot:
        self.call_count += 1
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        return CIStatusSnapshot(
            schema_version="hermes.ci-status-snapshot.v1",
            repository="life2boat/hermes",
            requested_sha=sha,
            observed_sha=sha,
            checked_at_utc=now,
            overall_status=self.overall_status,
            required_workflow_observations=(),
            all_required_completed=(self.overall_status in ("SUCCESS", "FAILURE")),
            all_required_success=(self.overall_status == "SUCCESS"),
            governance_observations=(),
            snapshot_digest="snap-" + sha[:16],
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Protocol & Contract Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_pr_identity_serialization_and_fields():
    pr = PullRequestIdentity(
        schema_version=PULL_REQUEST_IDENTITY_SCHEMA_VERSION,
        repository="life2boat/hermes",
        pr_number=123,
        pr_node_id="PR_kw123",
        head_branch="feat/test",
        head_sha="1" * 40,
        base_branch="main",
        base_sha="0" * 40,
        title="feat: test",
        body="body",
        is_draft=False,
        state="open",
        mergeable=True,
        mergeable_state="clean",
        merged=False,
        merged_at=None,
        merge_commit_sha=None,
        created_at_utc="2026-09-13T00:00:00Z",
        updated_at_utc="2026-09-13T00:00:00Z",
    )
    assert pr.pr_number == 123
    assert pr.head_sha == "1" * 40
    assert pr.mergeable is True
    d = asdict(pr)
    assert d["schema_version"] == PULL_REQUEST_IDENTITY_SCHEMA_VERSION


def test_pr_receipt_digest_deterministic():
    data = {
        "schema_version": PULL_REQUEST_RECEIPT_SCHEMA_VERSION,
        "run_id": "r1",
        "task_id": "t1",
        "policy_receipt_id": "pol-1",
        "repository": "life2boat/hermes",
        "pr_number": 123,
        "head_branch": "feat/t1",
        "head_sha": "a" * 40,
        "base_branch": "main",
        "base_sha": "0" * 40,
        "action": "CREATED",
        "created_at_utc": "2026-09-13T00:00:00Z",
    }
    dg1 = compute_pr_receipt_digest(data)
    dg2 = compute_pr_receipt_digest(data)
    assert dg1 == dg2
    assert len(dg1) == 64


def test_merge_receipt_digest_deterministic():
    data = {
        "schema_version": MERGE_RECEIPT_SCHEMA_VERSION,
        "run_id": "r1",
        "task_id": "t1",
        "policy_receipt_id": "pol-1",
        "repository": "life2boat/hermes",
        "pr_number": 123,
        "head_sha": "a" * 40,
        "base_sha": "0" * 40,
        "merged_commit_sha": "m" * 40,
        "merge_method": "squash",
        "merged_at_utc": "2026-09-13T00:00:00Z",
    }
    dg1 = compute_merge_receipt_digest(data)
    dg2 = compute_merge_receipt_digest(data)
    assert dg1 == dg2
    assert len(dg1) == 64


def test_secret_scrubbing():
    token1 = "ghp_1234567890abcdefghijklmnopqrstuvwxyz"
    token2 = "github_pat_1234567890abcdefghijklmnopqrstuvwxyz_0987654321"
    auth_bearer = "Bearer secret_jwt_token_here_12345"
    raw = f"Failed with {token1} and {token2} and {auth_bearer}"
    scrubbed = scrub_secrets(raw)
    assert token1 not in scrubbed
    assert token2 not in scrubbed
    assert "secret_jwt_token" not in scrubbed
    assert "[REDACTED]" in scrubbed


def test_pr_exceptions_scrub_secrets():
    token = "ghp_SECRET_TOKEN_DO_NOT_LEAK_999999"
    err = PRProviderError(f"Connection failed with {token}")
    assert token not in str(err)
    assert "[REDACTED]" in str(err)
    assert err.code == "PR_PROVIDER_ERROR"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. FakeGitHubPullRequestBackend Unit Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fake_create_and_get_pr():
    backend = FakeGitHubPullRequestBackend()
    pr = await backend.create_pr(
        repository="life2boat/hermes",
        head_branch="feat/test-1",
        base_branch="main",
        title="feat: test 1",
        body="testing",
        draft=False,
    )
    assert pr.pr_number == 100
    assert pr.head_branch == "feat/test-1"
    assert pr.state == "open"
    assert not pr.is_draft

    fetched = await backend.get_pr("life2boat/hermes", 100)
    assert fetched.pr_number == 100
    assert fetched.title == "feat: test 1"


@pytest.mark.asyncio
async def test_fake_get_pr_not_found():
    backend = FakeGitHubPullRequestBackend()
    with pytest.raises(PRNotFoundError) as exc_info:
        await backend.get_pr("life2boat/hermes", 999)
    assert exc_info.value.code == "PR_NOT_FOUND"


@pytest.mark.asyncio
async def test_fake_find_existing_pr():
    backend = FakeGitHubPullRequestBackend()
    pr = await backend.create_pr(
        repository="life2boat/hermes",
        head_branch="feat/test-2",
        base_branch="main",
        title="feat: test 2",
        body="testing",
    )
    found = await backend.find_existing_pr("life2boat/hermes", "feat/test-2", "main")
    assert found is not None
    assert found.pr_number == pr.pr_number

    # Different branch returns None
    not_found = await backend.find_existing_pr("life2boat/hermes", "feat/other", "main")
    assert not_found is None


@pytest.mark.asyncio
async def test_fake_merge_atomic_squash_success():
    backend = FakeGitHubPullRequestBackend()
    pr = await backend.create_pr("life2boat/hermes", "feat/m1", "main", "title", "body")
    receipt = await backend.merge_pr(
        repository="life2boat/hermes",
        pr_number=pr.pr_number,
        exact_head_sha=pr.head_sha,
        merge_method="squash",
    )
    assert receipt.pr_number == pr.pr_number
    assert receipt.head_sha == pr.head_sha
    assert len(receipt.merged_commit_sha) == 40

    updated = await backend.get_pr("life2boat/hermes", pr.pr_number)
    assert updated.merged is True
    assert updated.state == "closed"
    assert updated.merge_commit_sha == receipt.merged_commit_sha


@pytest.mark.asyncio
async def test_fake_merge_head_sha_mismatch():
    backend = FakeGitHubPullRequestBackend()
    pr = await backend.create_pr("life2boat/hermes", "feat/m2", "main", "title", "body")
    with pytest.raises(PRHeadMismatchError) as exc_info:
        await backend.merge_pr("life2boat/hermes", pr.pr_number, exact_head_sha="0" * 40)
    assert exc_info.value.code == "PR_HEAD_MISMATCH"


@pytest.mark.asyncio
async def test_fake_merge_conflict():
    backend = FakeGitHubPullRequestBackend()
    pr = await backend.create_pr("life2boat/hermes", "feat/m3", "main", "title", "body")
    backend.simulate_conflict(pr.pr_number)
    with pytest.raises(PRConflictError) as exc_info:
        await backend.merge_pr("life2boat/hermes", pr.pr_number, exact_head_sha=pr.head_sha)
    assert exc_info.value.code == "PR_CONFLICT"


@pytest.mark.asyncio
async def test_fake_merge_draft_fails():
    backend = FakeGitHubPullRequestBackend()
    pr = await backend.create_pr("life2boat/hermes", "feat/m4", "main", "title", "body", draft=True)
    with pytest.raises(PRMergeFailedError) as exc_info:
        await backend.merge_pr("life2boat/hermes", pr.pr_number, exact_head_sha=pr.head_sha)
    assert exc_info.value.code == "PR_IS_DRAFT"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. GitHubPullRequestProvider Secret Safety & No-Admin Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_github_provider_initialization():
    prov = GitHubPullRequestProvider(token="test-token-xyz", timeout_seconds=10.0)
    headers = prov._get_headers()
    assert headers["Authorization"] == "Bearer test-token-xyz"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_github_provider_scrubs_secrets_in_http_error(monkeypatch):
    prov = GitHubPullRequestProvider(token="ghp_SUPER_SECRET_TOKEN_1234567890")

    def mock_urlopen(*args, **kwargs):
        import urllib.error
        from io import BytesIO
        fp = BytesIO(b'{"message":"Not Found with ghp_SUPER_SECRET_TOKEN_1234567890"}')
        raise urllib.error.HTTPError("https://api.github.com/repos/test/pulls/1", 404, "Not Found", {}, fp)

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    with pytest.raises(PRNotFoundError) as exc_info:
        prov._sync_request("GET", "https://api.github.com/repos/test/pulls/1")
    assert "ghp_SUPER_SECRET_TOKEN" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)
    assert exc_info.value.code == "PR_NOT_FOUND"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Pre-PR & VerifiedResult Gate Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_pr_blocked_if_no_verified_result(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-nopr", "run-nopr")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-nopr", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-nopr", wp, ep)

    backend = FakeGitHubPullRequestBackend()
    proposal_provider = ScriptedAstraProposalProvider(
        scripted_actions=[NextActionType.CREATE_PR]
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-nopr",
        astra_provider=proposal_provider,
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason in ("PR_CREATE_NOT_EVIDENCED", "PR_CREATE_VERIFIED_RESULT_MISSING")


@pytest.mark.asyncio
async def test_create_pr_blocked_if_verified_result_fails(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-failvr", "run-failvr")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-failvr", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-failvr", wp, ep)

    # Ingest a FAIL VerifiedResult
    failing_vr = replace(_create_passing_vr("t-failvr", "run-failvr", intent_dg=intent_digest(intent)), status=Status.FAIL)
    loop.ingest_verified_result("run-failvr", failing_vr, "dg-fail")

    backend = FakeGitHubPullRequestBackend()
    proposal_provider = ScriptedAstraProposalProvider(
        scripted_actions=[NextActionType.CREATE_PR]
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-failvr",
        astra_provider=proposal_provider,
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason in ("PR_CREATE_NOT_EVIDENCED", "PR_CREATE_VERIFIED_RESULT_MISSING")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. PolicyEngine Gate for PR Creation
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_pr_denied_if_not_in_intent(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    # TaskIntent excludes PR_MUTATION
    intent = _create_intent(
        "t-pol-deny",
        "run-pol-deny",
        allowed_mutations=("code", "REPOSITORY_WRITE"),
        stop_boundary=StopBoundary.LOCAL_DIFF,
    )
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-pol-deny", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-pol-deny", wp, ep)

    # Ingest PASS VR
    vr = _create_passing_vr("t-pol-deny", "run-pol-deny", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-pol-deny", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    proposal_provider = ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR])

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-pol-deny",
        astra_provider=proposal_provider,
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert "POLICY_DENIED" in receipt.terminal_reason
    assert "TASK_INTENT_AUTHORITY_DENIED" in receipt.terminal_reason


@pytest.mark.asyncio
async def test_create_pr_denied_if_autonomy_level_too_low(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-lvl-low", "run-lvl-low")
    # WorkProfile max autonomy level = LEVEL_1_LOCAL_WRITE
    wp = _create_work_profile(max_level=AutonomyLevel.LEVEL_1_LOCAL_WRITE)
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-lvl-low", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-lvl-low", wp, ep)

    vr = _create_passing_vr("t-lvl-low", "run-lvl-low", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-lvl-low", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-lvl-low",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert "POLICY_DENIED" in receipt.terminal_reason
    assert "AUTONOMY_LEVEL_TOO_LOW" in receipt.terminal_reason


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Idempotent PR Creation & Recovery Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_pr_creates_new_pr_and_receipt(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-create-pr", "run-create-pr")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-create-pr", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-create-pr", wp, ep)

    vr = _create_passing_vr("t-create-pr", "run-create-pr", head_sha="1111" * 10, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-create-pr", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-create-pr",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR, NextActionType.STOP_SUCCESS]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert len(receipt.pr_receipts) == 1
    assert receipt.pr_number == 100

    events = store.load_events("run-create-pr")
    ev_types = [e.event_type.value for e in events]
    assert "PR_CREATE_REQUESTED" in ev_types
    assert "PR_CREATED" in ev_types


@pytest.mark.asyncio
async def test_create_pr_recovers_existing_open_pr(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-rec-pr", "run-rec-pr")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-rec-pr", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-rec-pr", wp, ep)

    vr = _create_passing_vr("t-rec-pr", "run-rec-pr", head_sha="2222" * 10, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-rec-pr", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    # Pre-create an open PR matching head_branch "feat/t-rec-pr"
    existing = await backend.create_pr("life2boat/hermes", "feat/t-rec-pr", "main", "existing title", "body")
    backend.simulate_head_change(existing.pr_number, "2222" * 10)

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-rec-pr",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR, NextActionType.STOP_SUCCESS]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert receipt.pr_number == existing.pr_number
    # Did NOT create a second PR
    assert len(backend._prs) == 1

    events = store.load_events("run-rec-pr")
    ev_types = [e.event_type.value for e in events]
    assert "PR_RECOVERED" in ev_types
    assert "PR_CREATED" not in ev_types


# ═══════════════════════════════════════════════════════════════════════════════
# 7. WAIT_FOR_CI Head-Binding & Head Change Detection Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_wait_for_ci_targets_pr_head_sha(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-ci-target", "run-ci-target")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-ci-target", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-ci-target", wp, ep)

    vr = _create_passing_vr("t-ci-target", "run-ci-target", head_sha="3333" * 10, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-ci-target", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    ci_provider = DeterministicCIProvider(overall_status="SUCCESS")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-ci-target",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.STOP_SUCCESS]
        ),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"

    events = store.load_events("run-ci-target")
    ev_types = [e.event_type.value for e in events]
    assert "CI_WAIT_STARTED" in ev_types
    assert "CI_GREEN" in ev_types

    ci_start_ev = next(e for e in events if e.event_type.value == "CI_WAIT_STARTED")
    assert ci_start_ev.payload["exact_sha"] == "3333" * 10


@pytest.mark.asyncio
async def test_wait_for_ci_detects_pr_head_changed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-head-chg", "run-head-chg")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-head-chg", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-head-chg", wp, ep)

    vr = _create_passing_vr("t-head-chg", "run-head-chg", head_sha="4444" * 10, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-head-chg", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    # Hook proposal to simulate head change right before WAIT_FOR_CI
    class HeadChangingAstra(ScriptedAstraProposalProvider):
        async def request_proposal(self, run_id, state, **kwargs):
            prop = await super().request_proposal(run_id, state, **kwargs)
            if prop.action_type == NextActionType.WAIT_FOR_CI:
                backend.simulate_head_change(100, "9999" * 10)
            return prop

    provider = HeadChangingAstra(
        scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI]
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-head-chg",
        astra_provider=provider,
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_HEAD_CHANGED"

    events = store.load_events("run-head-chg")
    ev_types = [e.event_type.value for e in events]
    assert "PR_HEAD_CHANGED" in ev_types


# ═══════════════════════════════════════════════════════════════════════════════
# 8. PolicyEngine Gate for PR Merge Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_merge_denied_if_pr_merge_not_in_intent(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    # Intent includes PR_MUTATION but NOT PR_MERGE
    intent = _create_intent(
        "t-nomerge-intent",
        "run-nomerge-intent",
        allowed_mutations=("code", "REPOSITORY_WRITE", "PR_MUTATION"),
        stop_boundary=StopBoundary.READY_PR,
    )
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-nomerge-intent", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-nomerge-intent", wp, ep)

    vr = _create_passing_vr("t-nomerge-intent", "run-nomerge-intent", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-nomerge-intent", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-nomerge-intent",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert "POLICY_DENIED" in receipt.terminal_reason
    assert "TASK_INTENT_AUTHORITY_DENIED" in receipt.terminal_reason


@pytest.mark.asyncio
async def test_merge_denied_if_autonomy_level_too_low(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-nomerge-lvl", "run-nomerge-lvl")
    # LEVEL_3_CI_AUTONOMY ceiling is READY_PR (rank 4). MERGE is rank 5 -> DENIED
    wp = _create_work_profile(max_level=AutonomyLevel.LEVEL_3_CI_AUTONOMY)
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-nomerge-lvl", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-nomerge-lvl", wp, ep)

    vr = _create_passing_vr("t-nomerge-lvl", "run-nomerge-lvl", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-nomerge-lvl", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-nomerge-lvl",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert "POLICY_DENIED" in receipt.terminal_reason
    assert "AUTONOMY_LEVEL_TOO_LOW" in receipt.terminal_reason


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Fresh Merge Gate Revalidation Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_merge_fails_if_merge_conflicts(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-conflict", "run-conflict")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-conflict", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-conflict", wp, ep)

    vr = _create_passing_vr("t-conflict", "run-conflict", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-conflict", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    class ConflictAstra(ScriptedAstraProposalProvider):
        async def request_proposal(self, run_id, state, **kwargs):
            prop = await super().request_proposal(run_id, state, **kwargs)
            if prop.action_type == NextActionType.MERGE_IF_GREEN:
                backend.simulate_conflict(100)
            return prop

    provider = ConflictAstra(
        scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-conflict",
        astra_provider=provider,
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_CONFLICT"


@pytest.mark.asyncio
async def test_merge_fails_if_ci_not_green(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-noci", "run-noci")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-noci", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-noci", wp, ep)

    vr = _create_passing_vr("t-noci", "run-noci", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-noci", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    # Skip WAIT_FOR_CI and go directly to MERGE_IF_GREEN
    provider = ScriptedAstraProposalProvider(
        scripted_actions=[NextActionType.CREATE_PR, NextActionType.MERGE_IF_GREEN]
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-noci",
        astra_provider=provider,
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "CI_NOT_GREEN"


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Atomic Squash Merge & Reconciliation Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_controlled_pr_lifecycle_e2e(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-full-e2e", "run-full-e2e")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-full-e2e", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-full-e2e", wp, ep)

    vr = _create_passing_vr("t-full-e2e", "run-full-e2e", head_sha="5555" * 10, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-full-e2e", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    ci_provider = DeterministicCIProvider()

    provider = ScriptedAstraProposalProvider(
        scripted_actions=[
            NextActionType.CREATE_PR,
            NextActionType.WAIT_FOR_CI,
            NextActionType.MERGE_IF_GREEN,
        ]
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-full-e2e",
        astra_provider=provider,
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert len(receipt.pr_receipts) == 1
    assert len(receipt.merge_receipts) == 1
    assert receipt.pr_number == 100
    assert receipt.merged_commit_sha is not None
    assert receipt.final_source_sha == receipt.merged_commit_sha

    # Verify PR state on provider
    pr = await backend.get_pr("life2boat/hermes", 100)
    assert pr.merged is True
    assert pr.state == "closed"

    # Verify event lineage
    events = store.load_events("run-full-e2e")
    ev_types = [e.event_type.value for e in events]
    assert "PR_CREATE_REQUESTED" in ev_types
    assert "PR_CREATED" in ev_types
    assert "CI_WAIT_STARTED" in ev_types
    assert "CI_GREEN" in ev_types
    assert "PR_MERGE_REQUESTED" in ev_types
    assert "PR_MERGED" in ev_types
    assert "SOURCE_RECONCILED" in ev_types


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Crash & Restart Rehydration Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_restart_rehydration_after_pr_create(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-crash-rec", "run-crash-rec")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-crash-rec", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-crash-rec", wp, ep)

    vr = _create_passing_vr("t-crash-rec", "run-crash-rec", head_sha="6666" * 10, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-crash-rec", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    # Step 1: Run only CREATE_PR
    coord1 = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-crash-rec",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    r1 = await coord1.run_until_terminal()
    assert r1.terminal_reason == "BUDGET_EXHAUSTED"
    assert r1.pr_number == 100

    # Step 2: "Crash" and re-instantiate fresh coordinator from same store
    store2 = FileSupervisorStateStore(tmp_path)
    loop2 = SupervisorLoop(store2)
    coord2 = AutonomousRunCoordinator(
        loop=loop2,
        store=store2,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store2, loop2), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=10),
        run_id="run-crash-rec",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    # Verify rehydration in __init__
    assert coord2.active_pr_number == 100
    assert coord2.active_pr_head_sha == "6666" * 10
    assert len(coord2.pr_receipts) == 1

    # Continue run to completion
    r2 = await coord2.run_until_terminal()
    assert r2.terminal_reason == "GOAL_COMPLETE"
    assert r2.pr_number == 100
    assert len(r2.merge_receipts) == 1
    assert r2.merged_commit_sha is not None


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Non-Production Mode & Permission Denials
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_pr_disallowed_when_flag_false(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-noflag-create", "run-noflag-create")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-noflag-create", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-noflag-create", wp, ep)

    vr = _create_passing_vr("t-noflag-create", "run-noflag-create", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-noflag-create", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-noflag-create",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=False,  # Disallowed
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_CREATION_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_merge_disallowed_when_flag_false(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-noflag-merge", "run-noflag-merge")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-noflag-merge", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-noflag-merge", wp, ep)

    vr = _create_passing_vr("t-noflag-merge", "run-noflag-merge", intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-noflag-merge", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-noflag-merge",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=False,  # Disallowed
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_MERGE_NOT_ALLOWED"


# ═══════════════════════════════════════════════════════════════════════════════
# 13. Section 23 Detailed Unit & Edge Case Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_candidate_head_not_published_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-head-not-pub", "run-head-not-pub")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-head-not-pub", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-head-not-pub", wp, ep)

    vr = _create_passing_vr("t-head-not-pub", "run-head-not-pub", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-head-not-pub", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    backend.auto_seed_remote_head = False  # Head not published!

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-head-not-pub",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_HEAD_NOT_PUBLISHED"
    events = store.load_events("run-head-not-pub")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "PR_HEAD_NOT_PUBLISHED" for e in events)


@pytest.mark.asyncio
async def test_remote_head_sha_mismatch_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-head-mismatch", "run-head-mismatch")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-head-mismatch", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-head-mismatch", wp, ep)

    vr = _create_passing_vr("t-head-mismatch", "run-head-mismatch", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-head-mismatch", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    backend.auto_seed_remote_head = False
    # Set remote head to different sha
    backend.set_remote_head("life2boat/hermes", "feat/t-head-mismatch", "9" * 40)

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-head-mismatch",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_HEAD_SHA_MISMATCH"
    events = store.load_events("run-head-mismatch")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "PR_HEAD_SHA_MISMATCH" for e in events)


@pytest.mark.asyncio
async def test_create_pr_exact_identity_mismatch_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-id-mismatch", "run-id-mismatch")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-id-mismatch", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-id-mismatch", wp, ep)

    vr = _create_passing_vr("t-id-mismatch", "run-id-mismatch", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-id-mismatch", vr, "vr-dg")

    class MismatchCreateBackend(FakeGitHubPullRequestBackend):
        async def create_pr(self, *args, **kwargs):
            raise PRProviderError("PR_IDENTITY_MISMATCH: base_branch mismatch", code="PR_IDENTITY_MISMATCH")

    backend = MismatchCreateBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-id-mismatch",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_IDENTITY_MISMATCH"
    events = store.load_events("run-id-mismatch")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "PR_IDENTITY_MISMATCH" for e in events)


@pytest.mark.asyncio
async def test_strict_recovery_head_sha_mismatch_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-rec-mismatch", "run-rec-mismatch")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-rec-mismatch", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-rec-mismatch", wp, ep)

    vr = _create_passing_vr("t-rec-mismatch", "run-rec-mismatch", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-rec-mismatch", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    # Create PR with mismatched head_sha
    await backend.create_pr("life2boat/hermes", "feat/t-rec-mismatch", "main", "existing", "body", head_sha="e" * 40)
    # Ensure remote head has candidate head sha so pre-check passes
    backend.set_remote_head("life2boat/hermes", "feat/t-rec-mismatch", "b" * 40)

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-rec-mismatch",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_HEAD_SHA_MISMATCH"
    events = store.load_events("run-rec-mismatch")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "PR_HEAD_SHA_MISMATCH" for e in events)


@pytest.mark.asyncio
async def test_recovery_ambiguous_prs_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-ambig", "run-ambig")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-ambig", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-ambig", wp, ep)

    vr = _create_passing_vr("t-ambig", "run-ambig", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-ambig", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    pr1 = await backend.create_pr("life2boat/hermes", "feat/t-ambig", "main", "pr1", "body1", head_sha="b" * 40)
    # Force two PRs with same head branch
    backend._prs[101] = replace(pr1, pr_number=101, pr_node_id="PR_101")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-ambig",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_IDENTITY_AMBIGUOUS"
    events = store.load_events("run-ambig")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "PR_IDENTITY_AMBIGUOUS" for e in events)


@pytest.mark.asyncio
async def test_canonical_decision_path_create_pr_has_genuine_receipts(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-can-create", "run-can-create")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-can-create", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-can-create", wp, ep)

    vr = _create_passing_vr("t-can-create", "run-can-create", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-can-create", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-can-create",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert len(coord.decision_receipts) >= 1
    assert len(coord.policy_receipts) >= 1
    events = store.load_events("run-can-create")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "DECISION_ACCEPTED" for e in events)


@pytest.mark.asyncio
async def test_canonical_decision_path_merge_has_genuine_receipts(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-can-merge", "run-can-merge")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-can-merge", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-can-merge", wp, ep)

    vr = _create_passing_vr("t-can-merge", "run-can-merge", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-can-merge", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-can-merge",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert len(coord.decision_receipts) >= 2
    assert len(coord.policy_receipts) >= 2
    assert backend.latest_merge_receipt is not None
    assert backend.latest_merge_receipt.decision_receipt_id != ""
    assert not backend.latest_merge_receipt.decision_receipt_id.startswith("rec-prop-")
    assert backend.latest_merge_receipt.policy_receipt_id != ""


@pytest.mark.asyncio
async def test_pr_refetch_failure_during_ci_wait_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-refetch-fail", "run-refetch-fail")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-refetch-fail", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-refetch-fail", wp, ep)

    vr = _create_passing_vr("t-refetch-fail", "run-refetch-fail", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-refetch-fail", vr, "vr-dg")

    class FailingGetPRBackend(FakeGitHubPullRequestBackend):
        def __init__(self):
            super().__init__()
            self.fail_get = False

        async def get_pr(self, repository: str, pr_number: int) -> PullRequestIdentity:
            if self.fail_get:
                raise PRProviderUnavailableError("Network timeout re-fetching PR", code="PR_IDENTITY_UNAVAILABLE")
            return await super().get_pr(repository, pr_number)

    backend = FailingGetPRBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-refetch-fail",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    await coord.run_until_terminal()
    assert coord.active_pr_number == 100

    backend.fail_get = True

    coord2 = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=5),
        run_id="run-refetch-fail",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.WAIT_FOR_CI]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    receipt = await coord2.run_until_terminal()
    assert receipt.terminal_reason == "PR_IDENTITY_UNAVAILABLE"
    events = store.load_events("run-refetch-fail")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "PR_IDENTITY_UNAVAILABLE" for e in events)


@pytest.mark.asyncio
async def test_ci_events_bind_exact_pr_identity(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-ci-bind", "run-ci-bind")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-ci-bind", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-ci-bind", wp, ep)

    vr = _create_passing_vr("t-ci-bind", "run-ci-bind", head_sha="c" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-ci-bind", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-ci-bind",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    await coord.run_until_terminal()
    events = store.load_events("run-ci-bind")
    wait_start = next(e for e in events if getattr(e.event_type, "value", str(e.event_type)) == "CI_WAIT_STARTED")
    ci_green = next(e for e in events if getattr(e.event_type, "value", str(e.event_type)) == "CI_GREEN")

    assert wait_start.payload["pr_number"] == 100
    assert wait_start.payload["pr_head_sha"] == "c" * 40
    assert wait_start.payload["exact_sha"] == "c" * 40

    assert ci_green.payload["pr_number"] == 100
    assert ci_green.payload["pr_head_sha"] == "c" * 40
    assert ci_green.payload["exact_sha"] == "c" * 40


@pytest.mark.asyncio
async def test_fresh_ci_qualification_revalidates_provider(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-fresh-ci", "run-fresh-ci")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-fresh-ci", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-fresh-ci", wp, ep)

    vr = _create_passing_vr("t-fresh-ci", "run-fresh-ci", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-fresh-ci", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    ci_provider = DeterministicCIProvider(overall_status="SUCCESS")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=2),
        run_id="run-fresh-ci",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI]
        ),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    await coord.run_until_terminal()

    ci_provider.overall_status = "FAILURE"

    coord2 = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=5),
        run_id="run-fresh-ci",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )
    receipt = await coord2.run_until_terminal()
    assert receipt.terminal_reason == "CI_NOT_GREEN"
    assert backend.latest_merge_receipt is None


@pytest.mark.asyncio
async def test_fresh_ci_qualification_requires_all_technical_checks(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-tech-chk", "run-tech-chk")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-tech-chk", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-tech-chk", wp, ep)

    vr = _create_passing_vr("t-tech-chk", "run-tech-chk", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-tech-chk", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    class FailingTechnicalWorkflowCIProvider(DeterministicCIProvider):
        async def check_ci(self, run_id: str, sha: str) -> CIStatusSnapshot:
            snap = await super().check_ci(run_id, sha)
            from ai_engineering.supervisor.ci_provider import WorkflowObservation
            obs = (
                WorkflowObservation(workflow_name="Tests", run_id=1, head_sha=sha, status="completed", conclusion="failure"),
                WorkflowObservation(workflow_name="Typecheck", run_id=2, head_sha=sha, status="completed", conclusion="success"),
            )
            return replace(snap, required_workflow_observations=obs, all_required_success=False)

    ci_provider = FailingTechnicalWorkflowCIProvider(overall_status="SUCCESS")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-tech-chk",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "CI_NOT_GREEN"
    assert backend.latest_merge_receipt is None


@pytest.mark.asyncio
async def test_main_advancement_before_qualification_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-adv-before-qual", "run-adv-before-qual")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-adv-before-qual", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-adv-before-qual", wp, ep)

    vr = _create_passing_vr("t-adv-before-qual", "run-adv-before-qual", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-adv-before-qual", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=2),
        run_id="run-adv-before-qual",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    await coord.run_until_terminal()

    backend.set_remote_head("life2boat/hermes", "main", "9" * 40)

    coord2 = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=5),
        run_id="run-adv-before-qual",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )
    receipt = await coord2.run_until_terminal()
    assert receipt.terminal_reason == "MAIN_ADVANCED_DURING_QUALIFICATION"
    assert backend.latest_merge_receipt is None


@pytest.mark.asyncio
async def test_main_advancement_before_merge_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-adv-before-merge", "run-adv-before-merge")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-adv-before-merge", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-adv-before-merge", wp, ep)

    vr = _create_passing_vr("t-adv-before-merge", "run-adv-before-merge", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-adv-before-merge", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    class AdvanceMainDuringCIProvider(DeterministicCIProvider):
        def __init__(self, backend):
            super().__init__()
            self.backend = backend

        async def check_ci(self, run_id: str, sha: str) -> CIStatusSnapshot:
            snap = await super().check_ci(run_id, sha)
            self.backend.set_remote_head("life2boat/hermes", "main", "8" * 40)
            return snap

    ci_provider = AdvanceMainDuringCIProvider(backend)

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-adv-before-merge",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "MAIN_ADVANCED_DURING_QUALIFICATION"
    assert backend.latest_merge_receipt is None


@pytest.mark.asyncio
async def test_mergeable_state_unknown_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-mergeable-unk", "run-mergeable-unk")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-mergeable-unk", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-mergeable-unk", wp, ep)

    vr = _create_passing_vr("t-mergeable-unk", "run-mergeable-unk", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-mergeable-unk", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=2),
        run_id="run-mergeable-unk",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    await coord.run_until_terminal()

    backend._prs[100] = replace(backend._prs[100], mergeable=None, mergeable_state="unknown")

    coord2 = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=5),
        run_id="run-mergeable-unk",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )
    receipt = await coord2.run_until_terminal()
    assert receipt.terminal_reason == "PR_MERGEABLE_UNKNOWN"


@pytest.mark.asyncio
async def test_merge_receipt_binds_all_fifteen_fields_and_digest(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-fifteen", "run-fifteen")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-fifteen", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-fifteen", wp, ep)

    vr = _create_passing_vr("t-fifteen", "run-fifteen", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-fifteen", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-fifteen",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"

    m_rcpt = backend.latest_merge_receipt
    assert m_rcpt is not None
    assert m_rcpt.schema_version == MERGE_RECEIPT_SCHEMA_VERSION
    assert m_rcpt.receipt_id.startswith("merge-rcpt-100-")
    assert m_rcpt.run_id == "run-fifteen"
    assert m_rcpt.task_id == "t-fifteen"
    assert m_rcpt.attempt_id != ""
    assert m_rcpt.policy_receipt_id != ""
    assert m_rcpt.decision_receipt_id != ""
    assert m_rcpt.repository == "life2boat/hermes"
    assert m_rcpt.pr_number == 100
    assert m_rcpt.base_sha_before_merge != ""
    assert m_rcpt.pr_head_sha == "b" * 40
    assert m_rcpt.ci_snapshot_digest != ""
    assert m_rcpt.qualification_main_sha != ""
    assert m_rcpt.merge_method == "squash"
    assert m_rcpt.merge_commit_sha != ""
    assert m_rcpt.merged_at_utc != ""
    assert m_rcpt.receipt_digest != ""

    assert m_rcpt.head_sha == m_rcpt.pr_head_sha
    assert m_rcpt.base_sha == m_rcpt.base_sha_before_merge
    assert m_rcpt.merged_commit_sha == m_rcpt.merge_commit_sha


@pytest.mark.asyncio
async def test_post_merge_attestation_verifies_pr_and_main(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-post-attest", "run-post-attest")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-post-attest", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-post-attest", wp, ep)

    vr = _create_passing_vr("t-post-attest", "run-post-attest", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-post-attest", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-post-attest",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"

    post_pr = await backend.get_pr("life2boat/hermes", 100)
    assert post_pr.merged is True
    assert post_pr.state == "closed"
    assert post_pr.merge_commit_sha == receipt.merged_commit_sha

    remote_main = await backend.get_remote_head_sha("life2boat/hermes", "main")
    assert remote_main == receipt.merged_commit_sha


@pytest.mark.asyncio
async def test_post_merge_attestation_failure_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-post-fail", "run-post-fail")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-post-fail", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-post-fail", wp, ep)

    vr = _create_passing_vr("t-post-fail", "run-post-fail", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-post-fail", vr, "vr-dg")

    class BrokenPostAttestationBackend(FakeGitHubPullRequestBackend):
        async def merge_pr(self, *args, **kwargs):
            rcpt = await super().merge_pr(*args, **kwargs)
            pr = self._prs[rcpt.pr_number]
            self._prs[rcpt.pr_number] = replace(pr, state="open")
            return rcpt

    backend = BrokenPostAttestationBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-post-fail",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "POST_MERGE_ATTESTATION_FAILED"
    events = store.load_events("run-post-fail")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "POST_MERGE_ATTESTATION_FAILED" for e in events)
    assert not any(getattr(e.event_type, "value", str(e.event_type)) == "SOURCE_RECONCILED" for e in events)


@pytest.mark.asyncio
async def test_source_reconciled_emitted_only_after_attestation(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-order", "run-order")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-order", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-order", wp, ep)

    vr = _create_passing_vr("t-order", "run-order", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-order", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-order",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"

    events = store.load_events("run-order")
    ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]

    assert "PR_MERGED" in ev_types
    assert "SOURCE_RECONCILED" in ev_types
    merged_idx = ev_types.index("PR_MERGED")
    recon_idx = ev_types.index("SOURCE_RECONCILED")
    assert merged_idx < recon_idx


@pytest.mark.asyncio
async def test_create_timeout_recovers_via_unknown_result(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-to-create", "run-to-create")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-to-create", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-to-create", wp, ep)

    vr = _create_passing_vr("t-to-create", "run-to-create", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-to-create", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()
    backend.simulate_create_timeout = True

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-to-create",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )

    receipt = await coord.run_until_terminal()
    assert coord.active_pr_number == 100
    events = store.load_events("run-to-create")
    ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
    assert "PR_MUTATION_RESULT_UNKNOWN" in ev_types
    assert "PR_CREATED" in ev_types


@pytest.mark.asyncio
async def test_merge_timeout_recovers_via_unknown_result(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-to-merge", "run-to-merge")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-to-merge", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-to-merge", wp, ep)

    vr = _create_passing_vr("t-to-merge", "run-to-merge", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-to-merge", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=2),
        run_id="run-to-merge",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    await coord.run_until_terminal()

    backend.simulate_merge_timeout = True

    coord2 = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=5),
        run_id="run-to-merge",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    receipt = await coord2.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert receipt.merged_commit_sha is not None
    events = store.load_events("run-to-merge")
    ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
    assert "MERGE_RESULT_UNKNOWN" in ev_types
    assert "SOURCE_RECONCILED" in ev_types


@pytest.mark.asyncio
async def test_restart_rehydrates_merged_pr_without_remerging(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-remerge-rehyd", "run-remerge-rehyd")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-remerge-rehyd", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-remerge-rehyd", wp, ep)

    vr = _create_passing_vr("t-remerge-rehyd", "run-remerge-rehyd", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-remerge-rehyd", vr, "vr-dg")

    backend = FakeGitHubPullRequestBackend()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-remerge-rehyd",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.CREATE_PR]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
    )
    r1 = await coord.run_until_terminal()
    assert r1.terminal_reason == "BUDGET_EXHAUSTED"
    assert coord.active_pr_number == 100

    # Simulate PR merged externally while supervisor was offline
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    backend._prs[100] = replace(
        backend._prs[100],
        state="closed",
        merged=True,
        merged_at=now,
        merge_commit_sha="m" * 40,
    )

    merge_call_count_before = backend.merge_call_count

    store2 = FileSupervisorStateStore(tmp_path)
    loop2 = SupervisorLoop(store2)
    coord2 = AutonomousRunCoordinator(
        loop=loop2,
        store=store2,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store2, loop2), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=5),
        run_id="run-remerge-rehyd",
        astra_provider=ScriptedAstraProposalProvider(
            scripted_actions=[NextActionType.MERGE_IF_GREEN]
        ),
        ci_provider=DeterministicCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=backend,
        allow_pr_create=True,
        allow_pr_merge=True,
    )

    r2 = await coord2.run_until_terminal()
    assert r2.terminal_reason == "GOAL_COMPLETE"
    assert backend.merge_call_count == merge_call_count_before
    events = store2.load_events("run-remerge-rehyd")
    assert any(getattr(e.event_type, "value", str(e.event_type)) == "PR_MERGE_RECOVERED" for e in events)
