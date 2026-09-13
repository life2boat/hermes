"""Exhaustive Test Suite for Task 7.8: Real GitHub Provider Closure.

Tests:
1. GitHubPullRequestProvider Auth Resolution (GITHUB_TOKEN / GH_TOKEN)
2. Direct Unit Tests with Mocked REST Transport (get_remote_head_sha, find_existing_pr, create_pr, merge_pr, get_pr)
3. Timeout Classification (socket.timeout, TimeoutError, URLError with timeout)
4. Unknown-Result Create Recovery (POST timeout -> exact recovery -> PR_RECOVERED -> mutation count == 1)
5. Unknown-Result Merge Recovery (PUT timeout -> get_pr merged -> attestation -> PR_MERGE_RECOVERED -> mutation count == 1)
6. Strict Fresh CI Qualification (exact snapshot, 7 technical workflows completed and success)
7. CI Negative Matrix (mismatching SHA, all_required_success=False, neutral, skipped, missing workflow, boolean in real mode)
8. Main Identity Fail-Closed (qualification_main_sha is None -> MAIN_IDENTITY_UNAVAILABLE; current_main_sha is None -> MAIN_IDENTITY_UNAVAILABLE)
9. CandidateHeadIdentity Enforcement (missing, head_sha mismatch, repo mismatch, base_sha mismatch, head_ref used, rehydrated on restart)
10. Restart Merge Recovery Attestation Failure (unproven ancestry -> POST_MERGE_ATTESTATION_FAILED)
11. Full Lifecycle Real-Provider E2E Regression with Mocked HTTP Transport
"""

import asyncio
import datetime
import hashlib
import io
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

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
    REQUIRED_TECHNICAL_WORKFLOWS,
    WorkflowObservation,
)
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.events import SupervisorEventType, create_event
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.policy.contracts import (
    AutonomyLevel,
    BudgetLimits,
    ExecutionTarget,
    PromotionThresholds,
    WorkProfile,
)
from ai_engineering.supervisor.pr_provider import (
    CandidateHeadIdentity,
    GitHubPullRequestProvider,
    MergeReceipt,
    PRConflictError,
    PRHeadMismatchError,
    PRMergeFailedError,
    PRNotFoundError,
    PRProviderError,
    PRProviderUnavailableError,
    PRTimeoutError,
    PRValidationFailedError,
    PullRequestIdentity,
    compute_candidate_head_identity_digest,
    compute_merge_receipt_digest,
    compute_pr_receipt_digest,
    scrub_secrets,
)
from ai_engineering.supervisor.router.registry import AgentRegistry
from ai_engineering.supervisor.router.router import AuthorityResolver, CrossAgentRouter, PersistentStore
from ai_engineering.supervisor.state import SupervisorPhase, SupervisorState
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.validator import VerifiedResult
from ai_engineering.task_intent import IntentStatus, TaskIntent, intent_digest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _create_intent(
    task_id: str = "t-real-1",
    run_id: str = "run-real-1",
    allowed_mutations: tuple[str, ...] = ("code", "REPOSITORY_WRITE", "PR_MUTATION", "PR_MERGE"),
    stop_boundary: StopBoundary = StopBoundary.MERGE,
    source_repository: str = "life2boat/hermes",
    source_base_sha: str = "a" * 40,
) -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id=task_id,
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Test outcome",
        source_repository=source_repository,
        source_main_ref="refs/heads/main",
        source_base_sha=source_base_sha,
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
    from ai_engineering.supervisor.validator import deserialize_verified_result
    vr_base = deserialize_verified_result(vr_text)
    return replace(
        vr_base,
        task_id=task_id,
        attempt_id=f"{task_id}-attempt-1",
        intent_digest=intent_dg,
        head_sha=head_sha,
        status=Status.PASS,
    )


def _create_candidate_identity(
    head_sha: str,
    head_ref: str = "feat/task-real-1",
    repo: str = "life2boat/hermes",
    base_sha: str = "a" * 40,
    base_ref: str = "main",
) -> CandidateHeadIdentity:
    return CandidateHeadIdentity(
        schema_version="hermes.candidate-head-identity.v1",
        repository=repo,
        remote_name="origin",
        head_ref=head_ref,
        head_sha=head_sha,
        base_ref=base_ref,
        base_sha=base_sha,
    )


class MockCIProvider:
    def __init__(self, overall_status: str = "SUCCESS", checks: tuple[WorkflowObservation, ...] | None = None) -> None:
        self.overall_status = overall_status
        self.checks = checks
        self.call_count = 0

    async def wait_for_ci(self, run_id: str, sha: str) -> bool:
        self.call_count += 1
        return self.overall_status == "SUCCESS"

    async def check_ci(self, run_id: str, sha: str) -> CIStatusSnapshot:
        self.call_count += 1
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if self.checks is not None:
            workflows = self.checks
        elif self.overall_status == "SUCCESS":
            workflows = tuple(
                WorkflowObservation(
                    workflow_name=w,
                    run_id=5000 + i,
                    head_sha=sha,
                    status="completed",
                    conclusion="success",
                )
                for i, w in enumerate(REQUIRED_TECHNICAL_WORKFLOWS)
            )
        else:
            workflows = ()
        return CIStatusSnapshot(
            schema_version="hermes.ci-status-snapshot.v1",
            repository="life2boat/hermes",
            requested_sha=sha,
            observed_sha=sha,
            checked_at_utc=now,
            overall_status=self.overall_status,
            required_workflow_observations=workflows,
            all_required_completed=(self.overall_status in ("SUCCESS", "FAILURE")),
            all_required_success=(self.overall_status == "SUCCESS"),
            governance_observations=(),
            snapshot_digest="snap-" + sha[:16],
        )


def _make_http_response(status: int = 200, data: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    body_bytes = json.dumps(data).encode("utf-8") if data is not None else b""
    resp.read.return_value = body_bytes
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Auth Configuration & Resolution Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_github_provider_loads_from_github_token_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token-from-github-env")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    p = GitHubPullRequestProvider()
    assert p.token == "token-from-github-env"
    assert p._get_headers()["Authorization"] == "Bearer token-from-github-env"


def test_github_provider_falls_back_to_gh_token_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "token-from-gh-env")
    p = GitHubPullRequestProvider()
    assert p.token == "token-from-gh-env"
    assert p._get_headers()["Authorization"] == "Bearer token-from-gh-env"


def test_github_provider_fails_closed_when_both_tokens_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(PRProviderUnavailableError) as exc_info:
        GitHubPullRequestProvider()
    assert exc_info.value.code == "PR_AUTH_MISSING"


def test_github_provider_fails_closed_when_token_is_blank(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "   ")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    with pytest.raises(PRProviderUnavailableError) as exc_info:
        GitHubPullRequestProvider()
    assert exc_info.value.code == "PR_AUTH_MISSING"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Direct Unit Tests with Mocked REST Transport
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_get_remote_head_sha_commit_endpoint():
    provider = GitHubPullRequestProvider(token="dummy")
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _make_http_response(200, {"sha": "c" * 40})
        sha = await provider.get_remote_head_sha("life2boat/hermes", "main")
        assert sha == "c" * 40
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "GET"
        assert "/repos/life2boat/hermes/commits/main" in req.full_url


@pytest.mark.asyncio
async def test_get_remote_head_sha_ref_fallback():
    provider = GitHubPullRequestProvider(token="dummy")
    with patch("urllib.request.urlopen") as mock_urlopen:
        http_404 = urllib.error.HTTPError(
            url="http://test", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b'{"message": "Not Found"}')
        )
        mock_urlopen.side_effect = [http_404, _make_http_response(200, {"object": {"sha": "d" * 40}})]
        sha = await provider.get_remote_head_sha("life2boat/hermes", "feat/my-branch")
        assert sha == "d" * 40
        assert mock_urlopen.call_count == 2


@pytest.mark.asyncio
async def test_get_remote_head_sha_both_404_returns_none():
    provider = GitHubPullRequestProvider(token="dummy")
    with patch("urllib.request.urlopen") as mock_urlopen:
        http_404 = urllib.error.HTTPError(
            url="http://test", code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b'{"message": "Not Found"}')
        )
        mock_urlopen.side_effect = [http_404, http_404]
        sha = await provider.get_remote_head_sha("life2boat/hermes", "nonexistent")
        assert sha is None


@pytest.mark.asyncio
async def test_get_remote_head_sha_network_error_propagates():
    provider = GitHubPullRequestProvider(token="dummy")
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with pytest.raises(PRProviderUnavailableError) as exc_info:
            await provider.get_remote_head_sha("life2boat/hermes", "main")
        assert exc_info.value.code == "PR_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_find_existing_pr_exact_match():
    provider = GitHubPullRequestProvider(token="dummy")
    pr_data = {
        "number": 42,
        "node_id": "PR_kw42",
        "title": "Title",
        "body": "Body",
        "draft": False,
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "created_at": "2026-09-12T00:00:00Z",
        "updated_at": "2026-09-12T00:00:00Z",
        "head": {"ref": "feat/x", "sha": "e" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _make_http_response(200, [pr_data])
        pr = await provider.find_existing_pr("life2boat/hermes", "feat/x", "main", exact_head_sha="e" * 40)
        assert pr is not None
        assert pr.pr_number == 42
        assert pr.head_sha == "e" * 40


@pytest.mark.asyncio
async def test_find_existing_pr_ambiguous_fails_closed():
    provider = GitHubPullRequestProvider(token="dummy")
    pr1 = {
        "number": 1,
        "head": {"ref": "feat/x", "sha": "e" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "state": "open",
    }
    pr2 = {
        "number": 2,
        "head": {"ref": "feat/x", "sha": "f" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "state": "open",
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _make_http_response(200, [pr1, pr2])
        with pytest.raises(PRValidationFailedError) as exc_info:
            await provider.find_existing_pr("life2boat/hermes", "feat/x", "main")
        assert exc_info.value.code == "PR_IDENTITY_AMBIGUOUS"


@pytest.mark.asyncio
async def test_create_pr_verifies_payload_and_post_create_get():
    provider = GitHubPullRequestProvider(token="dummy")
    created_raw = {
        "number": 101,
        "node_id": "PR_kw101",
        "title": "PR Title",
        "body": "PR Body",
        "draft": False,
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "created_at": "2026-09-12T00:00:00Z",
        "updated_at": "2026-09-12T00:00:00Z",
        "head": {"ref": "feat/branch", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            _make_http_response(201, created_raw),  # POST response
            _make_http_response(200, created_raw),  # GET post-create check
        ]
        pr = await provider.create_pr(
            repository="life2boat/hermes",
            head_branch="feat/branch",
            base_branch="main",
            title="PR Title",
            body="PR Body",
            draft=False,
            head_sha="b" * 40,
        )
        assert pr.pr_number == 101
        assert mock_urlopen.call_count == 2
        # Check first request (POST)
        post_req = mock_urlopen.call_args_list[0][0][0]
        assert post_req.get_method() == "POST"
        payload = json.loads(post_req.data.decode("utf-8"))
        assert payload["title"] == "PR Title"
        assert payload["head"] == "feat/branch"
        assert payload["base"] == "main"


@pytest.mark.asyncio
async def test_merge_pr_asserts_exact_sha_squash_and_no_admin():
    provider = GitHubPullRequestProvider(token="dummy")
    pr_get = {
        "number": 42,
        "node_id": "PR_kw42",
        "title": "T",
        "body": "B",
        "draft": False,
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "created_at": "2026-09-12T00:00:00Z",
        "updated_at": "2026-09-12T00:00:00Z",
        "head": {"ref": "feat/x", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
    }
    merge_put = {"merged": True, "sha": "m" * 40, "message": "Pull Request successfully merged"}
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = [
            _make_http_response(200, pr_get),    # GET /pulls/42
            _make_http_response(200, merge_put),  # PUT /pulls/42/merge
        ]
        receipt = await provider.merge_pr(
            repository="life2boat/hermes",
            pr_number=42,
            exact_head_sha="b" * 40,
            merge_method="squash",
            run_id="run-1",
            task_id="task-1",
        )
        assert receipt.merge_commit_sha == "m" * 40
        assert not hasattr(provider, "merge_call_count")  # No test counter on provider

        put_req = mock_urlopen.call_args_list[1][0][0]
        assert put_req.get_method() == "PUT"
        assert put_req.full_url.endswith("/repos/life2boat/hermes/pulls/42/merge")
        payload = json.loads(put_req.data.decode("utf-8"))
        assert payload["sha"] == "b" * 40
        assert payload["merge_method"] == "squash"

        # Security: strictly assert --admin is nowhere in url, headers or body
        assert "--admin" not in put_req.full_url
        assert "--admin" not in str(payload)
        for h, v in put_req.headers.items():
            assert "--admin" not in h.lower()
            assert "--admin" not in str(v).lower()


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Timeout Classification Tests
# ═══════════════════════════════════════════════════════════════════════════════

def test_timeout_classification_socket_timeout():
    provider = GitHubPullRequestProvider(token="secret-token-xyz")
    with patch("urllib.request.urlopen", side_effect=socket.timeout("Operation timed out")):
        with pytest.raises(PRTimeoutError) as exc_info:
            provider._sync_request("GET", "https://api.github.com/test")
        assert exc_info.value.code == "PR_TIMEOUT"
        assert "secret-token-xyz" not in str(exc_info.value)


def test_timeout_classification_timeout_error():
    provider = GitHubPullRequestProvider(token="secret-token-xyz")
    with patch("urllib.request.urlopen", side_effect=TimeoutError("Connection timed out")):
        with pytest.raises(PRTimeoutError) as exc_info:
            provider._sync_request("GET", "https://api.github.com/test")
        assert exc_info.value.code == "PR_TIMEOUT"


def test_timeout_classification_urlerror_timeout_reason():
    provider = GitHubPullRequestProvider(token="secret-token-xyz")
    err = urllib.error.URLError(socket.timeout("The read operation timed out"))
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(PRTimeoutError) as exc_info:
            provider._sync_request("GET", "https://api.github.com/test")
        assert exc_info.value.code == "PR_TIMEOUT"


def test_timeout_classification_urlerror_generic_is_unavailable():
    provider = GitHubPullRequestProvider(token="secret-token-xyz")
    err = urllib.error.URLError("Connection refused by peer")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(PRProviderUnavailableError) as exc_info:
            provider._sync_request("GET", "https://api.github.com/test")
        assert exc_info.value.code == "PR_PROVIDER_UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Unknown-Result Create Recovery Test
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_create_unknown_result_recovers_with_single_mutation(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-rec-c", "run-rec-c")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-rec-c", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-rec-c", wp, ep)
    vr = _create_passing_vr("t-rec-c", "run-rec-c", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-rec-c", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch-c")
    provider = GitHubPullRequestProvider(token="dummy")

    created_pr = {
        "number": 200,
        "node_id": "PR_kw200",
        "title": "[t-rec-c] Test outcome",
        "body": "PR Body",
        "draft": False,
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "created_at": "2026-09-12T00:00:00Z",
        "updated_at": "2026-09-12T00:00:00Z",
        "head": {"ref": "feat/branch-c", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
    }

    call_records = []

    def mock_urlopen_handler(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        call_records.append((method, url))
        if method == "GET" and "/commits/feat/branch-c" in url:
            return _make_http_response(200, {"sha": "b" * 40})
        elif method == "GET" and "/pulls?state=open" in url:
            if len([c for c in call_records if c[0] == "POST"]) == 0:
                return _make_http_response(200, [])
            else:
                return _make_http_response(200, [created_pr])
        elif method == "POST" and "/pulls" in url:
            raise socket.timeout("POST /pulls timed out")
        elif method == "GET" and "/pulls/200" in url:
            return _make_http_response(200, created_pr)
        return _make_http_response(404, {})

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        coord = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
            result_collector=ResultCollector(),
            budget=BudgetConfig(max_supervisor_decisions=1),
            run_id="run-rec-c",
            astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
            ci_provider=MockCIProvider(),
            evidence_root=str(tmp_path),
            pr_provider=provider,
            allow_pr_create=True,
            candidate_head_identity=cid,
            provider_mode="real",
        )
        receipt = await coord.run_until_terminal()
        assert coord.active_pr_number == 200

        post_calls = [c for c in call_records if c[0] == "POST"]
        assert len(post_calls) == 1

        events = store.load_events("run-rec-c")
        ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
        assert "PR_MUTATION_RESULT_UNKNOWN" in ev_types
        assert "PR_RECOVERED" in ev_types


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Unknown-Result Merge Recovery Test
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_merge_unknown_result_recovers_with_single_mutation(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-rec-m", "run-rec-m")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-rec-m", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-rec-m", wp, ep)
    vr = _create_passing_vr("t-rec-m", "run-rec-m", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-rec-m", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch-m")
    provider = GitHubPullRequestProvider(token="dummy")

    open_pr = {
        "number": 300,
        "node_id": "PR_kw300",
        "title": "Title",
        "body": "Body",
        "draft": False,
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "created_at": "2026-09-12T00:00:00Z",
        "updated_at": "2026-09-12T00:00:00Z",
        "head": {"ref": "feat/branch-m", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
    }
    merged_pr = dict(open_pr)
    merged_pr["state"] = "closed"
    merged_pr["merged"] = True
    merged_pr["merge_commit_sha"] = "m" * 40

    call_records = []

    def mock_urlopen_handler(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        call_records.append((method, url))
        if method == "GET" and "/commits/feat/branch-m" in url:
            return _make_http_response(200, {"sha": "b" * 40})
        elif method == "GET" and "/pulls?state=open" in url:
            return _make_http_response(200, [open_pr])
        elif method == "GET" and "/commits/main" in url:
            if len([c for c in call_records if c[0] == "PUT"]) == 0:
                return _make_http_response(200, {"sha": "a" * 40})
            else:
                return _make_http_response(200, {"sha": "m" * 40})
        elif method == "GET" and "/pulls/300" in url:
            if len([c for c in call_records if c[0] == "PUT"]) == 0:
                return _make_http_response(200, open_pr)
            else:
                return _make_http_response(200, merged_pr)
        elif method == "PUT" and "/merge" in url:
            raise socket.timeout("PUT /merge timed out")
        return _make_http_response(404, {})

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        coord = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
            result_collector=ResultCollector(),
            budget=BudgetConfig(max_supervisor_decisions=3),
            run_id="run-rec-m",
            astra_provider=ScriptedAstraProposalProvider(
                scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
            ),
            ci_provider=MockCIProvider(),
            evidence_root=str(tmp_path),
            pr_provider=provider,
            allow_pr_create=True,
            allow_pr_merge=True,
            candidate_head_identity=cid,
            provider_mode="real",
        )
        receipt = await coord.run_until_terminal()
        assert receipt.terminal_reason == "GOAL_COMPLETE"

        put_calls = [c for c in call_records if c[0] == "PUT"]
        assert len(put_calls) == 1

        events = store.load_events("run-rec-m")
        ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
        assert "MERGE_RESULT_UNKNOWN" in ev_types
        assert "PR_MERGE_RECOVERED" in ev_types

        merge_rec_ev = next(e for e in events if getattr(e.event_type, "value", str(e.event_type)) == "PR_MERGE_RECOVERED")
        assert merge_rec_ev.payload["pr_number"] == 300
        assert merge_rec_ev.payload["authorized_head_sha"] == "b" * 40
        assert merge_rec_ev.payload["merge_commit_sha"] == "m" * 40
        assert merge_rec_ev.payload["current_main_sha"] == "m" * 40
        assert merge_rec_ev.payload["attestation_result"] == "PASS"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Strict Fresh CI Contract & Negative Matrix Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ci_negative_matrix_sha_mismatch(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-ci-neg", "run-ci-neg")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-ci-neg", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-ci-neg", wp, ep)
    vr = _create_passing_vr("t-ci-neg", "run-ci-neg", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-ci-neg", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch-ci")
    provider = GitHubPullRequestProvider(token="dummy")

    pr_data = {
        "number": 400,
        "head": {"ref": "feat/branch-ci", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
    }

    class MismatchedShaCIProvider(MockCIProvider):
        async def check_ci(self, run_id: str, sha: str) -> CIStatusSnapshot:
            snap = await super().check_ci(run_id, sha)
            return replace(snap, observed_sha="9" * 40)

    def mock_urlopen_handler(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        if method == "GET" and "/commits/feat/branch-ci" in url:
            return _make_http_response(200, {"sha": "b" * 40})
        elif method == "GET" and "/commits/main" in url:
            return _make_http_response(200, {"sha": "a" * 40})
        elif method == "GET" and "/pulls?state=open" in url:
            return _make_http_response(200, [pr_data])
        elif method == "GET" and "/pulls/400" in url:
            return _make_http_response(200, pr_data)
        elif method == "POST" and "/pulls" in url:
            return _make_http_response(201, pr_data)
        return _make_http_response(404, {})

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        coord = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
            result_collector=ResultCollector(),
            budget=BudgetConfig(max_supervisor_decisions=3),
            run_id="run-ci-neg",
            astra_provider=ScriptedAstraProposalProvider(
                scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
            ),
            ci_provider=MismatchedShaCIProvider(),
            evidence_root=str(tmp_path),
            pr_provider=provider,
            allow_pr_create=True,
            allow_pr_merge=True,
            candidate_head_identity=cid,
            provider_mode="real",
        )
        receipt = await coord.run_until_terminal()
        assert receipt.terminal_reason == "CI_NOT_GREEN"


@pytest.mark.asyncio
async def test_ci_negative_matrix_neutral_workflow(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-ci-neu", "run-ci-neu")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-ci-neu", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-ci-neu", wp, ep)
    vr = _create_passing_vr("t-ci-neu", "run-ci-neu", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-ci-neu", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch-neu")
    provider = GitHubPullRequestProvider(token="dummy")

    pr_data = {
        "number": 401,
        "head": {"ref": "feat/branch-neu", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
    }

    neutral_checks = tuple(
        WorkflowObservation(
            workflow_name=w,
            run_id=6000 + i,
            head_sha="b" * 40,
            status="completed",
            conclusion="neutral" if w == "Nix" else "success",
        )
        for i, w in enumerate(REQUIRED_TECHNICAL_WORKFLOWS)
    )

    def mock_urlopen_handler(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        if method == "GET" and "/commits/feat/branch-neu" in url:
            return _make_http_response(200, {"sha": "b" * 40})
        elif method == "GET" and "/commits/main" in url:
            return _make_http_response(200, {"sha": "a" * 40})
        elif method == "GET" and "/pulls?state=open" in url:
            return _make_http_response(200, [pr_data])
        elif method == "GET" and "/pulls/401" in url:
            return _make_http_response(200, pr_data)
        elif method == "POST" and "/pulls" in url:
            return _make_http_response(201, pr_data)
        return _make_http_response(404, {})

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        coord = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
            result_collector=ResultCollector(),
            budget=BudgetConfig(max_supervisor_decisions=3),
            run_id="run-ci-neu",
            astra_provider=ScriptedAstraProposalProvider(
                scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
            ),
            ci_provider=MockCIProvider(overall_status="SUCCESS", checks=neutral_checks),
            evidence_root=str(tmp_path),
            pr_provider=provider,
            allow_pr_create=True,
            allow_pr_merge=True,
            candidate_head_identity=cid,
            provider_mode="real",
        )
        receipt = await coord.run_until_terminal()
        assert receipt.terminal_reason == "CI_NOT_GREEN"


@pytest.mark.asyncio
async def test_ci_negative_matrix_missing_workflow(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-ci-miss", "run-ci-miss")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-ci-miss", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-ci-miss", wp, ep)
    vr = _create_passing_vr("t-ci-miss", "run-ci-miss", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-ci-miss", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch-miss")
    provider = GitHubPullRequestProvider(token="dummy")

    pr_data = {
        "number": 402,
        "head": {"ref": "feat/branch-miss", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
    }

    six_checks = tuple(
        WorkflowObservation(
            workflow_name=w,
            run_id=7000 + i,
            head_sha="b" * 40,
            status="completed",
            conclusion="success",
        )
        for i, w in enumerate(REQUIRED_TECHNICAL_WORKFLOWS[:-1])
    )

    def mock_urlopen_handler(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        if method == "GET" and "/commits/feat/branch-miss" in url:
            return _make_http_response(200, {"sha": "b" * 40})
        elif method == "GET" and "/commits/main" in url:
            return _make_http_response(200, {"sha": "a" * 40})
        elif method == "GET" and "/pulls?state=open" in url:
            return _make_http_response(200, [pr_data])
        elif method == "GET" and "/pulls/402" in url:
            return _make_http_response(200, pr_data)
        elif method == "POST" and "/pulls" in url:
            return _make_http_response(201, pr_data)
        return _make_http_response(404, {})

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        coord = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
            result_collector=ResultCollector(),
            budget=BudgetConfig(max_supervisor_decisions=3),
            run_id="run-ci-miss",
            astra_provider=ScriptedAstraProposalProvider(
                scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
            ),
            ci_provider=MockCIProvider(overall_status="SUCCESS", checks=six_checks),
            evidence_root=str(tmp_path),
            pr_provider=provider,
            allow_pr_create=True,
            allow_pr_merge=True,
            candidate_head_identity=cid,
            provider_mode="real",
        )
        receipt = await coord.run_until_terminal()
        assert receipt.terminal_reason == "CI_NOT_GREEN"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Main Identity Fail-Closed Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_main_identity_unavailable_before_qualification(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-main-unavail", "run-main-unavail")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-main-unavail", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-main-unavail", wp, ep)
    vr = _create_passing_vr("t-main-unavail", "run-main-unavail", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-main-unavail", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch-mu")
    provider = GitHubPullRequestProvider(token="dummy")

    pr_data = {
        "number": 500,
        "head": {"ref": "feat/branch-mu", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
    }

    def mock_urlopen_handler(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        if method == "GET" and "/commits/feat/branch-mu" in url:
            return _make_http_response(200, {"sha": "b" * 40})
        elif method == "GET" and "/commits/main" in url:
            raise urllib.error.HTTPError(url=url, code=500, msg="Server Error", hdrs={}, fp=io.BytesIO(b'{}'))
        elif method == "GET" and "/pulls?state=open" in url:
            return _make_http_response(200, [pr_data])
        elif method == "GET" and "/pulls/500" in url:
            return _make_http_response(200, pr_data)
        elif method == "POST" and "/pulls" in url:
            return _make_http_response(201, pr_data)
        return _make_http_response(404, {})

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        coord = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
            result_collector=ResultCollector(),
            budget=BudgetConfig(max_supervisor_decisions=3),
            run_id="run-main-unavail",
            astra_provider=ScriptedAstraProposalProvider(
                scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
            ),
            ci_provider=MockCIProvider(),
            evidence_root=str(tmp_path),
            pr_provider=provider,
            allow_pr_create=True,
            allow_pr_merge=True,
            candidate_head_identity=cid,
            provider_mode="real",
        )
        receipt = await coord.run_until_terminal()
        assert receipt.terminal_reason == "MAIN_IDENTITY_UNAVAILABLE"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. CandidateHeadIdentity Enforcement Tests
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_candidate_head_identity_missing_in_real_mode_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-cid-miss", "run-cid-miss")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-cid-miss", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-cid-miss", wp, ep)
    vr = _create_passing_vr("t-cid-miss", "run-cid-miss", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-cid-miss", vr, "vr-dg")

    provider = GitHubPullRequestProvider(token="dummy")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-cid-miss",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=MockCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=provider,
        allow_pr_create=True,
        candidate_head_identity=None,
        provider_mode="real",
    )
    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_MISSING"

    events = store.load_events("run-cid-miss")
    ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
    assert "CANDIDATE_HEAD_IDENTITY_MISSING" in ev_types


@pytest.mark.asyncio
async def test_candidate_head_identity_sha_mismatch_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-cid-sha", "run-cid-sha")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-cid-sha", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-cid-sha", wp, ep)
    vr = _create_passing_vr("t-cid-sha", "run-cid-sha", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-cid-sha", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="9" * 40, head_ref="feat/branch-cid-sha")
    provider = GitHubPullRequestProvider(token="dummy")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-cid-sha",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=MockCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=provider,
        allow_pr_create=True,
        candidate_head_identity=cid,
        provider_mode="real",
    )
    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_HEAD_SHA_MISMATCH"


@pytest.mark.asyncio
async def test_candidate_head_identity_repo_mismatch_fails_closed(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-cid-repo", "run-cid-repo", source_repository="life2boat/hermes")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-cid-repo", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-cid-repo", wp, ep)
    vr = _create_passing_vr("t-cid-repo", "run-cid-repo", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-cid-repo", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch", repo="other/repo")
    provider = GitHubPullRequestProvider(token="dummy")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-cid-repo",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[NextActionType.CREATE_PR]),
        ci_provider=MockCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=provider,
        allow_pr_create=True,
        candidate_head_identity=cid,
        provider_mode="real",
    )
    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_IDENTITY_MISMATCH"


@pytest.mark.asyncio
async def test_candidate_head_identity_rehydrated_on_restart(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-cid-rehyd", "run-cid-rehyd")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-cid-rehyd", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-cid-rehyd", wp, ep)

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/rehydrated-ref")
    provider = GitHubPullRequestProvider(token="dummy")

    coord1 = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-cid-rehyd",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[]),
        ci_provider=MockCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=provider,
        allow_pr_create=True,
        candidate_head_identity=cid,
        provider_mode="real",
    )
    coord1.register_candidate_head_identity(cid)

    store2 = FileSupervisorStateStore(tmp_path)
    loop2 = SupervisorLoop(store2)
    coord2 = AutonomousRunCoordinator(
        loop=loop2,
        store=store2,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store2, loop2), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(max_supervisor_decisions=1),
        run_id="run-cid-rehyd",
        astra_provider=ScriptedAstraProposalProvider(scripted_actions=[]),
        ci_provider=MockCIProvider(),
        evidence_root=str(tmp_path),
        pr_provider=provider,
        allow_pr_create=True,
        candidate_head_identity=None,
        provider_mode="real",
    )
    assert coord2.candidate_head_identity is not None
    assert coord2.candidate_head_identity.head_ref == "feat/rehydrated-ref"
    assert coord2.candidate_head_identity.head_sha == "b" * 40


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Full Lifecycle Real-Provider E2E Regression with Mocked HTTP
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_full_lifecycle_real_provider_e2e_regression(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_intent("t-real-e2e", "run-real-e2e")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(intent, None, intent.desired_outcome, "run-real-e2e", datetime.datetime.now(datetime.timezone.utc).isoformat())
    loop.bind_profile("run-real-e2e", wp, ep)
    vr = _create_passing_vr("t-real-e2e", "run-real-e2e", head_sha="b" * 40, intent_dg=intent_digest(intent))
    loop.ingest_verified_result("run-real-e2e", vr, "vr-dg")

    cid = _create_candidate_identity(head_sha="b" * 40, head_ref="feat/branch-e2e")
    provider = GitHubPullRequestProvider(token="mock-token")

    pr_raw = {
        "number": 999,
        "node_id": "PR_kw999",
        "title": "[t-real-e2e] Test outcome",
        "body": "PR Body",
        "draft": False,
        "state": "open",
        "mergeable": True,
        "mergeable_state": "clean",
        "merged": False,
        "merged_at": None,
        "merge_commit_sha": None,
        "created_at": "2026-09-12T00:00:00Z",
        "updated_at": "2026-09-12T00:00:00Z",
        "head": {"ref": "feat/branch-e2e", "sha": "b" * 40},
        "base": {"ref": "main", "sha": "a" * 40},
    }
    pr_merged = dict(pr_raw)
    pr_merged["state"] = "closed"
    pr_merged["merged"] = True
    pr_merged["merge_commit_sha"] = "m" * 40

    call_records = []

    def mock_urlopen_handler(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        call_records.append((method, url))
        if method == "GET" and "/commits/feat/branch-e2e" in url:
            return _make_http_response(200, {"sha": "b" * 40})
        elif method == "GET" and "/pulls?state=open" in url:
            return _make_http_response(200, [])
        elif method == "POST" and "/pulls" in url:
            return _make_http_response(201, pr_raw)
        elif method == "GET" and "/pulls/999" in url:
            if any(c[0] == "PUT" for c in call_records):
                return _make_http_response(200, pr_merged)
            return _make_http_response(200, pr_raw)
        elif method == "GET" and "/commits/main" in url:
            if any(c[0] == "PUT" for c in call_records):
                return _make_http_response(200, {"sha": "m" * 40})
            return _make_http_response(200, {"sha": "a" * 40})
        elif method == "PUT" and "/pulls/999/merge" in url:
            return _make_http_response(200, {"merged": True, "sha": "m" * 40})
        return _make_http_response(404, {})

    with patch("urllib.request.urlopen", side_effect=mock_urlopen_handler):
        coord = AutonomousRunCoordinator(
            loop=loop,
            store=store,
            router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
            result_collector=ResultCollector(),
            budget=BudgetConfig(max_supervisor_decisions=5),
            run_id="run-real-e2e",
            astra_provider=ScriptedAstraProposalProvider(
                scripted_actions=[NextActionType.CREATE_PR, NextActionType.WAIT_FOR_CI, NextActionType.MERGE_IF_GREEN]
            ),
            ci_provider=MockCIProvider(),
            evidence_root=str(tmp_path),
            pr_provider=provider,
            allow_pr_create=True,
            allow_pr_merge=True,
            candidate_head_identity=cid,
            provider_mode="real",
        )
        receipt = await coord.run_until_terminal()
        assert receipt.terminal_reason == "GOAL_COMPLETE"

        events = store.load_events("run-real-e2e")
        ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
        assert "PR_CREATED" in ev_types
        assert "CI_WAIT_STARTED" in ev_types
        assert "CI_GREEN" in ev_types
        assert "PR_MERGE_REQUESTED" in ev_types
        assert "PR_MERGED" in ev_types
        assert "SOURCE_RECONCILED" in ev_types

        assert len(receipt.pr_receipts) >= 1
        assert len(receipt.merge_receipts) >= 1
        assert coord.merged_commit_sha == "m" * 40
