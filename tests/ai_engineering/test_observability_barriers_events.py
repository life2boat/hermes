"""PR-12 observability: barrier explanations, blockers, and event timeline."""

from __future__ import annotations

import dataclasses

from ai_engineering.control_plane.contracts import ControlPlaneEventType, ValidationEvidence
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.observability.contracts import BarrierName, OperatorHealthState, ProjectionStatus
from ai_engineering.observability.collector import OperatorQueries
from ai_engineering.requalification.requalification_contracts import RequalificationDecisionState
from tests.ai_engineering.observability_fixture_helpers import (
    CANDIDATE_ID,
    collect_full,
    make_event,
    make_requalification_result,
)
from tests.ai_engineering.control_plane_fixture_helpers import make_state


def barrier_map(snap):
    return {b.barrier_name: b for b in snap.barriers}


class TestBarrierExplanations:
    def test_all_canonical_barriers_present(self):
        snap = collect_full()
        names = {b.barrier_name for b in snap.barriers}
        assert names == {
            BarrierName.VALIDATION.value,
            BarrierName.REQUALIFICATION.value,
            BarrierName.HANDOFF_READINESS.value,
            BarrierName.PRODUCTION_SERIALIZATION.value,
            BarrierName.REMOTE_EXECUTION_VERIFIABILITY.value,
            BarrierName.CANDIDATE_COMPLETION.value,
            BarrierName.CANDIDATE_JUDGEMENT.value,
        }

    def test_ready_barriers_have_no_reasons(self):
        snap = collect_full()
        for barrier in snap.barriers:
            if barrier.ready:
                assert barrier.reason_codes == ()
                assert barrier.missing_requirements == ()

    def test_not_ready_barrier_explains_why(self):
        snap = collect_full(validation=None)
        barrier = barrier_map(snap)[BarrierName.HANDOFF_READINESS.value]
        assert barrier.ready is False
        assert barrier.reason_codes  # not just ready=false
        assert barrier.missing_requirements == barrier.reason_codes
        assert all(isinstance(code, str) for code in barrier.reason_codes)

    def test_validation_barrier_missing_evidence(self):
        snap = collect_full(validation=None)
        barrier = barrier_map(snap)[BarrierName.VALIDATION.value]
        assert barrier.ready is False
        assert "OBSERVABILITY_EVIDENCE_MISSING" in barrier.reason_codes

    def test_validation_barrier_stale_evidence(self):
        state = dataclasses.replace(make_state(), requalification_required=True)
        snap = collect_full(cycle=state, requalification_result=None)
        barrier = barrier_map(snap)[BarrierName.VALIDATION.value]
        assert barrier.ready is False
        assert "CANDIDATE_VALIDATION_STALE" in barrier.reason_codes

    def test_requalification_barrier(self):
        state = dataclasses.replace(make_state(), requalification_required=True)
        snap = collect_full(cycle=state, requalification_result=None)
        barrier = barrier_map(snap)[BarrierName.REQUALIFICATION.value]
        assert barrier.ready is False
        assert "CANDIDATE_REQUALIFICATION_REQUIRED" in barrier.reason_codes

    def test_requalification_barrier_satisfied(self):
        result = make_requalification_result(
            decision_state=RequalificationDecisionState.REQUALIFIED
        )
        state = dataclasses.replace(make_state(), requalification_required=True)
        snap = collect_full(cycle=state, requalification_result=result)
        assert barrier_map(snap)[BarrierName.REQUALIFICATION.value].ready is True

    def test_candidate_completion_barrier_unmet(self):
        snap = collect_full(candidate_results={})
        barrier = barrier_map(snap)[BarrierName.CANDIDATE_COMPLETION.value]
        assert barrier.ready is False

    def test_judgement_barrier_unmet(self):
        snap = collect_full(judge_result=None)
        barrier = barrier_map(snap)[BarrierName.CANDIDATE_JUDGEMENT.value]
        assert barrier.ready is False
        assert "OBSERVABILITY_EVIDENCE_MISSING" in barrier.reason_codes

    def test_remote_verifiability_barrier(self):
        from ai_engineering.execution.remote_contracts import RemoteExecutionState

        snap = collect_full(_remote_state=RemoteExecutionState.UNVERIFIABLE)
        barrier = barrier_map(snap)[BarrierName.REMOTE_EXECUTION_VERIFIABILITY.value]
        assert barrier.ready is False
        assert "REMOTE_EXECUTION_UNVERIFIABLE" in barrier.reason_codes


class TestMachineReasonCodes:
    def test_reason_codes_are_machine_readable(self):
        snap = collect_full(validation=None, judge_result=None)
        for barrier in snap.barriers:
            for code in barrier.reason_codes:
                assert code == code.upper()
                assert " " not in code

    def test_no_free_text_only_explanations(self):
        snap = collect_full(validation=None)
        barrier = barrier_map(snap)[BarrierName.HANDOFF_READINESS.value]
        assert barrier.reason_codes == barrier.missing_requirements
        for code in barrier.reason_codes:
            assert code.replace("_", "").isalnum()


class TestBlockerView:
    def test_cycle_blockers_aggregated(self):
        state = dataclasses.replace(
            make_state(), blockers=("BLOCKER_A", "BLOCKER_B")
        )
        snap = collect_full(cycle=state)
        codes = [b.code for b in snap.blockers]
        assert "BLOCKER_A" in codes and "BLOCKER_B" in codes

    def test_candidate_blockers_aggregated(self):
        from tests.ai_engineering.observability_fixture_helpers import (
            make_candidate_result,
        )
        from ai_engineering.candidates.candidate_contracts import CandidateState

        result = make_candidate_result(
            state=CandidateState.FAILED,
            success=False,
            blockers=("CANDIDATE_BLOCKER_X",),
        )
        snap = collect_full(candidate_results={CANDIDATE_ID: result})
        scopes = [(b.scope, b.code) for b in snap.blockers]
        assert ("candidate", "CANDIDATE_BLOCKER_X") in scopes

    def test_blockers_sorted_deterministically(self):
        state = dataclasses.replace(make_state(), blockers=("Z_BLOCKER", "A_BLOCKER", "M_BLOCKER"))
        snap = collect_full(cycle=state)
        cycle_blockers = [b.code for b in snap.blockers if b.scope == "cycle"]
        assert cycle_blockers == sorted(cycle_blockers)

    def test_blockers_cannot_be_cleared(self):
        state = dataclasses.replace(make_state(), blockers=("BLOCKER_A",))
        snap = collect_full(cycle=state)
        assert snap.control_plane.blockers == ("BLOCKER_A",)
        assert snap.projection_health.health is OperatorHealthState.BLOCKED


class TestEventTimeline:
    def test_deterministic_ordering_by_created_then_id(self):
        events = [
            make_event(event_id="evt-b", created_at="2026-01-15T12:05:00+00:00"),
            make_event(event_id="evt-a", created_at="2026-01-15T12:05:00+00:00"),
            make_event(event_id="evt-c", created_at="2026-01-15T12:04:00+00:00"),
        ]
        snap = collect_full(raw_events=events)
        ids = [e.event_id for e in snap.event_timeline]
        assert ids == ["evt-c", "evt-a", "evt-b"]

    def test_insertion_order_does_not_matter(self):
        events_a = [
            make_event(event_id="evt-1", created_at="2026-01-15T12:05:00+00:00"),
            make_event(event_id="evt-2", created_at="2026-01-15T12:06:00+00:00"),
        ]
        events_b = list(reversed(events_a))
        snap_a = collect_full(raw_events=events_a)
        snap_b = collect_full(raw_events=events_b)
        assert [e.event_id for e in snap_a.event_timeline] == [
            e.event_id for e in snap_b.event_timeline
        ]

    def test_duplicate_event_visibility(self):
        duplicate = make_event(event_id="evt-1", created_at="2026-01-15T12:05:00+00:00")
        snap = collect_full(raw_events=(duplicate, duplicate))
        statuses = [e.status for e in snap.event_timeline]
        assert statuses.count("DUPLICATE") == 1
        assert statuses.count("ACCEPTED") == 1

    def test_event_collision_visibility(self):
        original = make_event(event_id="evt-1", workspace_id="ws-1")
        collision = make_event(event_id="evt-1", workspace_id="ws-2")
        snap = collect_full(raw_events=(original, collision))
        statuses = [e.status for e in snap.event_timeline]
        assert "COLLISION_EVIDENCE" in statuses
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_event_wrong_cycle_is_conflict(self):
        event = make_event(event_id="evt-x", cycle_id="cycle-other")
        snap = collect_full(raw_events=(event,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_event_stale_epoch_is_stale(self):
        event = make_event(event_id="evt-old", execution_epoch=7)
        snap = collect_full(raw_events=(event,))
        assert snap.projection_status is ProjectionStatus.STALE

    def test_timeline_entry_fields(self):
        event = make_event(
            event_id="evt-full",
            run_id="run-1",
            workspace_id="ws-1",
            candidate_id="cand-1",
            execution_host_id="host-local",
        )
        snap = collect_full(raw_events=(event,))
        entry = snap.event_timeline[0]
        assert entry.event_type == ControlPlaneEventType.WORKSPACE_READY.value
        assert entry.run_id == "run-1"
        assert entry.workspace_id == "ws-1"
        assert entry.candidate_id == "cand-1"
        assert entry.execution_host_id == "host-local"
        assert entry.execution_epoch == 1

    def test_timeline_from_registry(self):
        snap = collect_full(raw_events=None)
        # Fixture registry has no recorded events: empty timeline, not a crash.
        assert snap.event_timeline == ()

    def test_bounded_timeline(self):
        from ai_engineering.observability.contracts import ProjectionLimits

        events = [
            make_event(event_id=f"evt-{i:04d}", created_at=f"2026-01-15T12:{i // 60:02d}:{i % 60:02d}+00:00")
            for i in range(30)
        ]
        snap = collect_full(raw_events=events, limits=ProjectionLimits(max_events=10))
        assert len(snap.event_timeline) == 10
        assert snap.truncations[0].truncated is True
        assert snap.truncations[0].original_count == 30
        assert snap.truncations[0].returned_count == 10


class TestQueries:
    def test_operator_queries_read_only(self):
        snap = collect_full()
        queries = OperatorQueries(snap)
        assert queries.get_cycle_summary()["phase"] is not None
        assert len(queries.get_candidate_statuses()) == 1
        assert len(queries.get_barrier_status()) == 7
        assert queries.get_handoff_status()["present"] is True
        assert queries.get_projection_health()["health"] == "OK"
        assert len(queries.get_active_runs()) == 1
        assert len(queries.get_active_workspaces()) == 1
