"""Unit tests for EngineeringCycleOrchestrator (PR-11.1 ordered transitions)."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
)
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)
from tests.ai_engineering.control_plane_fixture_helpers import (
    make_candidate,
    make_event,
    make_orchestrator,
    make_validation_evidence,
    drive_to_validating,
)


def _decision() -> ParallelizationDecision:
    return ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=True,
        requires_serialization_barrier=True,
        reason="Approved candidate strategy",
    )


def test_orchestrator_lifecycle_flow():
    orch = make_orchestrator()
    assert orch.state.phase == ControlPlanePhase.CREATED

    orch.qualify(_decision())
    assert orch.state.phase == ControlPlanePhase.QUALIFIED
    assert orch.state.selected_strategy == ParallelizationStrategy.CANDIDATE

    orch.prepare_workspaces(["ws1", "ws2"])
    assert orch.state.phase == ControlPlanePhase.PREPARING

    orch.start_investigation()
    assert orch.state.phase == ControlPlanePhase.INVESTIGATING

    orch.record_investigation_results(["inv-01"])
    assert orch.state.phase == ControlPlanePhase.IMPLEMENTING

    orch.record_candidate_results([make_candidate("c1")])
    orch.record_candidate_completed("c1")
    assert orch.state.phase == ControlPlanePhase.JUDGING

    orch.record_judgement("c1")
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.selected_candidate_id == "c1"

    orch.record_validation(make_validation_evidence(candidate_id="c1"))
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF

    handoff = orch.generate_handoff("n2", ["evidence-snap-1"])
    assert handoff.target_node_id == "n2"
    assert handoff.selected_candidate_id == "c1"


def test_illegal_phase_jumps_rejected():
    # CREATED -> READY_FOR_HANDOFF is impossible: only record_validation from
    # VALIDATING reaches it, and every helper guards its source phase.
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence())
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase.CREATED

    # PREPARING -> JUDGING (the audited skip) is now illegal.
    orch2 = make_orchestrator()
    orch2.qualify(_decision())
    orch2.prepare_workspaces(["ws1"])
    with pytest.raises(ControlPlaneError) as exc2:
        orch2.record_candidate_results([make_candidate()])
    assert exc2.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value

    # CREATED -> VALIDATING via judgement is illegal.
    orch3 = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch3.record_judgement("c1")
    assert orch3.state.phase == ControlPlanePhase.CREATED


def test_event_driven_projection_matches_direct_flow():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])

    orch.apply_event(make_event("ev-ws", ControlPlaneEventType.WORKSPACE_READY, workspace_id="ws-1"))
    assert orch.state.phase == ControlPlanePhase.INVESTIGATING

    orch.apply_event(make_event("ev-inv", ControlPlaneEventType.INVESTIGATION_COMPLETED))
    assert orch.state.phase == ControlPlanePhase.IMPLEMENTING

    orch.record_candidate_results([make_candidate("cand-1")])
    orch.apply_event(
        make_event(
            "ev-cand", ControlPlaneEventType.CANDIDATE_COMPLETED, candidate_id="cand-1"
        )
    )
    assert orch.state.phase == ControlPlanePhase.JUDGING

    orch.apply_event(
        make_event(
            "ev-judge", ControlPlaneEventType.JUDGEMENT_COMPLETED, candidate_id="cand-1"
        )
    )
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.selected_candidate_id == "cand-1"

    orch.apply_event(
        make_event(
            "ev-val",
            ControlPlaneEventType.VALIDATION_COMPLETED,
            candidate_id="cand-1",
            evidence_refs=("snap-1",),
        )
    )
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF


def test_invalid_event_source_phase_rejected():
    orch = make_orchestrator()
    # INVESTIGATION_COMPLETED has no meaning in CREATED.
    with pytest.raises(ControlPlaneError):
        orch.apply_event(make_event("ev-bad", ControlPlaneEventType.INVESTIGATION_COMPLETED))
    assert orch.state.phase == ControlPlanePhase.CREATED


def test_requalification_flow_via_events():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.trigger_requalification()
    assert orch.state.phase == ControlPlanePhase.REQUALIFYING
    assert orch.state.requalification_required is True

    orch.apply_event(
        make_event(
            "ev-req", ControlPlaneEventType.REQUALIFICATION_COMPLETED, evidence_refs=("req-ref-1",)
        )
    )
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.requalification_required is False

    orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF
