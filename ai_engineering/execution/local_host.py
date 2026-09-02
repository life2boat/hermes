"""Local execution host implementation executing commands directly on current OS.

Cancellation/reaping semantics (PR-13.1 hardening):

- Spawn registration and pending-cancellation consumption are atomic:
  a cancellation accepted for an execution_id is always either acted on
  immediately (registered, live process) or consumed at the moment the
  process becomes terminable. There is no lost-cancel window between
  ``Popen()`` and registration.
- Lifecycle per execution_id follows the canonical :class:`ExecutionState`
  vocabulary: STARTING -> LIVE -> (CANCEL_REQUESTED -> TERMINATING) ->
  EXITED / CANCELLED / TIMED_OUT / FAILED / UNVERIFIABLE.
- Timeout and cancellation follow: termination requested -> bounded
  graceful wait -> forced kill -> bounded reap. Terminal evidence is
  produced only when process exit is proven (``poll() is not None``);
  otherwise the result is UNVERIFIABLE, never a false terminal claim.
- On POSIX the child starts in its own session (``start_new_session``)
  and termination signals the whole process group. On Windows only
  direct-child termination is available (TerminateProcess): process-tree
  containment is PLATFORM_DEPENDENT, not guaranteed.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
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
)
from ai_engineering.execution.execution_host import ExecutionHost
from ai_engineering.execution.run_contracts import AgentRunIdentity
from ai_engineering.workspaces.snapshot_contracts import validate_repository_relative_path
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


def detect_current_platform() -> HostPlatform:
    sys_plat = sys.platform.lower()
    if sys_plat.startswith("linux"):
        return HostPlatform.LINUX
    if sys_plat.startswith("win"):
        return HostPlatform.WINDOWS
    if sys_plat.startswith("darwin"):
        return HostPlatform.DARWIN
    return HostPlatform.UNKNOWN


class LocalExecutionHost(ExecutionHost):
    """ExecutionHost executing processes directly on the local machine."""

    def __init__(
        self,
        execution_host_id: str = "host-local",
        hostname: str | None = None,
        architecture: str | None = None,
        available: bool = True,
        capabilities: tuple[HostCapability, ...] | None = None,
        created_at: str | None = None,
        process_launcher: Callable[..., subprocess.Popen] | None = None,
    ) -> None:
        plat = detect_current_platform()
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
            mode=ExecutionMode.LOCAL,
            controller_platform=plat,
            host_platform=plat,
            hostname=hostname or platform.node(),
            architecture=architecture or platform.machine(),
            available=available,
            capabilities=caps,
            created_at=created_at or "2026-09-01T00:00:00Z",
        )
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._cancel_requested: set[str] = set()
        # Cancellations for which termination was actually initiated
        # (pending cancel consumed at registration, or a live registered
        # process was signalled). Only these may mark a result cancelled.
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

    # ------------------------------------------------------------------
    # Termination helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _signal_process_tree(proc: subprocess.Popen, force: bool) -> None:
        """Signal a process, or its whole group on POSIX sessions.

        POSIX: the child runs in its own session, so the process group
        covers descendants it spawned. Windows: only the direct child can
        be terminated (TerminateProcess); group containment is not
        available and is documented as PLATFORM_DEPENDENT. ``force``
        selects kill (SIGKILL / TerminateProcess) over graceful
        termination (SIGTERM / TerminateProcess); on Windows both map to
        TerminateProcess.
        """
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL if force else signal.SIGTERM)
                return
            except Exception:
                pass
        if force:
            proc.kill()
        else:
            proc.terminate()

    def _terminate_and_reap(self, proc: subprocess.Popen) -> bool:
        """Terminate, escalate to kill, and reap. True iff exit is proven."""
        try:
            self._signal_process_tree(proc, force=False)
        except Exception:
            pass
        try:
            proc.wait(timeout=_GRACEFUL_TERMINATION_WAIT_SECONDS)
        except Exception:
            pass
        if proc.poll() is None:
            try:
                self._signal_process_tree(proc, force=True)
            except Exception:
                pass
            try:
                proc.wait(timeout=_FORCED_KILL_WAIT_SECONDS)
            except Exception:
                pass
        return proc.poll() is not None

    def probe(self) -> HostStatus:
        if not self._identity.available:
            return HostStatus.UNAVAILABLE
        return HostStatus.AVAILABLE

    def validate_request(
        self,
        request: ExecutionRequest,
        workspace_identity: WorkspaceIdentity | None = None,
        run_identity: AgentRunIdentity | None = None,
    ) -> tuple[bool, tuple[str, ...]]:
        blockers: list[str] = []

        if not self._identity.available:
            blockers.append(HostBlockingReason.EXECUTION_HOST_UNAVAILABLE.value)

        if request.mode != ExecutionMode.LOCAL:
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

        # Path validation: check cwd path safety
        cwd_str = request.cwd
        if workspace_identity is not None:
            ws_root = Path(workspace_identity.worktree_path).resolve()
            resolved_cwd = (ws_root / cwd_str).resolve() if not Path(cwd_str).is_absolute() else Path(cwd_str).resolve()
            try:
                resolved_cwd.relative_to(ws_root)
            except ValueError:
                blockers.append(HostBlockingReason.EXECUTION_PATH_INVALID.value)
        else:
            # Check for illegal escapes
            if ".." in cwd_str.replace("\\", "/").split("/"):
                blockers.append(HostBlockingReason.EXECUTION_PATH_INVALID.value)

        return (len(blockers) == 0, tuple(dict.fromkeys(blockers)))

    def request_cancel(self, execution_id: str) -> bool:
        """Record a cancellation request and signal a live process if terminable.

        The request is always remembered: if the process is not yet
        registered (spawn in flight), the pending cancellation is
        consumed atomically at spawn registration. Acknowledgement is
        not terminal evidence.
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
                    self._signal_process_tree(proc, signal.SIGTERM)
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

        # Resolve cwd
        if workspace_identity is not None:
            ws_root = Path(workspace_identity.worktree_path).resolve()
            target_cwd = str((ws_root / request.cwd).resolve() if not Path(request.cwd).is_absolute() else Path(request.cwd).resolve())
        else:
            target_cwd = str(Path(request.cwd).resolve())

        # Construct bounded env (never log secrets). Deny-by-default when
        # the request explicitly opts out of controller environment
        # inheritance (PR-13 controlled runtime).
        exec_env = dict(os.environ) if request.inherit_environment else {}
        if request.env:
            exec_env.update(request.env)

        popen_kwargs: dict = {
            "cwd": target_cwd,
            "env": exec_env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        if os.name == "posix":
            # Own session: enables process-group termination for the
            # whole descendant tree (PLATFORM_DEPENDENT containment).
            popen_kwargs["start_new_session"] = True
        try:
            proc = self._process_launcher(list(request.argv), **popen_kwargs)
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
                error_message=f"Subprocess launch failed: {exc}",
            )

        # Atomic spawn/cancel handshake: register the process and consume
        # any cancellation request that arrived between Popen() and this
        # registration under the same lock acquisition. There is no
        # lost-cancel window.
        pending_cancel = False
        with self._lock:
            self._active_processes[request.execution_id] = proc
            if request.execution_id in self._cancel_requested:
                if proc.poll() is None:
                    self._cancel_termination_initiated.add(request.execution_id)
                    self._lifecycle[request.execution_id] = ExecutionState.TERMINATING.value
                    pending_cancel = True
                # If the process already exited on its own, the cancel is
                # recorded but no cancellation outcome is claimed.
            else:
                self._lifecycle[request.execution_id] = ExecutionState.LIVE.value
        if pending_cancel:
            try:
                self._signal_process_tree(proc, force=False)
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

        # A cancellation outcome is claimed only when termination was
        # initiated by the cancel path AND the process exit was proven
        # before any independent timeout. A run that also hit its timeout
        # is reported TIMED_OUT (the cancel did not prove termination).
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

        # Truncation logic
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
            # TIMEOUT is never VERIFIED_EXIT: exit_code stays None even
            # when post-kill reaping proved the process ended.
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
                error_message=f"Process timed out after {request.timeout_seconds} seconds",
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
