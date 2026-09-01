"""Unit tests for ProductionSerializationBarrier."""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import ControlPlaneError


def test_production_serialization_barrier_states():
    b0 = ProductionSerializationBarrier(active_mutation_agents=2, single_production_owner="deployer")
    assert b0.ready is False

    b1 = ProductionSerializationBarrier(active_mutation_agents=0, single_production_owner=None)
    assert b1.ready is False

    b2 = ProductionSerializationBarrier(active_mutation_agents=0, single_production_owner="deployer")
    assert b2.ready is True

    with pytest.raises(ControlPlaneError):
        ProductionSerializationBarrier(active_mutation_agents=-1, single_production_owner="deployer")
