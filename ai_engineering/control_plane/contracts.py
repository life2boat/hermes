"""Control plane contracts, enums, error types, and reason codes (PR-11.1 hardened)."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from ai_engineering.control_plane._evidence_refs import validate_evidence_ref

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _secure_identifier(value: str) -> bool:
    """Identifiers must not embed traversal components or drive letters."""
    return ":" not in value and ".." not in value


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
            f"{label} must be a lowercase 64-hex SHA-256 digest, got {value!r}",
        )
    return value


def _require_commit_sha(value: object, label: str) -> str:
    """Git commit identities (TaskIntent.source_base_sha, cycle base) are 40-hex."""
    if not isinstance(value, str) or not _COMMIT_SHA_RE.fullmatch(value):
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
            f"{label} must be a lowercase 40-hex git commit SHA, got {value!r}",
        )
    return value

_CONTROL_PLANE_CONTRACT_VERSION = "4.1.1"
CONTROL_PLANE_CONTRACT_VERSION = _CONTROL_PLANE_CONTRACT_VERSION


class ControlPlanePhase(StrEnum):
    """Explicit bounded phases of an Engineering Cycle."""

    CREATED = "CREATED"
    QUALIFIED = "QUALIFIED"
    PLANNED = "PLANNED"
    PREPARING = "PREPARING"
    INVESTIGATING = "INVESTIGATING"
    IMPLEMENTING = "IMPLEMENTING"
    JUDGING = "JUDGING"
    VALIDATING = "VALIDATING"
    REQUALIFYING = "REQUALIFYING"
    READY_FOR_HANDOFF = "READY_FOR_HANDOFF"
    BLOCKED = "BLOCKED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


class ControlPlaneEventType(StrEnum):
    """Domain event types recognized by the Control Plane."""

    WORKSPACE_READY = "WORKSPACE_READY"
    INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"
    CANDIDATE_COMPLETED = "CANDIDATE_COMPLETED"
    JUDGEMENT_COMPLETED = "JUDGEMENT_COMPLETED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    REQUALIFICATION_COMPLETED = "REQUALIFICATION_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"
    BLOCKER_RAISED = "BLOCKER_RAISED"


class ControlPlaneBlockingReason(StrEnum):
    """Machine-readable reason codes for control plane blockers."""

    CONTROL_PLANE_STATE_INVALID = "CONTROL_PLANE_STATE_INVALID"
    CONTROL_PLANE_EVENT_COLLISION = "CONTROL_PLANE_EVENT_COLLISION"
    CONTROL_PLANE_STALE_EVENT = "CONTROL_PLANE_STALE_EVENT"
    CONTROL_PLANE_HANDOFF_INCOMPLETE = "CONTROL_PLANE_HANDOFF_INCOMPLETE"
    CONTROL_PLANE_AUTHORIZATION_MISMATCH = "CONTROL_PLANE_AUTHORIZATION_MISMATCH"
    CONTROL_PLANE_BARRIER_NOT_READY = "CONTROL_PLANE_BARRIER_NOT_READY"
    REMOTE_EXECUTION_UNVERIFIABLE = "REMOTE_EXECUTION_UNVERIFIABLE"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    EXECUTION_HOST_MISMATCH = "EXECUTION_HOST_MISMATCH"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"
    WORKTREE_IDENTITY_MISMATCH = "WORKTREE_IDENTITY_MISMATCH"


class ControlPlaneError(Exception):
    """Fail-closed error for control plane invariant and contract violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _require_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
            f"{label} must be a lowercase 64-hex SHA-256 digest, got {value!r}",
        )
    return value


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise ControlPlaneError(
            ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
            f"Invalid {label}: {value!r}",
        )
    return value


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    """Immutable validation evidence bound to one cycle, candidate, and base identity.

    A bare ``validation_passed`` boolean is never sufficient: reaching
    ``READY_FOR_HANDOFF`` requires one of these records whose identity fields
    exactly match the cycle state and the judged candidate.
    """

    evidence_id: str
    cycle_id: str
    task_id: str
    node_id: str
    candidate_id: str
    base_sha: str
    execution_epoch: int
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("evidence_id", self.evidence_id),
            ("cycle_id", self.cycle_id),
            ("task_id", self.task_id),
            ("node_id", self.node_id),
            ("candidate_id", self.candidate_id),
        ):
            _require_identifier(value, f"ValidationEvidence.{label}")
            if not _secure_identifier(value):
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"ValidationEvidence.{label} must not embed traversal or drive "
                    f"components: {value!r}",
                )
        _require_commit_sha(self.base_sha, "ValidationEvidence.base_sha")
        if not isinstance(self.execution_epoch, int) or isinstance(self.execution_epoch, bool):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"ValidationEvidence.execution_epoch must be int, got {self.execution_epoch!r}",
            )
        if self.execution_epoch < 1:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"ValidationEvidence.execution_epoch must be >= 1, got {self.execution_epoch}",
            )
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if not self.evidence_refs:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                "ValidationEvidence requires at least one concrete evidence reference",
            )
        for ref in self.evidence_refs:
            validate_evidence_ref(ref, ControlPlaneError)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "candidate_id": self.candidate_id,
            "base_sha": self.base_sha,
            "execution_epoch": self.execution_epoch,
            "evidence_refs": list(self.evidence_refs),
        }
