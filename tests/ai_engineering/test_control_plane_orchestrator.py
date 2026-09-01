"""Unit tests for EngineeringCycleOrchestrator."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.orchestrator import EngineeringCycleOrchestrator
from ai_engineering.parallel.parallel_contracts import ParallelizationDecision, ParallelizationStrategy


def test_orchestrator_lifecycle_flow():
    init_st = EngineeringCycleState(
        cycle_id="c1",
        task_id="t1",
        node_id="n1",
        intent_id="i1",
        base_sha="e3a4f268d68786728e88e6ae8953e79a6f694ada",
    )
    orch = EngineeringCycleOrchestrator(init_st)
    assert orch.state.phase == ControlPlanePhase.CREATED

    # 1. Qualify
    dec = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=True,
        requires_serialization_barrier=True,
        reason="Approved candidate strategy",
    )
    orch.qualify(dec)
    assert orch.state.phase == ControlPlanePhase.QUALIFIED
    assert orch.state.selected_strategy == ParallelizationStrategy.CANDIDATE

    # 2. Prepare workspaces
    orch.prepare_workspaces(["ws1", "ws2"])
    assert orch.state.phase == ControlPlanePhase.PREPARING

    # 3. Record candidate results
    orch.record_candidate_results(["c1", "c2"])
    assert orch.state.phase == ControlPlanePhase.JUDGING

    # 4. Record judgement
    orch.record_judgement("c1")
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.selected_candidate_id == "c1"

    # 5. Record validation
    orch.record_validation(validation_passed=True, evidence_refs=["val-01"])
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF

    # 6. Generate handoff
    handoff = orch.generate_handoff("n2", ["evidence-snap-1"])
    assert handoff.target_node_id == "n2"
    assert handoff.selected_candidate_id == "c1"
