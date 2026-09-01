"""Unit tests for ControlPlaneEvent and registry."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.registry import EngineeringCycleRegistry


def test_event_creation_and_registry():
    ev = ControlPlaneEvent(
        event_id="ev-01",
        cycle_id="c-01",
        task_id="t-01",
        node_id="n-01",
        execution_epoch=1,
        event_type=ControlPlaneEventType.WORKSPACE_READY,
        source_kind="WORKSPACE_MANAGER",
        source_id="ws-01",
    )
    assert ev.event_id == "ev-01"

    reg = EngineeringCycleRegistry()
    reg.record_event(ev)
    events = reg.get_events("c-01")
    assert len(events) == 1
    assert events[0].event_id == "ev-01"


def test_registry_cycle_collision():
    reg = EngineeringCycleRegistry()
    s1 = EngineeringCycleState("c1", "t1", "n1", "i1", "e3a4f268d68786728e88e6ae8953e79a6f694ada")
    reg.register_cycle(s1)
    reg.register_cycle(s1)  # Idempotent

    s2 = EngineeringCycleState("c1", "t2", "n1", "i1", "e3a4f268d68786728e88e6ae8953e79a6f694ada")
    with pytest.raises(ControlPlaneError):
        reg.register_cycle(s2)  # Collision
