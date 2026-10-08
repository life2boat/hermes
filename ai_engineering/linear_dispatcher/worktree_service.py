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
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.sshCommand=false",
    "-c", "core.askPass=false",
    "-c", "core.editor=false",
    "-c", "core.pager=cat",
    "-c", "credential.helper=",
    "-c", "diff.external=",
    "-c", "diff.command=",
    "-c", "diff.*.command=",
    "-c", "diff.*.textconv=",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.allow=https:ssh:file",
    "-c", "core.gitProxy=",
    "-c", "uploadpack.packObjectsHook=",
    "-c", "filter.*.process=",
    "-c", "filter.*.clean=",
    "-c", "filter.*.smudge=",
    "-c", "filter.*.required=false",
    "-c", "core.attributesFile=/dev/null",
    "-c", "core.autocrlf=input",
    "-c", "core.safecrlf=false",
]


TRUSTED_GIT_ENV_VARS: set[str] = {
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERPROFILE",
    "ALLUSERSPROFILE",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMDATA",
    "COMMONPROGRAMFILES",
    "COMMONPROGRAMFILES(X86)",
    "APPDATA",
    "LOCALAPPDATA",
    "HOME",
    "USER",
    "USERNAME",
    "LOGNAME",
    "SHELL",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
}


class WorktreeIsolationError(RuntimeError):
    """Raised when repository isolation verification or filter neutralization fails."""


def get_effective_filter_opts(repo_path: Path | str) -> list[str]:
    """Inspect repository configuration and attributes to discover and neutralize any filter or diff drivers."""
    rp = Path(repo_path)
    if not rp.exists():
        return []

    discovered_drivers: set[str] = set()
    clean_env = get_clean_git_env()

    # 1. Inspect repository and worktree git config for custom filter and diff drivers using sanitized env
    try:
        res = subprocess.run(
            ["git", "-C", str(rp), "config", "--get-regexp", r"^(filter|diff)\."],
            capture_output=True,
            text=True,
            timeout=5,
            env=clean_env,
        )
        if res.returncode not in (0, 1):
            raise WorktreeIsolationError(
                f"git config query failed with exit code {res.returncode}: {res.stderr.strip()}"
            )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                parts = line.split(maxsplit=1)
                if parts:
                    key = parts[0]
                    if key.startswith("filter."):
                        rest = key[len("filter."):]
                        if "." in rest:
                            name, _ = rest.rsplit(".", 1)
                            discovered_drivers.add(name)
                    elif key.startswith("diff."):
                        rest = key[len("diff."):]
                        if "." in rest:
                            name, _ = rest.rsplit(".", 1)
                            discovered_drivers.add(name)
    except subprocess.SubprocessError as exc:
        raise WorktreeIsolationError(f"git config execution error: {exc}") from exc

    # 2. Inspect all .gitattributes across the worktree tree and .git/info/attributes for driver names
    attr_files: list[Path] = []
    info_attr = rp / ".git" / "info" / "attributes"
    if info_attr.is_file():
        attr_files.append(info_attr)

    try:
        if rp.is_dir():
            for root, dirs, files in os.walk(rp, onerror=lambda _: None):
                if ".git" in dirs:
                    dirs.remove(".git")
                if ".gitattributes" in files:
                    attr_files.append(Path(root) / ".gitattributes")
    except Exception as exc:
        raise WorktreeIsolationError(f"Failed scanning .gitattributes in {rp}: {exc}") from exc

    for af in attr_files:
        try:
            if not af.is_file():
                continue
            content = af.read_text(encoding="utf-8", errors="ignore")
            for mf in re.finditer(r"filter=([^\s]+)", content):
                name = mf.group(1).strip()
                if name and name != "false" and name != "unset":
                    discovered_drivers.add(name)
            for md in re.finditer(r"diff=([^\s]+)", content):
                name = md.group(1).strip()
                if name and name != "false" and name != "unset":
                    discovered_drivers.add(name)
        except (FileNotFoundError, PermissionError):
            continue
        except Exception as exc:
            raise WorktreeIsolationError(f"Failed reading attributes file {af}: {exc}") from exc

    # 3. Neutralize all discovered drivers: empty clean, smudge, process, command, textconv, and required=false
    configs: list[str] = []
    for name in sorted(discovered_drivers):
        configs.extend([
            f"filter.{name}.clean=",
            f"filter.{name}.smudge=",
            f"filter.{name}.process=",
            f"filter.{name}.required=false",
            f"diff.{name}.command=",
            f"diff.{name}.textconv=",
        ])

    opts: list[str] = []
    for entry in configs:
        opts.extend(["-c", entry])
    return opts


def get_clean_git_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Retain only explicitly trusted environment variables, sanitizing all repository control variables."""
    source = dict(base_env if base_env is not None else os.environ)
    trusted_upper = {k.upper() for k in TRUSTED_GIT_ENV_VARS}
    clean_env: dict[str, str] = {}

    for k, v in source.items():
        k_upper = k.upper()
        # Strictly forbid any git repository control or hook variables
        if k_upper.startswith("GIT_"):
            continue
        # Strip pagers, editors, visual
        if k_upper in ("PAGER", "EDITOR", "VISUAL"):
            continue
        # Only retain explicitly trusted environment variables
        if k_upper in trusted_upper:
            clean_env[k] = v

    # Explicitly enforce safe Git configuration boundaries
    clean_env["GIT_CONFIG_NOSYSTEM"] = "1"
    clean_env["GIT_CONFIG_GLOBAL"] = os.devnull
    clean_env["GIT_CONFIG_SYSTEM"] = os.devnull
    clean_env["GIT_SSH_COMMAND"] = "false"
    clean_env["GIT_ASKPASS"] = "false"
    clean_env["GIT_TERMINAL_PROMPT"] = "0"
    clean_env["GIT_ALLOW_PROTOCOL"] = "https:ssh:file"
    clean_env["NoDefaultCurrentDirectoryInExePath"] = "1"
    clean_env["PYTHONNOUSERSITE"] = "1"
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"

    # Clean PATH: strip empty elements, relative directories, and current directory
    path_key = next((k for k in clean_env if k.upper() == "PATH"), None)
    if path_key and clean_env[path_key]:
        safe_dirs = [
            d for d in clean_env[path_key].split(os.pathsep)
            if d and Path(d).is_absolute() and not Path(d).name.startswith(".")
        ]
        clean_env[path_key] = os.pathsep.join(safe_dirs)
    return clean_env


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
        dynamic_opts = get_effective_filter_opts(cwd)
        cmd = ["git"] + SAFE_GIT_OPTS + dynamic_opts + ["-C", str(cwd)] + args
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
            canonical_git_common = self._run_git(
                ["rev-parse", "--git-common-dir"],
                self._canonical_root,
                capture_output=True,
                text=True,
            )
            if chk_common.returncode != 0 or canonical_git_common.returncode != 0:
                return None, None, BlockReasonCode.STALE_WORKTREE_DIRTY

            raw_common = chk_common.stdout.strip()
            p_common = Path(raw_common)
            common_resolved = (p_common if p_common.is_absolute() else (worktree_path / p_common)).resolve()

            raw_canonical = canonical_git_common.stdout.strip()
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
