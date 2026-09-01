"""Typed ControlPlaneEvent contract (PR-11.1 hardened with identity fencing)."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
            f"Invalid {label}: {value!r}",
        )
    if ":" in value or ".." in value:
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
            f"{label} must not embed drive or traversal components: {value!r}",
        )
    return value


def _require_optional_identifier(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_identifier(value, label)


@dataclass(frozen=True, slots=True)
class ControlPlaneEvent:
    """Strongly typed event envelope emitted to or within the Control Plane.

    Identity fencing fields (run_id, workspace_id, candidate_id,
    execution_host_id) are optional; when present, the orchestrator
    validates them against the identities it actually authorized.
    """

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
    run_id: str | None = None
    workspace_id: str | None = None
    candidate_id: str | None = None
    execution_host_id: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.event_id, "event_id")
        _require_identifier(self.cycle_id, "cycle_id")
        _require_identifier(self.task_id, "task_id")
        _require_identifier(self.node_id, "node_id")
        if not isinstance(self.execution_epoch, int) or isinstance(self.execution_epoch, bool):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                f"execution_epoch must be int >= 1, got {self.execution_epoch!r}",
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
        object.__setattr__(self, "run_id", _require_optional_identifier(self.run_id, "run_id"))
        object.__setattr__(
            self, "workspace_id", _require_optional_identifier(self.workspace_id, "workspace_id")
        )
        object.__setattr__(
            self, "candidate_id", _require_optional_identifier(self.candidate_id, "candidate_id")
        )
        object.__setattr__(
            self,
            "execution_host_id",
            _require_optional_identifier(self.execution_host_id, "execution_host_id"),
        )

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
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "execution_host_id": self.execution_host_id,
        }
