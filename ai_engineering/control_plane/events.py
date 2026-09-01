"""Typed ControlPlaneEvent contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import re
from typing import Any

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ControlPlaneEvent:
    """Strongly typed event envelope emitted to or within the Control Plane."""

    event_id: str
    cycle_id: str
    task_id: str
    node_id: str
    execution_epoch: int
    event_type: ControlPlaneEventType
    source_kind: str
    source_id: str
    created_at: str = "2026-09-01T00:00:00Z"
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not _IDENTIFIER_RE.match(self.event_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid event_id: {self.event_id!r}",
            )
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
        if self.execution_epoch < 1:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                f"execution_epoch must be >= 1, got {self.execution_epoch}",
            )
        if not isinstance(self.event_type, ControlPlaneEventType):
            try:
                object.__setattr__(self, "event_type", ControlPlaneEventType(str(self.event_type)))
            except ValueError as exc:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Invalid event_type: {self.event_type!r}",
                ) from exc
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "execution_epoch": self.execution_epoch,
            "event_type": self.event_type.value,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "created_at": self.created_at,
            "evidence_refs": list(self.evidence_refs),
        }
