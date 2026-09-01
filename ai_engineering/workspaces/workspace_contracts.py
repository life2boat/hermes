"""Immutable strongly-typed contracts and blockers for Workspace Identity and Worktree Safety."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

WORKSPACE_CONTRACT_VERSION = 1
WORKTREE_LEASE_VERSION = 1

_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)


class ExecutionMode(StrEnum):
    """Supported workspace execution modes."""

    LOCAL = "LOCAL"
    WSL = "WSL"
    ISOLATED = "ISOLATED"
    CONTAINER = "CONTAINER"
    REMOTE = "REMOTE"


class LeaseState(StrEnum):
    """Worktree lease lifecycle states."""

    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    RELEASE_PENDING = "RELEASE_PENDING"
    RELEASED = "RELEASED"
    QUARANTINED = "QUARANTINED"


class WorkspaceBlockingReason(StrEnum):
    """Machine-readable reason codes for workspace safety and identity gates."""

    WORKSPACE_PATH_ESCAPE = "WORKSPACE_PATH_ESCAPE"
    WORKTREE_BASE_SHA_MISMATCH = "WORKTREE_BASE_SHA_MISMATCH"
    WORKTREE_DIRTY_REUSE = "WORKTREE_DIRTY_REUSE"
    WORKTREE_IDENTITY_MISMATCH = "WORKTREE_IDENTITY_MISMATCH"
    CANONICAL_CHECKOUT_COLLISION = "CANONICAL_CHECKOUT_COLLISION"
    CANONICAL_CHECKOUT_PROTECTED = "CANONICAL_CHECKOUT_PROTECTED"
    WORKTREE_CREATION_FAILED = "WORKTREE_CREATION_FAILED"
    WORKTREE_REMOVAL_FAILED = "WORKTREE_REMOVAL_FAILED"
    LEASE_TRANSITION_INVALID = "LEASE_TRANSITION_INVALID"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_OWNERSHIP_MISMATCH = "LEASE_OWNERSHIP_MISMATCH"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    WORKSPACE_ALREADY_EXISTS = "WORKSPACE_ALREADY_EXISTS"


class WorkspaceSecurityError(ValueError):
    """Fail-closed validation error for workspace security, isolation, and path violations."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class WorktreeSafetyError(RuntimeError):
    """Fail-closed error for git worktree lifecycle and safety violations."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class LeaseTransitionError(ValueError):
    """Fail-closed error for invalid worktree lease transitions."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _validate_iso_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    raise WorkspaceSecurityError("INVALID_DATETIME", f"Expected datetime or ISO string, got {type(value)}")


def _format_iso_datetime(dt: datetime) -> str:
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc_dt.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """Immutable strongly typed identity and provenance for an isolated workspace."""

    workspace_id: str
    task_id: str
    candidate_id: str | None

    repository: str
    base_ref: str
    base_sha: str

    branch: str
    worktree_path: str

    execution_host_id: str
    execution_mode: str

    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not _IDENTIFIER_RE.match(self.workspace_id):
            raise WorkspaceSecurityError("INVALID_WORKSPACE_ID", f"Invalid workspace_id: {self.workspace_id!r}")
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise WorkspaceSecurityError("INVALID_TASK_ID", f"Invalid task_id: {self.task_id!r}")
        if self.candidate_id is not None:
            if not isinstance(self.candidate_id, str) or not _IDENTIFIER_RE.match(self.candidate_id):
                raise WorkspaceSecurityError("INVALID_CANDIDATE_ID", f"Invalid candidate_id: {self.candidate_id!r}")
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise WorkspaceSecurityError("INVALID_REPOSITORY", "repository must be a non-empty string")
        if not isinstance(self.base_ref, str) or not self.base_ref.strip():
            raise WorkspaceSecurityError("INVALID_BASE_REF", "base_ref must be a non-empty string")
        if not isinstance(self.base_sha, str) or not _SHA_RE.match(self.base_sha):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value,
                f"base_sha must be a 40-character hex string: {self.base_sha!r}",
            )
        if not isinstance(self.branch, str) or not self.branch.strip():
            raise WorkspaceSecurityError("INVALID_BRANCH", "branch must be a non-empty string")
        if not isinstance(self.worktree_path, str) or not self.worktree_path.strip():
            raise WorkspaceSecurityError("INVALID_WORKTREE_PATH", "worktree_path must be a non-empty string")
        if not isinstance(self.execution_host_id, str) or not self.execution_host_id.strip():
            raise WorkspaceSecurityError("INVALID_EXECUTION_HOST_ID", "execution_host_id must be non-empty")
        if not isinstance(self.execution_mode, str) or not self.execution_mode.strip():
            raise WorkspaceSecurityError("INVALID_EXECUTION_MODE", "execution_mode must be non-empty")
        if not isinstance(self.created_at, datetime):
            raise WorkspaceSecurityError("INVALID_CREATED_AT", "created_at must be a datetime")

    def to_dict(self) -> dict[str, Any]:
        """Serialize workspace identity to a canonical dictionary."""
        return {
            "schema_version": WORKSPACE_CONTRACT_VERSION,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "repository": self.repository,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha.lower(),
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "execution_host_id": self.execution_host_id,
            "execution_mode": self.execution_mode,
            "created_at": _format_iso_datetime(self.created_at),
        }

    def to_json(self) -> str:
        """Serialize to deterministic canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WorkspaceIdentity:
        """Deserialize from a dictionary with fail-closed validation."""
        if not isinstance(payload, Mapping):
            raise WorkspaceSecurityError("INVALID_PAYLOAD", "Payload must be a mapping")
        created_at = _validate_iso_datetime(payload.get("created_at"))
        return cls(
            workspace_id=str(payload.get("workspace_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            candidate_id=payload.get("candidate_id"),
            repository=str(payload.get("repository") or ""),
            base_ref=str(payload.get("base_ref") or ""),
            base_sha=str(payload.get("base_sha") or "").lower(),
            branch=str(payload.get("branch") or ""),
            worktree_path=str(payload.get("worktree_path") or ""),
            execution_host_id=str(payload.get("execution_host_id") or ""),
            execution_mode=str(payload.get("execution_mode") or ""),
            created_at=created_at,
        )

    @classmethod
    def from_json(cls, raw: str) -> WorkspaceIdentity:
        """Deserialize from a JSON string."""
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise WorkspaceSecurityError("INVALID_JSON", f"Could not parse JSON: {exc}") from exc
        return cls.from_dict(payload)


_VALID_LEASE_TRANSITIONS: dict[LeaseState, frozenset[LeaseState]] = {
    LeaseState.RESERVED: frozenset({LeaseState.ACTIVE, LeaseState.RELEASED, LeaseState.QUARANTINED}),
    LeaseState.ACTIVE: frozenset({LeaseState.RELEASE_PENDING, LeaseState.RELEASED, LeaseState.QUARANTINED}),
    LeaseState.RELEASE_PENDING: frozenset({LeaseState.RELEASED, LeaseState.QUARANTINED}),
    LeaseState.RELEASED: frozenset(),
    LeaseState.QUARANTINED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class WorktreeLease:
    """Represents an explicit, bounded lease on an isolated worktree."""

    workspace_id: str
    owner_run_id: str
    task_id: str
    acquired_at: datetime
    expires_at: datetime | None
    state: LeaseState

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not _IDENTIFIER_RE.match(self.workspace_id):
            raise LeaseTransitionError("INVALID_WORKSPACE_ID", f"Invalid workspace_id: {self.workspace_id!r}")
        if not isinstance(self.owner_run_id, str) or not _IDENTIFIER_RE.match(self.owner_run_id):
            raise LeaseTransitionError("INVALID_OWNER_RUN_ID", f"Invalid owner_run_id: {self.owner_run_id!r}")
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise LeaseTransitionError("INVALID_TASK_ID", f"Invalid task_id: {self.task_id!r}")
        if not isinstance(self.acquired_at, datetime):
            raise LeaseTransitionError("INVALID_ACQUIRED_AT", "acquired_at must be a datetime")
        if self.expires_at is not None and not isinstance(self.expires_at, datetime):
            raise LeaseTransitionError("INVALID_EXPIRES_AT", "expires_at must be a datetime or None")
        if not isinstance(self.state, LeaseState):
            raise LeaseTransitionError("INVALID_LEASE_STATE", f"Invalid state: {self.state!r}")

    def transition(
        self,
        new_state: LeaseState,
        *,
        actor_run_id: str | None = None,
        now: datetime | None = None,
    ) -> WorktreeLease:
        """Transition lease to a new state with strict fail-closed state machine validation."""
        if not isinstance(new_state, LeaseState):
            raise LeaseTransitionError(
                WorkspaceBlockingReason.LEASE_TRANSITION_INVALID.value,
                f"Target state must be a LeaseState enum, got {new_state!r}",
            )
        if actor_run_id is not None and actor_run_id != self.owner_run_id:
            raise LeaseTransitionError(
                WorkspaceBlockingReason.LEASE_OWNERSHIP_MISMATCH.value,
                f"Actor run {actor_run_id!r} does not own lease (owned by {self.owner_run_id!r})",
            )
        allowed = _VALID_LEASE_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise LeaseTransitionError(
                WorkspaceBlockingReason.LEASE_TRANSITION_INVALID.value,
                f"Invalid lease state transition: {self.state.value} -> {new_state.value}",
            )
        transition_time = _validate_iso_datetime(now) if now is not None else datetime.now(timezone.utc)
        return WorktreeLease(
            workspace_id=self.workspace_id,
            owner_run_id=self.owner_run_id,
            task_id=self.task_id,
            acquired_at=self.acquired_at if self.state != LeaseState.RESERVED or new_state != LeaseState.ACTIVE else transition_time,
            expires_at=self.expires_at,
            state=new_state,
        )

    def is_active(self, *, now: datetime | None = None) -> bool:
        """Check if the lease is currently in ACTIVE state and not expired."""
        if self.state != LeaseState.ACTIVE:
            return False
        if self.expires_at is not None:
            check_time = _validate_iso_datetime(now) if now is not None else datetime.now(timezone.utc)
            if check_time > self.expires_at:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        """Serialize lease to a canonical dictionary."""
        return {
            "schema_version": WORKTREE_LEASE_VERSION,
            "workspace_id": self.workspace_id,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "acquired_at": _format_iso_datetime(self.acquired_at),
            "expires_at": _format_iso_datetime(self.expires_at) if self.expires_at else None,
            "state": self.state.value,
        }

    def to_json(self) -> str:
        """Serialize to deterministic canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WorktreeLease:
        """Deserialize lease from dictionary with validation."""
        if not isinstance(payload, Mapping):
            raise LeaseTransitionError("INVALID_PAYLOAD", "Payload must be a mapping")
        acquired_at = _validate_iso_datetime(payload.get("acquired_at"))
        raw_expires = payload.get("expires_at")
        expires_at = _validate_iso_datetime(raw_expires) if raw_expires else None
        state_val = payload.get("state")
        try:
            state = LeaseState(str(state_val))
        except ValueError as exc:
            raise LeaseTransitionError("INVALID_LEASE_STATE", f"Unknown lease state: {state_val!r}") from exc
        return cls(
            workspace_id=str(payload.get("workspace_id") or ""),
            owner_run_id=str(payload.get("owner_run_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            acquired_at=acquired_at,
            expires_at=expires_at,
            state=state,
        )

    @classmethod
    def from_json(cls, raw: str) -> WorktreeLease:
        """Deserialize lease from JSON string."""
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise LeaseTransitionError("INVALID_JSON", f"Could not parse JSON: {exc}") from exc
        return cls.from_dict(payload)
