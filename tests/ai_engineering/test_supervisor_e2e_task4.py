"""End-to-End comprehensive test suite for Hermes Task 4 Autonomous Supervisor.

Covers ALL Task 4 rules:
1. HAPPY PATH
2. AUTOMATIC FIX
3. AUTOMATIC RETRY
4. SQUASH MERGE
5. CRASH / RESTART
6. BUDGET STOP
7. LATE RESULT
8. CLI ENTRY POINT E2E
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import pytest

from ai_engineering.contracts import EffectClass, Status, StopBoundary
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)
from ai_engineering.supervisor.decision import SupervisorAction
from ai_engineering.supervisor.dispatch.contracts import (
    DISPATCH_ENVELOPE_SCHEMA_VERSION,
    SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION,
    compute_dispatch_id,
)
from ai_engineering.supervisor.dispatch.coordinator import DispatchCoordinator
from ai_engineering.supervisor.dispatch.github import (
    CIDetailedState,
    CIState,
    FakeGitHubProvider,
    PRState,
)
from ai_engineering.supervisor.dispatch.worker import FakeWorkerDispatcher
from ai_engineering.supervisor.events import SupervisorEventType
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
    GateResult,
    VerifiedResult,
    VERIFIED_RESULT_SCHEMA_VERSION,
)
from ai_engineering.task_intent import (
    LineageNode,
    NodeKind,
    TaskIntent,
    TaskLineage,
    deserialize_intent,
    intent_digest,
)


# ── Test Helpers & Fixtures ───────────────────────────────────────────────────

BASE_SHA = "a9dc4b18ae8d29e8b2201285afc526e6d98be067"
HEAD_SHA = "b1ec5c29bf9e40f8c3302396bfd637e7e9bc5178"
RUN_ID = "run-task4-e2e"
TASK_ID = "task-task4-e2e"
ATTEMPT_ID = "task-task4-e2e-attempt-1"
TS = "2026-09-10T05:10:00+00:00"


def make_test_intent(
    task_id: str = TASK_ID,
    base_sha: str = BASE_SHA,
    stop_boundary: str = "MERGE",
    allowed_mutations: list[str] | None = None,
    forbidden_mutations: list[str] | None = None,
) -> TaskIntent:
    d = {
        "schema_version": 1,
        "task_id": task_id,
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Implement autonomous dispatch and supervisor loop",
        "source_repository": "life2boat/hermes",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": base_sha,
        "constraints": ["No new external deps"],
        "allowed_mutations": allowed_mutations if allowed_mutations is not None else ["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "PR_MERGE"],
        "forbidden_mutations": forbidden_mutations if forbidden_mutations is not None else ["DEPLOY"],
        "stop_boundary": stop_boundary,
        "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "All gates pass"}],
        "unknowns": [],
        "applicable_invariants": ["DETERMINISTIC_PIPELINE"],
        "required_gates": ["tests", "lint"],
        "parent_intent_digest": None,
    }
    return deserialize_intent(json.dumps(d))


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


def make_profile_dict(
    profile_id: str = "wp-task4-e2e",
    max_autonomy_level: str = "LEVEL_4_STAGING_AUTONOMY",
    budget_limits: dict | None = None,
    forbidden_effect_classes: list[str] | None = None,
) -> dict:
    d = {
        "schema_version": "hermes.work-profile.v1",
        "profile_id": profile_id,
        "profile_version": 1,
        "preferred_worker_model": "gemini-pro",
        "preferred_verifier_model": "gemini-pro",
        "preferred_supervisor_model": "gemini-pro",
        "escalation_model": "gemini-ultra",
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION", "ANALYSIS"],
        "maximum_autonomy_level": max_autonomy_level,
        "allowed_effect_classes": ["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "PR_MERGE"],
        "forbidden_effect_classes": forbidden_effect_classes if forbidden_effect_classes is not None else ["DEPLOY"],
        "allowed_targets": ["LOCAL", "DEV", "CI", "STAGING"],
        "required_validators": ["tests", "lint"],
        "promotion_thresholds": {
            "required_successful_runs": 1,
            "allowed_critical_failures": 0,
            "require_rollback_verified": False,
        },
        "budget_limits": budget_limits or {
            "max_supervisor_decisions": 20,
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
    return d


def make_test_work_profile(
    profile_id: str = "wp-task4-e2e",
    max_autonomy_level: str = "LEVEL_4_STAGING_AUTONOMY",
    budget_limits: dict | None = None,
    forbidden_effect_classes: list[str] | None = None,
) -> WorkProfile:
    return validate_work_profile(make_profile_dict(
        profile_id=profile_id,
        max_autonomy_level=max_autonomy_level,
        budget_limits=budget_limits,
        forbidden_effect_classes=forbidden_effect_classes,
    ))


def make_verified_result(
    task_id: str,
    attempt_id: str,
    intent_dg: str,
    base_sha: str = BASE_SHA,
    head_sha: str = HEAD_SHA,
    status: Status = Status.PASS,
) -> VerifiedResult:
    test_status = Status.PASS if status == Status.PASS else (Status.BLOCKED if status == Status.BLOCKED else Status.FAIL)
    lint_status = Status.PASS if status != Status.BLOCKED else Status.BLOCKED

    gr = (
        GateResult(gate_name="tests", required=True, status=test_status, evidence_refs=("ref-test",)),
        GateResult(gate_name="lint", required=True, status=lint_status, evidence_refs=("ref-lint",)),
    )
    res_id = compute_deterministic_digest({
        "task_id": task_id,
        "attempt_id": attempt_id,
        "intent_digest": intent_dg,
        "status": status.value,
    })
    return VerifiedResult(
        schema_version=VERIFIED_RESULT_SCHEMA_VERSION,
        result_id=res_id,
        task_id=task_id,
        attempt_id=attempt_id,
        intent_digest=intent_dg,
        base_sha=base_sha,
        head_sha=head_sha,
        normalized_evidence_digest="n" * 64,
        required_gates=("tests", "lint"),
        gate_results=gr,
        blockers=() if status != Status.BLOCKED else ("REASON_BLOCKED",),
        status=status,
        reason_codes=() if status == Status.PASS else ("TEST_FAILURE",),
        verified_at_utc=TS,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 1. HAPPY PATH E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2EHappyPath:
    def test_happy_path_complete_lifecycle(self, tmp_path):
        """Rule 1: HAPPY PATH
        Full cycle: init -> dispatch envelope -> worker result (pass) ->
        PR bound -> exact-head CI pass -> squash merge -> SOURCE_RECONCILED -> DONE.
        """
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent()
        lineage = make_test_lineage()
        eff_policy = make_test_effective_policy(intent)
        profile = make_test_work_profile()

        # Initialize run
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

        dispatcher = FakeWorkerDispatcher()
        github = FakeGitHubProvider()

        # Setup PR and CI in provider
        pr_number = 101
        github.add_pr(
            repository=intent.source_repository,
            pr_number=pr_number,
            pr=PRState(
                pr_number=pr_number,
                head_branch="feat/task4",
                base_branch="main",
                head_sha=HEAD_SHA,
                mergeable=True,
                merge_state_status="clean",
            ),
        )
        github.add_ci(
            repository=intent.source_repository,
            pr_number=pr_number,
            head_sha=HEAD_SHA,
            ci=CIDetailedState(
                head_sha=HEAD_SHA,
                overall_state=CIState.PASS,
                check_conclusions={"tests": "success", "lint": "success"},
            ),
        )

        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
        )

        # Step 1: Dispatches task
        s1 = coordinator.step()
        assert s1.phase == SupervisorPhase.RUNNING
        assert s1.active_dispatch_id is not None
        assert len(dispatcher.dispatched_envelopes) == 1
        assert dispatcher.dispatched_envelopes[0].task_id == TASK_ID

        # Stage passing worker result
        idg = intent_digest(intent)
        vr_pass = make_verified_result(TASK_ID, ATTEMPT_ID, idg, status=Status.PASS)
        coordinator.submit_worker_result(vr_pass)

        # Step 2: Ingests result, enters VERIFYING, binds PR
        s2 = coordinator.step()
        assert s2.phase == SupervisorPhase.VERIFYING
        assert s2.latest_verified_result_id == vr_pass.result_id

        # Bind PR explicitly if not automatically inferred
        if s2.pr_number is None:
            coordinator.bind_pr(pr_number, HEAD_SHA)

        # Step 3: Polls CI -> CI passes -> squash merges -> reconciles -> DONE
        s3 = coordinator.step()
        assert s3.phase == SupervisorPhase.DONE
        assert s3.ci_state == "PASS"

        # Verify SourceTransitionReceipt and base_sha update
        receipt = coordinator.latest_source_transition_receipt
        assert receipt is not None
        assert receipt.schema_version == SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION
        assert receipt.merge_method == "squash"
        assert receipt.previous_canonical_main_sha == BASE_SHA
        assert receipt.new_canonical_main_sha == s3.current_base_sha
        assert receipt.new_canonical_main_sha == receipt.merge_commit_sha

        # Verify event sequence
        events = store.load_events(RUN_ID)
        event_types = [e.event_type for e in events]
        assert SupervisorEventType.DISPATCH_SENT in event_types
        assert SupervisorEventType.WORKER_RESULT_RECEIVED in event_types
        assert SupervisorEventType.CI_PASSED in event_types
        assert SupervisorEventType.SOURCE_RECONCILED in event_types
        assert SupervisorEventType.RUN_COMPLETED in event_types


# ═══════════════════════════════════════════════════════════════════════════════
# 2. AUTOMATIC FIX E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2EAutomaticFix:
    def test_automatic_fix_on_fail_result(self, tmp_path):
        """Rule 2: AUTOMATIC FIX
        Worker result FAIL -> Astra automatically creates FIX decision ->
        next fix task generated and dispatched -> passes -> completes.
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

        dispatcher = FakeWorkerDispatcher()
        github = FakeGitHubProvider()
        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
        )

        # 1. First dispatch
        coordinator.step()
        assert len(dispatcher.dispatched_envelopes) == 1

        # 2. Worker result fails
        idg = intent_digest(intent)
        vr_fail = make_verified_result(TASK_ID, ATTEMPT_ID, idg, status=Status.FAIL)
        coordinator.submit_worker_result(vr_fail)

        # 3. Ingest FAIL result
        s_verifying = coordinator.step()
        assert s_verifying.phase == SupervisorPhase.VERIFYING

        # 4. Coordinator handles FAIL -> automatically generates FIX decision via Astra
        s_fix = coordinator.step()
        assert s_fix.phase == SupervisorPhase.RUNNING
        assert s_fix.current_task_id != TASK_ID
        assert "fix" in s_fix.current_task_id
        assert s_fix.budget_state.fix_cycles_used == 1

        # 5. Coordinator dispatches the new FIX task
        s_disp2 = coordinator.step()
        assert len(dispatcher.dispatched_envelopes) == 2
        assert dispatcher.dispatched_envelopes[1].task_id == s_fix.current_task_id

        # 6. Second worker result passes
        vr_pass = make_verified_result(
            s_fix.current_task_id,
            s_fix.current_attempt_id,
            s_fix.current_intent_digest,
            status=Status.PASS,
        )
        coordinator.submit_worker_result(vr_pass)

        # 7. Ingest passing result & complete
        coordinator.step()
        s_done = coordinator.step()
        assert s_done.phase == SupervisorPhase.DONE


# ═══════════════════════════════════════════════════════════════════════════════
# 3. AUTOMATIC RETRY E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2EAutomaticRetry:
    def test_automatic_retry_on_blocked(self, tmp_path):
        """Rule 3: AUTOMATIC RETRY
        Worker result BLOCKED / timeout -> Astra automatically creates RETRY decision ->
        next attempt generated -> dispatches retry -> passes -> completes.
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

        dispatcher = FakeWorkerDispatcher()
        github = FakeGitHubProvider()
        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
        )

        # 1. First dispatch
        coordinator.step()

        # 2. Worker result is BLOCKED
        idg = intent_digest(intent)
        vr_blocked = make_verified_result(TASK_ID, ATTEMPT_ID, idg, status=Status.BLOCKED)
        coordinator.submit_worker_result(vr_blocked)

        # 3. Ingest BLOCKED result
        coordinator.step()

        # 4. Coordinator handles BLOCKED -> automatically generates RETRY decision
        s_retry = coordinator.step()
        assert s_retry.phase == SupervisorPhase.RUNNING
        assert s_retry.attempt_number == 2
        assert s_retry.budget_state.retries_used == 1

        # 5. Coordinator dispatches retry attempt
        coordinator.step()
        assert len(dispatcher.dispatched_envelopes) == 2
        assert dispatcher.dispatched_envelopes[1].attempt_id == s_retry.current_attempt_id

        # 6. Retry attempt passes
        vr_pass = make_verified_result(
            s_retry.current_task_id,
            s_retry.current_attempt_id,
            s_retry.current_intent_digest,
            status=Status.PASS,
        )
        coordinator.submit_worker_result(vr_pass)

        # 7. Ingest passing result & complete
        coordinator.step()
        s_done = coordinator.step()
        assert s_done.phase == SupervisorPhase.DONE


# ═══════════════════════════════════════════════════════════════════════════════
# 4. SQUASH MERGE E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2ESquashMerge:
    def test_squash_merge_produces_receipt_and_updates_base(self, tmp_path):
        """Rule 4: SQUASH MERGE
        Explicit verification of squash merge reconciliation, receipt generation,
        and current_base_sha advancement.
        """
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent()
        profile = make_test_work_profile()
        eff_policy = make_test_effective_policy(intent)

        loop.initialize_run(
            intent=intent,
            lineage=make_test_lineage(),
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

        dispatcher = FakeWorkerDispatcher()
        github = FakeGitHubProvider()
        pr_number = 77
        github.add_pr(
            intent.source_repository,
            pr_number,
            PRState(pr_number, "head-branch", "main", HEAD_SHA, True, "clean"),
        )
        github.add_ci(
            intent.source_repository,
            pr_number,
            HEAD_SHA,
            CIDetailedState(HEAD_SHA, CIState.PASS, {"tests": "success"}),
        )

        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
        )

        # Dispatch and provide passing result
        coordinator.step()
        vr = make_verified_result(TASK_ID, ATTEMPT_ID, intent_digest(intent), status=Status.PASS)
        coordinator.submit_worker_result(vr)
        coordinator.bind_pr(pr_number, HEAD_SHA)

        # Run until quiescent
        final_state = coordinator.run_until_quiescent()
        assert final_state.phase == SupervisorPhase.DONE

        # Check receipt
        receipt = coordinator.latest_source_transition_receipt
        assert receipt is not None
        assert receipt.merge_method == "squash"
        assert receipt.previous_canonical_main_sha == BASE_SHA
        assert receipt.pr_number == pr_number
        assert receipt.pr_head_sha == HEAD_SHA
        assert len(receipt.new_canonical_main_sha) == 40
        assert final_state.current_base_sha == receipt.new_canonical_main_sha


# ═══════════════════════════════════════════════════════════════════════════════
# 5. CRASH / RESTART E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2ECrashRestart:
    def test_crash_after_dispatch_recovers_and_completes(self, tmp_path):
        """Rule 5: CRASH / RESTART
        Supervisor crashes right after dispatch. New coordinator instance loaded
        from disk recovers active dispatch and in-flight attempt seamlessly.
        """
        store1 = FileSupervisorStateStore(tmp_path)
        loop1 = SupervisorLoop(store1)
        intent = make_test_intent()
        profile = make_test_work_profile()
        eff_policy = make_test_effective_policy(intent)

        loop1.initialize_run(
            intent=intent,
            lineage=make_test_lineage(),
            root_goal=intent.desired_outcome,
            root_goal_id=RUN_ID,
            created_at_utc=TS,
            attempt_id=ATTEMPT_ID,
        )
        loop1.bind_profile(
            run_id=RUN_ID,
            profile=profile,
            initial_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY,
            effective_policy=eff_policy,
            created_at_utc=TS,
        )

        dispatcher1 = FakeWorkerDispatcher()
        github1 = FakeGitHubProvider()
        coord1 = DispatchCoordinator(
            loop=loop1,
            store=store1,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher1,
            github_provider=github1,
            work_profile=profile,
            effective_policy=eff_policy,
        )

        # 1. Dispatch sent
        s1 = coord1.step()
        assert s1.active_dispatch_id is not None
        dispatch_id = s1.active_dispatch_id

        # ──── CRASH HAPPENS HERE: Drop coord1, loop1, store1 completely ────
        del coord1
        del loop1
        del store1

        # 2. Restart supervisor with new instances pointing to the same disk directory
        store2 = FileSupervisorStateStore(tmp_path)
        loop2 = SupervisorLoop(store2)
        dispatcher2 = FakeWorkerDispatcher()
        github2 = FakeGitHubProvider()

        pr_number = 202
        github2.add_pr(
            intent.source_repository,
            pr_number,
            PRState(pr_number, "branch", "main", HEAD_SHA, True, "clean"),
        )
        github2.add_ci(
            intent.source_repository,
            pr_number,
            HEAD_SHA,
            CIDetailedState(HEAD_SHA, CIState.PASS, {"tests": "success"}),
        )

        coord2 = DispatchCoordinator(
            loop=loop2,
            store=store2,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher2,
            github_provider=github2,
            work_profile=profile,
            effective_policy=eff_policy,
        )

        recovered_state = coord2.get_state()
        assert recovered_state.active_dispatch_id == dispatch_id
        assert recovered_state.phase == SupervisorPhase.RUNNING
        assert recovered_state.current_task_id == TASK_ID

        # 3. Resume: provide worker result to recovered coordinator
        vr = make_verified_result(TASK_ID, ATTEMPT_ID, intent_digest(intent), status=Status.PASS)
        coord2.submit_worker_result(vr)
        coord2.bind_pr(pr_number, HEAD_SHA)

        # 4. Completes normally to DONE
        final_state = coord2.run_until_quiescent()
        assert final_state.phase == SupervisorPhase.DONE
        assert final_state.current_base_sha != BASE_SHA


# ═══════════════════════════════════════════════════════════════════════════════
# 6. BUDGET STOP E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2EBudgetStop:
    def test_budget_exhaustion_stops_autonomous_continuation(self, tmp_path):
        """Rule 6: BUDGET STOP
        WorkProfile sets max_fix_cycles_per_task=1. After 1 fix cycle, a second FAIL
        triggers policy DENY with BUDGET_EXHAUSTED -> stops in BLOCKED phase.
        """
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent()
        eff_policy = make_test_effective_policy(intent)

        # Budget allows only 1 fix cycle!
        strict_budget = {
            "max_supervisor_decisions": 10,
            "max_child_tasks": 5,
            "max_retries_per_task": 2,
            "max_fix_cycles_per_task": 1,  # LIMIT IS 1
            "max_consecutive_failures": 5,
            "max_provider_calls": 20,
            "max_policy_denials": 3,
        }
        profile = make_test_work_profile(budget_limits=strict_budget)

        loop.initialize_run(
            intent=intent,
            lineage=make_test_lineage(),
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

        dispatcher = FakeWorkerDispatcher()
        github = FakeGitHubProvider()
        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher,
            github_provider=github,
            work_profile=profile,
            effective_policy=eff_policy,
        )

        # Attempt 1: Dispatched, fails -> Allowed fix cycle 1
        coordinator.step()
        vr1 = make_verified_result(TASK_ID, ATTEMPT_ID, intent_digest(intent), status=Status.FAIL)
        coordinator.submit_worker_result(vr1)
        coordinator.step()  # Ingest
        s_fix1 = coordinator.step()  # Generates fix
        assert s_fix1.phase == SupervisorPhase.RUNNING
        assert s_fix1.budget_state.fix_cycles_used == 1

        # Attempt 2: Dispatched, fails again!
        coordinator.step()  # Dispatches fix task
        vr2 = make_verified_result(
            s_fix1.current_task_id,
            s_fix1.current_attempt_id,
            s_fix1.current_intent_digest,
            status=Status.FAIL,
        )
        coordinator.submit_worker_result(vr2)
        coordinator.step()  # Ingest

        # Next decision evaluation should DENY because fix_cycles_used >= max_fix_cycles_per_task
        s_blocked = coordinator.step()
        assert s_blocked.phase == SupervisorPhase.BLOCKED
        assert any("POLICY_DENIED" in b or "BUDGET_EXHAUSTED" in b for b in s_blocked.blockers)

        # Further stepping does NOT dispatch any new tasks
        count_before = len(dispatcher.dispatched_envelopes)
        coordinator.step()
        assert len(dispatcher.dispatched_envelopes) == count_before
        assert coordinator.get_state().phase == SupervisorPhase.BLOCKED


# ═══════════════════════════════════════════════════════════════════════════════
# 7. LATE RESULT E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2ELateResult:
    def test_late_result_from_older_attempt_is_rejected(self, tmp_path):
        """Rule 7: LATE RESULT
        Result from older attempt or wrong task received while supervisor is on a newer
        attempt/task is safely rejected without corrupting active state.
        """
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent()
        profile = make_test_work_profile()
        eff_policy = make_test_effective_policy(intent)

        loop.initialize_run(
            intent=intent,
            lineage=make_test_lineage(),
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

        dispatcher = FakeWorkerDispatcher()
        coordinator = DispatchCoordinator(
            loop=loop,
            store=store,
            run_id=RUN_ID,
            worker_dispatcher=dispatcher,
            github_provider=FakeGitHubProvider(),
            work_profile=profile,
            effective_policy=eff_policy,
        )

        coordinator.step()  # Dispatch attempt 1

        # Advance attempt to attempt 2 via retry
        idg = intent_digest(intent)
        vr_blocked = make_verified_result(TASK_ID, ATTEMPT_ID, idg, status=Status.BLOCKED)
        coordinator.submit_worker_result(vr_blocked)
        coordinator.step()  # Ingest
        s_retry = coordinator.step()  # Retry -> attempt 2
        assert s_retry.attempt_number == 2
        assert s_retry.current_attempt_id != ATTEMPT_ID

        # Now worker sends a late result for attempt 1!
        late_result = make_verified_result(TASK_ID, ATTEMPT_ID, idg, status=Status.PASS)
        coordinator.submit_worker_result(late_result)

        # Step coordinator
        s_after = coordinator.step()

        # The late result must NOT overwrite attempt 2!
        assert len(coordinator._late_results) == 1
        assert "ATTEMPT_MISMATCH" in coordinator._late_results[0]["reason"]
        assert s_after.current_attempt_id == s_retry.current_attempt_id
        assert s_after.attempt_number == 2


# ═══════════════════════════════════════════════════════════════════════════════
# 8. CLI ENTRY POINT E2E
# ═══════════════════════════════════════════════════════════════════════════════

class TestE2ECLIEntryPoint:
    def test_run_autonomous_supervisor_cli(self, tmp_path):
        """Rule 8: CLI entry point executes cleanly and produces exit 0 on DONE."""
        intent = make_test_intent()
        intent_file = tmp_path / "intent.json"
        intent_file.write_text(json.dumps({
            "schema_version": 1,
            "task_id": "TASK-CLI-01",
            "intent_revision": 1,
            "status": "READY",
            "task_class": "BOUNDED_IMPLEMENTATION",
            "desired_outcome": "CLI test run",
            "source_repository": "life2boat/hermes",
            "source_main_ref": "refs/remotes/github/main",
            "source_base_sha": BASE_SHA,
            "constraints": [],
            "allowed_mutations": ["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "PR_MERGE"],
            "forbidden_mutations": [],
            "stop_boundary": "MERGE",
            "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "Pass"}],
            "unknowns": [],
            "applicable_invariants": ["DETERMINISTIC_PIPELINE"],
            "required_gates": ["tests", "lint"],
            "parent_intent_digest": None,
        }))

        profile_file = tmp_path / "profile.json"
        profile_file.write_text(json.dumps(make_profile_dict()))

        # Create worker result file
        intent = deserialize_intent(intent_file.read_text())
        idg = intent_digest(intent)
        vr = make_verified_result("TASK-CLI-01", "TASK-CLI-01-attempt-1", idg, status=Status.PASS)
        vr_file = tmp_path / "vr_pass.json"
        from ai_engineering.supervisor.validator import canonical_serialize_verified_result
        vr_file.write_text(canonical_serialize_verified_result(vr))

        output_file = tmp_path / "final_state.json"
        state_dir = tmp_path / "supervisor_state"

        from scripts.run_autonomous_supervisor import main
        exit_code = main([
            "--intent", str(intent_file),
            "--profile", str(profile_file),
            "--state-dir", str(state_dir),
            "--worker-result", str(vr_file),
            "--pr-number", "555",
            "--ci-state", "PASS",
            "--output", str(output_file),
        ])

        assert exit_code == 0
        assert output_file.exists()
        final_state_data = json.loads(output_file.read_text())
        assert final_state_data["phase"] == "DONE"
