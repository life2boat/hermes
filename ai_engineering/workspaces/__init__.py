"""Hermes Workspace Identity & Worktree Safety package."""

from ai_engineering.workspaces.workspace_contracts import (
    WORKSPACE_CONTRACT_VERSION,
    WORKTREE_LEASE_VERSION,
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
from ai_engineering.workspaces.workspace_manager import (
    WorkspaceManager,
    resolve_against_workspace,
    validate_workspace_path,
)

__all__ = [
    "WORKSPACE_CONTRACT_VERSION",
    "WORKTREE_LEASE_VERSION",
    "ExecutionMode",
    "LeaseState",
    "LeaseTransitionError",
    "WorkspaceBlockingReason",
    "WorkspaceIdentity",
    "WorkspaceManager",
    "WorkspaceSecurityError",
    "WorktreeLease",
    "WorktreeManager",
    "WorktreeSafetyError",
    "resolve_against_workspace",
    "validate_workspace_path",
]
