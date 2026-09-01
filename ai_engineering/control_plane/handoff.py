"""NodeHandoff contract carrying immutable validation and execution evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import re
from typing import Any

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class NodeHandoff:
    """Immutable handoff payload passed from one Task Graph node to another."""

    handoff_id: str
    task_id: str
    source_node_id: str
    target_node_id: str
    cycle_id: str
    base_sha: str
    execution_epoch: int
    evidence_refs: tuple[str, ...] = ()
    blocker_refs: tuple[str, ...] = ()
    selected_candidate_id: str | None = None
    created_at: str = "2026-09-01T00:00:00Z"

    def __post_init__(self) -> None:
        if not isinstance(self.handoff_id, str) or not _IDENTIFIER_RE.match(self.handoff_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid handoff_id: {self.handoff_id!r}",
            )
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if not isinstance(self.source_node_id, str) or not _IDENTIFIER_RE.match(self.source_node_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid source_node_id: {self.source_node_id!r}",
            )
        if not isinstance(self.target_node_id, str) or not _IDENTIFIER_RE.match(self.target_node_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid target_node_id: {self.target_node_id!r}",
            )
        if not isinstance(self.cycle_id, str) or not _IDENTIFIER_RE.match(self.cycle_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid cycle_id: {self.cycle_id!r}",
            )
        if not isinstance(self.base_sha, str) or not _SHA_RE.match(self.base_sha):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )
        if self.execution_epoch < 1:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                f"execution_epoch must be >= 1, got {self.execution_epoch}",
            )
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if not isinstance(self.blocker_refs, tuple):
            object.__setattr__(self, "blocker_refs", tuple(self.blocker_refs))

        # Check for forbidden foreign absolute paths in evidence refs
        for ref in self.evidence_refs:
            if ref.startswith("/") or ":" in ref and "\\" in ref:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    f"Evidence refs must not contain foreign absolute paths: {ref}",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "task_id": self.task_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "cycle_id": self.cycle_id,
            "base_sha": self.base_sha,
            "execution_epoch": self.execution_epoch,
            "evidence_refs": list(self.evidence_refs),
            "blocker_refs": list(self.blocker_refs),
            "selected_candidate_id": self.selected_candidate_id,
            "created_at": self.created_at,
        }
