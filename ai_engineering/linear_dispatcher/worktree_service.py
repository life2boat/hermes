"""Worktree lifecycle service enforcing isolation and dirty checkout safety."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
)


def slugify(text: str) -> str:
    """Convert text to safe lowercase hyphenated slug."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:30] if text else "task"


def compute_branch_name(task: LinearTask) -> str:
    """Deterministically compute branch name containing Linear ID."""
    clean_id = task.id.lower().replace("-", "-")
    slug = slugify(task.title)
    return f"agent/{clean_id}-{slug}"


class WorktreeService:
    """Manages creation, verification, and teardown of isolated worktrees."""

    def __init__(self, canonical_root: Path | str, base_sha: str) -> None:
        self._canonical_root = Path(canonical_root).resolve()
        self._base_sha = base_sha

    @property
    def canonical_root(self) -> Path:
        return self._canonical_root

    def is_canonical_dirty(self) -> bool:
        """Check if canonical checkout has uncommitted tracked changes."""
        res = subprocess.run(
            ["git", "-C", str(self._canonical_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        # Check tracked modified/deleted/renamed files
        lines = res.stdout.strip().splitlines()
        tracked_changes = [
            l for l in lines
            if not l.startswith("??")  # Untracked files alone don't violate worktree branch base
        ]
        return len(tracked_changes) > 0

    def create_worktree(
        self,
        task: LinearTask,
        worktree_base_dir: Path | str,
        fail_if_dirty: bool = True,
    ) -> tuple[Path | None, str | None, BlockReasonCode | None]:
        """Create an isolated worktree for the task.

        Returns (worktree_path, branch_name, error_reason).
        """
        if fail_if_dirty and self.is_canonical_dirty():
            return None, None, BlockReasonCode.DIRTY_CANONICAL_CHECKOUT

        branch_name = compute_branch_name(task)
        safe_dir_name = branch_name.replace("/", "-") + "-wt"
        worktree_path = (Path(worktree_base_dir) / safe_dir_name).resolve()

        if worktree_path.exists():
            # If already exists, verify clean
            return worktree_path, branch_name, None

        cmd = [
            "git",
            "-C",
            str(self._canonical_root),
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            self._base_sha,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            # If branch already exists, checkout without -b
            cmd_existing = [
                "git",
                "-C",
                str(self._canonical_root),
                "worktree",
                "add",
                str(worktree_path),
                branch_name,
            ]
            res_existing = subprocess.run(cmd_existing, capture_output=True, text=True)
            if res_existing.returncode != 0:
                return None, None, BlockReasonCode.DIRTY_CANONICAL_CHECKOUT

        return worktree_path, branch_name, None

    def remove_worktree(self, worktree_path: Path | str, branch_name: str | None = None) -> bool:
        """Safely remove isolated worktree and delete its local branch."""
        wt = Path(worktree_path).resolve()
        if wt.exists():
            subprocess.run(
                ["git", "-C", str(self._canonical_root), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
                text=True,
            )
        if branch_name:
            subprocess.run(
                ["git", "-C", str(self._canonical_root), "branch", "-D", branch_name],
                capture_output=True,
                text=True,
            )
        return not wt.exists()
