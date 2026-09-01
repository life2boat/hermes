"""Immutable strongly-typed contracts for Agent Run Identity, Epochs, and Events."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

AGENT_RUN_CONTRACT_VERSION = 1
RUN_EVENT_SCHEMA_VERSION = 1

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)


class RunState(StrEnum):
    """Lifecycle states for an individual agent run."""

    CREATED = "CREATED"
    START_REQUESTED = "START_REQUESTED"
    LIVE = "LIVE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    EXITED = "EXITED"
    FAILED = "FAILED"


class RunEventType(StrEnum):
    """Typed domain event envelopes for run telemetry and lifecycle."""

    AGENT_RUN_CREATED = "AGENT_RUN_CREATED"
    AGENT_RUN_START_REQUESTED = "AGENT_RUN_START_REQUESTED"
    AGENT_RUN_LIVE = "AGENT_RUN_LIVE"
    AGENT_RUN_CANCEL_REQUESTED = "AGENT_RUN_CANCEL_REQUESTED"
    AGENT_RUN_EXITED = "AGENT_RUN_EXITED"
    AGENT_RUN_FAILED = "AGENT_RUN_FAILED"
    WORKSPACE_PREPARED = "WORKSPACE_PREPARED"
    VALIDATION_COMPLETED = "VALIDATION_COMPLETED"
    CANDIDATE_COMPLETED = "CANDIDATE_COMPLETED"
    STALE_RUN_EVENT_REJECTED = "STALE_RUN_EVENT_REJECTED"


class RunBlockingReason(StrEnum):
    """Deterministic machine-readable reason codes for execution and fencing blockers."""

    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    RUN_IDENTITY_COLLISION = "RUN_IDENTITY_COLLISION"
    RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
    RUN_STATE_TRANSITION_INVALID = "RUN_STATE_TRANSITION_INVALID"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"
    RUN_LEASE_OWNERSHIP_MISMATCH = "RUN_LEASE_OWNERSHIP_MISMATCH"
    LEASE_OWNERSHIP_MISMATCH = "LEASE_OWNERSHIP_MISMATCH"
    DUPLICATE_ACTIVE_RUN = "DUPLICATE_ACTIVE_RUN"
    INVALID_EPOCH = "INVALID_EPOCH"
    INVALID_RUN_IDENTITY = "INVALID_RUN_IDENTITY"
    UNKNOWN_WORKSPACE = "UNKNOWN_WORKSPACE"


class RunIdentityError(ValueError):
    """Fail-closed error for run identity, provenance, collision, and workspace binding violations."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class RunStateError(ValueError):
    """Fail-closed error for invalid run state transitions and lifecycle violations."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class StaleEventError(ValueError):
    """Fail-closed error for stale run event and stale epoch fencing."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class SpawnStatus(StrEnum):
    """Status returned by idempotent agent spawn attempts."""

    SPAWNED = "SPAWNED"
    ALREADY_ACTIVE = "ALREADY_ACTIVE"
    REJECTED = "REJECTED"


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
    raise RunIdentityError("INVALID_DATETIME", f"Expected datetime or ISO string, got {type(value)}")


def _format_iso_datetime(dt: datetime) -> str:
    utc_dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return utc_dt.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class AgentRunIdentity:
    """Immutable, strongly typed identity for an individual agent execution run."""

    run_id: str
    task_id: str
    node_id: str
    workspace_id: str
    candidate_id: str | None
    model: str
    agent_capability: str
    execution_host_id: str
    execution_epoch: int
    start_time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not _IDENTIFIER_RE.match(self.run_id):
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, f"Invalid run_id: {self.run_id!r}")
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, f"Invalid task_id: {self.task_id!r}")
        if not isinstance(self.node_id, str) or not _IDENTIFIER_RE.match(self.node_id):
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, f"Invalid node_id: {self.node_id!r}")
        if not isinstance(self.workspace_id, str) or not _IDENTIFIER_RE.match(self.workspace_id):
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, f"Invalid workspace_id: {self.workspace_id!r}")
        if self.candidate_id is not None:
            if not isinstance(self.candidate_id, str) or not _IDENTIFIER_RE.match(self.candidate_id):
                raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, f"Invalid candidate_id: {self.candidate_id!r}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, "model must be non-empty string")
        if not isinstance(self.agent_capability, str) or not self.agent_capability.strip():
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, "agent_capability must be non-empty string")
        if not isinstance(self.execution_host_id, str) or not self.execution_host_id.strip():
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, "execution_host_id must be non-empty string")
        if not isinstance(self.execution_epoch, int) or self.execution_epoch < 1:
            raise RunIdentityError(RunBlockingReason.INVALID_EPOCH.value, f"execution_epoch must be integer >= 1, got {self.execution_epoch!r}")
        if not isinstance(self.start_time, datetime):
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, "start_time must be datetime")

    def to_dict(self) -> dict[str, Any]:
        """Serialize run identity to a canonical dictionary."""
        return {
            "schema_version": AGENT_RUN_CONTRACT_VERSION,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "model": self.model,
            "agent_capability": self.agent_capability,
            "execution_host_id": self.execution_host_id,
            "execution_epoch": self.execution_epoch,
            "start_time": _format_iso_datetime(self.start_time),
        }

    def to_json(self) -> str:
        """Serialize to deterministic canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentRunIdentity:
        """Deserialize from dictionary with fail-closed validation."""
        if not isinstance(payload, Mapping):
            raise RunIdentityError(RunBlockingReason.INVALID_RUN_IDENTITY.value, "Payload must be a mapping")
        start_time = _validate_iso_datetime(payload.get("start_time"))
        return cls(
            run_id=str(payload.get("run_id") or ""),
            task_id=str(payload.get("task_id") or ""),
            node_id=str(payload.get("node_id") or ""),
            workspace_id=str(payload.get("workspace_id") or ""),
            candidate_id=payload.get("candidate_id"),
            model=str(payload.get("model") or ""),
            agent_capability=str(payload.get("agent_capability") or ""),
            execution_host_id=str(payload.get("execution_host_id") or ""),
            execution_epoch=int(payload.get("execution_epoch") or 0),
            start_time=start_time,
        )

    @classmethod
    def from_json(cls, raw: str) -> AgentRunIdentity:
        """Deserialize from a JSON string."""
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise RunIdentityError("INVALID_JSON", f"Could not parse JSON: {exc}") from exc
        return cls.from_dict(payload)


@dataclass(frozen=True, slots=True)
class RunEventEnvelope:
    """Bounded, typed event envelope for run lifecycle and telemetry fencing."""

    event_id: str
    run_id: str
    execution_epoch: int
    event_type: RunEventType
    payload: Mapping[str, Any]
    timestamp: datetime
    task_id: str | None = None
    node_id: str | None = None
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not _IDENTIFIER_RE.match(self.event_id):
            raise StaleEventError("INVALID_EVENT_ID", f"Invalid event_id: {self.event_id!r}")
        if not isinstance(self.run_id, str) or not _IDENTIFIER_RE.match(self.run_id):
            raise StaleEventError("INVALID_RUN_ID", f"Invalid run_id: {self.run_id!r}")
        if not isinstance(self.execution_epoch, int) or self.execution_epoch < 1:
            raise StaleEventError(RunBlockingReason.INVALID_EPOCH.value, f"Invalid execution_epoch: {self.execution_epoch!r}")
        if not isinstance(self.event_type, RunEventType):
            raise StaleEventError("INVALID_EVENT_TYPE", f"Invalid event_type: {self.event_type!r}")
        if not isinstance(self.payload, Mapping):
            raise StaleEventError("INVALID_PAYLOAD", "Payload must be a mapping")
        if not isinstance(self.timestamp, datetime):
            raise StaleEventError("INVALID_TIMESTAMP", "Timestamp must be datetime")

    def to_dict(self) -> dict[str, Any]:
        """Serialize event envelope to canonical dictionary."""
        return {
            "schema_version": RUN_EVENT_SCHEMA_VERSION,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "execution_epoch": self.execution_epoch,
            "event_type": self.event_type.value,
            "payload": dict(self.payload),
            "timestamp": _format_iso_datetime(self.timestamp),
            "task_id": self.task_id,
            "node_id": self.node_id,
            "workspace_id": self.workspace_id,
        }

    def to_json(self) -> str:
        """Serialize to deterministic canonical JSON."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RunEventEnvelope:
        """Deserialize from dictionary."""
        if not isinstance(payload, Mapping):
            raise StaleEventError("INVALID_PAYLOAD", "Payload must be a mapping")
        ts = _validate_iso_datetime(payload.get("timestamp"))
        raw_type = payload.get("event_type")
        try:
            event_type = RunEventType(str(raw_type))
        except ValueError as exc:
            raise StaleEventError("INVALID_EVENT_TYPE", f"Unknown event_type: {raw_type!r}") from exc
        return cls(
            event_id=str(payload.get("event_id") or ""),
            run_id=str(payload.get("run_id") or ""),
            execution_epoch=int(payload.get("execution_epoch") or 0),
            event_type=event_type,
            payload=payload.get("payload") if isinstance(payload.get("payload"), Mapping) else {},
            timestamp=ts,
            task_id=payload.get("task_id"),
            node_id=payload.get("node_id"),
            workspace_id=payload.get("workspace_id"),
        )

    @classmethod
    def from_json(cls, raw: str) -> RunEventEnvelope:
        """Deserialize from JSON string."""
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise StaleEventError("INVALID_JSON", f"Could not parse JSON: {exc}") from exc
        return cls.from_dict(payload)
