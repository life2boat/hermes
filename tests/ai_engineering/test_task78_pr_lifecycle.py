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
    assert receipt.terminal_reason == "PR_CREATE_VERIFIED_RESULT_MISSING"


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
    assert receipt.terminal_reason == "PR_CREATE_VERIFIED_RESULT_MISSING"


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
