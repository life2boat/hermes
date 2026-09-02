"""Immutable strongly-typed contracts for the Controlled Agent Runtime (PR-13).

The runtime activates real local/WSL agent process execution inside
candidate workspaces authorized by the existing control plane. It emits
evidence only; it never mutates control-plane state, canonical
repositories, or production surfaces.

Reused canonical identities (no duplicates):
- :class:`ai_engineering.execution.run_contracts.AgentRunIdentity`
- :class:`ai_engineering.workspaces.workspace_contracts.WorkspaceIdentity` /
  ``WorktreeLease``
- :class:`ai_engineering.execution.host_contracts.ExecutionRequest` /
  ``ExecutionResult`` / ``ExecutionState``
- :class:`ai_engineering.candidates.candidate_contracts.CandidateResult`
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

try:
    from enum import StrEnum
except ImportError:  # pragma: no cover - Python < 3.11 fallback used repo-wide
    from enum import Enum

    class StrEnum(str, Enum):
        pass


RUNTIME_CONTRACT_VERSION = "4.1.0"
RUNTIME_SCHEMA_VERSION = 1

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class RuntimeMode(StrEnum):
    """Explicit runtime activation policy modes.

    PR-13 is SHADOW-only: real processes may run only in an explicitly
    activated local/WSL shadow mode. There is no production mode and no
    remote mode.
    """

    DISABLED = "DISABLED"
    SHADOW_LOCAL = "SHADOW_LOCAL"
    SHADOW_WSL = "SHADOW_WSL"


class RuntimeBlockingReason(StrEnum):
    """Machine-readable reason codes specific to the controlled runtime.

    Canonical blockers (STALE_RUN_MUTATION, RUN_WORKSPACE_MISMATCH,
    EXECUTION_HOST_MISMATCH, WORKTREE_IDENTITY_MISMATCH,
    WORKSPACE_PATH_ESCAPE, CANONICAL_CHECKOUT_COLLISION,
    PARALLELIZATION_BUDGET_EXCEEDED, CONTROL_PLANE_AUTHORIZATION_MISMATCH,
    CANDIDATE_BASE_SHA_MISMATCH, RUN_LEASE_OWNERSHIP_MISMATCH,
    RUN_NOT_ACTIVE, STALE_RUN_EVENT, EXECUTION_MODE_INVALID,
    EXECUTION_HOST_UNAVAILABLE, WORKSPACE_NOT_FOUND, LEASE_EXPIRED) are
    surfaced under their canonical names and are intentionally not
    duplicated here.
    """

    RUNTIME_ACTIVATION_DISABLED = "RUNTIME_ACTIVATION_DISABLED"
    RUNTIME_WORKSPACE_ESCAPE = "RUNTIME_WORKSPACE_ESCAPE"
    RUNTIME_COMMAND_NOT_AUTHORIZED = "RUNTIME_COMMAND_NOT_AUTHORIZED"
    RUNTIME_ENVIRONMENT_NOT_AUTHORIZED = "RUNTIME_ENVIRONMENT_NOT_AUTHORIZED"
    RUNTIME_SPAWN_COLLISION = "RUNTIME_SPAWN_COLLISION"
    RUNTIME_PROCESS_UNVERIFIABLE = "RUNTIME_PROCESS_UNVERIFIABLE"
    RUNTIME_OUTPUT_CAPTURE_FAILED = "RUNTIME_OUTPUT_CAPTURE_FAILED"
    STALE_RUNTIME_EVENT = "STALE_RUNTIME_EVENT"
    RUNTIME_EVIDENCE_INCOMPLETE = "RUNTIME_EVIDENCE_INCOMPLETE"


class AgentRuntimeError(ValueError):
    """Fail-closed error for runtime authorization and lifecycle violations."""

    def __init__(self, code: str, message: str, blockers: tuple[str, ...] = ()) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.blockers = blockers if blockers else (code,)


def _require_identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.match(value):
        raise AgentRuntimeError(
            RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
            f"Invalid {label}: {value!r}",
        )
    return value


def _require_sha(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise AgentRuntimeError(
            RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
            f"{label} must be a 40-hex git commit SHA, got {value!r}",
        )
    return value


@dataclass(frozen=True, slots=True)
class AgentExecutionRequest:
    """Immutable typed request to execute one bounded agent process.

    Binds the spawn to existing canonical identities: TaskIntent (via
    ``authority_digest``, verified against the intent content digest by
    the spawn gate), WorkspaceIdentity/WorktreeLease, AgentRunIdentity,
    CandidateIdentity, ExecutionHostIdentity, and the cycle base
    identity (repository, base SHA, execution epoch).

    ``working_directory`` is repository-relative; it is resolved and
    confined to the authorized candidate worktree by the spawn gate.
    """

    execution_id: str
    run_id: str
    task_id: str
    node_id: str
    cycle_id: str
    workspace_id: str
    candidate_id: str
    repository_id: str
    base_sha: str
    execution_epoch: int
    execution_host_id: str
    agent_capability: str
    command_argv: tuple[str, ...]
    working_directory: str
    timeout_seconds: float
    max_stdout_bytes: int
    max_stderr_bytes: int
    authority_digest: str
    created_at: str = ""

    def __post_init__(self) -> None:
        for label in (
            "execution_id",
            "run_id",
            "task_id",
            "node_id",
            "cycle_id",
            "workspace_id",
            "candidate_id",
            "repository_id",
            "execution_host_id",
            "agent_capability",
        ):
            _require_identifier(getattr(self, label), f"AgentExecutionRequest.{label}")
        _require_sha(self.base_sha, "AgentExecutionRequest.base_sha")
        if not isinstance(self.execution_epoch, int) or isinstance(self.execution_epoch, bool):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                "AgentExecutionRequest.execution_epoch must be int",
            )
        if self.execution_epoch < 1:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                f"AgentExecutionRequest.execution_epoch must be >= 1, got {self.execution_epoch}",
            )
        if not isinstance(self.command_argv, tuple):
            object.__setattr__(self, "command_argv", tuple(self.command_argv))
        if not self.command_argv or not all(isinstance(a, str) for a in self.command_argv):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_COMMAND_NOT_AUTHORIZED.value,
                "AgentExecutionRequest.command_argv must be a non-empty tuple of strings",
            )
        wd = self.working_directory
        if not isinstance(wd, str) or not wd.strip():
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value,
                "AgentExecutionRequest.working_directory must be a non-empty repository-relative path",
            )
        wd_posix = wd.replace("\\", "/")
        if wd.startswith("/") or wd.startswith("\\") or ":" in wd:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value,
                f"working_directory must be repository-relative, got {wd!r}",
            )
        if any(part == ".." for part in wd_posix.split("/")):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value,
                f"working_directory must not traverse upwards: {wd!r}",
            )
        if not isinstance(self.timeout_seconds, (int, float)) or isinstance(self.timeout_seconds, bool):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                "timeout_seconds must be a number",
            )
        if self.timeout_seconds <= 0:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                "timeout_seconds must be positive",
            )
        for label in ("max_stdout_bytes", "max_stderr_bytes"):
            value = getattr(self, label)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise AgentRuntimeError(
                    RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                    f"{label} must be a positive int, got {value!r}",
                )
        if not isinstance(self.authority_digest, str) or not _SHA256_RE.fullmatch(self.authority_digest):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                "authority_digest must be a lowercase 64-hex SHA-256 digest",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "cycle_id": self.cycle_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "repository_id": self.repository_id,
            "base_sha": self.base_sha.lower(),
            "execution_epoch": self.execution_epoch,
            "execution_host_id": self.execution_host_id,
            "agent_capability": self.agent_capability,
            "command_argv": list(self.command_argv),
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "authority_digest": self.authority_digest,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class AgentProcessIdentity:
    """Process identity, deliberately separate from run identity.

    ``pid`` is host-reported best-effort telemetry only. Durable
    identity is ``process_id``; PID reuse can never admit a stale event
    because all runtime event/result fencing binds ``process_id`` in
    addition to run/workspace/candidate/host/epoch.
    """

    process_id: str
    run_id: str
    workspace_id: str
    candidate_id: str
    execution_host_id: str
    execution_epoch: int
    pid: int | None = None
    started_at: str = ""

    def __post_init__(self) -> None:
        for label in ("process_id", "run_id", "workspace_id", "candidate_id", "execution_host_id"):
            _require_identifier(getattr(self, label), f"AgentProcessIdentity.{label}")
        if not isinstance(self.execution_epoch, int) or isinstance(self.execution_epoch, bool) or self.execution_epoch < 1:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                f"AgentProcessIdentity.execution_epoch invalid: {self.execution_epoch!r}",
            )
        if self.pid is not None and (not isinstance(self.pid, int) or isinstance(self.pid, bool) or self.pid < 1):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                f"AgentProcessIdentity.pid must be a positive int or None, got {self.pid!r}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "process_id": self.process_id,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "execution_host_id": self.execution_host_id,
            "execution_epoch": self.execution_epoch,
            "pid": self.pid,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentProcessIdentity":
        pid = data.get("pid")
        return cls(
            process_id=str(data["process_id"]),
            run_id=str(data["run_id"]),
            workspace_id=str(data["workspace_id"]),
            candidate_id=str(data["candidate_id"]),
            execution_host_id=str(data["execution_host_id"]),
            execution_epoch=int(data["execution_epoch"]),
            pid=int(pid) if pid is not None else None,
            started_at=str(data.get("started_at", "")),
        )


@dataclass(frozen=True, slots=True)
class AgentExecutionEvidence:
    """Immutable execution evidence emitted by the runtime.

    Evidence is consumed by the control plane through the existing
    validated event path; it carries no mutation authority. Terminal
    success is claimed only when ``exit_proven`` is true (state EXITED
    with a concrete exit code); a timeout is never proof of exit and a
    cancellation acknowledgement is never terminal evidence.
    """

    execution_id: str
    run_id: str
    task_id: str
    node_id: str
    cycle_id: str
    workspace_id: str
    candidate_id: str
    repository_id: str
    base_sha: str
    execution_epoch: int
    execution_host_id: str
    agent_capability: str
    working_directory: str
    process: AgentProcessIdentity | None
    state: str
    exit_code: int | None
    exit_proven: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_bytes: int
    stderr_bytes: int
    started_at: str
    completed_at: str
    timed_out: bool
    cancelled: bool
    cancel_terminal: bool
    blockers: tuple[str, ...] = ()
    error_message: str | None = None

    def __post_init__(self) -> None:
        for label in (
            "execution_id",
            "run_id",
            "task_id",
            "node_id",
            "cycle_id",
            "workspace_id",
            "candidate_id",
            "repository_id",
            "execution_host_id",
            "agent_capability",
        ):
            _require_identifier(getattr(self, label), f"AgentExecutionEvidence.{label}")
        _require_sha(self.base_sha, "AgentExecutionEvidence.base_sha")
        if not isinstance(self.state, str) or not self.state:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value,
                "AgentExecutionEvidence.state must be a non-empty ExecutionState value",
            )
        if not isinstance(self.blockers, tuple):
            object.__setattr__(self, "blockers", tuple(self.blockers))
        if self.exit_proven and not isinstance(self.exit_code, int):
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value,
                "exit_proven=True requires a concrete exit code",
            )
        if self.cancel_terminal and not self.exit_proven:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value,
                "cancel_terminal=True requires proven process termination",
            )

    @property
    def success(self) -> bool:
        """True only for a proven clean exit with no blockers."""
        return self.exit_proven and self.exit_code == 0 and not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "cycle_id": self.cycle_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "repository_id": self.repository_id,
            "base_sha": self.base_sha.lower(),
            "execution_epoch": self.execution_epoch,
            "execution_host_id": self.execution_host_id,
            "agent_capability": self.agent_capability,
            "working_directory": self.working_directory,
            "process": self.process.to_dict() if self.process is not None else None,
            "state": self.state,
            "exit_code": self.exit_code,
            "exit_proven": self.exit_proven,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "cancel_terminal": self.cancel_terminal,
            "blockers": list(self.blockers),
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentExecutionEvidence":
        process_data = data.get("process")
        process = AgentProcessIdentity.from_dict(process_data) if isinstance(process_data, Mapping) else None
        return cls(
            execution_id=str(data["execution_id"]),
            run_id=str(data["run_id"]),
            task_id=str(data["task_id"]),
            node_id=str(data["node_id"]),
            cycle_id=str(data["cycle_id"]),
            workspace_id=str(data["workspace_id"]),
            candidate_id=str(data["candidate_id"]),
            repository_id=str(data["repository_id"]),
            base_sha=str(data["base_sha"]),
            execution_epoch=int(data["execution_epoch"]),
            execution_host_id=str(data["execution_host_id"]),
            agent_capability=str(data["agent_capability"]),
            working_directory=str(data["working_directory"]),
            process=process,
            state=str(data["state"]),
            exit_code=data.get("exit_code"),
            exit_proven=bool(data.get("exit_proven", False)),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            stdout_truncated=bool(data.get("stdout_truncated", False)),
            stderr_truncated=bool(data.get("stderr_truncated", False)),
            stdout_bytes=int(data.get("stdout_bytes", 0)),
            stderr_bytes=int(data.get("stderr_bytes", 0)),
            started_at=str(data.get("started_at", "")),
            completed_at=str(data.get("completed_at", "")),
            timed_out=bool(data.get("timed_out", False)),
            cancelled=bool(data.get("cancelled", False)),
            cancel_terminal=bool(data.get("cancel_terminal", False)),
            blockers=tuple(data.get("blockers", ())),
            error_message=data.get("error_message"),
        )
