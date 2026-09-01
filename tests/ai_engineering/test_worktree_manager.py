"""Unit and integration tests for WorktreeManager using isolated temporary repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path
import pytest

from ai_engineering.workspaces.workspace_contracts import (
    WorkspaceBlockingReason,
    WorkspaceSecurityError,
    WorktreeSafetyError,
)
from ai_engineering.workspaces.worktree_manager import WorktreeManager


def _init_repo(repo_path: Path) -> str:
    """Initialize a git repo with a commit and return its SHA."""
    subprocess.run(["git", "init", "-b", "main", str(repo_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.name", "Test User"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    (repo_path / "README.md").write_text("Hello")
    subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "commit", "-m", "Initial commit"], check=True, capture_output=True)
    proc = subprocess.run(["git", "-C", str(repo_path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def test_worktree_manager_init_canonical_root(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorktreeManager(repo_dir)
    assert manager.canonical_root == repo_dir.resolve()
    assert manager.is_canonical_checkout(repo_dir)
    assert manager.is_canonical_checkout(repo_dir / ".git")
    assert not manager.is_canonical_checkout(tmp_path / "other")


def test_worktree_manager_canonical_collision_rejected(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorktreeManager(repo_dir)
    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.create_worktree(
            worktree_path=repo_dir,
            branch="feature-1",
            base_sha=base_sha,
        )
    assert exc_info.value.code == WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value


def test_worktree_manager_create_and_validate_worktree(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorktreeManager(repo_dir)
    wt_dir = tmp_path / "worktrees" / "wt-1"

    created = manager.create_worktree(
        worktree_path=wt_dir,
        branch="feature-wt1",
        base_sha=base_sha,
    )
    assert created == wt_dir.resolve()
    assert (wt_dir / "README.md").exists()

    # Validations should pass
    assert manager.validate_worktree_base_sha(wt_dir, base_sha)
    assert manager.validate_worktree_branch(wt_dir, "feature-wt1")
    assert manager.validate_clean_worktree(wt_dir)


def test_worktree_manager_base_sha_mismatch(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    # Create a second commit
    (repo_dir / "file2.txt").write_text("Second")
    subprocess.run(["git", "-C", str(repo_dir), "add", "file2.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "Second commit"], check=True, capture_output=True)

    fake_wrong_sha = "0000000000000000000000000000000000000000"
    manager = WorktreeManager(repo_dir)
    wt_dir = tmp_path / "worktrees" / "wt-wrong-sha"

    with pytest.raises(WorktreeSafetyError) as exc_info:
        manager.create_worktree(
            worktree_path=wt_dir,
            branch="feature-wrong",
            base_sha=fake_wrong_sha,
        )
    assert exc_info.value.code == WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value or "WORKTREE" in exc_info.value.code


def test_worktree_manager_dirty_reuse_protection(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorktreeManager(repo_dir)
    wt_dir = tmp_path / "worktrees" / "wt-dirty"

    manager.create_worktree(
        worktree_path=wt_dir,
        branch="feature-dirty",
        base_sha=base_sha,
    )

    # Make the worktree dirty
    (wt_dir / "dirty_file.txt").write_text("uncommitted content")

    with pytest.raises(WorktreeSafetyError) as exc_info:
        manager.validate_clean_worktree(wt_dir)
    assert exc_info.value.code == WorkspaceBlockingReason.WORKTREE_DIRTY_REUSE.value

    # Trying to recreate/reuse existing dirty worktree must fail closed
    with pytest.raises(WorktreeSafetyError) as exc_info2:
        manager.create_worktree(
            worktree_path=wt_dir,
            branch="feature-dirty",
            base_sha=base_sha,
        )
    assert exc_info2.value.code == WorkspaceBlockingReason.WORKTREE_DIRTY_REUSE.value


def test_worktree_manager_remove_worktree(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorktreeManager(repo_dir)
    wt_dir = tmp_path / "worktrees" / "wt-remove"

    manager.create_worktree(
        worktree_path=wt_dir,
        branch="feature-remove",
        base_sha=base_sha,
    )
    assert wt_dir.exists()

    manager.remove_worktree(wt_dir)
    assert not wt_dir.exists()


def test_worktree_manager_remove_canonical_rejected(tmp_path: Path):
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    base_sha = _init_repo(repo_dir)

    manager = WorktreeManager(repo_dir)
    with pytest.raises(WorktreeSafetyError) as exc_info:
        manager.remove_worktree(repo_dir)
    assert exc_info.value.code == WorkspaceBlockingReason.CANONICAL_CHECKOUT_PROTECTED.value
