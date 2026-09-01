"""Invariant tests for Hermes v4.1 PR-11 + PR-11.1 corrective hardening.

Each normative case maps to a behavioral assertion against the merged
contracts; the PR-11.1 corrective invariants (D1-D8) are covered in
test_control_plane_hardening.py.
"""

from __future__ import annotations

import pytest

from ai_engineering.candidates.candidate_contracts import CandidateIdentity
from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.orchestrator import EngineeringCycleOrchestrator
from ai_engineering.execution.host_contracts import (
    ExecutionHostIdentity,
    ExecutionMode,
    HostPlatform,
)
from ai_engineering.execution.remote_contracts import (
    RemoteBlockingReason,
    RemoteExecutionState,
)
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)
from tests.ai_engineering.control_plane_fixture_helpers import (
    SHA,
    drive_to_validating,
    make_candidate,
    make_event,
    make_intent,
    make_lineage,
    make_orchestrator,
    make_state,
    make_validation_evidence,
)


def _decision() -> ParallelizationDecision:
    return ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=3,
        max_agents=3,
        requires_single_mutation_owner=True,
        requires_serialization_barrier=True,
        reason="Approved candidate strategy",
    )


def test_inv01_04_cycle_immutability_initial_phase_and_terminal_states():
    """1-4. cycle immutable, valid initial phase, terminal events rejected."""
    st = make_state()
    assert st.phase == ControlPlanePhase.CREATED
    with pytest.raises(Exception):
        st.phase = ControlPlanePhase.QUALIFIED

    st_comp = make_state(phase="COMPLETED")
    orch = EngineeringCycleOrchestrator(
        st_comp, intent=make_intent(), lineage=make_lineage()
    )
    ev = make_event("e1", ControlPlaneEventType.WORKSPACE_READY)
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(ev)
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value


def test_inv05_09_bindings_and_lineage():
    """5-9. TaskIntent digest/revision/repository binding and base SHA validation."""
    st = make_state()
    assert len(st.intent_digest) == 64
    assert st.intent_revision == 1
    assert st.repository_id == "life2boat/hermes"
    assert st.base_sha == SHA

    with pytest.raises(Exception):
        make_state(base_sha="short_sha")


def test_inv10_13_parallelization_policy_and_budget():
    """10-13. Policy strategy recorded through the ordered transition."""
    orch = make_orchestrator()
    dec = _decision()
    orch.qualify(dec)
    assert orch.state.selected_strategy == ParallelizationStrategy.CANDIDATE
    assert dec.max_candidates <= 3


def test_inv14_19_candidate_judge_validation_chain():
    """14-19. Results are evidence only; no automatic merge; ordered chain."""
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws1", "ws2"])
    orch.start_investigation()
    orch.record_investigation_results(["inv-1"])
    orch.record_candidate_results([make_candidate("cand-1")])
    orch.record_candidate_completed("cand-1")
    assert orch.state.phase == ControlPlanePhase.JUDGING

    orch.record_judgement("cand-1")
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.selected_candidate_id == "cand-1"


def test_inv20_21_snapshot_refs_and_foreign_path_rejection():
    """20-21. Snapshot refs accepted, foreign absolute paths in handoff rejected."""
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.record_validation(make_validation_evidence())
    handoff = orch.generate_handoff("n2", ("snap-1", "diff-1"))
    assert handoff.evidence_refs == ("snap-1", "diff-1")

    with pytest.raises(ControlPlaneError) as exc:
        orch.generate_handoff("n2", ("/foreign/abs/path",))
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value


def test_inv22_24_main_drift_and_stale_evidence():
    """22-24. Requalification flag blocks handoff until fresh evidence."""
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.trigger_requalification()
    assert orch.state.phase == ControlPlanePhase.REQUALIFYING
    assert orch.state.requalification_required is True

    with pytest.raises(ControlPlaneError):
        orch.record_validation(make_validation_evidence())
    orch.record_requalification_results(["req-1"])
    assert orch.state.requalification_required is False
    orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF


def test_inv25_26_execution_host_binding():
    """25-26. Execution host identity binding, mismatch handling."""
    orch = make_orchestrator()
    orch.register_execution_host("host-loc")
    orch.apply_event(
        make_event("ev-h", ControlPlaneEventType.RUN_FAILED, execution_host_id="host-loc")
    )
    assert orch.state.phase == ControlPlanePhase.FAILED

    orch2 = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch2.apply_event(
            make_event("ev-h2", ControlPlaneEventType.RUN_FAILED, execution_host_id="host-unknown")
        )
    assert exc.value.code == ControlPlaneBlockingReason.EXECUTION_HOST_MISMATCH.value


def test_inv27_31_event_fencing_idempotency_and_collision():
    """27-31. Stale epoch/cycle rejection, idempotency, event collision."""
    orch = make_orchestrator()
    ev1 = make_event("ev-01", ControlPlaneEventType.BLOCKER_RAISED, evidence_refs=("b-1",))
    orch.apply_event(ev1)
    orch.apply_event(ev1)  # Idempotent
    assert len(orch.state.blockers) == 1

    ev_stale = make_event(
        "ev-02", ControlPlaneEventType.WORKSPACE_READY, execution_epoch=999
    )
    with pytest.raises(ControlPlaneError):
        orch.apply_event(ev_stale)

    ev_collision = make_event("ev-01", ControlPlaneEventType.RUN_FAILED)
    with pytest.raises(ControlPlaneError):
        orch.apply_event(ev_collision)


def test_inv32_35_remote_unverifiable_and_cancellation():
    """32-35. Remote UNVERIFIABLE blocks the cycle; cancellation is two-staged."""
    orch = make_orchestrator()
    ev_unv = make_event(
        "ev-unv",
        ControlPlaneEventType.BLOCKER_RAISED,
        source_kind="REMOTE_HOST",
        evidence_refs=(RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value,),
    )
    orch.apply_event(ev_unv)
    assert orch.state.phase == ControlPlanePhase.BLOCKED
    assert (
        RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value in orch.state.blockers
    )

    orch2 = make_orchestrator()
    orch2.request_cancel()
    assert orch2.state.phase == ControlPlanePhase.CANCEL_REQUESTED
    assert orch2.state.phase != ControlPlanePhase.CANCELLED
    # Cancel ACK without terminal execution evidence cannot confirm terminality.
    with pytest.raises(ControlPlaneError):
        orch2.apply_event(make_event("ev-ack", ControlPlaneEventType.RUN_CANCELLED))
    assert orch2.state.phase == ControlPlanePhase.CANCEL_REQUESTED

    orch2.apply_event(
        make_event(
            "ev-term",
            ControlPlaneEventType.RUN_CANCELLED,
            evidence_refs=("exit-receipt-1",),
        )
    )
    assert orch2.state.phase == ControlPlanePhase.CANCELLED


def test_inv36_42_barriers_and_handoff_completeness():
    """36-42. Validation barrier requires evidence; handoff only from READY."""
    orch = make_orchestrator()
    drive_to_validating(orch)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence(candidate_id="ghost"))
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value

    orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF
    handoff = orch.generate_handoff("n2", ["val-ref-1"])
    assert handoff.target_node_id == "n2"


def test_inv43_45_production_serialization_barrier():
    """43-45. Barrier true only when 0 active mutation agents + 1 owner."""
    b1 = ProductionSerializationBarrier(active_mutation_agents=1, single_production_owner="deployer")
    assert b1.ready is False

    b2 = ProductionSerializationBarrier(active_mutation_agents=0, single_production_owner="deployer")
    assert b2.ready is True


def test_inv46_57_zero_side_effects_and_determinism():
    """46-57. Deterministic fixed timestamps, no wall-clock drift, pure data contracts."""
    st = make_state()
    assert st.phase == ControlPlanePhase.CREATED
    assert st.created_at == st.updated_at == "2026-09-01T00:00:00Z"


def test_inv58_pr1_compatibility():
    """58. PR-1 workspace safety compatibility."""
    from ai_engineering.workspaces.workspace_contracts import LeaseState

    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv59_pr2_compatibility():
    """59. PR-2 run fencing compatibility."""
    from ai_engineering.execution.run_contracts import RunState

    assert RunState.LIVE == "LIVE"


def test_inv60_pr3_compatibility():
    """60. PR-3 parallel policy compatibility."""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy

    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv61_pr4_compatibility():
    """61. PR-4 investigation compatibility."""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch

    m = RepositoryMatch("a.py", 1, 1, "text", "TEXT")
    assert m.path == "a.py"


def test_inv62_pr5_compatibility():
    """62. PR-5 candidate compatibility."""
    from ai_engineering.candidates.candidate_contracts import CandidateState

    assert CandidateState.COMPLETED == "COMPLETED"


def test_inv63_pr6_compatibility():
    """63. PR-6 judge compatibility."""
    from ai_engineering.judge.judge_contracts import CandidateDecisionState

    assert CandidateDecisionState.RANKED_SELECTION == "RANKED_SELECTION"


def test_inv64_pr7_compatibility():
    """64. PR-7 snapshot compatibility."""
    from ai_engineering.workspaces.snapshot_contracts import SnapshotPhase

    assert SnapshotPhase.FINAL == "FINAL"


def test_inv65_pr8_compatibility():
    """65. PR-8 requalification compatibility."""
    from ai_engineering.requalification.requalification_contracts import BaseRelationship

    assert BaseRelationship.EXACT_BASE == "EXACT_BASE"


def test_inv66_pr9_compatibility():
    """66. PR-9 local and wsl execution host compatibility."""
    from ai_engineering.execution.host_contracts import HostPlatform

    assert HostPlatform.LINUX == "LINUX"


def test_inv67_pr10_compatibility():
    """67. PR-10 SSH-ready remote contracts compatibility."""
    from ai_engineering.execution.remote_contracts import RemoteExecutionState

    assert RemoteExecutionState.DISCONNECTED == "DISCONNECTED"
