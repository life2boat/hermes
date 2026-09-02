"""Spawn authorization gate for the controlled agent runtime (PR-13).

Every real process spawn must pass this gate before any process is
created. The gate is fail-closed: any identity disagreement, missing
authority evidence, lease defect, workspace escape, unauthorized
command, or disabled activation policy blocks the spawn with
machine-readable blockers and no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ai_engineering.candidates.candidate_contracts import CandidateIdentity
from ai_engineering.contracts import AuthorityBoundary, EffectClass
from ai_engineering.execution.host_contracts import ExecutionHostIdentity
from ai_engineering.execution.run_state import AgentRunRecord
from ai_engineering.runtime.runtime_contracts import AgentExecutionRequest, RuntimeMode
from ai_engineering.runtime.runtime_policy import RuntimePolicy, validate_runtime_command
from ai_engineering.task_intent import TaskIntent, intent_digest, validate_intent
from ai_engineering.workspaces.workspace_contracts import LeaseState, WorkspaceIdentity
from ai_engineering.workspaces.workspace_manager import WorkspaceManager

# Effect classes that must never be authorized for a PR-13 runtime process.
_FORBIDDEN_EFFECT_CLASSES = frozenset(
    {
        EffectClass.GIT_PUSH,
        EffectClass.PR_MUTATION,
        EffectClass.PR_MERGE,
        EffectClass.DEPLOY,
        EffectClass.RUNTIME_MUTATION,
        EffectClass.DATA_MUTATION,
        EffectClass.VECTOR_MUTATION,
        EffectClass.SECRET_MUTATION,
        EffectClass.EXTERNAL_SEND,
    }
)


@dataclass(frozen=True, slots=True)
class SpawnAuthorization:
    """Explicit, immutable authorization decision for one spawn request."""

    authorized: bool
    blockers: tuple[str, ...]


def _check_authority(authority: AuthorityBoundary) -> list[str]:
    blockers: list[str] = []
    if authority.production_authorized:
        blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
    if authority.secret_access_authorized:
        blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
    if authority.data_access_authorized:
        blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
    if authority.stop_boundary.value in ("DEPLOY", "LIVE_SMOKE", "MERGE"):
        blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
    if any(effect in _FORBIDDEN_EFFECT_CLASSES for effect in authority.allowed_effect_classes):
        blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
    return blockers


def authorize_spawn(
    request: AgentExecutionRequest,
    *,
    policy: RuntimePolicy,
    intent: TaskIntent | None,
    authority: AuthorityBoundary | None,
    workspace: WorkspaceIdentity | None,
    run_record: AgentRunRecord | None,
    host: ExecutionHostIdentity | None,
    candidate: CandidateIdentity | None,
    workspace_manager: WorkspaceManager,
    clock: datetime | None = None,
) -> SpawnAuthorization:
    """Authorize one real agent process spawn against canonical contracts.

    No state is created or mutated; the gate is a pure read-only
    decision over explicit authoritative inputs.
    """
    blockers: list[str] = []

    # 1. Activation policy
    if policy.mode == RuntimeMode.DISABLED:
        blockers.append("RUNTIME_ACTIVATION_DISABLED")
    if request.timeout_seconds > policy.max_timeout_seconds:
        blockers.append("RUNTIME_ACTIVATION_DISABLED")
    if max(request.max_stdout_bytes, request.max_stderr_bytes) > policy.max_output_bytes:
        blockers.append("RUNTIME_ACTIVATION_DISABLED")

    # 2. TaskIntent binding: valid canonical intent, exact task and base,
    #    and the request's authority digest must be the intent content digest.
    if intent is None:
        blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
    else:
        try:
            validated = validate_intent(intent)
            if validated.task_id != request.task_id:
                blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
            if validated.source_base_sha.lower() != request.base_sha.lower():
                blockers.append("CANDIDATE_BASE_SHA_MISMATCH")
            if intent_digest(validated) != request.authority_digest:
                blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
        except Exception:
            blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")

    # 3. Authority boundary: production / secret / data authority absent.
    if authority is None:
        blockers.append("CONTROL_PLANE_AUTHORIZATION_MISMATCH")
    else:
        blockers.extend(_check_authority(authority))

    # 4. Workspace identity and lease
    if workspace is None:
        blockers.append("WORKSPACE_NOT_FOUND")
    else:
        if workspace.workspace_id != request.workspace_id:
            blockers.append("RUN_WORKSPACE_MISMATCH")
        if workspace.task_id != request.task_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")
        if workspace.candidate_id != request.candidate_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")
        if workspace.repository != request.repository_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")
        if workspace.base_sha.lower() != request.base_sha.lower():
            blockers.append("CANDIDATE_BASE_SHA_MISMATCH")
        if workspace.execution_host_id != request.execution_host_id:
            blockers.append("EXECUTION_HOST_MISMATCH")
        if policy.mode != RuntimeMode.DISABLED:
            try:
                required_mode = policy.requires_mode()
            except Exception:
                required_mode = None
            if required_mode is not None and workspace.execution_mode != required_mode:
                blockers.append("EXECUTION_MODE_INVALID")

        lease = workspace_manager.get_lease(request.workspace_id)
        if lease is None:
            blockers.append("WORKSPACE_NOT_FOUND")
        else:
            if lease.workspace_id != request.workspace_id:
                blockers.append("WORKTREE_IDENTITY_MISMATCH")
            if lease.state != LeaseState.ACTIVE:
                blockers.append("WORKTREE_IDENTITY_MISMATCH")
            elif not lease.is_active(now=clock):
                blockers.append("LEASE_EXPIRED")
            if lease.owner_run_id != request.run_id:
                blockers.append("RUN_LEASE_OWNERSHIP_MISMATCH")

    # 5. Run identity exactness and epoch fencing
    if run_record is None:
        blockers.append("STALE_RUN_EVENT")
    else:
        ident = run_record.identity
        if ident.run_id != request.run_id:
            blockers.append("RUN_WORKSPACE_MISMATCH")
        if ident.task_id != request.task_id:
            blockers.append("RUN_WORKSPACE_MISMATCH")
        if ident.node_id != request.node_id:
            blockers.append("RUN_WORKSPACE_MISMATCH")
        if ident.workspace_id != request.workspace_id:
            blockers.append("RUN_WORKSPACE_MISMATCH")
        if ident.candidate_id != request.candidate_id:
            blockers.append("RUN_WORKSPACE_MISMATCH")
        if ident.execution_host_id != request.execution_host_id:
            blockers.append("EXECUTION_HOST_MISMATCH")
        if ident.execution_epoch != request.execution_epoch:
            blockers.append("STALE_RUN_MUTATION")
        if run_record.state.value != "LIVE":
            blockers.append("RUN_NOT_ACTIVE")

    # 6. Candidate identity binding
    if candidate is None:
        blockers.append("WORKTREE_IDENTITY_MISMATCH")
    else:
        if candidate.candidate_id != request.candidate_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")
        if candidate.task_id != request.task_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")
        if candidate.node_id != request.node_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")
        if candidate.base_sha.lower() != request.base_sha.lower():
            blockers.append("CANDIDATE_BASE_SHA_MISMATCH")
        if candidate.workspace_id != request.workspace_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")
        if candidate.run_id != request.run_id:
            blockers.append("WORKTREE_IDENTITY_MISMATCH")

    # 7. Execution host exactness
    if host is None:
        blockers.append("EXECUTION_HOST_MISMATCH")
    else:
        if host.execution_host_id != request.execution_host_id:
            blockers.append("EXECUTION_HOST_MISMATCH")
        if not host.available:
            blockers.append("EXECUTION_HOST_UNAVAILABLE")

    # 8. Workspace must be clean/expected: a dirty or unreadable
    #    worktree fails closed before spawn.
    if workspace is not None:
        try:
            workspace_manager.worktree_manager.validate_clean_worktree(workspace.worktree_path)
        except Exception as exc:
            code = getattr(exc, "code", None)
            blockers.append(code if isinstance(code, str) else "WORKTREE_DIRTY_REUSE")

    # 9. Working directory confinement inside the authorized workspace.
    if workspace is not None:
        try:
            resolved = workspace_manager.validate_workspace_path(
                request.workspace_id,
                request.working_directory,
                caller_run_id=request.run_id,
            )
        except Exception:
            blockers.append("RUNTIME_WORKSPACE_ESCAPE")
        else:
            if workspace_manager.is_canonical_checkout(resolved):
                blockers.append("RUNTIME_WORKSPACE_ESCAPE")

    # 10. Command policy (argv-based; shell invocation unreachable)
    try:
        validate_runtime_command(request.command_argv)
    except Exception:
        blockers.append("RUNTIME_COMMAND_NOT_AUTHORIZED")

    return SpawnAuthorization(authorized=not blockers, blockers=tuple(dict.fromkeys(blockers)))
