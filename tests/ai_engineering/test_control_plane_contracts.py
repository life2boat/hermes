"""Unit tests for control plane contracts and serialization."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import (
    CONTROL_PLANE_CONTRACT_VERSION,
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff


def test_control_plane_phase_and_events():
    assert ControlPlanePhase.CREATED == "CREATED"
    assert ControlPlaneEventType.WORKSPACE_READY == "WORKSPACE_READY"


def test_cycle_state_serialization():
    st = EngineeringCycleState(
        cycle_id="cycle-01",
        task_id="task-01",
        node_id="node-01",
        intent_id="intent-01",
        base_sha="e3a4f268d68786728e88e6ae8953e79a6f694ada",
    )
    d = st.to_dict()
    assert d["cycle_id"] == "cycle-01"
    assert d["phase"] == "CREATED"
    assert d["execution_epoch"] == 1

    restored = EngineeringCycleState.from_dict(d)
    assert restored == st


def test_handoff_and_barrier_contracts():
    handoff = NodeHandoff(
        handoff_id="h-01",
        task_id="t-01",
        source_node_id="n-src",
        target_node_id="n-tgt",
        cycle_id="c-01",
        base_sha="e3a4f268d68786728e88e6ae8953e79a6f694ada",
        execution_epoch=1,
        evidence_refs=("snapshot-01", "diff-01"),
    )
    assert handoff.source_node_id == "n-src"

    barrier_not_ready = ProductionSerializationBarrier(
        active_mutation_agents=1,
        single_production_owner="agent-01",
    )
    assert barrier_not_ready.ready is False

    barrier_ready = ProductionSerializationBarrier(
        active_mutation_agents=0,
        single_production_owner="agent-01",
    )
    assert barrier_ready.ready is True
