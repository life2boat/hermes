"""Strongly typed contracts for ExecutionHost, ExecutionMode, ExecutionRequest, and ExecutionResult."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from ai_engineering.execution.run_contracts import AgentRunIdentity, RunBlockingReason
from ai_engineering.workspaces.snapshot_contracts import validate_repository_relative_path
from ai_engineering.workspaces.workspace_contracts import ExecutionMode, WorkspaceIdentity

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)

EXECUTION_HOST_CONTRACT_VERSION = "4.1.0"
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576  # 1 MB


class HostPlatform(StrEnum):
    """Host operating system platform."""

    LINUX = "LINUX"
    WINDOWS = "WINDOWS"
    DARWIN = "DARWIN"
    UNKNOWN = "UNKNOWN"


class HostCapability(StrEnum):
    """Explicit bounded capabilities advertised by an ExecutionHost."""

    CAN_RUN_COMMANDS = "CAN_RUN_COMMANDS"
    CAN_ACCESS_REPOSITORY = "CAN_ACCESS_REPOSITORY"
    CAN_CREATE_WORKTREE = "CAN_CREATE_WORKTREE"
    CAN_SIGNAL_PROCESS = "CAN_SIGNAL_PROCESS"
    CAN_CAPTURE_STDOUT = "CAN_CAPTURE_STDOUT"
    CAN_CAPTURE_STDERR = "CAN_CAPTURE_STDERR"


class ExecutionState(StrEnum):
    """Lifecycle states of a command execution on an ExecutionHost."""

    CREATED = "CREATED"
    STARTING = "STARTING"
    LIVE = "LIVE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    EXITED = "EXITED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"


class HostStatus(StrEnum):
    """Health / probe status of an ExecutionHost."""

    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class HostBlockingReason(StrEnum):
    """Deterministic machine-readable reason codes for host and execution blockers."""

    EXECUTION_HOST_MISMATCH = "EXECUTION_HOST_MISMATCH"
    EXECUTION_HOST_UNAVAILABLE = "EXECUTION_HOST_UNAVAILABLE"
    EXECUTION_MODE_INVALID = "EXECUTION_MODE_INVALID"
    EXECUTION_REQUEST_INVALID = "EXECUTION_REQUEST_INVALID"
    EXECUTION_PATH_INVALID = "EXECUTION_PATH_INVALID"
    EXECUTION_COMMAND_INVALID = "EXECUTION_COMMAND_INVALID"
    EXECUTION_ID_COLLISION = "EXECUTION_ID_COLLISION"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    EXECUTION_CANCEL_FAILED = "EXECUTION_CANCEL_FAILED"
    # Reused blockers
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"
    WORKTREE_IDENTITY_MISMATCH = "WORKTREE_IDENTITY_MISMATCH"


class ExecutionHostError(Exception):
    """Fail-closed error for execution host contract and invariant violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ExecutionHostIdentity:
    """Immutable, strongly typed identity for an ExecutionHost."""

    execution_host_id: str
    mode: ExecutionMode
    controller_platform: HostPlatform
    host_platform: HostPlatform
    hostname: str
    architecture: str
    available: bool
    capabilities: tuple[HostCapability, ...]
    created_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution_host_id, str) or not _IDENTIFIER_RE.match(self.execution_host_id):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                f"Invalid execution_host_id: {self.execution_host_id!r}",
            )
        if not isinstance(self.mode, ExecutionMode):
            try:
                object.__setattr__(self, "mode", ExecutionMode(str(self.mode)))
            except ValueError as exc:
                raise ExecutionHostError(
                    HostBlockingReason.EXECUTION_MODE_INVALID.value,
                    f"Unsupported execution mode: {self.mode!r}",
                ) from exc
        if self.mode not in (ExecutionMode.LOCAL, ExecutionMode.WSL):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_MODE_INVALID.value,
                f"Execution mode {self.mode!r} not supported in PR-9 (LOCAL and WSL only)",
            )
        if not isinstance(self.capabilities, tuple):
            object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_host_id": self.execution_host_id,
            "mode": self.mode.value,
            "controller_platform": self.controller_platform.value if isinstance(self.controller_platform, HostPlatform) else str(self.controller_platform),
            "host_platform": self.host_platform.value if isinstance(self.host_platform, HostPlatform) else str(self.host_platform),
            "hostname": self.hostname,
            "architecture": self.architecture,
            "available": self.available,
            "capabilities": [c.value if isinstance(c, HostCapability) else str(c) for c in self.capabilities],
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionHostIdentity:
        mode = ExecutionMode(data["mode"]) if isinstance(data["mode"], str) else data["mode"]
        c_plat = HostPlatform(data["controller_platform"]) if isinstance(data["controller_platform"], str) else data["controller_platform"]
        h_plat = HostPlatform(data["host_platform"]) if isinstance(data["host_platform"], str) else data["host_platform"]
        caps = tuple(HostCapability(c) if isinstance(c, str) else c for c in data.get("capabilities", ()))
        return cls(
            execution_host_id=str(data["execution_host_id"]),
            mode=mode,
            controller_platform=c_plat,
            host_platform=h_plat,
            hostname=str(data.get("hostname", "")),
            architecture=str(data.get("architecture", "")),
            available=bool(data.get("available", True)),
            capabilities=caps,
            created_at=str(data.get("created_at", "")),
        )


@dataclass(frozen=True, slots=True)
class WslExecutionConfig:
    """Explicit configuration for WSL execution environment."""

    distro_name: str
    wsl_binary: str = "wsl.exe"
    working_directory_policy: str = "REPOSITORY_RELATIVE"

    def __post_init__(self) -> None:
        if not isinstance(self.distro_name, str) or not self.distro_name.strip():
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "WSL distro_name must be a non-empty string",
            )


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    """Immutable typed request for executing a command on an ExecutionHost."""

    execution_id: str
    run_id: str
    task_id: str
    workspace_id: str
    execution_host_id: str
    mode: ExecutionMode
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    stdin_mode: str = "DEVNULL"
    max_stdout_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    max_stderr_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    created_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution_id, str) or not _IDENTIFIER_RE.match(self.execution_id):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                f"Invalid execution_id: {self.execution_id!r}",
            )
        if not isinstance(self.run_id, str) or not _IDENTIFIER_RE.match(self.run_id):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                f"Invalid run_id: {self.run_id!r}",
            )
        if not isinstance(self.workspace_id, str) or not _IDENTIFIER_RE.match(self.workspace_id):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                f"Invalid workspace_id: {self.workspace_id!r}",
            )
        if not isinstance(self.execution_host_id, str) or not _IDENTIFIER_RE.match(self.execution_host_id):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                f"Invalid execution_host_id: {self.execution_host_id!r}",
            )
        if not isinstance(self.mode, ExecutionMode):
            try:
                object.__setattr__(self, "mode", ExecutionMode(str(self.mode)))
            except ValueError as exc:
                raise ExecutionHostError(
                    HostBlockingReason.EXECUTION_MODE_INVALID.value,
                    f"Invalid execution mode: {self.mode!r}",
                ) from exc

        if self.mode not in (ExecutionMode.LOCAL, ExecutionMode.WSL):
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_MODE_INVALID.value,
                f"Execution mode {self.mode!r} not supported in PR-9 (LOCAL and WSL only)",
            )

        if not isinstance(self.argv, tuple):
            object.__setattr__(self, "argv", tuple(self.argv))

        if not self.argv:
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_COMMAND_INVALID.value,
                "ExecutionRequest argv must not be empty",
            )

        for arg in self.argv:
            if not isinstance(arg, str):
                raise ExecutionHostError(
                    HostBlockingReason.EXECUTION_COMMAND_INVALID.value,
                    f"All argv items must be strings, got {type(arg)}",
                )

        if not isinstance(self.cwd, str) or not self.cwd.strip():
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_PATH_INVALID.value,
                "ExecutionRequest cwd must be non-empty",
            )

        if self.timeout_seconds <= 0:
            raise ExecutionHostError(
                HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                "timeout_seconds must be positive",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "execution_host_id": self.execution_host_id,
            "mode": self.mode.value,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "stdin_mode": self.stdin_mode,
            "max_stdout_bytes": self.max_stdout_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Strongly typed result of a command execution on an ExecutionHost."""

    execution_id: str
    run_id: str
    workspace_id: str
    execution_host_id: str
    state: ExecutionState
    exit_code: int | None
    stdout: str
    stderr: str
    started_at: str
    completed_at: str
    timed_out: bool
    cancelled: bool
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    blockers: tuple[str, ...] = ()
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ExecutionState):
            try:
                object.__setattr__(self, "state", ExecutionState(str(self.state)))
            except ValueError as exc:
                raise ExecutionHostError(
                    HostBlockingReason.EXECUTION_REQUEST_INVALID.value,
                    f"Invalid execution state: {self.state!r}",
                ) from exc

        if not isinstance(self.blockers, tuple):
            object.__setattr__(self, "blockers", tuple(self.blockers))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_HOST_CONTRACT_VERSION,
            "execution_id": self.execution_id,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "execution_host_id": self.execution_host_id,
            "state": self.state.value,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "blockers": list(self.blockers),
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutionResult:
        st = ExecutionState(data["state"]) if isinstance(data["state"], str) else data["state"]
        return cls(
            execution_id=str(data["execution_id"]),
            run_id=str(data["run_id"]),
            workspace_id=str(data["workspace_id"]),
            execution_host_id=str(data["execution_host_id"]),
            state=st,
            exit_code=data.get("exit_code"),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            started_at=str(data.get("started_at", "")),
            completed_at=str(data.get("completed_at", "")),
            timed_out=bool(data.get("timed_out", False)),
            cancelled=bool(data.get("cancelled", False)),
            stdout_truncated=bool(data.get("stdout_truncated", False)),
            stderr_truncated=bool(data.get("stderr_truncated", False)),
            blockers=tuple(data.get("blockers", ())),
            error_message=data.get("error_message"),
        )

    @classmethod
    def from_json(cls, raw: str) -> ExecutionResult:
        return cls.from_dict(json.loads(raw))
