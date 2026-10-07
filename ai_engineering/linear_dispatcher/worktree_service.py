"""Worktree lifecycle service enforcing isolation and dirty checkout safety."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
)

SAFE_GIT_OPTS: list[str] = [
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.sshCommand=false",
    "-c", "core.askPass=false",
    "-c", "core.editor=false",
    "-c", "core.pager=cat",
    "-c", "credential.helper=",
    "-c", "diff.external=",
    "-c", "diff.command=",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.allow=https:ssh:file",
    "-c", "core.gitProxy=",
    "-c", "uploadpack.packObjectsHook=",
    "-c", "filter.lfs.smudge=",
    "-c", "filter.lfs.clean=",
    "-c", "filter.lfs.process=",
    "-c", "filter.lfs.required=false",
    "-c", "core.autocrlf=input",
    "-c", "core.safecrlf=false",
]


def get_clean_git_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base_env or os.environ)
    for k in list(env.keys()):
        if k.startswith("GIT_") and k not in ("GIT_DIR", "GIT_WORK_TREE"):
            env.pop(k, None)
    for k in ("PAGER", "EDITOR", "VISUAL"):
        env.pop(k, None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_SSH_COMMAND"] = "false"
    env["GIT_ASKPASS"] = "false"
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh:file"
    return env


def is_canonical_remote_url(
    url: str,
    canonical_repo: str = "life2boat/hermes",
    canonical_root: Path | str | None = None,
) -> bool:
    clean = url.strip()
    if clean.endswith(".git"):
        clean = clean[:-4]
    if clean in (
        f"https://github.com/{canonical_repo}",
        f"git@github.com:{canonical_repo}",
        f"ssh://git@github.com/{canonical_repo}",
    ):
        return True
    if canonical_root is not None:
        try:
            clean_path = clean
            if clean_path.startswith("file://"):
                clean_path = clean_path[7:]
            if Path(clean_path).resolve() == Path(canonical_root).resolve():
                return True
        except Exception:
            pass
    return False


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

    def __init__(
        self,
        canonical_root: Path | str,
        base_sha: str,
        canonical_remote: str = "github",
        canonical_repo: str = "life2boat/hermes",
    ) -> None:
        self._canonical_root = Path(canonical_root).resolve()
        self._base_sha = base_sha
        self._canonical_remote = canonical_remote
        self._canonical_repo = canonical_repo

    def _run_git(
        self, args: list[str], cwd: Path | str, **kwargs
    ) -> subprocess.CompletedProcess:
        env = get_clean_git_env(kwargs.pop("env", None))
        cmd = ["git"] + SAFE_GIT_OPTS + ["-C", str(cwd)] + args
        return subprocess.run(cmd, env=env, **kwargs)

    @property
    def canonical_root(self) -> Path:
        return self._canonical_root

    def is_canonical_dirty(self) -> bool:
        """Check if canonical checkout has uncommitted tracked changes."""
        res = self._run_git(["status", "--porcelain"], self._canonical_root, capture_output=True, text=True)
        if res.returncode != 0:
            return True
        lines = res.stdout.strip().splitlines()
        tracked_changes = [
            l for l in lines
            if not l.startswith("??")
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
        chk_obj = self._run_git(
            ["cat-file", "-e", f"{self._base_sha}^{{commit}}"],
            self._canonical_root,
            capture_output=True,
        )
        if chk_obj.returncode != 0:
            # Validate canonical remote URL before fetching
            chk_remote = self._run_git(
                ["remote", "get-url", self._canonical_remote],
                self._canonical_root,
                capture_output=True,
                text=True,
            )
            if chk_remote.returncode != 0 or not is_canonical_remote_url(
                chk_remote.stdout.strip(), self._canonical_repo, self._canonical_root
            ):
                return None, None, BlockReasonCode.CANONICAL_ROOT_INVALID

            # Fetch base object from remote
            fetch_res = self._run_git(
                ["fetch", self._canonical_remote, self._base_sha],
                self._canonical_root,
                capture_output=True,
            )
            if fetch_res.returncode != 0:
                return None, None, BlockReasonCode.UNKNOWN_BASE_COMMIT

        if worktree_path.exists():
            # If already exists, verify it is a registered worktree of canonical root with matching branch and base
            chk_common = self._run_git(
                ["rev-parse", "--git-common-dir"],
                worktree_path,
                capture_output=True,
                text=True,
            )
            canonical_git_dir = self._run_git(
                ["rev-parse", "--git-dir"],
                self._canonical_root,
                capture_output=True,
                text=True,
            )
            if chk_common.returncode != 0 or canonical_git_dir.returncode != 0:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            raw_common = chk_common.stdout.strip()
            p_common = Path(raw_common)
            common_resolved = (p_common if p_common.is_absolute() else (worktree_path / p_common)).resolve()

            raw_canonical = canonical_git_dir.stdout.strip()
            p_canonical = Path(raw_canonical)
            canonical_resolved = (p_canonical if p_canonical.is_absolute() else (Path(self._canonical_root) / p_canonical)).resolve()

            if common_resolved != canonical_resolved:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            # Verify worktree is registered in canonical worktree list
            chk_wt_list = self._run_git(
                ["worktree", "list", "--porcelain"],
                self._canonical_root,
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
            chk_branch = self._run_git(
                ["symbolic-ref", "--short", "HEAD"],
                worktree_path,
                capture_output=True,
                text=True,
            )
            if chk_branch.returncode != 0 or chk_branch.stdout.strip() != branch_name:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            # Verify clean status and matching base HEAD
            chk_git = self._run_git(
                ["status", "--porcelain"],
                worktree_path,
                capture_output=True,
                text=True,
            )
            if chk_git.returncode != 0 or chk_git.stdout.strip():
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            chk_head = self._run_git(
                ["rev-parse", "HEAD"],
                worktree_path,
                capture_output=True,
                text=True,
            )
            if chk_head.returncode != 0 or chk_head.stdout.strip() != self._base_sha:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            return worktree_path, branch_name, None

        cmd = [
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            self._base_sha,
        ]
        res = self._run_git(cmd, self._canonical_root, capture_output=True, text=True)
        if res.returncode != 0:
            # Check if branch already exists pointing to exact base_sha
            branch_head = self._run_git(
                ["rev-parse", branch_name],
                self._canonical_root,
                capture_output=True,
                text=True,
            )
            if branch_head.returncode == 0 and branch_head.stdout.strip() == self._base_sha:
                cmd_existing = [
                    "worktree",
                    "add",
                    str(worktree_path),
                    branch_name,
                ]
                res_existing = self._run_git(cmd_existing, self._canonical_root, capture_output=True, text=True)
                if res_existing.returncode != 0:
                    return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY
            else:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

        return worktree_path, branch_name, None

    def remove_worktree(self, worktree_path: Path | str, branch_name: str | None = None) -> bool:
        """Safely remove isolated worktree and delete its local branch."""
        wt = Path(worktree_path).resolve()
        if wt.exists():
            self._run_git(
                ["worktree", "remove", "--force", str(wt)],
                self._canonical_root,
                capture_output=True,
                text=True,
            )
        if branch_name:
            self._run_git(
                ["branch", "-D", branch_name],
                self._canonical_root,
                capture_output=True,
                text=True,
            )
        return not wt.exists()
