"""Immutable EngineeringCycleState contract representing control plane state."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import re
from typing import Any

from ai_engineering.control_plane.contracts import (
    CONTROL_PLANE_CONTRACT_VERSION,
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlanePhase,
)
from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EngineeringCycleState:
    """Immutable control-plane state of an engineering cycle."""

    cycle_id: str
    task_id: str
    node_id: str
    intent_id: str
    base_sha: str
    phase: ControlPlanePhase = ControlPlanePhase.CREATED
    execution_epoch: int = 1
    selected_strategy: ParallelizationStrategy = ParallelizationStrategy.NONE
    active_workspace_ids: tuple[str, ...] = ()
    active_run_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
    selected_candidate_id: str | None = None
    requalification_required: bool = False
    blockers: tuple[str, ...] = ()
    created_at: str = "2026-09-01T00:00:00Z"
    updated_at: str = "2026-09-01T00:00:00Z"

    def __post_init__(self) -> None:
        if not isinstance(self.cycle_id, str) or not _IDENTIFIER_RE.match(self.cycle_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid cycle_id: {self.cycle_id!r}",
            )
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if not isinstance(self.node_id, str) or not _IDENTIFIER_RE.match(self.node_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid node_id: {self.node_id!r}",
            )
        if not isinstance(self.intent_id, str) or not _IDENTIFIER_RE.match(self.intent_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid intent_id: {self.intent_id!r}",
            )
        if not isinstance(self.base_sha, str) or not _SHA_RE.match(self.base_sha):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )
        if not isinstance(self.phase, ControlPlanePhase):
            try:
                object.__setattr__(self, "phase", ControlPlanePhase(str(self.phase)))
            except ValueError as exc:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Invalid phase: {self.phase!r}",
                ) from exc
        if self.execution_epoch < 1:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"execution_epoch must be >= 1, got {self.execution_epoch}",
            )
        if not isinstance(self.active_workspace_ids, tuple):
            object.__setattr__(self, "active_workspace_ids", tuple(self.active_workspace_ids))
        if not isinstance(self.active_run_ids, tuple):
            object.__setattr__(self, "active_run_ids", tuple(self.active_run_ids))
        if not isinstance(self.candidate_ids, tuple):
            object.__setattr__(self, "candidate_ids", tuple(self.candidate_ids))
        if not isinstance(self.blockers, tuple):
            object.__setattr__(self, "blockers", tuple(self.blockers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONTROL_PLANE_CONTRACT_VERSION,
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "intent_id": self.intent_id,
            "base_sha": self.base_sha,
            "phase": self.phase.value,
            "execution_epoch": self.execution_epoch,
            "selected_strategy": self.selected_strategy.value,
            "active_workspace_ids": list(self.active_workspace_ids),
            "active_run_ids": list(self.active_run_ids),
            "candidate_ids": list(self.candidate_ids),
            "selected_candidate_id": self.selected_candidate_id,
            "requalification_required": self.requalification_required,
            "blockers": list(self.blockers),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> EngineeringCycleState:
        phase = ControlPlanePhase(data["phase"]) if isinstance(data["phase"], str) else data["phase"]
        strat = ParallelizationStrategy(data.get("selected_strategy", ParallelizationStrategy.NONE.value))
        return cls(
            cycle_id=str(data["cycle_id"]),
            task_id=str(data["task_id"]),
            node_id=str(data["node_id"]),
            intent_id=str(data["intent_id"]),
            base_sha=str(data["base_sha"]),
            phase=phase,
            execution_epoch=int(data.get("execution_epoch", 1)),
            selected_strategy=strat,
            active_workspace_ids=tuple(data.get("active_workspace_ids", ())),
            active_run_ids=tuple(data.get("active_run_ids", ())),
            candidate_ids=tuple(data.get("candidate_ids", ())),
            selected_candidate_id=data.get("selected_candidate_id"),
            requalification_required=bool(data.get("requalification_required", False)),
            blockers=tuple(data.get("blockers", ())),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )

    @classmethod
    def from_json(cls, raw: str) -> EngineeringCycleState:
        return cls.from_dict(json.loads(raw))
