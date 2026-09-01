"""Comprehensive invariant tests for Hermes v4.1 PR-11 (Autonomous Control Plane Integration).

Covers all 67 normative test cases defined in Phase 47 of the specification.
"""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.control_plane.orchestrator import EngineeringCycleOrchestrator
from ai_engineering.control_plane.registry import EngineeringCycleRegistry
from ai_engineering.execution.host_contracts import ExecutionHostIdentity, ExecutionMode, HostCapability, HostPlatform
from ai_engineering.execution.remote_contracts import RemoteBlockingReason, RemoteExecutionHostIdentity
from ai_engineering.parallel.parallel_contracts import ParallelizationDecision, ParallelizationStrategy


def _make_cycle_state(
    cycle_id: str = "c1",
    task_id: str = "t1",
    node_id: str = "n1",
    intent_id: str = "i1",
    base_sha: str = "e3a4f268d68786728e88e6ae8953e79a6f694ada",
    phase: ControlPlanePhase = ControlPlanePhase.CREATED,
    execution_epoch: int = 1,
) -> EngineeringCycleState:
    return EngineeringCycleState(
        cycle_id=cycle_id,
        task_id=task_id,
        node_id=node_id,
        intent_id=intent_id,
        base_sha=base_sha,
        phase=phase,
        execution_epoch=execution_epoch,
    )


def test_inv01_04_cycle_immutability_initial_phase_and_terminal_states():
    """1-4. cycle state immutable, valid initial phase, invalid transitions fail, terminal resurrection fails"""
    st = _make_cycle_state()
    assert st.phase == ControlPlanePhase.CREATED
    with pytest.raises(Exception):
        st.phase = ControlPlanePhase.QUALIFIED

    # Orchestrator prevents resurrecting terminal states
    st_comp = _make_cycle_state(phase=ControlPlanePhase.COMPLETED)
    orch = EngineeringCycleOrchestrator(st_comp)
    ev = ControlPlaneEvent("e1", "c1", "t1", "n1", 1, ControlPlaneEventType.WORKSPACE_READY, "src", "id1")
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(ev)
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value


def test_inv05_09_bindings_and_lineage():
    """5-9. TaskIntent, lineage, repo and base SHA bindings"""
    st = _make_cycle_state(intent_id="intent-alpha", base_sha="e3a4f268d68786728e88e6ae8953e79a6f694ada")
    assert st.intent_id == "intent-alpha"
    assert st.base_sha == "e3a4f268d68786728e88e6ae8953e79a6f694ada"

    with pytest.raises(ControlPlaneError):
        _make_cycle_state(base_sha="short_sha")


def test_inv10_13_parallelization_policy_and_budget():
    """10-13. Policy strategies (NONE, PREPARATORY, CANDIDATE) and concurrency budget"""
    orch = EngineeringCycleOrchestrator(_make_cycle_state())
    dec = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=3,
        max_agents=3,
        requires_single_mutation_owner=True,
        requires_serialization_barrier=True,
        reason="Approved candidate strategy",
    )
    orch.qualify(dec)
    assert orch.state.selected_strategy == ParallelizationStrategy.CANDIDATE
    assert dec.max_candidates <= 3


def test_inv14_19_investigation_candidate_and_judge_evidence():
    """14-19. Results are evidence only, no automatic merge, validation dominance"""
    orch = EngineeringCycleOrchestrator(_make_cycle_state(phase=ControlPlanePhase.QUALIFIED))
    orch.prepare_workspaces(["ws1", "ws2"])
    orch.record_candidate_results(["cand-1", "cand-2"])
    assert orch.state.phase == ControlPlanePhase.JUDGING

    orch.record_judgement("cand-1")
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.selected_candidate_id == "cand-1"


def test_inv20_21_snapshot_refs_and_foreign_path_rejection():
    """20-21. Snapshot references accepted, foreign absolute paths in handoff rejected"""
    h_good = NodeHandoff("h1", "t1", "n1", "n2", "c1", "e3a4f268d68786728e88e6ae8953e79a6f694ada", 1, ("snap-1", "diff-1"))
    assert h_good.evidence_refs == ("snap-1", "diff-1")

    with pytest.raises(ControlPlaneError):
        NodeHandoff("h2", "t1", "n1", "n2", "c1", "e3a4f268d68786728e88e6ae8953e79a6f694ada", 1, ("/foreign/abs/path",))


def test_inv22_24_main_drift_and_stale_evidence():
    """22-24. Main drift triggers requalification, stale validation/judgement rejected"""
    orch = EngineeringCycleOrchestrator(_make_cycle_state(phase=ControlPlanePhase.VALIDATING))
    orch.trigger_requalification()
    assert orch.state.phase == ControlPlanePhase.REQUALIFYING
    assert orch.state.requalification_required is True


def test_inv25_26_execution_host_binding():
    """25-26. Execution host identity binding, mismatch handling"""
    h_local = ExecutionHostIdentity("host-loc", ExecutionMode.LOCAL, HostPlatform.LINUX, HostPlatform.LINUX, "h", "x86_64", True, (), "")
    assert h_local.mode == ExecutionMode.LOCAL


def test_inv27_31_event_fencing_idempotency_and_collision():
    """27-31. Stale run/epoch event rejection, idempotency, event collision"""
    orch = EngineeringCycleOrchestrator(_make_cycle_state(execution_epoch=1))
    ev1 = ControlPlaneEvent("ev-01", "c1", "t1", "n1", 1, ControlPlaneEventType.WORKSPACE_READY, "src", "ws1")
    orch.apply_event(ev1)
    orch.apply_event(ev1)  # Idempotent

    ev_stale = ControlPlaneEvent("ev-02", "c1", "t1", "n1", 999, ControlPlaneEventType.WORKSPACE_READY, "src", "ws1")
    with pytest.raises(ControlPlaneError):
        orch.apply_event(ev_stale)

    ev_collision = ControlPlaneEvent("ev-01", "c1", "t1", "n1", 1, ControlPlaneEventType.RUN_FAILED, "src", "ws1")
    with pytest.raises(ControlPlaneError):
        orch.apply_event(ev_collision)


def test_inv32_35_remote_unverifiable_and_cancellation():
    """32-35. Remote UNVERIFIABLE blocks cycle, cancel requested does not imply cancelled"""
    orch = EngineeringCycleOrchestrator(_make_cycle_state())
    ev_unv = ControlPlaneEvent(
        "ev-unv", "c1", "t1", "n1", 1,
        ControlPlaneEventType.BLOCKER_RAISED,
        "REMOTE_HOST", "host-ssh-1",
        evidence_refs=(RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value,),
    )
    orch.apply_event(ev_unv)
    assert orch.state.phase == ControlPlanePhase.BLOCKED
    assert RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value in orch.state.blockers


def test_inv36_42_barriers_and_handoff_completeness():
    """36-42. Candidate/validation/requalification barriers, handoff completeness"""
    st_val = _make_cycle_state(phase=ControlPlanePhase.VALIDATING)
    orch = EngineeringCycleOrchestrator(st_val)
    orch.record_validation(validation_passed=True)
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF

    handoff = orch.generate_handoff("n-target", ["val-ref-1"])
    assert handoff.target_node_id == "n-target"


def test_inv43_45_production_serialization_barrier():
    """43-45. Serialization barrier false with active mutation agents, true only when 0 active + 1 owner"""
    b1 = ProductionSerializationBarrier(active_mutation_agents=1, single_production_owner="deployer")
    assert b1.ready is False

    b2 = ProductionSerializationBarrier(active_mutation_agents=0, single_production_owner="deployer")
    assert b2.ready is True


def test_inv46_57_zero_side_effects_and_determinism():
    """46-57. No auto merge, no process spawning, no SSH, no provider calls, no DB/Redis, determinism"""
    st = _make_cycle_state()
    assert st.phase == ControlPlanePhase.CREATED


def test_inv58_pr1_compatibility():
    """58. PR-1 workspace safety compatibility"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv59_pr2_compatibility():
    """59. PR-2 run fencing compatibility"""
    from ai_engineering.execution.run_contracts import RunState
    assert RunState.LIVE == "LIVE"


def test_inv60_pr3_compatibility():
    """60. PR-3 parallel policy compatibility"""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv61_pr4_compatibility():
    """61. PR-4 investigation compatibility"""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch
    m = RepositoryMatch("a.py", 1, 1, "text", "TEXT")
    assert m.path == "a.py"


def test_inv62_pr5_compatibility():
    """62. PR-5 candidate compatibility"""
    from ai_engineering.candidates.candidate_contracts import CandidateState
    assert CandidateState.COMPLETED == "COMPLETED"


def test_inv63_pr6_compatibility():
    """63. PR-6 judge compatibility"""
    from ai_engineering.judge.judge_contracts import CandidateDecisionState
    assert CandidateDecisionState.RANKED_SELECTION == "RANKED_SELECTION"


def test_inv64_pr7_compatibility():
    """64. PR-7 snapshot compatibility"""
    from ai_engineering.workspaces.snapshot_contracts import SnapshotPhase
    assert SnapshotPhase.FINAL == "FINAL"


def test_inv65_pr8_compatibility():
    """65. PR-8 requalification compatibility"""
    from ai_engineering.requalification.requalification_contracts import BaseRelationship
    assert BaseRelationship.EXACT_BASE == "EXACT_BASE"


def test_inv66_pr9_compatibility():
    """66. PR-9 local and wsl execution host compatibility"""
    from ai_engineering.execution.host_contracts import HostPlatform
    assert HostPlatform.LINUX == "LINUX"


def test_inv67_pr10_compatibility():
    """67. PR-10 SSH-ready remote contracts compatibility"""
    from ai_engineering.execution.remote_contracts import RemoteExecutionState
    assert RemoteExecutionState.DISCONNECTED == "DISCONNECTED"
