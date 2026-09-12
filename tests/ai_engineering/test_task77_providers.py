"""Test Suite for Task 7.7: Real Astra Proposal Provider + GitHub CI Status Provider."""

import asyncio
import datetime
import json
import os
import sys
from pathlib import Path
import pytest

from ai_engineering.contracts import EffectClass, StopBoundary, TaskClass
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)
from ai_engineering.supervisor.astra_provider import (
    AstraProposalActionUnsupportedError,
    AstraProposalIdentityMismatchError,
    AstraProposalInvalidError,
    AstraProviderInvalidJsonError,
    AstraProviderTimeoutError,
    AstraProviderUnavailableError,
    ConfiguredAstraProposalProvider,
    build_canonical_astra_request,
    compute_astra_receipt_digest,
    parse_and_validate_astra_proposal,
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
    GitHubCIStatusProvider,
    REQUIRED_TECHNICAL_CHECKS,
    compute_ci_receipt_digest,
    evaluate_ci_checks,
)
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.policy.contracts import (
    AutonomyLevel,
    BudgetLimits,
    ExecutionTarget,
    PromotionThresholds,
    WorkProfile,
)
from ai_engineering.supervisor.router.adapters import CodexAdapter
from ai_engineering.supervisor.router.envelope import MessageType
from ai_engineering.supervisor.router.registry import AgentDefinition, AgentRegistry
from ai_engineering.supervisor.router.router import AuthorityResolver, CrossAgentRouter, PersistentStore
from ai_engineering.supervisor.router.transports import FakeAgentTransport
from ai_engineering.supervisor.state import SupervisorPhase, SupervisorState
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.task_intent import IntentStatus, TaskIntent, intent_digest


def _create_minimal_intent(task_id: str = "t1", run_id: str = "run-1") -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id=task_id,
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Test intent execution",
        source_repository="github",
        source_main_ref="refs/remotes/github/main",
        source_base_sha="a" * 40,
        constraints=(),
        allowed_mutations=("code", "REPOSITORY_WRITE"),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.LOCAL_DIFF,
        acceptance_criteria=(),
        unknowns=(),
        applicable_invariants=(),
        required_gates=(),
        parent_intent_digest=None,
    )


def _create_state(tmp_path: Path, run_id: str = "run-1", task_id: str = "t1") -> SupervisorState:
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent(task_id, run_id)
    return loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )


def _create_work_profile() -> WorkProfile:
    return WorkProfile(
        schema_version="hermes.work-profile.v1",
        profile_id="test-profile",
        profile_version=1,
        profile_digest="test-profile-digest",
        preferred_worker_model=None,
        preferred_verifier_model=None,
        preferred_supervisor_model=None,
        escalation_model=None,
        allowed_task_classes=("BOUNDED_IMPLEMENTATION",),
        maximum_autonomy_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION,
        allowed_effect_classes=(EffectClass.READ_ONLY, EffectClass.REPOSITORY_WRITE),
        forbidden_effect_classes=(),
        allowed_targets=(ExecutionTarget.DEV,),
        required_validators=(),
        promotion_thresholds=PromotionThresholds(1, 0, False),
        budget_limits=BudgetLimits(20, 10, 3, 3, 2, 50, 10),
        production_execution_allowed=False,
        vector_mutation_allowed=False,
        secret_mutation_allowed=False,
        external_send_allowed=False,
    )


def _create_effective_policy(intent: TaskIntent) -> EffectivePolicyReport:
    tpa = TaskPolicyAttribution(
        task_id=intent.task_id,
        intent_revision=intent.intent_revision,
        intent_digest=intent_digest(intent),
        source_base_sha=intent.source_base_sha,
        constraints=intent.constraints,
        allowed_mutations=intent.allowed_mutations,
        forbidden_mutations=intent.forbidden_mutations,
        stop_boundary=intent.stop_boundary.value if hasattr(intent.stop_boundary, "value") else str(intent.stop_boundary),
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


# ==============================================================================
# ASTRA PROPOSAL PROVIDER UNIT TESTS
# ==============================================================================

def test_canonical_astra_request_builder(tmp_path: Path):
    intent = _create_minimal_intent()
    state = _create_state(tmp_path)
    budget = BudgetConfig(max_retries=3, max_fix_cycles=4)
    stats = {"retries": 1, "fix_cycles": 2, "iterations": 5}

    req = build_canonical_astra_request("run-1", state, intent, budget, stats)
    assert req["schema_version"] == "hermes.astra-request.v1"
    assert req["run_id"] == "run-1"
    assert req["task_id"] == "t1"
    assert req["budget"]["max_retries"] == 3
    assert req["budget"]["consumed"]["retries"] == 1
    assert req["budget"]["remaining"]["retries"] == 2
    assert req["authority_boundary"]["stop_boundary"] == "LOCAL_DIFF"


def test_parse_and_validate_astra_proposal_valid():
    raw = {
        "schema_version": "hermes.astra-next-action.v1",
        "proposal_id": "prop-123",
        "run_id": "run-1",
        "task_id": "t1",
        "action_type": "IMPLEMENT",
        "objective": "Build component",
        "recommended_capability": "code",
        "recommended_worker": "codex",
        "expected_effect_class": "REPOSITORY_WRITE",
        "expected_stop_boundary": "LOCAL_DIFF",
        "allowed_scope": ["src/"],
        "required_validators": ["pytest"],
        "success_criteria": "PASS",
        "reasoning_summary": "Implementation needed",
    }
    proposal = parse_and_validate_astra_proposal(raw, expected_run_id="run-1", expected_task_id="t1")
    assert proposal.proposal_id == "prop-123"
    assert proposal.action_type == NextActionType.IMPLEMENT


def test_parse_and_validate_identity_mismatch():
    raw = {
        "schema_version": "hermes.astra-next-action.v1",
        "proposal_id": "prop-123",
        "run_id": "wrong-run",
        "task_id": "t1",
        "action_type": "IMPLEMENT",
        "objective": "x",
        "recommended_capability": "code",
        "recommended_worker": "codex",
        "expected_effect_class": "REPOSITORY_WRITE",
        "expected_stop_boundary": "LOCAL_DIFF",
    }
    with pytest.raises(AstraProposalIdentityMismatchError):
        parse_and_validate_astra_proposal(raw, expected_run_id="run-1", expected_task_id="t1")

    raw["run_id"] = "run-1"
    raw["task_id"] = "wrong-task"
    with pytest.raises(AstraProposalIdentityMismatchError):
        parse_and_validate_astra_proposal(raw, expected_run_id="run-1", expected_task_id="t1")


def test_parse_and_validate_unsupported_action():
    raw = {
        "schema_version": "hermes.astra-next-action.v1",
        "proposal_id": "prop-123",
        "run_id": "run-1",
        "task_id": "t1",
        "action_type": "LAUNCH_NUCLEAR_MISSILE",
        "objective": "x",
        "recommended_capability": "code",
        "recommended_worker": "codex",
        "expected_effect_class": "REPOSITORY_WRITE",
        "expected_stop_boundary": "LOCAL_DIFF",
    }
    with pytest.raises(AstraProposalActionUnsupportedError):
        parse_and_validate_astra_proposal(raw, expected_run_id="run-1", expected_task_id="t1")


def test_parse_and_validate_missing_required_fields():
    raw = {
        "schema_version": "hermes.astra-next-action.v1",
        "proposal_id": "prop-123",
        "run_id": "run-1",
    }
    with pytest.raises(AstraProposalInvalidError):
        parse_and_validate_astra_proposal(raw, expected_run_id="run-1", expected_task_id="t1")


@pytest.mark.asyncio
async def test_configured_astra_subprocess_valid(tmp_path: Path):
    script_path = tmp_path / "mock_astra.py"
    script_path.write_text("""
import sys, json
req = json.load(sys.stdin)
proposal = {
    "schema_version": "hermes.astra-next-action.v1",
    "proposal_id": "prop-subprocess-1",
    "run_id": req["run_id"],
    "task_id": req["task_id"],
    "action_type": "IMPLEMENT",
    "objective": "Subprocess task",
    "recommended_capability": "code",
    "recommended_worker": "codex",
    "expected_effect_class": "REPOSITORY_WRITE",
    "expected_stop_boundary": "LOCAL_DIFF",
    "allowed_scope": [],
    "required_validators": [],
    "success_criteria": "",
    "reasoning_summary": "Generated by subprocess"
}
sys.stdout.write(json.dumps(proposal))
sys.stdout.flush()
""", encoding="utf-8")

    provider = ConfiguredAstraProposalProvider([sys.executable, str(script_path)], timeout_seconds=5.0)
    state = _create_state(tmp_path, run_id="run-sub-1", task_id="t-sub")
    prop = await provider.request_proposal("run-sub-1", state)
    assert prop.proposal_id == "prop-subprocess-1"
    assert prop.action_type == NextActionType.IMPLEMENT
    assert provider.latest_receipt is not None
    assert provider.latest_receipt.exit_code == 0
    assert len(provider.latest_receipt.receipt_id) == 64


@pytest.mark.asyncio
async def test_configured_astra_subprocess_invalid_json(tmp_path: Path):
    script_path = tmp_path / "mock_bad_json.py"
    script_path.write_text("import sys\nsys.stdout.write('THIS IS NOT JSON')\n", encoding="utf-8")

    provider = ConfiguredAstraProposalProvider([sys.executable, str(script_path)], timeout_seconds=5.0)
    state = _create_state(tmp_path, run_id="run-bad", task_id="t1")
    with pytest.raises(AstraProviderInvalidJsonError):
        await provider.request_proposal("run-bad", state)


@pytest.mark.asyncio
async def test_configured_astra_subprocess_timeout(tmp_path: Path):
    script_path = tmp_path / "mock_hang.py"
    script_path.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")

    provider = ConfiguredAstraProposalProvider([sys.executable, str(script_path)], timeout_seconds=0.5)
    state = _create_state(tmp_path, run_id="run-to", task_id="t1")
    with pytest.raises(AstraProviderTimeoutError):
        await provider.request_proposal("run-to", state)


@pytest.mark.asyncio
async def test_configured_astra_subprocess_failure_code(tmp_path: Path):
    script_path = tmp_path / "mock_fail.py"
    script_path.write_text("import sys\nsys.stderr.write('CRITICAL ERROR')\nsys.exit(2)\n", encoding="utf-8")

    provider = ConfiguredAstraProposalProvider([sys.executable, str(script_path)], timeout_seconds=5.0)
    state = _create_state(tmp_path, run_id="run-fail", task_id="t1")
    with pytest.raises(AstraProviderUnavailableError):
        await provider.request_proposal("run-fail", state)
    assert provider.latest_receipt is not None
    assert provider.latest_receipt.exit_code == 2


# ==============================================================================
# GITHUB CI STATUS PROVIDER UNIT TESTS
# ==============================================================================

def test_evaluate_ci_checks_sha_mismatch():
    status, passed, issues = evaluate_ci_checks(
        requested_sha="a" * 40,
        observed_sha="b" * 40,
        checks=(),
    )
    assert status == "SHA_MISMATCH"


def test_evaluate_ci_checks_all_green():
    sha = "c" * 40
    checks = [
        CICheckResult("Tests", "completed", "success"),
        CICheckResult("Lint", "completed", "success"),
        CICheckResult("Typecheck", "completed", "success"),
        CICheckResult("Nix", "completed", "success"),
        CICheckResult("Agent Release Gate", "completed", "success"),
        CICheckResult("Supply Chain Audit", "completed", "success"),
        CICheckResult("History Check", "completed", "success"),
    ]
    status, passed, issues = evaluate_ci_checks(sha, sha, checks)
    assert status == "SUCCESS"
    assert len(issues) == 0


def test_evaluate_ci_checks_attribution_governance_only():
    """Contributor Attribution failure must NOT block overall technical CI success!"""
    sha = "c" * 40
    checks = [
        CICheckResult("Tests", "completed", "success"),
        CICheckResult("Lint", "completed", "success"),
        CICheckResult("Typecheck", "completed", "success"),
        CICheckResult("Nix", "completed", "success"),
        CICheckResult("Agent Release Gate", "completed", "success"),
        CICheckResult("Supply Chain Audit", "completed", "success"),
        CICheckResult("History Check", "completed", "success"),
        CICheckResult("Contributor Attribution", "completed", "failure"),  # Governance-only!
    ]
    status, passed, issues = evaluate_ci_checks(sha, sha, checks)
    assert status == "SUCCESS"


def test_evaluate_ci_checks_in_progress_queued():
    sha = "c" * 40
    checks = [
        CICheckResult("Tests", "in_progress", ""),
        CICheckResult("Lint", "queued", ""),
        CICheckResult("Typecheck", "completed", "success"),
    ]
    status, passed, issues = evaluate_ci_checks(sha, sha, checks)
    assert status == "IN_PROGRESS"


def test_evaluate_ci_checks_failure():
    sha = "c" * 40
    checks = [
        CICheckResult("Tests", "completed", "failure"),
        CICheckResult("Lint", "completed", "success"),
    ]
    status, passed, issues = evaluate_ci_checks(sha, sha, checks)
    assert status == "FAILURE"
    assert any("FAILED: Tests" in iss for iss in issues)


def test_evaluate_ci_checks_missing():
    sha = "c" * 40
    checks = [
        CICheckResult("Tests", "completed", "success"),
    ]
    status, passed, issues = evaluate_ci_checks(sha, sha, checks)
    assert status == "MISSING"
    assert any("MISSING: Lint" in iss for iss in issues)


@pytest.mark.asyncio
async def test_github_ci_provider_runner_polling():
    sha = "d" * 40
    call_count = 0

    async def mock_runner(repo: str, target_sha: str):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return target_sha, [
                {"name": "Tests", "status": "in_progress", "conclusion": ""},
            ]
        return target_sha, [
            {"name": "Tests", "status": "completed", "conclusion": "success"},
            {"name": "Lint", "status": "completed", "conclusion": "success"},
            {"name": "Typecheck", "status": "completed", "conclusion": "success"},
            {"name": "Nix", "status": "completed", "conclusion": "success"},
            {"name": "Agent Release Gate", "status": "completed", "conclusion": "success"},
            {"name": "Supply Chain Audit", "status": "completed", "conclusion": "success"},
            {"name": "History Check", "status": "completed", "conclusion": "success"},
        ]

    provider = GitHubCIStatusProvider(
        repository="life2boat/hermes",
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
        runner=mock_runner,
    )

    snapshot = await provider.wait_for_ci("run-ci-1", sha)
    assert snapshot.overall_status == "SUCCESS"
    assert call_count == 2
    assert provider.latest_receipt is not None
    assert provider.latest_receipt.overall_status == "SUCCESS"


@pytest.mark.asyncio
async def test_github_ci_provider_timeout():
    sha = "e" * 40

    async def mock_runner_hang(repo: str, target_sha: str):
        return target_sha, [
            {"name": "Tests", "status": "in_progress", "conclusion": ""},
        ]

    provider = GitHubCIStatusProvider(
        repository="life2boat/hermes",
        timeout_seconds=0.05,
        poll_interval_seconds=0.01,
        runner=mock_runner_hang,
    )

    snapshot = await provider.wait_for_ci("run-ci-to", sha)
    assert snapshot.overall_status == "TIMED_OUT"
    assert provider.latest_receipt is not None
    assert provider.latest_receipt.overall_status == "TIMED_OUT"


# ==============================================================================
# INTEGRATED AUTONOMOUS RUN COORDINATOR E2E TESTS (TASK 7.7)
# ==============================================================================

@pytest.mark.asyncio
async def test_e2e_astra_and_ci_green_path(tmp_path: Path):
    """Full loop: Subprocess Astra proposes IMPLEMENT -> worker passes -> Astra proposes WAIT_FOR_CI -> CI GREEN -> Astra proposes STOP_SUCCESS -> GOAL_COMPLETE."""
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent("t-e2e", "run-e2e")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id="run-e2e",
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    loop.bind_profile("run-e2e", wp, ep)

    astra_script = tmp_path / "astra_e2e.py"
    astra_script.write_text("""
import sys, json

req = json.load(sys.stdin)
run_id = req["run_id"]
task_id = req["task_id"]

state_file = "astra_step.txt"
try:
    with open(state_file, "r") as f:
        step = int(f.read().strip())
except Exception:
    step = 0

step += 1
with open(state_file, "w") as f:
    f.write(str(step))

if step == 1:
    act = "IMPLEMENT"
elif step == 2:
    act = "WAIT_FOR_CI"
else:
    act = "STOP_SUCCESS"

proposal = {
    "schema_version": "hermes.astra-next-action.v1",
    "proposal_id": f"prop-{step}",
    "run_id": run_id,
    "task_id": task_id,
    "action_type": act,
    "objective": f"Step {step} objective",
    "recommended_capability": "code",
    "recommended_worker": "codex",
    "expected_effect_class": "REPOSITORY_WRITE" if act == "IMPLEMENT" else "READ_ONLY",
    "expected_stop_boundary": "LOCAL_DIFF" if act == "IMPLEMENT" else "READ_ONLY",
    "allowed_scope": [],
    "required_validators": [],
    "success_criteria": "",
    "reasoning_summary": f"Astra reasoning for {act}"
}
sys.stdout.write(json.dumps(proposal))
sys.stdout.flush()
""", encoding="utf-8")

    astra_provider = ConfiguredAstraProposalProvider(
        [sys.executable, str(astra_script)],
        cwd=str(tmp_path),
        timeout_seconds=5.0,
    )

    async def mock_ci_runner(repo: str, target_sha: str):
        return target_sha, [
            {"name": "Tests", "status": "completed", "conclusion": "success"},
            {"name": "Lint", "status": "completed", "conclusion": "success"},
            {"name": "Typecheck", "status": "completed", "conclusion": "success"},
            {"name": "Nix", "status": "completed", "conclusion": "success"},
            {"name": "Agent Release Gate", "status": "completed", "conclusion": "success"},
            {"name": "Supply Chain Audit", "status": "completed", "conclusion": "success"},
            {"name": "History Check", "status": "completed", "conclusion": "success"},
        ]

    ci_provider = GitHubCIStatusProvider(
        repository="life2boat/hermes",
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
        runner=mock_ci_runner,
    )

    transport = FakeAgentTransport()
    registry = AgentRegistry()
    registry.register(
        AgentDefinition("codex", ["code"], [MessageType.WORK_REQUEST], [EffectClass.REPOSITORY_WRITE], 300),
        CodexAdapter(transport),
    )
    router_store = PersistentStore(tmp_path / "router")
    router = CrossAgentRouter(
        registry=registry,
        authority_resolver=AuthorityResolver(supervisor_store=store, supervisor_loop=loop),
        store=router_store,
    )
    collector = ResultCollector()
    budget = BudgetConfig(max_supervisor_decisions=10, max_child_tasks=5)

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=router,
        result_collector=collector,
        budget=budget,
        run_id="run-e2e",
        astra_provider=astra_provider,
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert receipt.iterations >= 3
    assert len(receipt.policy_receipts) >= 1
    assert len(receipt.routing_receipts) >= 1
    assert len(receipt.verified_results) >= 1
    assert len(receipt.astra_receipts) >= 1
    assert len(receipt.ci_receipts) >= 1

    events = store.load_events("run-e2e")
    event_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
    assert "ASTRA_REQUESTED" in event_types
    assert "ASTRA_PROPOSAL_CREATED" in event_types
    assert "CI_WAIT_STARTED" in event_types
    assert "CI_STATUS_OBSERVED" in event_types
    assert "CI_GREEN" in event_types


@pytest.mark.asyncio
async def test_e2e_ci_sha_mismatch_blocks(tmp_path: Path):
    """When observed CI sha mismatches requested sha, autonomous run terminates with CI_SHA_MISMATCH."""
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent("t-mismatch", "run-mismatch")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id="run-mismatch",
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    loop.bind_profile("run-mismatch", wp, ep)

    class MockAstraCI(ConfiguredAstraProposalProvider):
        def __init__(self):
            pass
        async def request_proposal(self, run_id, state, **kwargs):
            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id="prop-ci",
                run_id=run_id,
                task_id=state.current_task_id,
                action_type=NextActionType.WAIT_FOR_CI,
                objective="Wait for CI",
                recommended_capability="none",
                recommended_worker="none",
                expected_effect_class="READ_ONLY",
                expected_stop_boundary="READ_ONLY",
                allowed_scope=(),
                required_validators=(),
                success_criteria="",
                reasoning_summary="Check CI",
            )

    async def mock_ci_runner_mismatch(repo: str, target_sha: str):
        return "DIFFERENT_SHA_11111111111111111111111111111", [
            {"name": "Tests", "status": "completed", "conclusion": "success"},
        ]

    ci_provider = GitHubCIStatusProvider(
        repository="life2boat/hermes",
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
        runner=mock_ci_runner_mismatch,
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-mismatch",
        astra_provider=MockAstraCI(),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "CI_SHA_MISMATCH"


@pytest.mark.asyncio
async def test_e2e_ci_timeout_blocks(tmp_path: Path):
    """When CI polling times out, autonomous run terminates with CI_WAIT_TIMEOUT."""
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent("t-to", "run-to")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id="run-to",
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    loop.bind_profile("run-to", wp, ep)

    class MockAstraCI(ConfiguredAstraProposalProvider):
        def __init__(self):
            pass
        async def request_proposal(self, run_id, state, **kwargs):
            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id="prop-ci",
                run_id=run_id,
                task_id=state.current_task_id,
                action_type=NextActionType.WAIT_FOR_CI,
                objective="Wait for CI",
                recommended_capability="none",
                recommended_worker="none",
                expected_effect_class="READ_ONLY",
                expected_stop_boundary="READ_ONLY",
                allowed_scope=(),
                required_validators=(),
                success_criteria="",
                reasoning_summary="Check CI",
            )

    async def mock_ci_runner_hang(repo: str, target_sha: str):
        return target_sha, [
            {"name": "Tests", "status": "in_progress", "conclusion": ""},
        ]

    ci_provider = GitHubCIStatusProvider(
        repository="life2boat/hermes",
        timeout_seconds=0.05,
        poll_interval_seconds=0.01,
        runner=mock_ci_runner_hang,
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-to",
        astra_provider=MockAstraCI(),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "CI_WAIT_TIMEOUT"


@pytest.mark.asyncio
async def test_e2e_create_pr_and_merge_unsupported_in_nonprod(tmp_path: Path):
    """In non-prod mode, CREATE_PR and MERGE_IF_GREEN fail closed."""
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent("t-np", "run-np")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id="run-np",
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    loop.bind_profile("run-np", wp, ep)

    class MockAstraPR(ConfiguredAstraProposalProvider):
        def __init__(self):
            pass
        async def request_proposal(self, run_id, state, **kwargs):
            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id="prop-pr",
                run_id=run_id,
                task_id=state.current_task_id,
                action_type=NextActionType.CREATE_PR,
                objective="Create PR",
                recommended_capability="none",
                recommended_worker="none",
                expected_effect_class="READ_ONLY",
                expected_stop_boundary="READ_ONLY",
                allowed_scope=(),
                required_validators=(),
                success_criteria="",
                reasoning_summary="",
            )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-np",
        astra_provider=MockAstraPR(),
        ci_provider=GitHubCIStatusProvider(),
        evidence_root=str(tmp_path),
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "PR_PROVIDER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_e2e_astra_provider_failure_events(tmp_path: Path):
    """When Astra provider fails, ASTRA_PROVIDER_FAILED event is recorded and terminal_reason is formatted."""
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent("t-fail", "run-fail")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id="run-fail",
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    loop.bind_profile("run-fail", wp, ep)

    class FailingAstra(ConfiguredAstraProposalProvider):
        def __init__(self):
            pass
        async def request_proposal(self, run_id, state, **kwargs):
            raise AstraProviderUnavailableError("Subprocess connection refused", code="ASTRA_PROVIDER_UNAVAILABLE")

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-fail",
        astra_provider=FailingAstra(),
        ci_provider=GitHubCIStatusProvider(),
        evidence_root=str(tmp_path),
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "ASTRA_PROVIDER_FAILED_ASTRA_PROVIDER_UNAVAILABLE"

    events = store.load_events("run-fail")
    event_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
    assert "ASTRA_REQUESTED" in event_types
    assert "ASTRA_PROVIDER_FAILED" in event_types


# ==============================================================================
# PR #298 SHAPED WORKFLOW FIXTURE & NEGATIVE TESTS
# ==============================================================================

def test_pr298_shaped_workflow_fixture():
    sha = "d65ef87d02dba27d687043f6c7fa5c5648c6d54d"
    raw_runs = [
        {"id": 101, "name": "Tests", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 102, "name": "Lint (ruff + ty)", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 103, "name": "Typecheck", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 104, "name": "Nix", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 105, "name": "Agent Release Gate", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 106, "name": "Supply Chain Audit", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 107, "name": "History Check", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 108, "name": "Contributor Attribution Check", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 109, "name": "Docker Build and Publish", "head_sha": sha, "status": "completed", "conclusion": "success"},
    ]
    from ai_engineering.supervisor.ci_provider import evaluate_workflow_runs
    snapshot = evaluate_workflow_runs(
        repository="life2boat/hermes",
        requested_sha=sha,
        raw_runs=raw_runs,
    )
    assert snapshot.overall_status == "SUCCESS"
    assert snapshot.all_required_completed is True
    assert snapshot.all_required_success is True
    assert len(snapshot.required_workflow_observations) == 7
    assert len(snapshot.governance_observations) == 1
    assert snapshot.governance_observations[0].workflow_name == "Contributor Attribution Check"
    assert snapshot.governance_observations[0].conclusion == "success"
    assert snapshot.observed_sha == sha
    assert len(snapshot.snapshot_digest) == 64


def test_workflow_negative_tests_tests_failed():
    sha = "d65ef87d02dba27d687043f6c7fa5c5648c6d54d"
    raw_runs = [
        {"id": 101, "name": "Tests", "head_sha": sha, "status": "completed", "conclusion": "failure"},
        {"id": 102, "name": "Lint (ruff + ty)", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 103, "name": "Typecheck", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 104, "name": "Nix", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 105, "name": "Agent Release Gate", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 106, "name": "Supply Chain Audit", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 107, "name": "History Check", "head_sha": sha, "status": "completed", "conclusion": "success"},
    ]
    from ai_engineering.supervisor.ci_provider import evaluate_workflow_runs
    snapshot = evaluate_workflow_runs("life2boat/hermes", sha, raw_runs)
    assert snapshot.overall_status == "FAILURE"
    assert snapshot.all_required_success is False


def test_workflow_negative_tests_nix_in_progress():
    sha = "d65ef87d02dba27d687043f6c7fa5c5648c6d54d"
    raw_runs = [
        {"id": 101, "name": "Tests", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 102, "name": "Lint (ruff + ty)", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 103, "name": "Typecheck", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 104, "name": "Nix", "head_sha": sha, "status": "in_progress", "conclusion": None},
        {"id": 105, "name": "Agent Release Gate", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 106, "name": "Supply Chain Audit", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 107, "name": "History Check", "head_sha": sha, "status": "completed", "conclusion": "success"},
    ]
    from ai_engineering.supervisor.ci_provider import evaluate_workflow_runs
    snapshot = evaluate_workflow_runs("life2boat/hermes", sha, raw_runs)
    assert snapshot.overall_status == "IN_PROGRESS"


def test_workflow_negative_tests_history_check_missing():
    sha = "d65ef87d02dba27d687043f6c7fa5c5648c6d54d"
    raw_runs = [
        {"id": 101, "name": "Tests", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 102, "name": "Lint (ruff + ty)", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 103, "name": "Typecheck", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 104, "name": "Nix", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 105, "name": "Agent Release Gate", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 106, "name": "Supply Chain Audit", "head_sha": sha, "status": "completed", "conclusion": "success"},
    ]
    from ai_engineering.supervisor.ci_provider import evaluate_workflow_runs
    snapshot = evaluate_workflow_runs("life2boat/hermes", sha, raw_runs)
    assert snapshot.overall_status == "MISSING"


def test_workflow_strict_success_semantics_skipped_is_failure():
    """Strict success semantics: skipped or neutral conclusion for required technical check is FAILURE."""
    sha = "d65ef87d02dba27d687043f6c7fa5c5648c6d54d"
    raw_runs = [
        {"id": 101, "name": "Tests", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 102, "name": "Lint (ruff + ty)", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 103, "name": "Typecheck", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 104, "name": "Nix", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 105, "name": "Agent Release Gate", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 106, "name": "Supply Chain Audit", "head_sha": sha, "status": "completed", "conclusion": "skipped"},
        {"id": 107, "name": "History Check", "head_sha": sha, "status": "completed", "conclusion": "success"},
    ]
    from ai_engineering.supervisor.ci_provider import evaluate_workflow_runs
    snapshot = evaluate_workflow_runs("life2boat/hermes", sha, raw_runs)
    assert snapshot.overall_status == "FAILURE"
    assert snapshot.all_required_success is False


def test_workflow_attribution_failure_does_not_block_technical():
    """Contributor Attribution Check failure is recorded as governance but does NOT block technical CI."""
    sha = "d65ef87d02dba27d687043f6c7fa5c5648c6d54d"
    raw_runs = [
        {"id": 101, "name": "Tests", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 102, "name": "Lint (ruff + ty)", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 103, "name": "Typecheck", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 104, "name": "Nix", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 105, "name": "Agent Release Gate", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 106, "name": "Supply Chain Audit", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 107, "name": "History Check", "head_sha": sha, "status": "completed", "conclusion": "success"},
        {"id": 108, "name": "Contributor Attribution Check", "head_sha": sha, "status": "completed", "conclusion": "failure"},
    ]
    from ai_engineering.supervisor.ci_provider import evaluate_workflow_runs
    snapshot = evaluate_workflow_runs("life2boat/hermes", sha, raw_runs)
    assert snapshot.overall_status == "SUCCESS"
    assert snapshot.all_required_success is True
    assert len(snapshot.governance_observations) == 1
    assert snapshot.governance_observations[0].conclusion == "failure"


def test_workflow_exact_sha_mismatch():
    sha = "d65ef87d02dba27d687043f6c7fa5c5648c6d54d"
    raw_runs = [
        {"id": 101, "name": "Tests", "head_sha": "different_sha_000000000000000000000000", "status": "completed", "conclusion": "success"},
    ]
    from ai_engineering.supervisor.ci_provider import evaluate_workflow_runs
    snapshot = evaluate_workflow_runs("life2boat/hermes", sha, raw_runs)
    assert snapshot.overall_status == "SHA_MISMATCH"


def test_ci_restart_resume_preserves_sha(tmp_path: Path):
    """Crash/restart during CI wait retains pending SHA in event replay."""
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent("t-res", "run-res")
    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id="run-res",
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    from ai_engineering.supervisor.events import create_event, SupervisorEventType
    events = store.load_events("run-res")
    target_sha = "f00baa" * 6 + "0000"
    wait_ev = create_event(
        run_id="run-res",
        sequence=len(events) + 1,
        previous_event_digest=events[-1].event_digest if events else None,
        event_type=SupervisorEventType.CI_WAIT_STARTED,
        state_revision=1,
        task_id="t-res",
        attempt_id="att-1",
        intent_digest=intent_digest(intent),
        payload={"sha": target_sha},
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    store.save_event(wait_ev)

    # Coordinator restarted
    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-res",
        astra_provider=ScriptedAstraProposalProvider(),
        ci_provider=GitHubCIStatusProvider(),
        evidence_root=str(tmp_path),
    )
    assert coord.pending_ci_sha == target_sha


@pytest.mark.asyncio
async def test_ci_max_poll_attempts_terminates_timeout(tmp_path: Path):
    """When polling exceeds max_poll_attempts, coordinator emits CI_WAIT_TIMEOUT."""
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _create_minimal_intent("t-to-poll", "run-to-poll")
    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal=intent.desired_outcome,
        root_goal_id="run-to-poll",
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)
    loop.bind_profile("run-to-poll", wp, ep)

    class MockAstraCI(ConfiguredAstraProposalProvider):
        def __init__(self):
            pass
        async def request_proposal(self, run_id, state, **kwargs):
            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id="prop-ci",
                run_id=run_id,
                task_id=state.current_task_id,
                action_type=NextActionType.WAIT_FOR_CI,
                objective="Wait for CI",
                recommended_capability="none",
                recommended_worker="none",
                expected_effect_class="READ_ONLY",
                expected_stop_boundary="READ_ONLY",
                allowed_scope=(),
                required_validators=(),
                success_criteria="",
                reasoning_summary="Check CI",
            )

    async def mock_runner_hang(repo: str, target_sha: str):
        return target_sha, [
            {"name": "Tests", "status": "in_progress", "conclusion": ""},
        ]

    ci_provider = GitHubCIStatusProvider(
        repository="life2boat/hermes",
        timeout_seconds=10.0,
        poll_interval_seconds=0.01,
        max_poll_attempts=2,
        runner=mock_runner_hang,
    )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=CrossAgentRouter(registry=AgentRegistry(), authority_resolver=AuthorityResolver(store, loop), store=PersistentStore(tmp_path / "router")),
        result_collector=ResultCollector(),
        budget=BudgetConfig(),
        run_id="run-to-poll",
        astra_provider=MockAstraCI(),
        ci_provider=ci_provider,
        evidence_root=str(tmp_path),
    )

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "CI_WAIT_TIMEOUT"

    events = store.load_events("run-to-poll")
    ev_types = [getattr(e.event_type, "value", str(e.event_type)) for e in events]
    assert "CI_STATUS_OBSERVED" in ev_types
    assert "CI_WAIT_TIMEOUT" in ev_types


@pytest.mark.asyncio
async def test_cli_provider_mode_fail_closed(tmp_path: Path, monkeypatch):
    """In real provider-mode, CLI fails closed if --astra-cmd is missing or --ci-mode != github."""
    from scripts.autonomous_run import async_main
    intent_file = tmp_path / "intent.json"
    intent = _create_minimal_intent("t-cli", "run-cli")
    from ai_engineering.task_intent import serialize_intent
    intent_file.write_text(serialize_intent(intent), encoding="utf-8")

    # 1. Real mode without --astra-cmd -> fails closed (exit code 1)
    test_args = [
        "autonomous_run.py",
        "--intent", str(intent_file),
        "--state-dir", str(tmp_path),
        "--run-id", "run-cli-1",
        "--worker-cmd", "echo",
        "--provider-mode", "real",
        "--ci-mode", "github",
    ]
    monkeypatch.setattr("sys.argv", test_args)
    rc1 = await async_main()
    assert rc1 == 1

    # 2. Real mode with --ci-mode local -> fails closed (exit code 1)
    test_args = [
        "autonomous_run.py",
        "--intent", str(intent_file),
        "--state-dir", str(tmp_path),
        "--run-id", "run-cli-2",
        "--worker-cmd", "echo",
        "--astra-cmd", "echo",
        "--provider-mode", "real",
        "--ci-mode", "local",
    ]
    monkeypatch.setattr("sys.argv", test_args)
    rc2 = await async_main()
    assert rc2 == 1

    # 3. Test mode without --astra-cmd and with --ci-mode local -> allowed
    test_args = [
        "autonomous_run.py",
        "--intent", str(intent_file),
        "--state-dir", str(tmp_path),
        "--run-id", "run-cli-3",
        "--worker-cmd", "echo",
        "--provider-mode", "test",
        "--ci-mode", "local",
    ]
    monkeypatch.setattr("sys.argv", test_args)
    rc3 = await async_main()
    assert rc3 in (0, 1)


def test_astra_proposal_schema_and_enum_validation():
    """Astra proposal validator strictly validates schema version and EffectClass/StopBoundary enums."""
    base_raw = {
        "schema_version": "hermes.astra-next-action.v1",
        "proposal_id": "prop-valid",
        "run_id": "run-1",
        "task_id": "t1",
        "action_type": "IMPLEMENT",
        "objective": "Build",
        "recommended_capability": "code",
        "recommended_worker": "codex",
        "expected_effect_class": "REPOSITORY_WRITE",
        "expected_stop_boundary": "LOCAL_DIFF",
    }

    # 1. Schema version mismatch
    bad_schema = dict(base_raw, schema_version="hermes.astra-next-action.v2")
    with pytest.raises(AstraProposalInvalidError):
        parse_and_validate_astra_proposal(bad_schema, expected_run_id="run-1", expected_task_id="t1")

    # 2. Invalid EffectClass
    bad_effect = dict(base_raw, expected_effect_class="INVALID_MUTATION_EFFECT")
    with pytest.raises(AstraProposalInvalidError):
        parse_and_validate_astra_proposal(bad_effect, expected_run_id="run-1", expected_task_id="t1")

    # 3. Invalid StopBoundary
    bad_boundary = dict(base_raw, expected_stop_boundary="RUNAWAY_UNBOUNDED")
    with pytest.raises(AstraProposalInvalidError):
        parse_and_validate_astra_proposal(bad_boundary, expected_run_id="run-1", expected_task_id="t1")


def test_astra_request_contract_rich_payload(tmp_path: Path):
    """Astra request payload contains all Section 15 contract fields."""
    intent = _create_minimal_intent("t-rich", "run-rich")
    state = _create_state(tmp_path, run_id="run-rich", task_id="t-rich")
    wp = _create_work_profile()
    ep = _create_effective_policy(intent)

    from ai_engineering.contracts import GateResult, Status
    from ai_engineering.supervisor.validator import VerifiedResult
    vr = VerifiedResult(
        schema_version="hermes.verified-result.v1",
        result_id="f" * 64,
        task_id="t-rich",
        attempt_id="att-1",
        intent_digest="a" * 64,
        base_sha="b" * 40,
        head_sha="c" * 40,
        normalized_evidence_digest="d" * 64,
        required_gates=("unit_tests",),
        gate_results=(
            GateResult(
                gate_name="unit_tests",
                required=True,
                status=Status.PASS,
                evidence_refs=(),
            ),
        ),
        blockers=(),
        status=Status.PASS,
        reason_codes=(),
        verified_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )

    req = build_canonical_astra_request(
        run_id="run-rich",
        state=state,
        intent=intent,
        budget=BudgetConfig(),
        stats={"iterations": 2},
        work_profile=wp,
        effective_policy=ep,
        verified_result=vr,
        context_pack_digest="e" * 64,
    )

    assert req["schema_version"] == "hermes.astra-request.v1"
    assert req["source"]["repository"] == "github" or req["source"]["repository"] == "life2boat/hermes"
    assert req["source"]["canonical_remote"] == "github"
    assert len(req["task_intent"]["task_intent_digest"]) == 64
    assert req["latest_verified_result"]["status"] == "PASS"
    assert len(req["latest_verified_result"]["required_gate_results"]) == 1
    assert req["latest_verified_result"]["required_gate_results"][0]["gate_name"] == "unit_tests"
    assert req["authority_boundary"]["stop_boundary"] == "LOCAL_DIFF"
    assert req["authority_boundary"]["production_execution_allowed"] is False
    assert req["context"]["context_pack_digest"] == "e" * 64


def test_astra_receipt_secret_redaction():
    """Astra receipt stores executable, argument count, and digest, never raw sensitive args."""
    from ai_engineering.supervisor.astra_provider import AstraProviderReceipt, compute_command_digest
    cmd = ["python", "-m", "secret_worker", "--token", "super_secret_password_12345"]
    cmd_dg = compute_command_digest(cmd)
    receipt = AstraProviderReceipt(
        schema_version="hermes.astra-provider-receipt.v1",
        receipt_id="r" * 64,
        run_id="run-sec",
        task_id="t-sec",
        proposal_id="prop-sec",
        action_type="IMPLEMENT",
        provider_executable="python",
        command_digest=cmd_dg,
        argument_count=len(cmd),
        exit_code=0,
        stdout_digest="out_dg",
        stderr_digest="err_dg",
        duration_ms=42,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    import dataclasses
    receipt_dict = dataclasses.asdict(receipt)
    serialized = json.dumps(receipt_dict)
    assert "super_secret_password_12345" not in serialized
    assert receipt.provider_executable == "python"
    assert receipt.argument_count == 5
    assert receipt.command_digest == cmd_dg
