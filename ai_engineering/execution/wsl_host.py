"""WSL execution host implementation executing commands across the WSL boundary."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
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
        self._lock = threading.Lock()
        self._process_launcher = process_launcher or subprocess.Popen

    def identity(self) -> ExecutionHostIdentity:
        return self._identity

    def probe(self) -> HostStatus:
        if not self._identity.available or not self.config.distro_name:
            return HostStatus.UNAVAILABLE
        return HostStatus.AVAILABLE

    def build_wsl_command(self, argv: tuple[str, ...], cwd: str | None = None) -> list[str]:
        """Construct deterministic argv for wsl.exe without shell interpolation."""
        cmd = [self.config.wsl_binary, "-d", self.config.distro_name]
        if cwd:
            cmd.extend(["--cd", cwd])
        cmd.append("--exec")
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
        with self._lock:
            self._cancel_requested.add(execution_id)
            proc = self._active_processes.get(execution_id)
            if proc is not None and proc.poll() is None:
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

        wsl_cmd = self.build_wsl_command(request.argv, cwd=request.cwd)

        # Deny-by-default child environment when the request explicitly
        # opts out of controller environment inheritance (PR-13).
        exec_env = dict(os.environ) if request.inherit_environment else {}
        if request.env:
            exec_env.update(request.env)

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

        timed_out = False
        was_cancelled = False
        stdout_bytes = b""
        stderr_bytes = b""

        with self._lock:
            self._active_processes[request.execution_id] = proc

        try:
            stdout_bytes, stderr_bytes = proc.communicate(timeout=request.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5.0)
            except Exception:
                pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            with self._lock:
                self._active_processes.pop(request.execution_id, None)
                was_cancelled = request.execution_id in self._cancel_requested

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
            return ExecutionResult(
                execution_id=request.execution_id,
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                execution_host_id=self._identity.execution_host_id,
                state=ExecutionState.TIMED_OUT,
                exit_code=None,
                stdout=stdout_str,
                stderr=stderr_str,
                started_at=started_at,
                completed_at=completed_at,
                timed_out=True,
                cancelled=was_cancelled,
                stdout_truncated=stdout_trunc,
                stderr_truncated=stderr_trunc,
                blockers=(HostBlockingReason.EXECUTION_TIMEOUT.value,),
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
