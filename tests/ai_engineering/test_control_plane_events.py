"""Unit tests for ControlPlaneEvent and registry (PR-11.1 collision semantics)."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.control_plane.registry import EngineeringCycleRegistry
from tests.ai_engineering.control_plane_fixture_helpers import (
    SHA,
    make_event,
    make_state,
)


def test_event_creation_and_registry():
    ev = make_event("ev-01", ControlPlaneEventType.WORKSPACE_READY)
    reg = EngineeringCycleRegistry()
    reg.record_event(ev)
    events = reg.get_events("c1")
    assert len(events) == 1
    assert events[0].event_id == "ev-01"


def test_registry_cycle_collision():
    reg = EngineeringCycleRegistry()
    s1 = make_state()
    reg.register_cycle(s1)
    reg.register_cycle(s1)  # Idempotent

    s2 = EngineeringCycleState(
        cycle_id="c1",
        task_id="t-other",
        node_id="n1",
        intent_digest=s1.intent_digest,
        intent_revision=1,
        repository_id="life2boat/hermes",
        base_sha=SHA,
    )
    with pytest.raises(ControlPlaneError):
        reg.register_cycle(s2)  # Collision


# ---------------------------------------------------------------------------
# D8: registry event collision semantics
# ---------------------------------------------------------------------------


def test_registry_event_duplicate_is_idempotent():
    reg = EngineeringCycleRegistry()
    ev = make_event("ev-01", ControlPlaneEventType.WORKSPACE_READY)
    reg.record_event(ev)
    reg.record_event(ev)
    assert len(reg.get_events("c1")) == 1


def test_registry_event_collision_fails_closed():
    reg = EngineeringCycleRegistry()
    ev1 = make_event("ev-01", ControlPlaneEventType.WORKSPACE_READY)
    ev2 = make_event("ev-01", ControlPlaneEventType.RUN_FAILED)
    reg.record_event(ev1)
    with pytest.raises(ControlPlaneError) as exc:
        reg.record_event(ev2)
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_EVENT_COLLISION.value


# ---------------------------------------------------------------------------
# D8: registry handoff collision semantics (no last-writer-wins)
# ---------------------------------------------------------------------------


def _handoff(target: str) -> NodeHandoff:
    return NodeHandoff(
        handoff_id="h1",
        task_id="t1",
        source_node_id="n1",
        target_node_id=target,
        cycle_id="c1",
        base_sha=SHA,
        execution_epoch=1,
        evidence_refs=("snap-1",),
    )


def test_registry_handoff_duplicate_is_idempotent():
    reg = EngineeringCycleRegistry()
    reg.record_handoff(_handoff("n2"))
    reg.record_handoff(_handoff("n2"))
    assert reg.get_handoff("h1").target_node_id == "n2"


def test_registry_handoverwrite_rejected():
    reg = EngineeringCycleRegistry()
    reg.record_handoff(_handoff("n2"))
    with pytest.raises(ControlPlaneError) as exc:
        reg.record_handoff(_handoff("n-OTHER"))
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_EVENT_COLLISION.value
    assert reg.get_handoff("h1").target_node_id == "n2"


# ---------------------------------------------------------------------------
# Phase 9: event identity fencing fields
# ---------------------------------------------------------------------------


def test_event_identity_fields_roundtrip():
    ev = make_event(
        "ev-id-1",
        ControlPlaneEventType.CANDIDATE_COMPLETED,
        run_id="run-1",
        workspace_id="ws-1",
        candidate_id="cand-1",
        execution_host_id="host-1",
    )
    d = ev.to_dict()
    assert d["run_id"] == "run-1"
    assert d["workspace_id"] == "ws-1"
    assert d["candidate_id"] == "cand-1"
    assert d["execution_host_id"] == "host-1"


def test_event_rejects_invalid_optional_identity():
    with pytest.raises(ControlPlaneError):
        make_event("ev-x", ControlPlaneEventType.WORKSPACE_READY, workspace_id="ws/../bad")
    with pytest.raises(ControlPlaneError):
        make_event("ev-y", ControlPlaneEventType.WORKSPACE_READY, run_id="C:/foreign/run")
