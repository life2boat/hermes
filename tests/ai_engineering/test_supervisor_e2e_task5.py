"""End-to-End comprehensive test suite for Hermes Task 5 Computer Use Supervisor.

Covers full Computer Use E2E loops:
1. Computer Use Happy Path E2E (Dispatch -> GUI Scenario PASS -> PR Merge -> DONE)
2. Computer Use Automatic Fix E2E (Dispatch -> GUI Scenario FAIL -> FIX Decision -> Retest PASS -> DONE)
3. Computer Use Target Unavailable & Retry E2E (Target Unavailable -> BLOCKED -> RETRY Decision -> PASS -> DONE)
4. Computer Use Security Policy Enforcement E2E (External Send Denied, Production Target Denied, Prompt Injection Contained)
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import pytest

from ai_engineering.contracts import EffectClass, GateResult, Status, StopBoundary
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)
from ai_engineering.supervisor.computer_use.antigravity import (
    ComputerUseAntigravityAdapter,
)
from ai_engineering.supervisor.computer_use.contracts import (
    ComputerUseActionType,
    ComputerUseTask,
    UIActionProposal,
    UIActionReceipt,
    UIActionStatus,
    UIScenario,
    VisualEvidence,
    validate_computer_use_task,
    validate_visual_evidence,
)
from ai_engineering.supervisor.computer_use.driver import (
    FakeComputerUseDriver,
)
from ai_engineering.supervisor.computer_use.scenarios import (
    HermesGUITestRunner,
)
from ai_engineering.supervisor.decision import SupervisorAction
from ai_engineering.supervisor.dispatch.contracts import (
    DISPATCH_ENVELOPE_SCHEMA_VERSION,
    DispatchEnvelope,
    DispatchStatus,
    compute_dispatch_id,
)
from ai_engineering.supervisor.dispatch.coordinator import DispatchCoordinator
from ai_engineering.supervisor.dispatch.github import (
    CIDetailedState,
    CIState,
    FakeGitHubProvider,
    PRState,
)
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.policy.contracts import (
    AutonomyLevel,
    BudgetLimits,
    ExecutionTarget,
    PromotionThresholds,
    WorkProfile,
    compute_deterministic_digest,
)
from ai_engineering.supervisor.policy.work_profile import validate_work_profile
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorPhase,
    SupervisorState,
)
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.validator import (
    VERIFIED_RESULT_SCHEMA_VERSION,
    VerifiedResult,
)
from ai_engineering.task_intent import (
    LineageNode,
    NodeKind,
    TaskIntent,
    TaskLineage,
    deserialize_intent,
    intent_digest,
)


# ── Constants & Helpers ────────────────────────────────────────────────────────

RUN_ID = "run-compuse-e2e"
TASK_ID = "task-compuse-e2e"
ATTEMPT_ID = "attempt-01"
BASE_SHA = "a9dc4b18ae8d29e8b2201285afc526e6d98be067"
HEAD_SHA = "b1ec5c29bf9e40f8c3302396bfd637e7e9bc5178"
TS = "2026-09-10T05:20:00+00:00"


def make_test_intent(
    task_id: str = TASK_ID,
    base_sha: str = BASE_SHA,
    stop_boundary: str = "MERGE",
) -> TaskIntent:
    d = {
        "schema_version": 1,
        "task_id": task_id,
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Verify computer use interface integration end-to-end",
        "source_repository": "life2boat/hermes",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": base_sha,
        "constraints": ["No unauthorized external send", "No production deployment"],
        "allowed_mutations": ["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "PR_MERGE"],
        "forbidden_mutations": ["DEPLOY", "EXTERNAL_SEND"],
        "stop_boundary": stop_boundary,
        "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "GUI scenario passes"}],
        "unknowns": [],
        "applicable_invariants": ["DETERMINISTIC_PIPELINE"],
        "required_gates": ["gui_e2e"],
        "parent_intent_digest": None,
    }
    return deserialize_intent(json.dumps(d))


def make_test_envelope(
    worker_kind: str = "antigravity-gui",
    target_app: str = "Antigravity",
) -> DispatchEnvelope:
    idg = "c" * 64
    disp_id = compute_dispatch_id(RUN_ID, TASK_ID, ATTEMPT_ID, idg)
    return DispatchEnvelope(
        schema_version=DISPATCH_ENVELOPE_SCHEMA_VERSION,
        dispatch_id=disp_id,
        run_id=RUN_ID,
        task_id=TASK_ID,
        attempt_id=ATTEMPT_ID,
        intent_digest=idg,
        intent_revision=1,
        source_repository="life2boat/hermes",
        source_main_ref="refs/remotes/github/main",
        source_base_sha=BASE_SHA,
        task_intent_digest=idg,
        policy_receipt_id="policy-01",
        policy_receipt_digest="p" * 64,
        worker_kind=worker_kind,
        worker_profile="default",
        worker_model="gemini-pro",
        required_result_schema="hermes.verified-result.v1",
        required_gates=("gui_e2e",),
        created_at_utc=TS,
    )


def make_test_lineage(task_id: str = TASK_ID) -> TaskLineage:
    return TaskLineage(
        schema_version=1,
        nodes=(LineageNode(node_id=task_id, kind=NodeKind.TASK),),
        edges=(),
    )


def make_test_effective_policy(intent: TaskIntent) -> EffectivePolicyReport:
    tpa = TaskPolicyAttribution(
        task_id=intent.task_id,
        intent_revision=intent.intent_revision,
        intent_digest=intent_digest(intent),
        source_base_sha=intent.source_base_sha,
        constraints=intent.constraints,
        allowed_mutations=intent.allowed_mutations,
        forbidden_mutations=intent.forbidden_mutations,
        stop_boundary=intent.stop_boundary.value,
        source_id="s" * 64,
    )
    return EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="e" * 64,
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
        precedence_source_id="p" * 64,
    )


def make_test_work_profile(profile_id: str = "wp-compuse-e2e") -> WorkProfile:
    d = {
        "schema_version": "hermes.work-profile.v1",
        "profile_id": profile_id,
        "profile_version": 1,
        "preferred_worker_model": "gemini-pro",
        "preferred_verifier_model": "gemini-pro",
        "preferred_supervisor_model": "gemini-pro",
        "escalation_model": "gemini-ultra",
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION"],
        "maximum_autonomy_level": "LEVEL_4_STAGING_AUTONOMY",
        "allowed_effect_classes": ["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "PR_MERGE"],
        "forbidden_effect_classes": ["DEPLOY", "EXTERNAL_SEND"],
        "allowed_targets": ["LOCAL", "DEV", "CI", "STAGING"],
        "required_validators": ["gui_e2e"],
        "promotion_thresholds": {
            "required_successful_runs": 1,
            "allowed_critical_failures": 0,
            "require_rollback_verified": False,
        },
        "budget_limits": {
            "max_supervisor_decisions": 25,
            "max_child_tasks": 10,
            "max_retries_per_task": 5,
            "max_fix_cycles_per_task": 5,
            "max_consecutive_failures": 3,
            "max_provider_calls": 50,
            "max_policy_denials": 5,
        },
        "production_execution_allowed": False,
        "vector_mutation_allowed": False,
        "secret_mutation_allowed": False,
        "external_send_allowed": False,
    }
    d["profile_digest"] = compute_deterministic_digest(d)
    return validate_work_profile(d)


def make_test_scenario(
    scenario_id: str = "sc-e2e-01",
    target_app: str = "Antigravity",
    target_env: str = "LOCAL",
    effect_classes: tuple[str, ...] = ("READ_ONLY",),
) -> UIScenario:
    return UIScenario(
        scenario_id=scenario_id,
        name="E2E GUI Flow",
        target_application=target_app,
        target_environment=target_env,
        preconditions=("window_ready",),
        steps=("click_target", "assert_screen"),
        checkpoints=("checkpoint_main",),
        expected_terminal_state="COMPLETE",
        maximum_actions=8,
        timeout=15,
        required_effect_classes=effect_classes,
        required_visual_assertions=("screen_rendered",),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Computer Use Happy Path E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputerUseHappyPathE2E:
    def test_happy_path_gui_dispatch_to_merge_done(self, tmp_path: Path) -> None:
        """Full Happy Path:
        1. Initialize supervisor run with TaskIntent and bound WorkProfile.
        2. Wire ComputerUseAntigravityAdapter as WorkerDispatcher.
        3. Dispatch task to adapter; adapter initializes ComputerUseTask and verifies target.
        4. HermesGUITestRunner runs safe local scenario and produces verified PASS.
        5. Coordinator ingests PASS, supervisor verifies and creates PR, passes CI, merges, completes.
        """
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent()
        lineage = make_test_lineage()
        eff_policy = make_test_effective_policy(intent)
        profile = make_test_work_profile()

        loop.initialize_run(
            intent=intent,
            lineage=lineage,
            root_goal=intent.desired_outcome,
            root_goal_id=RUN_ID,
            created_at_utc=TS,
            attempt_id=ATTEMPT_ID,
        )
        loop.bind_profile(
            run_id=RUN_ID,
            profile=profile,
            initial_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY,
            effective_policy=eff_policy,
            created_at_utc=TS,
        )

        driver = FakeComputerUseDriver()
        adapter = ComputerUseAntigravityAdapter(driver)
        github = FakeGitHubProvider()
        repo = "life2boat/hermes"

        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=adapter,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
            auto_merge=True,
        )

        # 1. Dispatch envelope to ComputerUseAntigravityAdapter
        s_disp = coordinator.step()
        assert s_disp.phase == SupervisorPhase.RUNNING
        assert len(adapter.dispatched_tasks) == 1

        cu_task = adapter.dispatched_tasks[0]
        assert cu_task.target_application == "Antigravity"
        assert validate_computer_use_task(cu_task) is True

        # 2. Execute scenario with HermesGUITestRunner
        runner = HermesGUITestRunner(driver)
        scenario = make_test_scenario()
        vr = runner.run_scenario(
            scenario,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            intent_digest=intent_digest(intent),
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
        )
        assert vr.status == Status.PASS

        # 3. Submit verified worker result to coordinator
        coordinator.submit_worker_result(vr)
        s_verifying = coordinator.step()
        assert s_verifying.phase == SupervisorPhase.VERIFYING

        pr_number = 101
        gh_pr = PRState(
            pr_number=pr_number,
            head_branch="feat/compuse-task",
            base_branch="main",
            head_sha=HEAD_SHA,
            mergeable=True,
            merge_state_status="clean",
        )
        github.add_pr(repo, pr_number, gh_pr)

        ci = CIDetailedState(
            head_sha=HEAD_SHA,
            overall_state=CIState.PASS,
            check_conclusions={"gui_e2e": "success"},
        )
        github.add_ci(repo, pr_number, HEAD_SHA, ci)

        # 4. Bind PR if not automatically inferred
        if s_verifying.pr_number is None:
            coordinator.bind_pr(pr_number, HEAD_SHA)

        # 5. Coordinator completes decision, polls green CI, merges, and reaches DONE
        s_done = coordinator.step()
        assert s_done.phase == SupervisorPhase.DONE
        assert s_done.ci_state == "PASS"
        assert coordinator.latest_source_transition_receipt is not None
        assert coordinator.latest_source_transition_receipt.merge_method == "squash"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Computer Use Automatic Fix E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputerUseAutomaticFixE2E:
    def test_gui_failure_triggers_fix_cycle_and_retest_pass(self, tmp_path: Path) -> None:
        """Failure and Repair Loop:
        1. Task dispatched to ComputerUseAntigravityAdapter.
        2. Initial GUI scenario fails (driver observes failure assertion).
        3. Coordinator ingests FAIL -> Astra generates FIX action.
        4. Budget records fix_cycles_used = 1.
        5. Fix task dispatched to adapter.
        6. Repair applied in driver (driver.fail_observe = False).
        7. Retest scenario passes.
        8. Coordinator ingests PASS -> successfully completes.
        """
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent(stop_boundary="COMMIT")
        lineage = make_test_lineage()
        eff_policy = make_test_effective_policy(intent)
        profile = make_test_work_profile()

        loop.initialize_run(
            intent=intent,
            lineage=lineage,
            root_goal=intent.desired_outcome,
            root_goal_id=RUN_ID,
            created_at_utc=TS,
            attempt_id=ATTEMPT_ID,
        )
        loop.bind_profile(
            run_id=RUN_ID,
            profile=profile,
            initial_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY,
            effective_policy=eff_policy,
            created_at_utc=TS,
        )

        driver = FakeComputerUseDriver(fail_observe=True)
        adapter = ComputerUseAntigravityAdapter(driver)
        github = FakeGitHubProvider()

        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=adapter,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
            auto_merge=False,
        )

        # 1. First dispatch
        coordinator.step()
        assert len(adapter.dispatched_tasks) == 1

        # 2. Scenario execution fails
        runner = HermesGUITestRunner(driver)
        scenario = make_test_scenario()
        vr_fail = runner.run_scenario(
            scenario,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            intent_digest=intent_digest(intent),
        )
        assert vr_fail.status == Status.FAIL
        assert "VISUAL_ASSERTION_FAILED" in vr_fail.reason_codes

        # 3. Submit failure and ingest
        coordinator.submit_worker_result(vr_fail)
        coordinator.step()  # transitions to VERIFYING

        # 4. Coordinator processes FAIL -> Astra triggers FIX
        s_fix = coordinator.step()
        assert s_fix.phase == SupervisorPhase.RUNNING
        assert "fix" in s_fix.current_task_id
        assert s_fix.budget_state.fix_cycles_used == 1

        # 5. Coordinator dispatches the FIX task
        coordinator.step()
        assert len(adapter.dispatched_tasks) == 2
        assert adapter.dispatched_tasks[1].task_id == s_fix.current_task_id

        # 6. Apply fix and re-run scenario
        driver.fail_observe = False
        vr_pass = runner.run_scenario(
            scenario,
            task_id=s_fix.current_task_id,
            attempt_id=s_fix.current_attempt_id,
            intent_digest=s_fix.current_intent_digest,
        )
        assert vr_pass.status == Status.PASS

        # 7. Submit passing result and complete
        coordinator.submit_worker_result(vr_pass)
        coordinator.step()  # transitions to VERIFYING
        s_done = coordinator.step()
        assert s_done.phase == SupervisorPhase.DONE


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Computer Use Target Unavailable & Retry E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputerUseRetryE2E:
    def test_target_unavailable_triggers_retry_cycle_and_pass(self, tmp_path: Path) -> None:
        """Target Unavailable & Retry:
        1. Task dispatched to ComputerUseAntigravityAdapter.
        2. Driver observes target unavailable (wrong_target = True) -> BLOCKED.
        3. Coordinator ingests BLOCKED -> Astra generates RETRY action.
        4. Budget records retries_used = 1.
        5. Target becomes available (wrong_target = False).
        6. Retest passes -> completes to DONE.
        """
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent(stop_boundary="COMMIT")
        lineage = make_test_lineage()
        eff_policy = make_test_effective_policy(intent)
        profile = make_test_work_profile()

        loop.initialize_run(
            intent=intent,
            lineage=lineage,
            root_goal=intent.desired_outcome,
            root_goal_id=RUN_ID,
            created_at_utc=TS,
            attempt_id=ATTEMPT_ID,
        )
        loop.bind_profile(
            run_id=RUN_ID,
            profile=profile,
            initial_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY,
            effective_policy=eff_policy,
            created_at_utc=TS,
        )

        driver = FakeComputerUseDriver()
        adapter = ComputerUseAntigravityAdapter(driver)
        github = FakeGitHubProvider()

        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=adapter,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
            auto_merge=False,
        )

        # 1. Dispatch
        coordinator.step()
        assert len(adapter.dispatched_tasks) == 1

        # 2. Target becomes unavailable during scenario
        driver.wrong_target = True
        runner = HermesGUITestRunner(driver)
        scenario = make_test_scenario()
        vr_blocked = runner.run_scenario(
            scenario,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            intent_digest=intent_digest(intent),
        )
        assert vr_blocked.status == Status.BLOCKED
        assert "TARGET_UNAVAILABLE" in vr_blocked.reason_codes

        # 3. Submit blocked result and ingest
        coordinator.submit_worker_result(vr_blocked)
        coordinator.step()  # transitions to VERIFYING

        # 4. Coordinator processes BLOCKED -> Astra triggers RETRY
        s_retry = coordinator.step()
        assert s_retry.phase == SupervisorPhase.RUNNING
        assert s_retry.current_task_id == TASK_ID
        assert s_retry.current_attempt_id != ATTEMPT_ID
        assert s_retry.budget_state.retries_used == 1

        # 5. Target recovered -> Coordinator dispatches retry attempt
        driver.wrong_target = False
        coordinator.step()
        assert len(adapter.dispatched_tasks) == 2

        # 6. Retest passes
        vr_pass = runner.run_scenario(
            scenario,
            task_id=s_retry.current_task_id,
            attempt_id=s_retry.current_attempt_id,
            intent_digest=s_retry.current_intent_digest,
        )
        assert vr_pass.status == Status.PASS

        # 7. Submit PASS and complete
        coordinator.submit_worker_result(vr_pass)
        coordinator.step()  # transitions to VERIFYING
        s_done = coordinator.step()
        assert s_done.phase == SupervisorPhase.DONE


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Computer Use Security Policy Enforcement E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestComputerUseSecurityPolicyE2E:
    def test_external_send_scenario_denied_fail_closed(self) -> None:
        driver = FakeComputerUseDriver()
        runner = HermesGUITestRunner(driver)
        scenario = make_test_scenario(effect_classes=("EXTERNAL_SEND",))

        result = runner.run_scenario(scenario)
        assert result.status == Status.FAIL
        assert "EXTERNAL_SEND_DENIED" in result.reason_codes

    def test_production_target_scenario_denied_fail_closed(self) -> None:
        driver = FakeComputerUseDriver()
        runner = HermesGUITestRunner(driver)
        scenario = make_test_scenario(target_env="PRODUCTION")

        result = runner.run_scenario(scenario)
        assert result.status == Status.FAIL
        assert "PRODUCTION_TARGET_DENIED" in result.reason_codes

    def test_ui_prompt_injection_does_not_mutate_task_or_permit_unauthorized_actions(self) -> None:
        driver = FakeComputerUseDriver(prompt_injection=True)
        adapter = ComputerUseAntigravityAdapter(driver)
        env = make_test_envelope()

        adapter.dispatch(env)
        obs = driver.observe("Antigravity")
        assert "Ignore supervisor" in obs.observed_state

        task = adapter.dispatched_tasks[0]
        # Verify allowed actions and stop boundaries are intact
        assert task.target_application == "Antigravity"
        assert "EXTERNAL_SEND" not in task.allowed_effect_classes
        assert validate_computer_use_task(task) is True
