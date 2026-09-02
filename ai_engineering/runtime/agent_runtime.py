"""Controlled agent runtime facade (PR-13).

Activates real local/WSL agent process execution inside an isolated
candidate workspace authorized by the existing control plane:

    TaskIntent -> Control Plane -> Workspace reservation ->
    AgentRunIdentity -> ExecutionHost -> real bounded process ->
    captured result -> snapshots/diff -> candidate evidence

Fail-closed guarantees:

- no spawn unless the activation policy is explicitly SHADOW_LOCAL or
  SHADOW_WSL and the spawn authorization gate passes;
- no host fallback (LOCAL failure never silently runs in WSL and vice
  versa);
- the child process cwd is confined to the authorized candidate
  worktree and its environment is deny-by-default;
- duplicate spawn identities are idempotent or collide fail-closed;
- timeout is never proof of exit and a cancellation acknowledgement is
  never terminal evidence;
- the runtime emits evidence only and never mutates control-plane
  state; canonical repositories must remain clean.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from ai_engineering.candidates.candidate_contracts import CandidateIdentity
from ai_engineering.contracts import AuthorityBoundary
from ai_engineering.execution.execution_registry import ExecutionRegistry
from ai_engineering.execution.execution_host import ExecutionHost
from ai_engineering.execution.host_contracts import ExecutionResult, ExecutionState
from ai_engineering.execution.local_host import LocalExecutionHost
from ai_engineering.execution.run_contracts import RunEventEnvelope, RunEventType, RunState
from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.execution.run_state import AgentRunRecord
from ai_engineering.execution.wsl_host import WslExecutionHost
from ai_engineering.runtime.process_runner import AgentProcessRunner
from ai_engineering.runtime.runtime_contracts import (
    AgentExecutionEvidence,
    AgentExecutionRequest,
    AgentProcessIdentity,
    AgentRuntimeError,
    RuntimeBlockingReason,
    RuntimeMode,
)
from ai_engineering.runtime.runtime_evidence import evidence_run_event_payload
from ai_engineering.runtime.runtime_policy import RuntimePolicy
from ai_engineering.runtime.runtime_registry import RuntimeRegistry
from ai_engineering.runtime.spawn_gate import authorize_spawn
from ai_engineering.workspaces.snapshot_contracts import DiffArtifact, SnapshotPhase, WorkspaceSnapshot
from ai_engineering.workspaces.snapshot_manager import WorkspaceSnapshotManager
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity
from ai_engineering.workspaces.workspace_manager import WorkspaceManager


@dataclass(frozen=True, slots=True)
class ExecutionArtifacts:
    """Snapshot/diff evidence produced for one execution (read-only)."""

    execution_id: str
    pre_execution_snapshot: WorkspaceSnapshot | None
    post_execution_snapshot: WorkspaceSnapshot | None
    diff_artifact: DiffArtifact | None


class ControlledAgentRuntime:
    """Facade for bounded real agent process execution in candidate workspaces."""

    def __init__(
        self,
        *,
        policy: RuntimePolicy | None = None,
        workspace_manager: WorkspaceManager,
        run_registry: ActiveRunRegistry,
        snapshot_manager: WorkspaceSnapshotManager | None = None,
        runtime_registry: RuntimeRegistry | None = None,
        local_host: ExecutionHost | None = None,
        wsl_host: ExecutionHost | None = None,
        parent_env: dict[str, str] | None = None,
    ) -> None:
        self.policy = policy or RuntimePolicy()
        self.workspace_manager = workspace_manager
        self.run_registry = run_registry
        self.snapshot_manager = snapshot_manager or WorkspaceSnapshotManager(
            canonical_repo_path=workspace_manager.canonical_root
        )
        self.runtime_registry = runtime_registry or RuntimeRegistry()
        self.execution_registry = ExecutionRegistry()
        self._local_host = local_host or LocalExecutionHost()
        self._wsl_host = wsl_host
        self._runner = AgentProcessRunner(
            local_host=self._local_host,
            wsl_host=self._wsl_host,
            parent_env=parent_env,
        )
        self._artifacts: dict[str, ExecutionArtifacts] = {}

    # ------------------------------------------------------------------
    # Real process execution
    # ------------------------------------------------------------------
    def execute_agent_process(
        self,
        request: AgentExecutionRequest,
        *,
        intent,
        authority: AuthorityBoundary,
        run_identity,
        candidate: CandidateIdentity | None = None,
        clock: datetime | None = None,
    ) -> AgentExecutionEvidence:
        """Authorize and execute one real bounded agent process.

        Raises :class:`AgentRuntimeError` fail-closed before any spawn
        when authorization fails. After a spawn, all failures are
        reported as evidence blockers (never a false success).
        """
        # 1. Activation policy: DISABLED blocks everything up front.
        if self.policy.mode == RuntimeMode.DISABLED:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_ACTIVATION_DISABLED.value,
                "Runtime policy is DISABLED; real process execution requires "
                "explicit SHADOW_LOCAL or SHADOW_WSL activation",
            )

        # 2. Idempotency / collision bookkeeping.
        status, existing = self.runtime_registry.register_spawn(request)
        if status.value == "ALREADY_ACTIVE":
            if existing.evidence is not None:
                return existing.evidence
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_SPAWN_COLLISION.value,
                f"Duplicate in-flight spawn for execution_id {request.execution_id!r}",
            )

        # 3. Concurrency slot reservation (atomic; canonical budget).
        slot_key = f"{request.task_id}:{request.node_id}"
        self.runtime_registry.slot_allocator.reserve(
            slot_key, request.run_id, self.policy.max_concurrent_processes
        )

        try:
            return self._execute_authorized(request, run_identity, intent, authority, candidate, clock)
        finally:
            self.runtime_registry.slot_allocator.release(slot_key, request.run_id)

    def _execute_authorized(
        self,
        request: AgentExecutionRequest,
        run_identity,
        intent,
        authority: AuthorityBoundary,
        candidate: CandidateIdentity | None,
        clock: datetime | None,
    ) -> AgentExecutionEvidence:
        workspace = self.workspace_manager.get_workspace(request.workspace_id)
        lease = self.workspace_manager.get_lease(request.workspace_id)
        run_record: AgentRunRecord | None = self.run_registry.get_run(request.run_id)
        host = self._resolve_host_identity(request)

        # 4. Spawn authorization gate (fail closed before spawn).
        authorization = authorize_spawn(
            request,
            policy=self.policy,
            intent=intent,
            authority=authority,
            workspace=workspace,
            run_record=run_record,
            host=host,
            candidate=candidate,
            workspace_manager=self.workspace_manager,
            clock=clock,
        )
        if not authorization.authorized:
            raise AgentRuntimeError(
                authorization.blockers[0],
                f"Spawn authorization failed for execution_id {request.execution_id!r}",
                blockers=authorization.blockers,
            )

        # 5. Register/activate the run through the canonical registry.
        record, _spawn_status = self.run_registry.spawn_agent(run_identity)
        if record.state != RunState.LIVE:
            raise AgentRuntimeError(
                "RUN_NOT_ACTIVE",
                f"Run {request.run_id!r} did not reach LIVE state",
            )

        # 6. PRE_EXECUTION snapshot (fail-closed: no snapshot, no spawn).
        workspace_obj: WorkspaceIdentity = workspace  # type: ignore[assignment]
        try:
            pre_snapshot = self.snapshot_manager.capture_snapshot(
                workspace_obj,
                run_identity,
                SnapshotPhase.PRE_EXECUTION,
                lease=lease,
            )
        except Exception as exc:
            return self._fail_after_spawn(
                request,
                blockers=(getattr(exc, "code", RuntimeBlockingReason.RUNTIME_OUTPUT_CAPTURE_FAILED.value),),
                error_message=f"PRE_EXECUTION snapshot failed: {exc}",
            )

        # 7. Resolve the confined working directory and execute.
        resolved_cwd = self.workspace_manager.validate_workspace_path(
            request.workspace_id,
            request.working_directory,
            caller_run_id=request.run_id,
        )
        execution_request = self._runner.build_execution_request(
            request,
            workspace_root=Path(workspace_obj.worktree_path),
            resolved_working_directory=resolved_cwd,
        )
        self.execution_registry.record_request(execution_request)
        process_identity, evidence = self._runner.execute(
            request,
            execution_request,
            workspace_root=Path(workspace_obj.worktree_path),
        )
        self.runtime_registry.register_process_identity(process_identity)
        self.runtime_registry.record_result(
            evidence,
            process_id=process_identity.process_id,
            request=request,
        )
        self.execution_registry.record_result(
            self._to_execution_result(evidence, execution_request.created_at)
        )

        # 8. POST_EXECUTION snapshot + diff evidence.
        post_snapshot: WorkspaceSnapshot | None = None
        diff_artifact: DiffArtifact | None = None
        post_blockers: list[str] = []
        try:
            post_snapshot = self.snapshot_manager.capture_snapshot(
                workspace_obj,
                run_identity,
                SnapshotPhase.POST_EXECUTION,
                lease=lease,
            )
            diff_artifact = self.snapshot_manager.create_diff_artifact(
                workspace_obj,
                run_identity,
                lease=lease,
            )
        except Exception as exc:
            post_blockers.append(
                getattr(exc, "code", RuntimeBlockingReason.RUNTIME_OUTPUT_CAPTURE_FAILED.value)
            )
            evidence = replace(evidence, blockers=evidence.blockers + tuple(post_blockers))

        self._artifacts[request.execution_id] = ExecutionArtifacts(
            execution_id=request.execution_id,
            pre_execution_snapshot=pre_snapshot,
            post_execution_snapshot=post_snapshot,
            diff_artifact=diff_artifact,
        )

        # 9. Canonical repository must remain untouched.
        evidence = self._verify_canonical_clean(evidence)

        # 10. Terminal run event through the canonical event path.
        self._record_terminal_run_event(request, evidence)

        return evidence

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    def request_cancel(self, execution_id: str, *, reason: str | None = None) -> AgentRunRecord:
        """Request cancellation: signal the process and mark the run CANCEL_REQUESTED.

        Acknowledgement is not terminal: only a subsequently proven
        process termination may produce terminal cancellation evidence.
        """
        spawn_record = self.runtime_registry.get_spawn_record(execution_id)
        if spawn_record is None:
            raise AgentRuntimeError(
                RuntimeBlockingReason.STALE_RUNTIME_EVENT.value,
                f"Cancel requested for unknown execution_id {execution_id!r}",
            )
        self._host_for_request(spawn_record.request).request_cancel(execution_id)
        return self.run_registry.request_cancel(spawn_record.request.run_id, reason=reason)

    def get_evidence(self, execution_id: str) -> AgentExecutionEvidence:
        """Return recorded evidence or fail closed when absent."""
        evidence = self.runtime_registry.get_result(execution_id)
        if evidence is None:
            raise AgentRuntimeError(
                RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value,
                f"No terminal evidence recorded for execution_id {execution_id!r}",
            )
        return evidence

    def get_artifacts(self, execution_id: str) -> ExecutionArtifacts | None:
        """Return the snapshot/diff artifacts recorded for one execution."""
        return self._artifacts.get(execution_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _resolve_host_identity(self, request: AgentExecutionRequest):
        try:
            host = self._runner.host_for(request)
        except AgentRuntimeError:
            return None
        return host.identity()

    def _host_for_request(self, request: AgentExecutionRequest) -> ExecutionHost:
        return self._runner.host_for(request)

    def _to_execution_result(self, evidence: AgentExecutionEvidence, default_started: str) -> ExecutionResult:
        return ExecutionResult(
            execution_id=evidence.execution_id,
            run_id=evidence.run_id,
            workspace_id=evidence.workspace_id,
            execution_host_id=evidence.execution_host_id,
            state=ExecutionState(evidence.state),
            exit_code=evidence.exit_code,
            stdout=evidence.stdout,
            stderr=evidence.stderr,
            started_at=evidence.started_at or default_started,
            completed_at=evidence.completed_at,
            timed_out=evidence.timed_out,
            cancelled=evidence.cancelled,
            stdout_truncated=evidence.stdout_truncated,
            stderr_truncated=evidence.stderr_truncated,
            blockers=evidence.blockers,
            error_message=evidence.error_message,
        )

    def _verify_canonical_clean(self, evidence: AgentExecutionEvidence) -> AgentExecutionEvidence:
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.workspace_manager.canonical_root),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if proc.returncode != 0 or proc.stdout.strip():
                return replace(
                    evidence,
                    blockers=evidence.blockers + ("CANONICAL_CHECKOUT_PROTECTED",),
                    error_message=evidence.error_message
                    or "Canonical repository is not clean after runtime execution",
                )
        except Exception:
            return replace(
                evidence,
                blockers=evidence.blockers + ("CANONICAL_CHECKOUT_PROTECTED",),
            )
        return evidence

    def _record_terminal_run_event(
        self,
        request: AgentExecutionRequest,
        evidence: AgentExecutionEvidence,
    ) -> None:
        payload = evidence_run_event_payload(evidence)
        if evidence.exit_proven:
            payload["exit_code"] = evidence.exit_code
            event_type = RunEventType.AGENT_RUN_EXITED
        else:
            payload = {**payload, "error_message": evidence.error_message or "run failed"}
            event_type = RunEventType.AGENT_RUN_FAILED
        event = RunEventEnvelope(
            event_id=f"evt-{evidence.execution_id}",
            run_id=request.run_id,
            execution_epoch=request.execution_epoch,
            event_type=event_type,
            payload=payload,
            timestamp=datetime.now(timezone.utc),
            task_id=request.task_id,
            node_id=request.node_id,
            workspace_id=request.workspace_id,
        )
        self.run_registry.process_event(event)

    def _fail_after_spawn(
        self,
        request: AgentExecutionRequest,
        *,
        blockers: tuple[str, ...],
        error_message: str,
    ) -> AgentExecutionEvidence:
        """Build failed evidence after a spawn, without false success."""
        process_identity = AgentProcessIdentity(
            process_id=f"proc-{request.execution_id}",
            run_id=request.run_id,
            workspace_id=request.workspace_id,
            candidate_id=request.candidate_id,
            execution_host_id=request.execution_host_id,
            execution_epoch=request.execution_epoch,
            started_at="",
        )
        evidence = AgentExecutionEvidence(
            execution_id=request.execution_id,
            run_id=request.run_id,
            task_id=request.task_id,
            node_id=request.node_id,
            cycle_id=request.cycle_id,
            workspace_id=request.workspace_id,
            candidate_id=request.candidate_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            execution_epoch=request.execution_epoch,
            execution_host_id=request.execution_host_id,
            agent_capability=request.agent_capability,
            working_directory=request.working_directory,
            process=process_identity,
            state="FAILED",
            exit_code=None,
            exit_proven=False,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            stdout_bytes=0,
            stderr_bytes=0,
            started_at="",
            completed_at="",
            timed_out=False,
            cancelled=False,
            cancel_terminal=False,
            blockers=blockers,
            error_message=error_message,
        )
        self.runtime_registry.register_process_identity(process_identity)
        self.runtime_registry.record_result(
            evidence,
            process_id=process_identity.process_id,
            request=request,
        )
        return evidence
