"""Unit and integration tests for WorkspaceManager."""

from __future__ import annotations

import subprocess
from pathlib import Path
import pytest

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
from ai_engineering.workspaces.workspace_manager import (
    WorkspaceManager,
    resolve_against_workspace,
    validate_workspace_path,
)


def _init_repo(repo_path: Path) -> str:
    """Initialize a git repo with a commit and return its SHA."""
    subprocess.run(["git", "init", "-b", "main", str(repo_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Test User"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    (repo_path / "README.md").write_text("Canonical")
    subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "Initial commit"], check=True, capture_output=True)
    proc = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def test_workspace_manager_create_isolated_workspace(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorkspaceManager(repo_dir)
    wt_dir = tmp_path / "workspaces" / "ws-task-1"

    identity, lease = manager.create_isolated_workspace(
        workspace_id="ws-task-1",
        task_id="task-1",
        candidate_id="cand-1",
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=base_sha,
        branch="codex/task-1-cand-1",
        worktree_path=wt_dir,
        owner_run_id="run-001",
        auto_acquire_lease=True,
    )

    assert identity.workspace_id == "ws-task-1"
    assert identity.task_id == "task-1"
    assert identity.worktree_path == str(wt_dir.resolve())
    assert lease.state == LeaseState.ACTIVE
    assert lease.owner_run_id == "run-001"

    # Verify lookups
    assert manager.get_workspace("ws-task-1") == identity
    assert manager.get_lease("ws-task-1") == lease
    assert len(manager.list_workspaces()) == 1


def test_workspace_manager_path_containment_and_escape_rejection(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorkspaceManager(repo_dir)
    wt_dir = tmp_path / "workspaces" / "ws-task-1"

    manager.create_isolated_workspace(
        workspace_id="ws-task-1",
        task_id="task-1",
        candidate_id="cand-1",
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=base_sha,
        branch="codex/task-1-cand-1",
        worktree_path=wt_dir,
        owner_run_id="run-001",
        auto_acquire_lease=True,
    )

    # Valid child path (relative)
    p1 = manager.validate_workspace_path("ws-task-1", "src/module.py")
    assert p1 == (wt_dir / "src/module.py").resolve()

    # Valid child path (absolute inside worktree)
    p2 = manager.validate_workspace_path("ws-task-1", wt_dir / "docs/SPEC.md")
    assert p2 == (wt_dir / "docs/SPEC.md").resolve()

    # Path escape with ../ (relative)
    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-task-1", "../outside.txt")
    assert exc_info.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value

    # Deep ../ escape
    with pytest.raises(WorkspaceSecurityError) as exc_info2:
        manager.validate_workspace_path("ws-task-1", "src/../../outside.txt")
    assert exc_info2.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value

    # Absolute path outside workspace
    outside_path = tmp_path / "outside_file.txt"
    outside_path.write_text("outside")
    with pytest.raises(WorkspaceSecurityError) as exc_info3:
        manager.validate_workspace_path("ws-task-1", outside_path)
    assert exc_info3.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value

    # Path resolving to canonical repository
    with pytest.raises(WorkspaceSecurityError) as exc_info4:
        manager.validate_workspace_path("ws-task-1", repo_dir / "README.md")
    assert exc_info4.value.code in (
        WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value,
        WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
    )


def test_workspace_manager_foreign_workspace_rejection(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorkspaceManager(repo_dir)
    wt_dir_1 = tmp_path / "workspaces" / "ws-1"
    wt_dir_2 = tmp_path / "workspaces" / "ws-2"

    manager.create_isolated_workspace(
        workspace_id="ws-1",
        task_id="task-1",
        candidate_id="cand-1",
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=base_sha,
        branch="codex/task-1",
        worktree_path=wt_dir_1,
        owner_run_id="run-1",
        auto_acquire_lease=True,
    )

    manager.create_isolated_workspace(
        workspace_id="ws-2",
        task_id="task-2",
        candidate_id="cand-2",
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=base_sha,
        branch="codex/task-2",
        worktree_path=wt_dir_2,
        owner_run_id="run-2",
        auto_acquire_lease=True,
    )

    # Trying to validate path belonging to ws-2 within ws-1 context
    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-1", wt_dir_2 / "code.py")
    assert exc_info.value.code in (
        WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
        WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
    )

    # Caller mismatch check
    with pytest.raises(WorkspaceSecurityError) as exc_info2:
        manager.validate_workspace_path("ws-1", "file.py", caller_run_id="foreign-run-99")
    assert exc_info2.value.code == WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value


def test_resolve_against_workspace(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorkspaceManager(repo_dir)
    wt_dir = tmp_path / "workspaces" / "ws-logical"

    ident, _ = manager.create_isolated_workspace(
        workspace_id="ws-logical",
        task_id="task-1",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=base_sha,
        branch="codex/logical-test",
        worktree_path=wt_dir,
        owner_run_id="run-1",
        auto_acquire_lease=True,
    )

    # Resolve logical relative path
    resolved = resolve_against_workspace(ident, "ai_engineering/task_graph.py")
    assert resolved == (wt_dir / "ai_engineering/task_graph.py").resolve()

    # Reject absolute path
    with pytest.raises(WorkspaceSecurityError) as exc_info:
        resolve_against_workspace(ident, "/etc/passwd")
    assert exc_info.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value

    # Reject ../ traversal
    with pytest.raises(WorkspaceSecurityError) as exc_info2:
        resolve_against_workspace(ident, "../../secret.txt")
    assert exc_info2.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value
