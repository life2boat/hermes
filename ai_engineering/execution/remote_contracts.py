"""Strongly typed contracts for SSH-ready remote execution, sessions, process identity, and reconciliation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from ai_engineering.execution.host_contracts import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ExecutionHostIdentity,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    HostCapability,
    HostPlatform,
)
from ai_engineering.execution.run_contracts import AgentRunIdentity, RunBlockingReason
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)

REMOTE_CONTRACT_VERSION = "4.1.0"


class RemoteHostPlatform(StrEnum):
    """Remote host operating system platform."""

    LINUX = "LINUX"
    DARWIN = "DARWIN"
    WINDOWS = "WINDOWS"
    UNKNOWN = "UNKNOWN"


class RemoteHostState(StrEnum):
    """Remote execution host reachability and verification state."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"
    UNVERIFIABLE = "UNVERIFIABLE"


class RemoteExecutionState(StrEnum):
    """Lifecycle states of a remote command execution."""

    CREATED = "CREATED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    STARTING = "STARTING"
    LIVE = "LIVE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    DISCONNECTED = "DISCONNECTED"
    UNVERIFIABLE = "UNVERIFIABLE"
    EXITED = "EXITED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class RemoteEventType(StrEnum):
    """Event types emitted during remote lifecycle."""

    REMOTE_CONNECTED = "REMOTE_CONNECTED"
    REMOTE_EXECUTION_STARTED = "REMOTE_EXECUTION_STARTED"
    REMOTE_OUTPUT_RECEIVED = "REMOTE_OUTPUT_RECEIVED"
    REMOTE_CANCEL_ACK = "REMOTE_CANCEL_ACK"
    REMOTE_DISCONNECTED = "REMOTE_DISCONNECTED"
    REMOTE_RECONNECTED = "REMOTE_RECONNECTED"
    REMOTE_EXECUTION_EXITED = "REMOTE_EXECUTION_EXITED"
    REMOTE_RECONCILED = "REMOTE_RECONCILED"


class ReconciliationOutcome(StrEnum):
    """Outcome of attempting to reconcile a disconnected or unverifiable remote execution."""

    CONFIRMED_LIVE = "CONFIRMED_LIVE"
    CONFIRMED_EXITED = "CONFIRMED_EXITED"
    UNVERIFIABLE = "UNVERIFIABLE"
    FAILED = "FAILED"


class RemoteBlockingReason(StrEnum):
    """Machine-readable reason codes for remote execution blockers."""

    REMOTE_EXECUTION_UNVERIFIABLE = "REMOTE_EXECUTION_UNVERIFIABLE"
    REMOTE_CONNECTION_FAILED = "REMOTE_CONNECTION_FAILED"
    REMOTE_AUTH_UNAVAILABLE = "REMOTE_AUTH_UNAVAILABLE"
    REMOTE_HOST_TRUST_UNVERIFIED = "REMOTE_HOST_TRUST_UNVERIFIED"
    REMOTE_SESSION_INVALID = "REMOTE_SESSION_INVALID"
    REMOTE_RECONCILIATION_REQUIRED = "REMOTE_RECONCILIATION_REQUIRED"
    EXECUTION_HOST_MISMATCH = "EXECUTION_HOST_MISMATCH"
    EXECUTION_HOST_UNAVAILABLE = "EXECUTION_HOST_UNAVAILABLE"
    EXECUTION_MODE_INVALID = "EXECUTION_MODE_INVALID"
    EXECUTION_REQUEST_INVALID = "EXECUTION_REQUEST_INVALID"
    EXECUTION_PATH_INVALID = "EXECUTION_PATH_INVALID"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"
    WORKTREE_IDENTITY_MISMATCH = "WORKTREE_IDENTITY_MISMATCH"


class RemoteExecutionError(Exception):
    """Fail-closed error for remote execution contract and state violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class RemoteExecutionHostIdentity:
    """Immutable identity contract for an SSH-ready remote execution host."""

    execution_host_id: str
    mode: ExecutionMode = ExecutionMode.SSH
    host_alias: str = ""
    remote_platform: RemoteHostPlatform = RemoteHostPlatform.LINUX
    architecture: str = "x86_64"
    capabilities: tuple[HostCapability, ...] = (
        HostCapability.CAN_RUN_COMMANDS,
        HostCapability.CAN_ACCESS_REPOSITORY,
        HostCapability.CAN_CREATE_WORKTREE,
        HostCapability.CAN_SIGNAL_PROCESS,
        HostCapability.CAN_CAPTURE_STDOUT,
        HostCapability.CAN_CAPTURE_STDERR,
    )
    trust_domain: str = "default"
    created_at: str = "2026-09-01T00:00:00Z"

    def __post_init__(self) -> None:
        if not isinstance(self.execution_host_id, str) or not _IDENTIFIER_RE.match(self.execution_host_id):
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                f"Invalid execution_host_id: {self.execution_host_id!r}",
            )
        if not isinstance(self.host_alias, str) or not self.host_alias.strip():
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "Remote host_alias must be non-empty",
            )
        if not isinstance(self.capabilities, tuple):
            object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_host_id": self.execution_host_id,
            "mode": self.mode.value,
            "host_alias": self.host_alias,
            "remote_platform": self.remote_platform.value,
            "architecture": self.architecture,
            "capabilities": [c.value for c in self.capabilities],
            "trust_domain": self.trust_domain,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RemoteExecutionHostIdentity:
        mode = ExecutionMode(data.get("mode", ExecutionMode.SSH.value))
        plat = RemoteHostPlatform(data.get("remote_platform", RemoteHostPlatform.LINUX.value))
        caps = tuple(HostCapability(c) for c in data.get("capabilities", ()))
        return cls(
            execution_host_id=str(data["execution_host_id"]),
            mode=mode,
            host_alias=str(data.get("host_alias", "")),
            remote_platform=plat,
            architecture=str(data.get("architecture", "x86_64")),
            capabilities=caps,
            trust_domain=str(data.get("trust_domain", "default")),
            created_at=str(data.get("created_at", "")),
        )


@dataclass(frozen=True, slots=True)
class SshExecutionConfig:
    """Strongly typed configuration for SSH execution (uses opaque credential references only)."""

    host_alias: str
    port: int = 22
    username_ref: str = "ref://auth/user/default"
    credential_ref: str = "ref://vault/ssh/key-default"
    known_host_ref: str = "ref://known_hosts/default"
    connect_timeout_seconds: float = 10.0
    command_timeout_seconds: float = 30.0
    working_directory_policy: str = "REPOSITORY_RELATIVE"
    verification_required: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.host_alias, str) or not self.host_alias.strip():
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "host_alias must be non-empty",
            )
        if self.port <= 0 or self.port > 65535:
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                f"Invalid port: {self.port}",
            )
        if not self.credential_ref.startswith("ref://"):
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "credential_ref must be an opaque reference ('ref://...')",
            )
        if not self.known_host_ref.startswith("ref://"):
            raise RemoteExecutionError(
                RemoteBlockingReason.REMOTE_HOST_TRUST_UNVERIFIED.value,
                "known_host_ref must be an opaque reference ('ref://...')",
            )
        if not self.verification_required:
            raise RemoteExecutionError(
                RemoteBlockingReason.REMOTE_HOST_TRUST_UNVERIFIED.value,
                "verification_required cannot be false by default",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "host_alias": self.host_alias,
            "port": self.port,
            "username_ref": self.username_ref,
            "credential_ref": self.credential_ref,
            "known_host_ref": self.known_host_ref,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "command_timeout_seconds": self.command_timeout_seconds,
            "working_directory_policy": self.working_directory_policy,
            "verification_required": self.verification_required,
        }


@dataclass(frozen=True, slots=True)
class RemoteSessionIdentity:
    """Immutable identity for a remote transport session."""

    session_id: str
    execution_host_id: str
    execution_epoch: int
    transport_kind: str = "SSH_CONTRACT"
    remote_host_fingerprint_ref: str | None = "ref://fingerprints/sha256/default"
    created_at: str = "2026-09-01T00:00:00Z"

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not _IDENTIFIER_RE.match(self.session_id):
            raise RemoteExecutionError(
                RemoteBlockingReason.REMOTE_SESSION_INVALID.value,
                f"Invalid session_id: {self.session_id!r}",
            )
        if not isinstance(self.execution_host_id, str) or not _IDENTIFIER_RE.match(self.execution_host_id):
            raise RemoteExecutionError(
                RemoteBlockingReason.EXECUTION_HOST_MISMATCH.value,
                f"Invalid execution_host_id: {self.execution_host_id!r}",
            )
        if self.execution_epoch < 1:
            raise RemoteExecutionError(
                RemoteBlockingReason.STALE_RUN_MUTATION.value,
                f"execution_epoch must be >= 1, got {self.execution_epoch}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "execution_host_id": self.execution_host_id,
            "execution_epoch": self.execution_epoch,
            "transport_kind": self.transport_kind,
            "remote_host_fingerprint_ref": self.remote_host_fingerprint_ref,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class RemoteProcessIdentity:
    """Strongly typed composite identity for a process running on a remote host."""

    execution_id: str
    run_id: str
    workspace_id: str
    execution_host_id: str
    session_id: str
    remote_process_id: str
    execution_epoch: int

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not _IDENTIFIER_RE.match(self.execution_id):
            raise RemoteExecutionError(RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value, "Invalid execution_id")
        if not isinstance(self.run_id, str) or not _IDENTIFIER_RE.match(self.run_id):
            raise RemoteExecutionError(RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value, "Invalid run_id")
        if not isinstance(self.workspace_id, str) or not _IDENTIFIER_RE.match(self.workspace_id):
            raise RemoteExecutionError(RemoteBlockingReason.RUN_WORKSPACE_MISMATCH.value, "Invalid workspace_id")
        if not isinstance(self.execution_host_id, str) or not _IDENTIFIER_RE.match(self.execution_host_id):
            raise RemoteExecutionError(RemoteBlockingReason.EXECUTION_HOST_MISMATCH.value, "Invalid execution_host_id")
        if not isinstance(self.session_id, str) or not _IDENTIFIER_RE.match(self.session_id):
            raise RemoteExecutionError(RemoteBlockingReason.REMOTE_SESSION_INVALID.value, "Invalid session_id")
        if not isinstance(self.remote_process_id, str) or not self.remote_process_id.strip():
            raise RemoteExecutionError(RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value, "remote_process_id required")
        if self.execution_epoch < 1:
            raise RemoteExecutionError(RemoteBlockingReason.STALE_RUN_MUTATION.value, "Invalid execution_epoch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "execution_host_id": self.execution_host_id,
            "session_id": self.session_id,
            "remote_process_id": self.remote_process_id,
            "execution_epoch": self.execution_epoch,
        }


@dataclass(frozen=True, slots=True)
class RemoteOutputChunk:
    """Ordered, bounded chunk of stdout/stderr received from a remote session."""

    execution_id: str
    session_id: str
    execution_epoch: int
    stream: str  # "stdout" | "stderr"
    sequence_number: int
    data: str
    is_eof: bool = False

    def __post_init__(self) -> None:
        if self.stream not in ("stdout", "stderr"):
            raise RemoteExecutionError(RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value, "Invalid stream name")
        if self.sequence_number < 0:
            raise RemoteExecutionError(RemoteBlockingReason.EXECUTION_REQUEST_INVALID.value, "Invalid sequence_number")


@dataclass(frozen=True, slots=True)
class RemoteEventEnvelope:
    """Typed domain event envelope for remote execution lifecycle."""

    event_id: str
    event_type: RemoteEventType
    execution_id: str
    run_id: str
    execution_host_id: str
    session_id: str
    execution_epoch: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    timestamp: str = "2026-09-01T00:00:00Z"


@dataclass(frozen=True, slots=True)
class RemoteReconciliationRequest:
    """Request to reconcile process state on a remote host after disconnect."""

    execution_id: str
    run_id: str
    execution_host_id: str
    session_id: str
    execution_epoch: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "execution_host_id": self.execution_host_id,
            "session_id": self.session_id,
            "execution_epoch": self.execution_epoch,
        }


@dataclass(frozen=True, slots=True)
class RemoteReconciliationResult:
    """Deterministic result of reconciling remote process status."""

    execution_id: str
    run_id: str
    execution_host_id: str
    session_id: str
    execution_epoch: int
    outcome: ReconciliationOutcome
    process_confirmed_live: bool
    process_confirmed_exited: bool
    exit_code: int | None
    evidence: str
    reconciled_at: str
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReconciliationOutcome):
            try:
                object.__setattr__(self, "outcome", ReconciliationOutcome(str(self.outcome)))
            except ValueError:
                object.__setattr__(self, "outcome", ReconciliationOutcome.FAILED)
        if not isinstance(self.blockers, tuple):
            object.__setattr__(self, "blockers", tuple(self.blockers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "execution_host_id": self.execution_host_id,
            "session_id": self.session_id,
            "execution_epoch": self.execution_epoch,
            "outcome": self.outcome.value,
            "process_confirmed_live": self.process_confirmed_live,
            "process_confirmed_exited": self.process_confirmed_exited,
            "exit_code": self.exit_code,
            "evidence": self.evidence,
            "reconciled_at": self.reconciled_at,
            "blockers": list(self.blockers),
        }
