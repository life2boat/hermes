"""Unit tests for EngineeringCycleState (PR-11.1 canonical TaskIntent binding)."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from tests.ai_engineering.control_plane_fixture_helpers import (
    SHA,
    make_state,
)


def test_cycle_state_serialization_roundtrip():
    st = make_state()
    d = st.to_dict()
    assert d["cycle_id"] == "c1"
    assert d["phase"] == "CREATED"
    assert d["execution_epoch"] == 1
    assert len(d["intent_digest"]) == 64
    assert d["repository_id"] == "life2boat/hermes"

    restored = EngineeringCycleState.from_dict(d)
    assert restored == st


def test_cycle_state_json_roundtrip():
    st = make_state()
    restored = EngineeringCycleState.from_json(st.to_json())
    assert restored == st


def test_intent_id_alias_matches_digest():
    st = make_state()
    assert st.intent_id == st.intent_digest
    assert len(st.intent_id) == 64


def test_fake_intent_digest_rejected():
    with pytest.raises(ControlPlaneError) as exc:
        EngineeringCycleState(
            cycle_id="c1",
            task_id="t1",
            node_id="n1",
            intent_digest="totally-fake-intent",
            intent_revision=1,
            repository_id="life2boat/hermes",
            base_sha=SHA,
        )
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_missing_repository_binding_rejected():
    with pytest.raises(ControlPlaneError) as exc:
        EngineeringCycleState(
            cycle_id="c1",
            task_id="t1",
            node_id="n1",
            intent_digest="a" * 64,
            intent_revision=1,
            repository_id="",
            base_sha=SHA,
        )
    assert exc.value.code == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_invalid_sha_rejected():
    with pytest.raises(Exception):
        make_state(base_sha="invalid_sha")


def test_invalid_epoch_rejected():
    with pytest.raises(ControlPlaneError):
        make_state(execution_epoch=0)


def test_cycle_state_immutability():
    st = make_state()
    with pytest.raises(Exception):
        st.phase = ControlPlanePhase.QUALIFIED
