"""Unit and component tests for ai_engineering/supervisor/dispatch."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import pytest

from ai_engineering.contracts import EffectClass, Status, StopBoundary
from ai_engineering.supervisor.dispatch.contracts import (
    DISPATCH_ENVELOPE_SCHEMA_VERSION,
    DISPATCH_RECEIPT_SCHEMA_VERSION,
    SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION,
    DispatchEnvelope,
    DispatchReceipt,
    DispatchStatus,
    SourceTransitionReceipt,
    compute_deterministic_digest,
    compute_dispatch_id,
)
from ai_engineering.supervisor.dispatch.github import (
    CIDetailedState,
    CIState,
    FakeGitHubProvider,
    PRState,
)
from ai_engineering.supervisor.dispatch.worker import (
    AntigravityAdapter,
    FakeWorkerDispatcher,
)
from ai_engineering.supervisor.events import (
    SupervisorEvent,
    SupervisorEventType,
    create_event,
    deserialize_event,
)
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorPhase,
    SupervisorState,
    canonical_serialize_state,
    create_initial_state,
    deserialize_state,
    validate_phase_transition,
)


# ── Fixtures & Constants ───────────────────────────────────────────────────────

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
RUN_ID = "test-run-01"
TASK_ID = "task-dispatch-01"
ATTEMPT_ID = "attempt-01"
INTENT_DIGEST = "c" * 64
TS = "2026-09-10T05:00:00+00:00"


def make_test_envelope(
    run_id: str = RUN_ID,
    task_id: str = TASK_ID,
    attempt_id: str = ATTEMPT_ID,
    intent_digest: str = INTENT_DIGEST,
) -> DispatchEnvelope:
    disp_id = compute_dispatch_id(run_id, task_id, attempt_id, intent_digest)
    return DispatchEnvelope(
        schema_version=DISPATCH_ENVELOPE_SCHEMA_VERSION,
        dispatch_id=disp_id,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        intent_digest=intent_digest,
        intent_revision=1,
        source_repository="life2boat/hermes",
        source_main_ref="refs/remotes/github/main",
        source_base_sha=BASE_SHA,
        task_intent_digest=intent_digest,
        policy_receipt_id="policy-01",
        policy_receipt_digest="p" * 64,
        worker_kind="test-worker",
        worker_profile="default",
        worker_model="gemini-pro",
        required_result_schema="hermes.verified-result.v1",
        required_gates=("tests", "lint"),
        created_at_utc=TS,
    )


# ── Tests: Dispatch Contracts & IDs ───────────────────────────────────────────

class TestDispatchContracts:
    def test_compute_dispatch_id_deterministic(self):
        id1 = compute_dispatch_id(RUN_ID, TASK_ID, ATTEMPT_ID, INTENT_DIGEST)
        id2 = compute_dispatch_id(RUN_ID, TASK_ID, ATTEMPT_ID, INTENT_DIGEST)
        assert id1 == id2
        assert len(id1) == 64

    def test_compute_dispatch_id_changes_on_attempt_or_task(self):
        id1 = compute_dispatch_id(RUN_ID, TASK_ID, "attempt-01", INTENT_DIGEST)
        id2 = compute_dispatch_id(RUN_ID, TASK_ID, "attempt-02", INTENT_DIGEST)
        assert id1 != id2

    def test_dispatch_envelope_attributes(self):
        env = make_test_envelope()
        assert env.schema_version == DISPATCH_ENVELOPE_SCHEMA_VERSION
        assert env.run_id == RUN_ID
        assert env.source_base_sha == BASE_SHA
        assert env.required_gates == ("tests", "lint")


# ── Tests: Worker Dispatchers ──────────────────────────────────────────────────

class TestWorkerDispatchers:
    def test_fake_worker_dispatcher_success(self):
        dispatcher = FakeWorkerDispatcher(worker_identity="test-bot")
        env = make_test_envelope()

        receipt = dispatcher.dispatch(env)
        assert isinstance(receipt, DispatchReceipt)
        assert receipt.status == DispatchStatus.DISPATCHED
        assert receipt.worker_identity == "test-bot"
        assert receipt.dispatch_id == env.dispatch_id
        assert receipt.reason_code is None
        assert len(dispatcher.dispatched_envelopes) == 1

    def test_fake_worker_dispatcher_failure(self):
        dispatcher = FakeWorkerDispatcher(fail_to_dispatch=True)
        env = make_test_envelope()

        receipt = dispatcher.dispatch(env)
        assert receipt.status == DispatchStatus.FAILED
        assert receipt.reason_code == "DISPATCH_UNAVAILABLE"
        assert len(dispatcher.dispatched_envelopes) == 0

    def test_fake_worker_dispatcher_stage_and_poll(self):
        dispatcher = FakeWorkerDispatcher()
        env = make_test_envelope()
        dispatcher.dispatch(env)

        assert dispatcher.poll_result(env.dispatch_id) is None
        dispatcher.stage_result(env.dispatch_id, {"status": "SUCCESS"})
        assert dispatcher.poll_result(env.dispatch_id) == {"status": "SUCCESS"}

    def test_antigravity_adapter_fails_closed_when_unavailable(self):
        """Per Task 4 prompt instructions:
        The real Antigravity adapter must report: ANTIGRAVITY_STRUCTURED_DISPATCH_UNAVAILABLE
        when no supported integration is available. It must never silently pretend a task was dispatched.
        """
        adapter = AntigravityAdapter()
        env = make_test_envelope()

        receipt = adapter.dispatch(env)
        assert receipt.status == DispatchStatus.FAILED
        assert receipt.reason_code == "ANTIGRAVITY_STRUCTURED_DISPATCH_UNAVAILABLE"
        assert adapter.poll_result(env.dispatch_id) is None


# ── Tests: GitHub Provider ─────────────────────────────────────────────────────

class TestGitHubProvider:
    def test_fake_github_provider_pr_and_ci(self):
        gh = FakeGitHubProvider()
        repo = "life2boat/hermes"
        pr_number = 42

        pr = PRState(
            pr_number=pr_number,
            head_branch="feat/test",
            base_branch="main",
            head_sha=HEAD_SHA,
            mergeable=True,
            merge_state_status="clean",
        )
        gh.add_pr(repo, pr_number, pr)

        ci = CIDetailedState(
            head_sha=HEAD_SHA,
            overall_state=CIState.PASS,
            check_conclusions={"unit-tests": "success", "lint": "success"},
        )
        gh.add_ci(repo, pr_number, HEAD_SHA, ci)

        assert gh.get_pr_state(repo, pr_number) == pr
        assert gh.get_pr_state(repo, 999) is None

        # Exact-head check
        assert gh.get_ci_state(repo, pr_number, HEAD_SHA) == ci
        assert gh.get_ci_state(repo, pr_number, "other-sha") is None

    def test_fake_github_provider_merge(self):
        gh = FakeGitHubProvider()
        repo = "life2boat/hermes"
        pr = PRState(
            pr_number=10,
            head_branch="feature",
            base_branch="main",
            head_sha=HEAD_SHA,
            mergeable=True,
            merge_state_status="clean",
        )
        gh.add_pr(repo, 10, pr)

        merge_sha = gh.merge_pr(repo, 10, "squash")
        assert merge_sha is not None
        assert len(merge_sha) == 40


# ── Tests: SourceTransitionReceipt & Transitions ───────────────────────────────

class TestSourceTransitionReceipt:
    def test_source_transition_receipt_structure(self):
        receipt = SourceTransitionReceipt(
            schema_version=SOURCE_TRANSITION_RECEIPT_SCHEMA_VERSION,
            receipt_id="st-receipt-01",
            repository="life2boat/hermes",
            canonical_remote="life2boat/hermes",
            previous_canonical_main_sha=BASE_SHA,
            pr_number=101,
            pr_head_sha=HEAD_SHA,
            merge_method="squash",
            merge_commit_sha="m" * 40,
            new_canonical_main_sha="m" * 40,
            reconciliation_evidence="PR #101 squash merged",
            created_at_utc=TS,
        )
        assert receipt.schema_version == "hermes.source-transition-receipt.v1"
        assert receipt.merge_method == "squash"
        assert receipt.new_canonical_main_sha == "m" * 40

    def test_valid_phase_transition_running_to_done(self):
        # RUNNING -> DONE is valid when reconciliation completes
        validate_phase_transition(SupervisorPhase.RUNNING, SupervisorPhase.DONE)
        validate_phase_transition(SupervisorPhase.VERIFYING, SupervisorPhase.DONE)


# ── Tests: State Persistence with Active Dispatch & PR ─────────────────────────

class TestSupervisorStateDispatchParity:
    def test_state_serialization_with_dispatch_and_pr(self):
        state = create_initial_state(
            run_id=RUN_ID,
            root_goal_id=RUN_ID,
            root_goal="Task 4 Goal",
            repository="life2boat/hermes",
            canonical_remote="life2boat/hermes",
            canonical_main_ref="refs/remotes/github/main",
            task_id=TASK_ID,
            intent_digest_val=INTENT_DIGEST,
            intent_revision=1,
            base_sha=BASE_SHA,
            attempt_id=ATTEMPT_ID,
            created_at_utc=TS,
            active_dispatch_id="disp-1234",
            active_dispatch_digest="d" * 64,
            pr_number=42,
            pr_head_sha=HEAD_SHA,
            ci_state="PASS",
        )

        assert state.active_dispatch_id == "disp-1234"
        assert state.pr_number == 42
        assert state.pr_head_sha == HEAD_SHA
        assert state.ci_state == "PASS"

        # Canonical serialize & deserialize
        serialized = canonical_serialize_state(state)
        restored = deserialize_state(serialized)

        assert restored.active_dispatch_id == state.active_dispatch_id
        assert restored.active_dispatch_digest == state.active_dispatch_digest
        assert restored.pr_number == state.pr_number
        assert restored.pr_head_sha == state.pr_head_sha
        assert restored.ci_state == state.ci_state
        assert restored.run_id == state.run_id
        assert restored.current_base_sha == state.current_base_sha
