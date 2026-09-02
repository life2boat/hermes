"""Bounded real process execution adapter over canonical ExecutionHosts (PR-13).

The runner converts an authorized :class:`AgentExecutionRequest` into a
canonical :class:`ExecutionRequest`, executes it on the exact bound
LOCAL or WSL host (never a fallback host), and maps the result into
immutable :class:`AgentExecutionEvidence`. It records process identity
separately from run identity and never synthesizes terminal evidence.

Environment handling is deny-by-default: the child process receives
only the explicit allowlisted environment built by
:func:`ai_engineering.runtime.runtime_policy.build_child_environment`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

from ai_engineering.execution.execution_host import ExecutionHost
from ai_engineering.execution.host_contracts import (
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
)
from ai_engineering.runtime.runtime_contracts import (
    AgentExecutionEvidence,
    AgentExecutionRequest,
    AgentProcessIdentity,
    AgentRuntimeError,
    RuntimeBlockingReason,
)
from ai_engineering.runtime.runtime_policy import build_child_environment

_TERMINAL_PROVEN_STATES = frozenset({ExecutionState.EXITED.value})


class AgentProcessRunner:
    """Executes one authorized runtime request on its exact bound host."""

    def __init__(
        self,
        *,
        local_host: ExecutionHost | None = None,
        wsl_host: ExecutionHost | None = None,
        parent_env: Mapping[str, str] | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self._local_host = local_host
        self._wsl_host = wsl_host
        self._parent_env = parent_env
        self._extra_env = dict(extra_env or {})

    def host_for(self, request: AgentExecutionRequest) -> ExecutionHost:
        """Return the exact host bound by the request; never a fallback."""
        if request.execution_host_id == (self._local_host.identity().execution_host_id if self._local_host else None):
            return self._local_host  # type: ignore[return-value]
        if request.execution_host_id == (self._wsl_host.identity().execution_host_id if self._wsl_host else None):
            return self._wsl_host  # type: ignore[return-value]
        raise AgentRuntimeError(
            "EXECUTION_HOST_MISMATCH",
            f"No registered host matches execution_host_id {request.execution_host_id!r}; "
            "host fallback is forbidden",
        )

    def build_execution_request(
        self,
        request: AgentExecutionRequest,
        *,
        workspace_root: Path,
        resolved_working_directory: Path,
    ) -> ExecutionRequest:
        """Construct the canonical ExecutionRequest with a sanitized environment.

        The returned ``env`` is the AGENT CHILD environment (deny-by-default).
        For WSL requests the child runs on Linux, so a POSIX allowlist is
        used and the controller PATH is never propagated (it is
        meaningless inside the WSL distro; the WSL host injects a Linux
        default PATH). The transport environment used to launch wsl.exe
        is derived separately inside the WSL host.
        """
        host = self.host_for(request)
        mode = host.identity().mode
        if mode == ExecutionMode.WSL:
            env = build_child_environment(
                self._parent_env,
                extra=self._extra_env,
                target_platform="posix",
            )
            env.pop("PATH", None)
        else:
            env = build_child_environment(
                self._parent_env,
                extra=self._extra_env,
                target_platform="windows" if _controller_is_windows() else "posix",
            )
        return ExecutionRequest(
            execution_id=request.execution_id,
            run_id=request.run_id,
            task_id=request.task_id,
            workspace_id=request.workspace_id,
            execution_host_id=request.execution_host_id,
            mode=mode,
            argv=request.command_argv,
            cwd=str(resolved_working_directory),
            env=env,
            inherit_environment=False,
            timeout_seconds=request.timeout_seconds,
            max_stdout_bytes=request.max_stdout_bytes,
            max_stderr_bytes=request.max_stderr_bytes,
            created_at=request.created_at,
        )

    def execute(
        self,
        request: AgentExecutionRequest,
        execution_request: ExecutionRequest,
        *,
        workspace_root: Path,
    ) -> tuple[AgentProcessIdentity, AgentExecutionEvidence]:
        """Execute and produce immutable evidence; never a false success."""
        host = self.host_for(request)
        started_at = execution_request.created_at
        process_identity = AgentProcessIdentity(
            process_id=f"proc-{request.execution_id}",
            run_id=request.run_id,
            workspace_id=request.workspace_id,
            candidate_id=request.candidate_id,
            execution_host_id=request.execution_host_id,
            execution_epoch=request.execution_epoch,
            pid=None,
            started_at=started_at,
        )

        result: ExecutionResult
        try:
            result = host.execute(execution_request)
        except Exception as exc:
            result = ExecutionResult(
                execution_id=request.execution_id,
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                execution_host_id=request.execution_host_id,
                state=ExecutionState.FAILED,
                exit_code=None,
                stdout="",
                stderr="",
                started_at=execution_request.created_at,
                completed_at="",
                timed_out=False,
                cancelled=False,
                blockers=(RuntimeBlockingReason.RUNTIME_OUTPUT_CAPTURE_FAILED.value,),
                error_message=f"Host execution raised: {exc}",
            )

        evidence = self._to_evidence(
            request,
            process_identity,
            result,
            workspace_root=workspace_root,
        )
        return process_identity, evidence

    def _to_evidence(
        self,
        request: AgentExecutionRequest,
        process_identity: AgentProcessIdentity,
        result: ExecutionResult,
        *,
        workspace_root: Path,
    ) -> AgentExecutionEvidence:
        blockers = list(result.blockers)
        state_value = result.state.value

        exit_proven = state_value == ExecutionState.EXITED.value and isinstance(result.exit_code, int)
        if state_value in (ExecutionState.DISCONNECTED.value, ExecutionState.UNVERIFIABLE.value, ExecutionState.CONNECTING.value):
            blockers.append(RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value)
        if state_value == ExecutionState.EXITED.value and not exit_proven:
            blockers.append(RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value)

        stdout_bytes = len(result.stdout.encode("utf-8"))
        stderr_bytes = len(result.stderr.encode("utf-8"))
        cancel_terminal = bool(result.cancelled and exit_proven)

        return AgentExecutionEvidence(
            execution_id=result.execution_id,
            run_id=result.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            cycle_id=request.cycle_id,
            workspace_id=result.workspace_id,
            candidate_id=request.candidate_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            execution_epoch=request.execution_epoch,
            execution_host_id=result.execution_host_id,
            agent_capability=request.agent_capability,
            working_directory=request.working_directory,
            process=process_identity,
            state=state_value,
            exit_code=result.exit_code,
            exit_proven=exit_proven,
            stdout=result.stdout,
            stderr=result.stderr,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            started_at=result.started_at,
            completed_at=result.completed_at,
            timed_out=result.timed_out,
            cancelled=result.cancelled,
            cancel_terminal=cancel_terminal,
            blockers=tuple(dict.fromkeys(blockers)),
            error_message=result.error_message,
        )


def _controller_is_windows() -> bool:
    import sys

    return sys.platform.lower().startswith("win")
