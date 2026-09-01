"""Hermes Workspace Identity & Worktree Safety package."""

from ai_engineering.workspaces.diff_artifacts import (
    compute_diff_digest,
    generate_diff_artifact,
    verify_diff_artifact,
)
from ai_engineering.workspaces.snapshot_contracts import (
    DIFF_ARTIFACT_CONTRACT_VERSION,
    WORKSPACE_SNAPSHOT_CONTRACT_VERSION,
    DiffArtifact,
    SnapshotBlockingReason,
    SnapshotPhase,
    WorkspaceSnapshot,
    WorkspaceSnapshotError,
    validate_repository_relative_path,
)
from ai_engineering.workspaces.snapshot_manager import (
    SnapshotRegistry,
    WorkspaceSnapshotManager,
    normalize_git_status,
)
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
from ai_engineering.workspaces.workspace_manager import (
    WorkspaceManager,
    resolve_against_workspace,
    validate_workspace_path,
)
from ai_engineering.workspaces.worktree_manager import WorktreeManager

__all__ = [
    "DIFF_ARTIFACT_CONTRACT_VERSION",
    "DiffArtifact",
    "ExecutionMode",
    "LeaseState",
    "LeaseTransitionError",
    "SnapshotBlockingReason",
    "SnapshotPhase",
    "SnapshotRegistry",
    "WORKSPACE_CONTRACT_VERSION",
    "WORKSPACE_SNAPSHOT_CONTRACT_VERSION",
    "WorkspaceBlockingReason",
    "WorkspaceIdentity",
    "WorkspaceManager",
    "WorkspaceSecurityError",
    "WorkspaceSnapshot",
    "WorkspaceSnapshotError",
    "WorkspaceSnapshotManager",
    "WorktreeLease",
    "WorktreeManager",
    "WorktreeSafetyError",
    "compute_diff_digest",
    "generate_diff_artifact",
    "normalize_git_status",
    "resolve_against_workspace",
    "validate_repository_relative_path",
    "validate_workspace_path",
    "verify_diff_artifact",
]
