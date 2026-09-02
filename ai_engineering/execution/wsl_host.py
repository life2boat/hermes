"""WSL execution host implementation executing commands across the WSL boundary.

PR-13.1 hardening:

- Cancellation/reaping semantics mirror :class:`LocalExecutionHost`:
  atomic spawn/cancel handshake (no lost-cancel window), canonical
  lifecycle states, terminate -> bounded wait -> kill -> bounded reap,
  and terminal evidence only when exit is proven (UNVERIFIABLE
  otherwise).
- Transport/child environment separation: the environment used to
  launch ``wsl.exe`` (transport) is derived from the controller
  environment allowlist and is never replaced by the sanitized agent
  child environment. The agent child environment (``request.env``) is
  injected into the Linux side through ``env -i`` so the WSL child
  receives a deny-by-default environment.
- Process-tree containment: terminating ``wsl.exe`` terminates the
  Windows-side transport process. Linux-side descendant containment is
  NOT guaranteed (PLATFORM_DEPENDENT / NOT_GUARANTEED).
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import subprocess
import threading
from typing import Callable, Mapping

from ai_engineering.execution.host_contracts import (
    ExecutionHostError,
    ExecutionHostIdentity,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    HostBlockingReason,
    HostCapability,
    HostPlatform,
    HostStatus,
    WslExecutionConfig,
)
from ai_engineering.execution.execution_host import ExecutionHost
from ai_engineering.execution.local_host import detect_current_platform
from ai_engineering.execution.run_contracts import AgentRunIdentity
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity

_GRACEFUL_TERMINATION_WAIT_SECONDS = 5.0
_FORCED_KILL_WAIT_SECONDS = 5.0
_POST_TERMINATION_CAPTURE_SECONDS = 10.0

_TERMINAL_LIFECYCLE_STATES = frozenset(
    {
        ExecutionState.EXITED.value,
        ExecutionState.FAILED.value,
        ExecutionState.TIMED_OUT.value,
        ExecutionState.UNVERIFIABLE.value,
    }
)

# Controller-side allowlist for launching the WSL transport binary.
# This is the TRANSPORT environment, not the agent child environment.
_WSL_TRANSPORT_ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "WINDIR",
    "WSLENV",
    "WSL_DISTRO_NAME",
    "WSL_INTEROP",
)

# Default Linux PATH injected into the WSL agent child when the
# sanitized child environment does not carry one.
_DEFAULT_WSL_CHILD_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def build_wsl_transport_environment() -> dict[str, str]:
    """Build the controller-side environment used to launch wsl.exe.

    Derived from the real controller environment via an explicit
    allowlist so the transport binary can always be located and started,
    independently of the sanitized agent child environment.
    """
    return {
        name: value
        for name in _WSL_TRANSPORT_ENV_ALLOWLIST
        if (value := os.environ.get(name)) is not None
    }


class WslExecutionHost(ExecutionHost):
    """ExecutionHost executing processes within a configured Windows Subsystem for Linux distro."""

    def __init__(
        self,
        execution_host_id: str = "host-wsl",
        config: WslExecutionConfig | None = None,
        distro_name: str = "Ubuntu",
        hostname: str | None = None,
        architecture: str | None = None,
        available: bool = True,
        capabilities: tuple[HostCapability, ...] | None = None,
        created_at: str | None = None,
        process_launcher: Callable[..., subprocess.Popen] | None = None,
    ) -> None:
        self.config = config or WslExecutionConfig(distro_name=distro_name)
        c_plat = detect_current_platform()
        h_plat = HostPlatform.LINUX
        caps = capabilities or (
            HostCapability.CAN_RUN_COMMANDS,
            HostCapability.CAN_ACCESS_REPOSITORY,
            HostCapability.CAN_CREATE_WORKTREE,
            HostCapability.CAN_SIGNAL_PROCESS,
            HostCapability.CAN_CAPTURE_STDOUT,
            HostCapability.CAN_CAPTURE_STDERR,
        )
        self._identity = ExecutionHostIdentity(
            execution_host_id=execution_host_id,
            mode=ExecutionMode.WSL,
            controller_platform=c_plat,
            host_platform=h_plat,
            hostname=hostname or f"wsl-{self.config.distro_name}",
            architecture=architecture or "x86_64",
            available=available,
            capabilities=caps,
            created_at=created_at or "2026-09-01T00:00:00Z",
        )
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._cancel_requested: set[str] = set()
        self._cancel_termination_initiated: set[str] = set()
        self._lifecycle: dict[str, str] = {}
        self._lock = threading.Lock()
        self._process_launcher = process_launcher or subprocess.Popen

    def identity(self) -> ExecutionHostIdentity:
        return self._identity

    def lifecycle_state(self, execution_id: str) -> str | None:
        """Read-only view of the per-execution lifecycle state."""
        with self._lock:
            return self._lifecycle.get(execution_id)

    def _terminate_and_reap(self, proc: subprocess.Popen) -> bool:
        """Terminate (direct child only), escalate to kill, and reap.

        Returns True iff exit is proven. Linux-side descendants of the
        WSL child are NOT covered: process-tree containment is
        NOT_GUARANTEED for WSL transport execution.
        """
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=_GRACEFUL_TERMINATION_WAIT_SECONDS)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=_FORCED_KILL_WAIT_SECONDS)
            except Exception:
                pass
        return proc.poll() is not None

    def probe(self) -> HostStatus:
        if not self._identity.available or not self.config.distro_name:
            return HostStatus.UNAVAILABLE
        return HostStatus.AVAILABLE

    def build_wsl_command(
        self,
        argv: tuple[str, ...],
        cwd: str | None = None,
        child_env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Construct deterministic argv for wsl.exe without shell interpolation.

        When ``child_env`` is supplied the Linux child is launched through
        ``env -i`` so the agent child receives exactly the sanitized
        environment (deny-by-default) and nothing else. The transport
        environment of wsl.exe itself is unaffected.
        """
        cmd = [self.config.wsl_binary, "-d", self.config.distro_name]
        if cwd:
            cmd.extend(["--cd", cwd])
        cmd.append("--exec")
        if child_env is not None:
            # ``env -i VAR=VALUE ... argv`` (GNU and BusyBox compatible;
            # BusyBox env rejects the ``--`` separator).
            cmd.append("env")
            cmd.append("-i")
            cmd.extend(f"{key}={value}" for key, value in child_env.items())
        cmd.extend(argv)
        return cmd

    def validate_request(
        self,
        request: ExecutionRequest,
        workspace_identity: WorkspaceIdentity | None = None,
        run_identity: AgentRunIdentity | None = None,
    ) -> tuple[bool, tuple[str, ...]]:
        blockers: list[str] = []

        if not self._identity.available or not self.config.distro_name:
            blockers.append(HostBlockingReason.EXECUTION_HOST_UNAVAILABLE.value)

        if request.mode != ExecutionMode.WSL:
            blockers.append(HostBlockingReason.EXECUTION_MODE_INVALID.value)

        if request.execution_host_id != self._identity.execution_host_id:
            blockers.append(HostBlockingReason.EXECUTION_HOST_MISMATCH.value)

        if workspace_identity is not None:
            if workspace_identity.execution_host_id != self._identity.execution_host_id:
                blockers.append(HostBlockingReason.EXECUTION_HOST_MISMATCH.value)
            if workspace_identity.workspace_id != request.workspace_id:
                blockers.append(HostBlockingReason.RUN_WORKSPACE_MISMATCH.value)

        if run_identity is not None:
            if run_identity.execution_host_id != self._identity.execution_host_id:
                blockers.append(HostBlockingReason.EXECUTION_HOST_MISMATCH.value)
            if run_identity.workspace_id != request.workspace_id:
                blockers.append(HostBlockingReason.RUN_WORKSPACE_MISMATCH.value)
            if run_identity.run_id != request.run_id:
                blockers.append(HostBlockingReason.EXECUTION_REQUEST_INVALID.value)

        # Check path escapes
        cwd_str = request.cwd
        if ".." in cwd_str.replace("\\", "/").split("/"):
            blockers.append(HostBlockingReason.EXECUTION_PATH_INVALID.value)

        return (len(blockers) == 0, tuple(dict.fromkeys(blockers)))

    def request_cancel(self, execution_id: str) -> bool:
        """Record a cancellation request and signal a live process if terminable.

        Pending cancellations (process not yet registered) are consumed
        atomically at spawn registration — no lost-cancel window.
        Acknowledgement is not terminal evidence. Only the Windows-side
        transport process can be terminated; Linux-side descendant
        containment is NOT guaranteed.
        """
        with self._lock:
            self._cancel_requested.add(execution_id)
            current = self._lifecycle.get(execution_id)
            if current is not None and current not in _TERMINAL_LIFECYCLE_STATES:
                self._lifecycle[execution_id] = ExecutionState.CANCEL_REQUESTED.value
            proc = self._active_processes.get(execution_id)
            if proc is not None and proc.poll() is None:
                self._cancel_termination_initiated.add(execution_id)
                self._lifecycle[execution_id] = ExecutionState.TERMINATING.value
                try:
                    proc.terminate()
                    return True
                except Exception:
                    return False
            return True

    def execute(
        self,
        request: ExecutionRequest,
        workspace_identity: WorkspaceIdentity | None = None,
        run_identity: AgentRunIdentity | None = None,
    ) -> ExecutionResult:
        started_at = datetime.now(timezone.utc).isoformat()
        is_valid, blockers = self.validate_request(request, workspace_identity, run_identity)
        if not is_valid:
            return ExecutionResult(
                execution_id=request.execution_id,
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                execution_host_id=self._identity.execution_host_id,
                state=ExecutionState.FAILED,
                exit_code=None,
                stdout="",
                stderr="",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                timed_out=False,
                cancelled=False,
                blockers=blockers,
                error_message=f"Request validation failed: {', '.join(blockers)}",
            )

        # Transport vs agent-child environment separation (PR-13.1):
        # - legacy inherit_environment=True: full controller environment
        #   (unchanged PR-9 behavior);
        # - inherit_environment=False (controlled runtime): the launcher
        #   process gets the controller-side transport allowlist so
        #   wsl.exe can always be located, while the sanitized agent
        #   child environment is injected into the Linux side via env -i.
        child_env: dict[str, str] | None = None
        if request.inherit_environment:
            exec_env = dict(os.environ)
            if request.env:
                exec_env.update(request.env)
        else:
            exec_env = build_wsl_transport_environment()
            child_env = dict(request.env or {})
            child_env.setdefault("PATH", _DEFAULT_WSL_CHILD_PATH)
        wsl_cmd = self.build_wsl_command(request.argv, cwd=request.cwd, child_env=child_env)

        try:
            proc = self._process_launcher(
                wsl_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=exec_env,
            )
        except Exception as exc:
            with self._lock:
                self._lifecycle[request.execution_id] = ExecutionState.FAILED.value
            return ExecutionResult(
                execution_id=request.execution_id,
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                execution_host_id=self._identity.execution_host_id,
                state=ExecutionState.FAILED,
                exit_code=None,
                stdout="",
                stderr="",
                started_at=started_at,
                completed_at=datetime.now(timezone.utc).isoformat(),
                timed_out=False,
                cancelled=False,
                blockers=(HostBlockingReason.EXECUTION_REQUEST_INVALID.value,),
                error_message=f"WSL process launch failed: {exc}",
            )

        # Atomic spawn/cancel handshake: register and consume any pending
        # cancellation under one lock acquisition (no lost-cancel window).
        pending_cancel = False
        with self._lock:
            self._active_processes[request.execution_id] = proc
            if request.execution_id in self._cancel_requested:
                if proc.poll() is None:
                    self._cancel_termination_initiated.add(request.execution_id)
                    self._lifecycle[request.execution_id] = ExecutionState.TERMINATING.value
                    pending_cancel = True
            else:
                self._lifecycle[request.execution_id] = ExecutionState.LIVE.value
        if pending_cancel:
            try:
                proc.terminate()
            except Exception:
                pass

        timed_out = False
        exit_proven = False
        stdout_bytes: bytes = b""
        stderr_bytes: bytes = b""

        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=request.timeout_seconds)
            exit_proven = True
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_proven = self._terminate_and_reap(proc)
            try:
                stdout_bytes, stderr_bytes = proc.communicate(
                    timeout=_POST_TERMINATION_CAPTURE_SECONDS
                )
            except Exception:
                stdout_bytes, stderr_bytes = b"", b""
        except Exception:
            exit_proven = self._terminate_and_reap(proc)
            try:
                stdout_bytes, stderr_bytes = proc.communicate(
                    timeout=_POST_TERMINATION_CAPTURE_SECONDS
                )
            except Exception:
                stdout_bytes, stderr_bytes = b"", b""
        finally:
            with self._lock:
                self._active_processes.pop(request.execution_id, None)
                if timed_out and exit_proven:
                    self._lifecycle[request.execution_id] = ExecutionState.TIMED_OUT.value
                elif exit_proven and request.execution_id in self._cancel_termination_initiated:
                    self._lifecycle[request.execution_id] = ExecutionState.CANCEL_REQUESTED.value
                elif exit_proven:
                    self._lifecycle[request.execution_id] = ExecutionState.EXITED.value
                elif timed_out:
                    self._lifecycle[request.execution_id] = ExecutionState.UNVERIFIABLE.value
                else:
                    self._lifecycle[request.execution_id] = ExecutionState.FAILED.value

        # Cancellation outcome only when termination was cancel-initiated
        # and proven before any independent timeout.
        was_cancelled = (
            request.execution_id in self._cancel_termination_initiated
            and exit_proven
            and not timed_out
        )

        if stdout_bytes is None:
            stdout_bytes = b""
        if stderr_bytes is None:
            stderr_bytes = b""

        completed_at = datetime.now(timezone.utc).isoformat()

        stdout_trunc = False
        if len(stdout_bytes) > request.max_stdout_bytes:
            stdout_bytes = stdout_bytes[: request.max_stdout_bytes]
            stdout_trunc = True

        stderr_trunc = False
        if len(stderr_bytes) > request.max_stderr_bytes:
            stderr_bytes = stderr_bytes[: request.max_stderr_bytes]
            stderr_trunc = True

        stdout_str = stdout_bytes.decode("utf-8", errors="replace")
        stderr_str = stderr_bytes.decode("utf-8", errors="replace")

        if timed_out:
            # TIMEOUT is never VERIFIED_EXIT.
            state = ExecutionState.TIMED_OUT if exit_proven else ExecutionState.UNVERIFIABLE
            blockers: tuple[str, ...] = (HostBlockingReason.EXECUTION_TIMEOUT.value,)
            if not exit_proven:
                blockers = blockers + ("RUNTIME_PROCESS_UNVERIFIABLE",)
            return ExecutionResult(
                execution_id=request.execution_id,
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                execution_host_id=self._identity.execution_host_id,
                state=state,
                exit_code=None,
                stdout=stdout_str,
                stderr=stderr_str,
                started_at=started_at,
                completed_at=completed_at,
                timed_out=True,
                cancelled=was_cancelled,
                stdout_truncated=stdout_trunc,
                stderr_truncated=stderr_trunc,
                blockers=blockers,
                error_message=f"WSL process timed out after {request.timeout_seconds} seconds",
            )

        return ExecutionResult(
            execution_id=request.execution_id,
            run_id=request.run_id,
            workspace_id=request.workspace_id,
            execution_host_id=self._identity.execution_host_id,
            state=ExecutionState.EXITED,
            exit_code=proc.returncode,
            stdout=stdout_str,
            stderr=stderr_str,
            started_at=started_at,
            completed_at=completed_at,
            timed_out=False,
            cancelled=was_cancelled,
            stdout_truncated=stdout_trunc,
            stderr_truncated=stderr_trunc,
            blockers=(),
            error_message=None,
        )
