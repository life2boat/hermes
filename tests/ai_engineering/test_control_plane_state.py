"""Unit tests for EngineeringCycleState."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState


def test_cycle_state_validation():
    # Invalid SHA rejected
    with pytest.raises(ControlPlaneError):
        EngineeringCycleState(
            cycle_id="c1",
            task_id="t1",
            node_id="n1",
            intent_id="i1",
            base_sha="invalid_sha",
        )

    # Invalid epoch rejected
    with pytest.raises(ControlPlaneError):
        EngineeringCycleState(
            cycle_id="c1",
            task_id="t1",
            node_id="n1",
            intent_id="i1",
            base_sha="e3a4f268d68786728e88e6ae8953e79a6f694ada",
            execution_epoch=0,
        )


def test_cycle_state_immutability():
    st = EngineeringCycleState(
        cycle_id="c1",
        task_id="t1",
        node_id="n1",
        intent_id="i1",
        base_sha="e3a4f268d68786728e88e6ae8953e79a6f694ada",
    )
    with pytest.raises(Exception):
        st.phase = ControlPlanePhase.QUALIFIED
