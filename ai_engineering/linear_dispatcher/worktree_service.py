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
        if res.returncode != 0:
            # If status check fails, fail closed
            return True
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

        # Ensure base_sha object is available in local object store
        chk_obj = subprocess.run(
            ["git", "-C", str(self._canonical_root), "cat-file", "-e", f"{self._base_sha}^{{commit}}"],
            capture_output=True,
        )
        if chk_obj.returncode != 0:
            # Fetch base object from remote
            fetch_res = subprocess.run(
                ["git", "-C", str(self._canonical_root), "fetch", "github", self._base_sha],
                capture_output=True,
            )
            if fetch_res.returncode != 0:
                return None, None, BlockReasonCode.UNKNOWN_BASE_COMMIT

        if worktree_path.exists():
            # If already exists, verify it is a registered worktree of canonical root with matching branch and base
            chk_common = subprocess.run(
                ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-C", str(worktree_path), "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
            )
            canonical_git_dir = subprocess.run(
                ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-C", str(self._canonical_root), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
            )
            if chk_common.returncode != 0 or canonical_git_dir.returncode != 0:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY
            common_resolved = Path(chk_common.stdout.strip()).resolve()
            if not common_resolved.is_absolute():
                common_resolved = (worktree_path / common_resolved).resolve()
            canonical_resolved = Path(canonical_git_dir.stdout.strip()).resolve()
            if not canonical_resolved.is_absolute():
                canonical_resolved = (Path(self._canonical_root) / canonical_resolved).resolve()
            if common_resolved != canonical_resolved:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            # Verify worktree is registered in canonical worktree list
            chk_wt_list = subprocess.run(
                ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-C", str(self._canonical_root), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
            )
            if chk_wt_list.returncode != 0:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY
            wt_lines = chk_wt_list.stdout.splitlines()
            registered_wts = [
                Path(line.split(maxsplit=1)[1].strip()).resolve()
                for line in wt_lines
                if line.startswith("worktree ")
            ]
            if worktree_path.resolve() not in registered_wts:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            # Verify task branch ownership
            chk_branch = subprocess.run(
                ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-C", str(worktree_path), "symbolic-ref", "--short", "HEAD"],
                capture_output=True,
                text=True,
            )
            if chk_branch.returncode != 0 or chk_branch.stdout.strip() != branch_name:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            # Verify clean status and matching base HEAD
            chk_git = subprocess.run(
                ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-C", str(worktree_path), "status", "--porcelain"],
                capture_output=True,
                text=True,
            )
            if chk_git.returncode != 0 or chk_git.stdout.strip():
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            chk_head = subprocess.run(
                ["git", "-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null", "-C", str(worktree_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            if chk_head.returncode != 0 or chk_head.stdout.strip() != self._base_sha:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

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
            # Check if branch already exists pointing to exact base_sha
            branch_head = subprocess.run(
                ["git", "-C", str(self._canonical_root), "rev-parse", branch_name],
                capture_output=True,
                text=True,
            )
            if branch_head.returncode == 0 and branch_head.stdout.strip() == self._base_sha:
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
                    return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY
            else:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

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
