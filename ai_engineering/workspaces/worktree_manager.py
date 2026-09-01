"""Git-native worktree operations and safety validation for Hermes workspaces."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from ai_engineering.workspaces.workspace_contracts import (
    WorkspaceBlockingReason,
    WorkspaceSecurityError,
    WorktreeSafetyError,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


def _run_git_command(
    cwd: Path,
    *args: str,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute git in a subprocess safely without shell expansion."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise WorktreeSafetyError("GIT_COMMAND_UNAVAILABLE", f"git execution failed: {exc}") from exc
    if result.returncode != 0 and not allow_failure:
        err_msg = result.stderr.strip() or f"git exited with code {result.returncode}"
        raise WorktreeSafetyError("GIT_COMMAND_FAILED", err_msg)
    return result


class WorktreeManager:
    """Safe lifecycle manager for Git worktrees."""

    def __init__(self, canonical_root: Path | str) -> None:
        raw_path = Path(canonical_root)
        if raw_path.is_symlink():
            raise WorktreeSafetyError("CANONICAL_ROOT_UNSAFE", "canonical_root cannot be a symlink")
        try:
            self._canonical_root = raw_path.resolve()
        except OSError as exc:
            raise WorktreeSafetyError("CANONICAL_ROOT_UNSAFE", f"Could not resolve canonical_root: {exc}") from exc

        if not self._canonical_root.is_dir():
            raise WorktreeSafetyError("CANONICAL_ROOT_UNSAFE", f"canonical_root is not a directory: {self._canonical_root}")

        # Verify git toplevel matches canonical_root
        proc = _run_git_command(self._canonical_root, "rev-parse", "--show-toplevel", allow_failure=True)
        if proc.returncode != 0:
            raise WorktreeSafetyError("CANONICAL_ROOT_NOT_GIT", f"Not a git repository: {self._canonical_root}")
        top_level = Path(proc.stdout.strip()).resolve()
        if top_level != self._canonical_root:
            raise WorktreeSafetyError("CANONICAL_ROOT_MISMATCH", f"Top-level {top_level} != {self._canonical_root}")

    @property
    def canonical_root(self) -> Path:
        """Return the authoritative canonical repository root path."""
        return self._canonical_root

    def is_canonical_checkout(self, candidate_path: Path | str) -> bool:
        """Check if a candidate path is the canonical checkout or inside its administrative metadata."""
        try:
            resolved = Path(candidate_path).resolve()
        except OSError:
            return False
        if resolved == self._canonical_root:
            return True
        # Check if inside .git of canonical root
        canonical_git = (self._canonical_root / ".git").resolve()
        try:
            if resolved == canonical_git or resolved.is_relative_to(canonical_git):
                return True
        except ValueError:
            pass
        return False

    def validate_clean_worktree(self, worktree_path: Path | str) -> bool:
        """Ensure the worktree has no uncommitted tracked or untracked changes."""
        wt = Path(worktree_path).resolve()
        if not wt.is_dir():
            raise WorktreeSafetyError("WORKTREE_DIR_MISSING", f"Worktree directory missing: {wt}")
        proc = _run_git_command(wt, "status", "--porcelain")
        if proc.stdout.strip():
            raise WorktreeSafetyError(
                WorkspaceBlockingReason.WORKTREE_DIRTY_REUSE.value,
                f"Worktree has uncommitted modifications:\n{proc.stdout.strip()}",
            )
        return True

    def validate_worktree_base_sha(self, worktree_path: Path | str, expected_base_sha: str) -> bool:
        """Verify the worktree HEAD matches the required base commit SHA."""
        if not _SHA_RE.match(expected_base_sha):
            raise WorktreeSafetyError("INVALID_BASE_SHA", f"Invalid base SHA: {expected_base_sha!r}")
        wt = Path(worktree_path).resolve()
        proc = _run_git_command(wt, "rev-parse", "HEAD")
        actual_sha = proc.stdout.strip().lower()
        if actual_sha != expected_base_sha.lower():
            raise WorktreeSafetyError(
                WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value,
                f"Worktree HEAD {actual_sha} does not match expected base SHA {expected_base_sha.lower()}",
            )
        return True

    def validate_worktree_branch(self, worktree_path: Path | str, expected_branch: str) -> bool:
        """Verify the worktree is checked out to the expected branch."""
        wt = Path(worktree_path).resolve()
        proc = _run_git_command(wt, "branch", "--show-current", allow_failure=True)
        current_branch = proc.stdout.strip()
        if current_branch != expected_branch:
            raise WorktreeSafetyError(
                WorkspaceBlockingReason.WORKTREE_IDENTITY_MISMATCH.value,
                f"Worktree branch {current_branch!r} does not match expected {expected_branch!r}",
            )
        return True

    def create_worktree(
        self,
        *,
        worktree_path: Path | str,
        branch: str,
        base_sha: str,
        base_ref: str | None = None,
    ) -> Path:
        """Create an isolated git worktree backed by a specific branch and base SHA."""
        if not isinstance(branch, str) or not branch.strip():
            raise WorktreeSafetyError("INVALID_BRANCH", "branch must be a non-empty string")
        if not isinstance(base_sha, str) or not _SHA_RE.match(base_sha):
            raise WorktreeSafetyError(
                WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value,
                f"Invalid base SHA: {base_sha!r}",
            )

        target_path = Path(worktree_path)
        # Protect canonical repository
        if self.is_canonical_checkout(target_path):
            raise WorkspaceSecurityError(
                WorkspaceBlockingReason.CANONICAL_CHECKOUT_COLLISION.value,
                f"Cannot use canonical checkout as isolated worktree: {target_path}",
            )

        resolved_target = target_path.resolve()
        # Verify parent directory exists or can be created
        parent_dir = resolved_target.parent
        parent_dir.mkdir(parents=True, exist_ok=True)

        if resolved_target.exists():
            # Check if this is already an active worktree matching the expected parameters
            if (resolved_target / ".git").exists():
                self.validate_worktree_base_sha(resolved_target, base_sha)
                self.validate_worktree_branch(resolved_target, branch)
                self.validate_clean_worktree(resolved_target)
                return resolved_target
            raise WorktreeSafetyError(
                WorkspaceBlockingReason.WORKTREE_CREATION_FAILED.value,
                f"Path {resolved_target} already exists and is not a valid worktree",
            )

        # Check if the branch already exists in canonical repo
        branch_check = _run_git_command(
            self._canonical_root,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            allow_failure=True,
        )

        if branch_check.returncode == 0:
            existing_commit = branch_check.stdout.strip().lower()
            if existing_commit != base_sha.lower():
                raise WorktreeSafetyError(
                    WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value,
                    f"Branch {branch!r} already exists at {existing_commit} != base SHA {base_sha.lower()}",
                )
            # Add existing branch
            proc = _run_git_command(
                self._canonical_root,
                "worktree",
                "add",
                str(resolved_target),
                branch,
                allow_failure=True,
            )
        else:
            # Create new branch starting at base_sha
            proc = _run_git_command(
                self._canonical_root,
                "worktree",
                "add",
                "-b",
                branch,
                str(resolved_target),
                base_sha,
                allow_failure=True,
            )

        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or f"git worktree add failed with code {proc.returncode}"
            raise WorktreeSafetyError(
                WorkspaceBlockingReason.WORKTREE_CREATION_FAILED.value,
                err_msg,
            )

        # Validate post-creation invariants
        self.validate_worktree_base_sha(resolved_target, base_sha)
        self.validate_worktree_branch(resolved_target, branch)
        self.validate_clean_worktree(resolved_target)

        return resolved_target

    def remove_worktree(self, worktree_path: Path | str, *, force: bool = False) -> None:
        """Remove a git worktree safely."""
        target_path = Path(worktree_path).resolve()
        if self.is_canonical_checkout(target_path):
            raise WorktreeSafetyError(
                WorkspaceBlockingReason.CANONICAL_CHECKOUT_PROTECTED.value,
                "Refusing to remove canonical repository checkout",
            )
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(target_path))
        _run_git_command(self._canonical_root, *args)

    def prune_worktrees(self) -> None:
        """Prune dead worktree administrative references."""
        _run_git_command(self._canonical_root, "worktree", "prune")
