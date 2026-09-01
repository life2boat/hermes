"""Immutable EngineeringCycleState contract representing control plane state (PR-11.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
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
from ai_engineering.task_intent import TaskIntent, intent_digest, validate_intent

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _fail_state(code: str) -> None:
    raise ControlPlaneError(code, f"EngineeringCycleState validation failed: {code}")


def _require_state_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
            f"Invalid {label}: {value!r}",
        )
    if ":" in value or ".." in value:
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
            f"{label} must not embed drive or traversal components: {value!r}",
        )
    return value


@dataclass(frozen=True, slots=True)
class EngineeringCycleState:
    """Immutable control-plane state of an engineering cycle.

    The cycle is canonically bound to a validated :class:`TaskIntent`:
    ``intent_digest`` is the content-addressed identity of the intent,
    ``intent_revision`` its revision, and ``repository_id`` the intent's
    source repository. Arbitrary regex-valid intent identifiers are not
    authority.
    """

    cycle_id: str
    task_id: str
    node_id: str
    intent_digest: str
    intent_revision: int
    repository_id: str
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

    @property
    def intent_id(self) -> str:
        """Backward-compatible read-only alias: the intent's content digest."""
        return self.intent_digest

    def __post_init__(self) -> None:
        _require_state_identifier(self.cycle_id, "cycle_id")
        _require_state_identifier(self.task_id, "task_id")
        _require_state_identifier(self.node_id, "node_id")
        if not isinstance(self.intent_digest, str) or not _SHA256_RE.fullmatch(self.intent_digest):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "intent_digest must be a lowercase 64-hex SHA-256 digest of a canonical "
                f"TaskIntent; got {self.intent_digest!r}",
            )
        if not isinstance(self.intent_revision, int) or isinstance(self.intent_revision, bool):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"intent_revision must be int, got {self.intent_revision!r}",
            )
        if self.intent_revision < 0:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"intent_revision must be >= 0, got {self.intent_revision}",
            )
        if not isinstance(self.repository_id, str) or not _IDENTIFIER_RE.match(self.repository_id):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"repository_id must be bound to the intent's source repository; "
                f"got {self.repository_id!r}",
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
        if not isinstance(self.execution_epoch, int) or isinstance(self.execution_epoch, bool):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"execution_epoch must be int, got {self.execution_epoch!r}",
            )
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
            "intent_digest": self.intent_digest,
            "intent_revision": self.intent_revision,
            "repository_id": self.repository_id,
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
    def from_task_intent(
        cls,
        intent: TaskIntent | Mapping[str, object],
        *,
        cycle_id: str,
        node_id: str,
    ) -> "EngineeringCycleState":
        """Bind a cycle to a canonical, fully validated :class:`TaskIntent`."""
        validated = validate_intent(intent)
        return cls(
            cycle_id=cycle_id,
            task_id=validated.task_id,
            node_id=node_id,
            intent_digest=intent_digest(validated),
            intent_revision=validated.intent_revision,
            repository_id=validated.source_repository,
            base_sha=validated.source_base_sha,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EngineeringCycleState":
        raw_phase = data.get("phase", ControlPlanePhase.CREATED.value)
        phase = ControlPlanePhase(raw_phase) if isinstance(raw_phase, str) else raw_phase
        strat_raw = data.get("selected_strategy", ParallelizationStrategy.NONE.value)
        strat = ParallelizationStrategy(strat_raw) if isinstance(strat_raw, str) else strat_raw
        return cls(
            cycle_id=str(data["cycle_id"]),
            task_id=str(data["task_id"]),
            node_id=str(data["node_id"]),
            intent_digest=str(data["intent_digest"]),
            intent_revision=int(data["intent_revision"]),
            repository_id=str(data["repository_id"]),
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
    def from_json(cls, raw: str) -> "EngineeringCycleState":
        return cls.from_dict(json.loads(raw))
