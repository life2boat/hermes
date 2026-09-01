"""Production serialization barrier contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
)


@dataclass(frozen=True, slots=True)
class ProductionSerializationBarrier:
    """Deterministic barrier ensuring complete convergence before single-owner mutation."""

    active_mutation_agents: int
    single_production_owner: str | None
    ready: bool = False

    def __post_init__(self) -> None:
        if self.active_mutation_agents < 0:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                "active_mutation_agents cannot be negative",
            )
        is_ready = (self.active_mutation_agents == 0) and (self.single_production_owner is not None) and bool(self.single_production_owner.strip())
        object.__setattr__(self, "ready", is_ready)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_mutation_agents": self.active_mutation_agents,
            "single_production_owner": self.single_production_owner,
            "ready": self.ready,
        }
