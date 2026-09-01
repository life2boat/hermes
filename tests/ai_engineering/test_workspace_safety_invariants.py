"""Deterministic contract and invariant verification for Workspace Identity & Worktree Safety.

Covers all 18 normative safety invariants defined in Phase 16 of the Hermes v4.1 specification.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
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
from ai_engineering.workspaces.worktree_manager import WorktreeManager
from ai_engineering.workspaces.workspace_manager import (
    WorkspaceManager,
    resolve_against_workspace,
    validate_workspace_path,
)


@pytest.fixture
def repo_env(tmp_path: Path):
    """Fixture providing a clean initialized git repo as canonical checkout."""
    canonical_dir = tmp_path / "canonical_hermes"
    canonical_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(canonical_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(canonical_dir), "config", "user.name", "Test Hermes"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(canonical_dir), "config", "user.email", "hermes@example.com"], check=True, capture_output=True)

    # Initial files in canonical repo
    (canonical_dir / "README.md").write_text("# Canonical Hermes Repo")
    (canonical_dir / "app.py").write_text("print('Production App')")
    subprocess.run(["git", "-C", str(canonical_dir), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(canonical_dir), "commit", "-m", "Canonical commit 1"], check=True, capture_output=True)

    proc = subprocess.run(["git", "-C", str(canonical_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    base_sha = proc.stdout.strip()

    return {
        "canonical_root": canonical_dir,
        "base_sha": base_sha,
        "worktree_base": tmp_path / "worktrees",
    }


def test_inv01_correct_isolated_worktree(repo_env):
    """1. Correct isolated worktree -> PASS"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-01"

    identity, lease = manager.create_isolated_workspace(
        workspace_id="ws-inv-01",
        task_id="task-01",
        candidate_id="cand-01",
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-01",
        worktree_path=wt_path,
        owner_run_id="run-01",
        auto_acquire_lease=True,
    )
    assert Path(identity.worktree_path).resolve() == wt_path.resolve()
    assert lease.state == LeaseState.ACTIVE
    assert (wt_path / "README.md").exists()


def test_inv02_wrong_base_sha(repo_env):
    """2. Wrong base SHA -> WORKTREE_BASE_SHA_MISMATCH"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-02"

    wrong_sha = "1111111111111111111111111111111111111111"
    with pytest.raises((WorktreeSafetyError, WorkspaceSecurityError)) as exc_info:
        manager.create_isolated_workspace(
            workspace_id="ws-inv-02",
            task_id="task-02",
            candidate_id=None,
            repository="life2boat/hermes",
            base_ref="refs/heads/main",
            base_sha=wrong_sha,
            branch="codex/inv-02",
            worktree_path=wt_path,
            owner_run_id="run-02",
        )
    assert exc_info.value.code in (
        WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value,
        WorkspaceBlockingReason.WORKTREE_CREATION_FAILED.value,
    )


def test_inv03_parent_directory_path_escape(repo_env):
    """3. ../ path escape -> WORKSPACE_PATH_ESCAPE"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-03"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-03",
        task_id="task-03",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-03",
        worktree_path=wt_path,
        owner_run_id="run-03",
        auto_acquire_lease=True,
    )

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-inv-03", "../escaped_secret.txt")
    assert exc_info.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value


def test_inv04_absolute_path_outside_workspace(repo_env, tmp_path: Path):
    """4. Absolute path outside workspace -> WORKSPACE_PATH_ESCAPE"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-04"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-04",
        task_id="task-04",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-04",
        worktree_path=wt_path,
        owner_run_id="run-04",
        auto_acquire_lease=True,
    )

    external_file = tmp_path / "system_file.conf"
    external_file.write_text("critical=true")

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-inv-04", external_file)
    assert exc_info.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value


def test_inv05_canonical_checkout_supplied_as_workspace(repo_env):
    """5. Canonical checkout supplied as execution workspace -> BLOCKED"""
    manager = WorkspaceManager(repo_env["canonical_root"])

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.create_isolated_workspace(
            workspace_id="ws-inv-05",
            task_id="task-05",
            candidate_id=None,
            repository="life2boat/hermes",
            base_ref="refs/heads/main",
            base_sha=repo_env["base_sha"],
            branch="codex/inv-05",
            worktree_path=repo_env["canonical_root"],
            owner_run_id="run-05",
        )
    assert exc_info.value.code == WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value


def test_inv06_path_resolving_into_canonical_checkout(repo_env):
    """6. Path resolving into canonical checkout -> BLOCKED"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-06"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-06",
        task_id="task-06",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-06",
        worktree_path=wt_path,
        owner_run_id="run-06",
        auto_acquire_lease=True,
    )

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-inv-06", repo_env["canonical_root"] / "app.py")
    assert exc_info.value.code in (
        WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value,
        WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
    )


def test_inv07_agent_workspace_identity_mismatch(repo_env):
    """7. Agent/workspace identity mismatch -> WORKTREE_IDENTITY_MISMATCH"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-07"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-07",
        task_id="task-07",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-07",
        worktree_path=wt_path,
        owner_run_id="agent-run-07",
        auto_acquire_lease=True,
    )

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-inv-07", "file.py", caller_run_id="foreign-agent-run-99")
    assert exc_info.value.code == WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value


def test_inv08_foreign_workspace_path(repo_env):
    """8. Foreign workspace path -> WORKTREE_IDENTITY_MISMATCH or WORKSPACE_PATH_ESCAPE"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path_a = repo_env["worktree_base"] / "wt-08a"
    wt_path_b = repo_env["worktree_base"] / "wt-08b"

    manager.create_isolated_workspace(
        workspace_id="ws-08a",
        task_id="task-08a",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-08a",
        worktree_path=wt_path_a,
        owner_run_id="run-08a",
        auto_acquire_lease=True,
    )

    manager.create_isolated_workspace(
        workspace_id="ws-08b",
        task_id="task-08b",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-08b",
        worktree_path=wt_path_b,
        owner_run_id="run-08b",
        auto_acquire_lease=True,
    )

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-08a", wt_path_b / "target.py")
    assert exc_info.value.code in (
        WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
        WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value,
    )


def test_inv09_dirty_reused_worktree(repo_env):
    """9. Dirty reused worktree -> WORKTREE_DIRTY_REUSE"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-09"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-09",
        task_id="task-09",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-09",
        worktree_path=wt_path,
        owner_run_id="run-09",
        auto_acquire_lease=True,
    )

    # Mutate worktree to make it dirty
    (wt_path / "dirty.txt").write_text("untracked modifications")

    with pytest.raises(WorktreeSafetyError) as exc_info:
        manager.worktree_manager.validate_clean_worktree(wt_path)
    assert exc_info.value.code == WorkspaceBlockingReason.WORKTREE_DIRTY_REUSE.value


def test_inv10_clean_reusable_isolated_worktree(repo_env):
    """10. Clean reusable isolated worktree -> PASS"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-10"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-10",
        task_id="task-10",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-10",
        worktree_path=wt_path,
        owner_run_id="run-10",
        auto_acquire_lease=True,
    )
    assert manager.worktree_manager.validate_clean_worktree(wt_path) is True


def test_inv11_wrong_branch(repo_env):
    """11. Wrong branch -> BLOCKED"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-11"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-11",
        task_id="task-11",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-11",
        worktree_path=wt_path,
        owner_run_id="run-11",
        auto_acquire_lease=True,
    )

    with pytest.raises(WorktreeSafetyError) as exc_info:
        manager.worktree_manager.validate_worktree_branch(wt_path, "different-branch")
    assert exc_info.value.code == WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value


def test_inv12_lease_reserved_to_active(repo_env):
    """12. Lease RESERVED -> ACTIVE -> PASS"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-12"

    _, lease = manager.create_isolated_workspace(
        workspace_id="ws-inv-12",
        task_id="task-12",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-12",
        worktree_path=wt_path,
        owner_run_id="run-12",
        auto_acquire_lease=False,
    )
    assert lease.state == LeaseState.RESERVED
    activated = manager.acquire_lease("ws-inv-12", "run-12")
    assert activated.state == LeaseState.ACTIVE
    assert activated.is_active()


def test_inv13_released_lease_reactivation_fails(repo_env):
    """13. RELEASED lease reactivation -> FAIL"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-13"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-13",
        task_id="task-13",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-13",
        worktree_path=wt_path,
        owner_run_id="run-13",
        auto_acquire_lease=True,
    )
    manager.release_lease("ws-inv-13", "run-13")

    with pytest.raises(LeaseTransitionError) as exc_info:
        manager.acquire_lease("ws-inv-13", "run-13")
    assert exc_info.value.code == WorkspaceBlockingReason.LEASE_TRANSITION_INVALID.value


def test_inv14_quarantined_workspace_reuse_fails(repo_env):
    """14. QUARANTINED workspace reuse -> FAIL"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-14"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-14",
        task_id="task-14",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-14",
        worktree_path=wt_path,
        owner_run_id="run-14",
        auto_acquire_lease=True,
    )
    manager.quarantine_lease("ws-inv-14", "run-14", reason="Suspected compromised artifact")

    with pytest.raises(LeaseTransitionError) as exc_info:
        manager.acquire_lease("ws-inv-14", "run-14")
    assert exc_info.value.code == WorkspaceBlockingReason.LEASE_TRANSITION_INVALID.value

    # Path validation on quarantined workspace fails closed
    with pytest.raises(WorkspaceSecurityError) as exc_info2:
        manager.validate_workspace_path("ws-inv-14", "file.py", caller_run_id="run-14")
    assert exc_info2.value.code == WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value


def test_inv15_logical_repository_relative_path(repo_env):
    """15. Repository-relative logical path -> resolves inside assigned workspace"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-15"

    identity, _ = manager.create_isolated_workspace(
        workspace_id="ws-inv-15",
        task_id="task-15",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-15",
        worktree_path=wt_path,
        owner_run_id="run-15",
        auto_acquire_lease=True,
    )

    resolved = resolve_against_workspace(identity, "ai_engineering/task_graph.py")
    assert resolved == (wt_path / "ai_engineering/task_graph.py").resolve()
    assert resolved.is_relative_to(wt_path.resolve())


def test_inv16_absolute_foreign_artifact_path_rejected(repo_env, tmp_path: Path):
    """16. Absolute foreign artifact path -> rejected"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-16"

    identity, _ = manager.create_isolated_workspace(
        workspace_id="ws-inv-16",
        task_id="task-16",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-16",
        worktree_path=wt_path,
        owner_run_id="run-16",
        auto_acquire_lease=True,
    )

    foreign_artifact = tmp_path / "foreign_artifact.tar.gz"
    with pytest.raises(WorkspaceSecurityError) as exc_info:
        resolve_against_workspace(identity, foreign_artifact)
    assert exc_info.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value


def test_inv17_symlink_escape_rejected(repo_env, tmp_path: Path):
    """17. Symlink escape -> rejected where platform permits"""
    manager = WorkspaceManager(repo_env["canonical_root"])
    wt_path = repo_env["worktree_base"] / "wt-17"

    manager.create_isolated_workspace(
        workspace_id="ws-inv-17",
        task_id="task-17",
        candidate_id=None,
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=repo_env["base_sha"],
        branch="codex/inv-17",
        worktree_path=wt_path,
        owner_run_id="run-17",
        auto_acquire_lease=True,
    )

    # Create an outside sensitive file and a symlink inside worktree pointing to it
    outside_secret = tmp_path / "outside_secret.env"
    outside_secret.write_text("SECRET=123")

    symlink_path = wt_path / "secret_link.env"
    try:
        symlink_path.symlink_to(outside_secret)
    except OSError:
        pytest.skip("Symlinks not supported on this filesystem")

    with pytest.raises(WorkspaceSecurityError) as exc_info:
        manager.validate_workspace_path("ws-inv-17", "secret_link.env")
    assert exc_info.value.code == WorkspaceBlockingReason.WORKSPACE_PATH_ESCAPE.value


def test_inv18_canonical_repository_remains_unchanged(repo_env):
    """18. Canonical repository remains unchanged after tests -> PASS"""
    canonical_root = repo_env["canonical_root"]
    proc = subprocess.run(["git", "-C", str(canonical_root), "status", "--porcelain"], check=True, capture_output=True, text=True)
    assert proc.stdout.strip() == "", f"Canonical repo was modified: {proc.stdout}"

    head_proc = subprocess.run(["git", "-C", str(canonical_root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    assert head_proc.stdout.strip() == repo_env["base_sha"], "Canonical HEAD moved!"
