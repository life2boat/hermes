"""Authoritative Workspace Manager and Path Containment Controller."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_engineering.workspaces.workspace_contracts import (
    ExecutionMode,
    LeaseState,
    LeaseTransitionError,
    WorkspaceBlockingReason,
    WorkspaceIdentity,
    WorkspaceSecurityError,
    WorktreeLease,
    WorktreeSafetyError,
)
from ai_engineering.workspaces.worktree_manager import WorktreeManager


def resolve_against_workspace(
    workspace: WorkspaceIdentity | str,
    repository_relative_path: str | Path,
    *,
    workspace_manager: WorkspaceManager | None = None,
) -> Path:
    """Resolve a repository-relative logical path against an authoritative workspace root."""
    if isinstance(workspace, str):
        if workspace_manager is None:
            raise WorkspaceSecurityError("WORKSPACE_MANAGER_REQUIRED", "workspace_manager required when resolving by ID")
        ws_identity = workspace_manager.get_workspace(workspace)
        if ws_identity is None:
            raise WorkspaceSecurityError(WorkspaceBlockingReason.WORKSPACE_NOT_FOUND.value, f"Workspace {workspace} not found")
    else:
        ws_identity = workspace

    raw_path_str = str(repository_relative_path)
    # Reject empty or whitespace-only paths
    if not raw_path_str.strip():
        raise WorkspaceSecurityError(WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value, "Relative path cannot be empty")

    path_obj = Path(repository_relative_path)
    if path_obj.is_absolute():
        raise WorkspaceSecurityError(
            WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
            f"Expected repository-relative path, got absolute: {repository_relative_path}",
        )

    root = Path(ws_identity.worktree_path).resolve()
    target = (root / path_obj).resolve()

    try:
        if not target.is_relative_to(root):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
                f"Path {repository_relative_path} escapes workspace root {root}",
            )
    except ValueError as exc:
        raise WorkspaceSecurityError(
            WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
            f"Path {repository_relative_path} escapes workspace root {root}",
        ) from exc

    return target


def validate_workspace_path(
    workspace: WorkspaceIdentity | str,
    requested_path: str | Path,
    *,
    workspace_manager: WorkspaceManager | None = None,
    caller_run_id: str | None = None,
) -> Path:
    """Convenience standalone helper to validate a requested filesystem path within a workspace."""
    if workspace_manager is not None:
        ws_id = workspace if isinstance(workspace, str) else workspace.workspace_id
        return workspace_manager.validate_workspace_path(ws_id, requested_path, caller_run_id=caller_run_id)

    if isinstance(workspace, str):
        raise WorkspaceSecurityError("WORKSPACE_MANAGER_REQUIRED", "workspace_manager required when validating by ID")

    root = Path(workspace.worktree_path).resolve()
    path_obj = Path(requested_path)
    resolved = (root / path_obj).resolve() if not path_obj.is_absolute() else path_obj.resolve()

    try:
        if not resolved.is_relative_to(root):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
                f"Requested path {requested_path} escapes workspace {root}",
            )
    except ValueError as exc:
        raise WorkspaceSecurityError(
            WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
            f"Requested path {requested_path} escapes workspace {root}",
        ) from exc

    return resolved


class WorkspaceManager:
    """Coordinates workspace identity, worktree lifecycle, leases, and containment boundaries."""

    def __init__(
        self,
        canonical_root: Path | str,
        *,
        worktree_manager: WorktreeManager | None = None,
    ) -> None:
        self._worktree_manager = worktree_manager or WorktreeManager(canonical_root)
        self._canonical_root = self._worktree_manager.canonical_root
        self._workspaces: dict[str, WorkspaceIdentity] = {}
        self._leases: dict[str, WorktreeLease] = {}

    @property
    def canonical_root(self) -> Path:
        """Return the canonical repository root path."""
        return self._canonical_root

    @property
    def worktree_manager(self) -> WorktreeManager:
        """Return the underlying Git worktree manager."""
        return self._worktree_manager

    def is_canonical_checkout(self, path: Path | str) -> bool:
        """Check if a path points to or resolves inside the canonical checkout."""
        return self._worktree_manager.is_canonical_checkout(path)

    def register_workspace(
        self,
        identity: WorkspaceIdentity,
        lease: WorktreeLease | None = None,
    ) -> WorkspaceIdentity:
        """Register a WorkspaceIdentity and associate its active/reserved lease."""
        # Validate canonical checkout collision
        resolved_path = Path(identity.worktree_path).resolve()
        if self.is_canonical_checkout(resolved_path):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value,
                f"Workspace cannot be registered at canonical checkout: {resolved_path}",
            )

        if identity.workspace_id in self._workspaces:
            existing = self._workspaces[identity.workspace_id]
            if existing != identity:
                raise WorkspaceSecurityError(
                    WorkspaceBlockingReason.WORKSPACE_ALREADY_EXISTS.value,
                    f"Workspace {identity.workspace_id} already registered with different configuration",
                )
            return existing

        self._workspaces[identity.workspace_id] = identity
        if lease is not None:
            if lease.workspace_id != identity.workspace_id:
                raise WorkspaceSecurityError(
                    WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                    f"Lease workspace_id {lease.workspace_id} != identity {identity.workspace_id}",
                )
            self._leases[identity.workspace_id] = lease
        else:
            self._leases[identity.workspace_id] = WorktreeLease(
                workspace_id=identity.workspace_id,
                owner_run_id=identity.task_id,
                task_id=identity.task_id,
                acquired_at=datetime.now(timezone.utc),
                expires_at=None,
                state=LeaseState.RESERVED,
            )

        return identity

    def create_isolated_workspace(
        self,
        *,
        workspace_id: str,
        task_id: str,
        candidate_id: str | None = None,
        repository: str,
        base_ref: str,
        base_sha: str,
        branch: str,
        worktree_path: Path | str,
        execution_host_id: str = "local",
        execution_mode: str = ExecutionMode.ISOLATED.value,
        owner_run_id: str,
        auto_acquire_lease: bool = False,
        now: datetime | None = None,
    ) -> tuple[WorkspaceIdentity, WorktreeLease]:
        """Create a dedicated git worktree and register its WorkspaceIdentity and WorktreeLease."""
        target_path = Path(worktree_path)
        if self.is_canonical_checkout(target_path):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value,
                f"Cannot create isolated workspace at canonical checkout: {target_path}",
            )

        resolved_wt = self._worktree_manager.create_worktree(
            worktree_path=target_path,
            branch=branch,
            base_sha=base_sha,
            base_ref=base_ref,
        )

        creation_time = now if now is not None else datetime.now(timezone.utc)
        identity = WorkspaceIdentity(
            workspace_id=workspace_id,
            task_id=task_id,
            candidate_id=candidate_id,
            repository=repository,
            base_ref=base_ref,
            base_sha=base_sha.lower(),
            branch=branch,
            worktree_path=str(resolved_wt),
            execution_host_id=execution_host_id,
            execution_mode=execution_mode,
            created_at=creation_time,
        )

        lease_state = LeaseState.ACTIVE if auto_acquire_lease else LeaseState.RESERVED
        lease = WorktreeLease(
            workspace_id=workspace_id,
            owner_run_id=owner_run_id,
            task_id=task_id,
            acquired_at=creation_time,
            expires_at=None,
            state=lease_state,
        )

        self.register_workspace(identity, lease)
        return identity, lease

    def get_workspace(self, workspace_id: str) -> WorkspaceIdentity | None:
        """Retrieve WorkspaceIdentity by workspace_id."""
        return self._workspaces.get(workspace_id)

    def get_lease(self, workspace_id: str) -> WorktreeLease | None:
        """Retrieve WorktreeLease by workspace_id."""
        return self._leases.get(workspace_id)

    def list_workspaces(self) -> list[WorkspaceIdentity]:
        """Return a list of all registered workspace identities."""
        return list(self._workspaces.values())

    def acquire_lease(
        self,
        workspace_id: str,
        owner_run_id: str,
        *,
        now: datetime | None = None,
    ) -> WorktreeLease:
        """Acquire an active lease for an owner run ID."""
        lease = self._leases.get(workspace_id)
        if lease is None:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_NOT_FOUND.value,
                f"Workspace {workspace_id} not registered",
            )
        updated = lease.transition(LeaseState.ACTIVE, actor_run_id=owner_run_id, now=now)
        self._leases[workspace_id] = updated
        return updated

    def release_lease(
        self,
        workspace_id: str,
        owner_run_id: str,
        *,
        now: datetime | None = None,
    ) -> WorktreeLease:
        """Release a lease into terminal RELEASED state."""
        lease = self._leases.get(workspace_id)
        if lease is None:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_NOT_FOUND.value,
                f"Workspace {workspace_id} not registered",
            )
        updated = lease.transition(LeaseState.RELEASED, actor_run_id=owner_run_id, now=now)
        self._leases[workspace_id] = updated
        return updated

    def quarantine_lease(
        self,
        workspace_id: str,
        owner_run_id: str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> WorktreeLease:
        """Quarantine a lease into terminal QUARANTINED state."""
        lease = self._leases.get(workspace_id)
        if lease is None:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_NOT_FOUND.value,
                f"Workspace {workspace_id} not registered",
            )
        updated = lease.transition(LeaseState.QUARANTINED, actor_run_id=owner_run_id, now=now)
        self._leases[workspace_id] = updated
        return updated

    def validate_workspace_identity(
        self,
        workspace_id: str,
        *,
        expected_task_id: str | None = None,
        expected_run_id: str | None = None,
        expected_base_sha: str | None = None,
        expected_branch: str | None = None,
    ) -> WorkspaceIdentity:
        """Validate workspace identity and runtime state against expected task/run/git metadata."""
        identity = self.get_workspace(workspace_id)
        if identity is None:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_NOT_FOUND.value,
                f"Workspace {workspace_id} not found",
            )
        if expected_task_id is not None and identity.task_id != expected_task_id:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                f"Workspace task_id {identity.task_id!r} != expected {expected_task_id!r}",
            )
        if expected_run_id is not None:
            lease = self.get_lease(workspace_id)
            if lease is None or lease.owner_run_id != expected_run_id:
                raise WorkspaceSecurityError(
                    WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                    f"Workspace lease owner {getattr(lease, 'owner_run_id', None)!r} != expected {expected_run_id!r}",
                )
        if expected_base_sha is not None:
            if identity.base_sha.lower() != expected_base_sha.lower():
                raise WorkspaceSecurityError(
                    WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value,
                    f"Identity base_sha {identity.base_sha} != expected {expected_base_sha}",
                )
            self._worktree_manager.validate_worktree_base_sha(identity.worktree_path, expected_base_sha)
        if expected_branch is not None:
            if identity.branch != expected_branch:
                raise WorkspaceSecurityError(
                    WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                    f"Identity branch {identity.branch} != expected {expected_branch}",
                )
            self._worktree_manager.validate_worktree_branch(identity.worktree_path, expected_branch)
        return identity

    def validate_workspace_path(
        self,
        workspace_id: str,
        requested_path: str | Path,
        *,
        caller_run_id: str | None = None,
    ) -> Path:
        """Validate that requested_path strictly resolves within the workspace root."""
        identity = self.get_workspace(workspace_id)
        if identity is None:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_NOT_FOUND.value,
                f"Workspace {workspace_id} not registered",
            )

        lease = self.get_lease(workspace_id)
        if lease is None or lease.state in (LeaseState.RELEASED, LeaseState.QUARANTINED):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                f"Workspace {workspace_id} lease is in {getattr(lease, 'state', None)} state",
            )
        if caller_run_id is not None and lease.owner_run_id != caller_run_id:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                f"Caller {caller_run_id!r} does not match lease owner {lease.owner_run_id!r}",
            )

        authoritative_root = Path(identity.worktree_path).resolve()
        path_obj = Path(requested_path)

        # Resolve requested path relative to authoritative workspace root if relative,
        # or directly if absolute.
        if path_obj.is_absolute():
            resolved = path_obj.resolve()
        else:
            resolved = (authoritative_root / path_obj).resolve()

        # Check containment within authoritative root
        try:
            if not resolved.is_relative_to(authoritative_root):
                raise WorkspaceSecurityError(
                    WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
                    f"Requested path {requested_path} resolves outside workspace {authoritative_root}: {resolved}",
                )
        except ValueError as exc:
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
                f"Requested path {requested_path} resolves outside workspace {authoritative_root}: {resolved}",
            ) from exc

        # Protect canonical repository
        if self.is_canonical_checkout(resolved):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value,
                f"Requested path resolves into canonical repository: {resolved}",
            )

        # Protect against other workspaces' roots
        for other_id, other_ws in self._workspaces.items():
            if other_id == workspace_id:
                continue
            other_root = Path(other_ws.worktree_path).resolve()
            try:
                if resolved == other_root or resolved.is_relative_to(other_root):
                    raise WorkspaceSecurityError(
                        WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                        f"Requested path resolves into foreign workspace {other_id}: {resolved}",
                    )
            except ValueError:
                pass

        return resolved
