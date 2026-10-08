"""Task execution contracts and adapters for Hermes Linear Dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Protocol, Sequence
import uuid
import yaml

from ai_engineering.linear_dispatcher.contracts import LinearTask
from ai_engineering.linear_dispatcher.worktree_service import (
    get_clean_git_env,
    get_effective_filter_opts,
)



@dataclass(frozen=True, slots=True)
class ExecutionResult:
    status: str  # "SUCCESS", "FAILED", "BLOCKED"
    changed_files: tuple[str, ...]
    execution_id: str
    executor_name: str
    evidence: dict[str, Any] = field(default_factory=dict)
    error_reason: str | None = None


class ITaskExecutor(Protocol):
    """Protocol defining the task execution contract."""

    def execute(
        self,
        task: LinearTask,
        worktree_path: Path,
        canonical_base_sha: str,
    ) -> ExecutionResult:
        """Execute task mutations confined to the specified worktree."""
        ...

    def health(self) -> bool:
        """Return True if executor dependencies and runtimes are available."""
        ...

    def execution_ready(self) -> bool:
        """Return True if executor is functionally capable of executing tasks safely."""
        ...


class CodexTaskExecutor:
    """Production task executor invoking Codex non-interactively within the worktree sandbox."""

    def __init__(
        self,
        codex_bin: str | None = None,
        timeout_sec: int = 180,
        model: str | None = None,
        allow_windows_unsandboxed: bool = False,
    ) -> None:
        self.codex_bin = codex_bin or shutil.which("codex")
        self.timeout_sec = timeout_sec
        self.model = model
        self.allow_windows_unsandboxed = allow_windows_unsandboxed

    def health(self) -> bool:
        if sys.platform == "win32" and not self.allow_windows_unsandboxed:
            return False
        if not self.codex_bin:
            return False
        # Verify the binary runs
        try:
            res = subprocess.run(
                [self.codex_bin, "exec", "--help"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return res.returncode == 0
        except Exception:
            return False

    def execution_ready(self) -> bool:
        """Return False for CodexTaskExecutor as host unsandboxed execution is not production ready."""
        return False

    def execute(
        self,
        task: LinearTask,
        worktree_path: Path,
        canonical_base_sha: str,
    ) -> ExecutionResult:
        exec_id = f"exec-{uuid.uuid4().hex[:12]}"
        wt = Path(worktree_path).resolve()
        if not wt.exists() or not wt.is_dir():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                error_reason="INVALID_WORKTREE_PATH",
            )

        if sys.platform == "win32" and not self.allow_windows_unsandboxed:
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                error_reason="WINDOWS_CODEX_UNSUPPORTED",
            )

        if not self.health():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                error_reason="CODEX_EXECUTOR_UNAVAILABLE",
            )

        # Build English task prompt
        prompt = (
            f"Objective: Implement the task described below in this repository worktree.\n\n"
            f"Linear Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description:\n{task.description}\n\n"
            f"Canonical Base SHA: {canonical_base_sha}\n"
            f"Constraints:\n"
            f"- Confine all creations and edits to this repository worktree only ({wt}).\n"
            f"- Do not modify production credentials, databases, Qdrant, or any path outside this directory.\n"
            f"- Do not commit or push to remote.\n"
            f"- Make only the minimal necessary coherent code and documentation changes.\n"
        )

        cmd = [
            self.codex_bin,
            "exec",
            "-C",
            str(wt),
            "--approve-for-me",
            "--ephemeral",
        ]
        if self.model:
            cmd.extend(["-m", self.model])
        cmd.append(prompt)

        start_time = time.time()
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(wt),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
            duration = time.time() - start_time
            if proc.returncode != 0:
                return ExecutionResult(
                    status="FAILED",
                    changed_files=(),
                    execution_id=exec_id,
                    executor_name="CodexTaskExecutor",
                    evidence={
                        "duration_sec": duration,
                        "exit_code": proc.returncode,
                        "stderr": proc.stderr[-1000:] if proc.stderr else "",
                    },
                    error_reason=f"CODEX_EXEC_FAILED_EXIT_{proc.returncode}",
                )
        except subprocess.TimeoutExpired as exc:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                evidence={"duration_sec": duration, "timed_out": True},
                error_reason="CODEX_EXEC_TIMEOUT",
            )
        except Exception as exc:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                evidence={"duration_sec": duration, "error": str(exc)},
                error_reason="CODEX_EXEC_EXCEPTION",
            )

        # Independent git-derived changes check
        changed_files = self._detect_changed_files(wt)
        if not changed_files:
            return ExecutionResult(
                status="BLOCKED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                evidence={"duration_sec": duration, "exit_code": proc.returncode},
                error_reason="NO_CHANGES_PRODUCED",
            )

        return ExecutionResult(
            status="SUCCESS",
            changed_files=changed_files,
            execution_id=exec_id,
            executor_name="CodexTaskExecutor",
            evidence={
                "duration_sec": duration,
                "exit_code": proc.returncode,
                "changed_count": len(changed_files),
            },
        )

    def _detect_changed_files(self, worktree_path: Path) -> tuple[str, ...]:
        """Detect actual modified, added, or untracked files relative to worktree."""
        try:
            clean_env = get_clean_git_env()
            dynamic_opts = get_effective_filter_opts(worktree_path)
            cmd = [
                "git",
                "-c", "core.fsmonitor=",
                "-c", "core.hooksPath=/dev/null",
                "-c", "core.attributesFile=/dev/null",
                *dynamic_opts,
                "-C", str(worktree_path),
                "status",
                "--porcelain",
            ]
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                env=clean_env,
            )
            files = []
            for line in res.stdout.splitlines():
                if not line.strip():
                    continue
                if len(line) >= 4 and line[2] == " ":
                    path_part = line[3:].strip()
                else:
                    parts = line.strip().split(maxsplit=1)
                    path_part = parts[1] if len(parts) > 1 else parts[0]
                target_path = path_part.split(" -> ")[-1].strip('"')
                files.append(target_path)
            return tuple(sorted(set(files)))
        except Exception:
            return ()


class LinuxCodexTaskExecutor:
    """Production task executor running Codex inside a minimal allowlist Bubblewrap sandbox.

    Enforces strict filesystem and secret isolation:
    - Root filesystem is NOT mounted (--ro-bind / / is strictly forbidden).
    - Minimal allowlist mounts only:
      - System binaries & libraries (/usr, /bin -> usr/bin, /lib -> usr/lib, /lib64 -> usr/lib64, /sbin -> usr/sbin)
      - Resolv.conf for DNS (/mnt/wsl/resolv.conf or /etc/resolv.conf)
      - CA certificates (/etc/ssl, /etc/ca-certificates)
      - NSS & core config (/etc/nsswitch.conf, /etc/hosts, /etc/passwd, /etc/group, /etc/alternatives)
      - Dev and proc (/dev, /proc)
    - Ephemeral tmpfs (/tmp, /run)
    - Host homes (/root, /home) are completely hidden.
    - Windows drives (/mnt) and Docker sockets are completely hidden.
    - Host environment is cleared (--clearenv), preventing secret inheritance.
    - Persistent credentials (/var/lib/hermes/codex-credentials/auth.json) are NEVER mounted into sandbox.
    - Ephemeral credentials in /tmp/codex-home/auth.json are unlinked immediately after process start.
    - ONLY the designated task worktree is mounted read-write (--bind <wt> <wt>).
    - Supports running natively on Linux or from Windows via WSL (wsl.exe -d <distro>).
    """

    def __init__(
        self,
        distro: str | None = None,
        bwrap_bin: str = "bwrap",
        codex_bin: str = "codex",
        credentials_path: str | Path | None = None,
        timeout_sec: int = 180,
        model: str | None = None,
        is_windows: bool | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        popen: Any = None,
        allow_unsupported_codex_credential_boundary: bool = False,
    ) -> None:
        self.is_windows = (sys.platform == "win32") if is_windows is None else is_windows
        self.distro = distro or os.environ.get("HERMES_WSL_DISTRO", "Ubuntu")
        self.bwrap_bin = bwrap_bin
        self.codex_bin = codex_bin
        self.credentials_path = str(
            credentials_path
            or os.environ.get(
                "CODEX_CREDENTIALS_PATH",
                "/var/lib/hermes/codex-credentials/auth.json",
            )
        )
        self.timeout_sec = timeout_sec
        self.model = model
        self.allow_unsupported_codex_credential_boundary = (
            allow_unsupported_codex_credential_boundary
            or (os.environ.get("ALLOW_UNSUPPORTED_CODEX_CREDENTIAL_BOUNDARY", "").lower() in ("1", "true"))
        )
        self._runner = runner or subprocess.run
        if popen is not None:
            self._popen = popen
        elif runner is not None:
            class _MockWatcherProc:
                returncode = None
                stderr = None
                def poll(self): return None
                def terminate(self): pass
                def kill(self): pass
                def wait(self, timeout=None): pass
            self._popen = lambda *a, **kw: _MockWatcherProc()
        else:
            self._popen = subprocess.Popen

    def validate_isolation(self) -> tuple[bool, str | None]:
        """Verify that the executor satisfies write and secret read isolation invariants."""
        sample_cmd = self.build_bwrap_command("/var/tmp/sample_wt")

        # Invariant 1: No entire host root ro-bind
        for i in range(len(sample_cmd) - 2):
            if sample_cmd[i] == "--ro-bind" and sample_cmd[i + 1] == "/" and sample_cmd[i + 2] == "/":
                return False, "ROOT_FILESYSTEM_EXPOSED"

        # Invariant 2: Host environment cleared
        if "--clearenv" not in sample_cmd:
            return False, "CLEARENV_MISSING"

        # Invariant 3: Sensitive host paths must not be mounted
        forbidden_targets = {"/root", "/home", "/mnt", "/var", "/opt"}
        for i in range(len(sample_cmd) - 1):
            if sample_cmd[i] in ("--bind", "--ro-bind") and sample_cmd[i + 1] in forbidden_targets:
                return False, f"FORBIDDEN_PATH_MOUNTED_{sample_cmd[i+1]}"

        # Invariant 4: Persistent credentials file must not be directly bound
        if self.credentials_path in sample_cmd:
            return False, "PERSISTENT_CREDENTIALS_DIRECTLY_MOUNTED"

        # Invariant 5: PID namespace must be unshared to prevent /proc host inspection
        if "--unshare-pid" not in sample_cmd and "--unshare-all" not in sample_cmd:
            return False, "PID_NAMESPACE_NOT_UNSHARED"

        # Invariant 6: Credential watcher script syntax must be valid Python
        try:
            compile(self._get_watcher_script("/tmp/test"), "<watcher>", "exec")
        except SyntaxError as exc:
            return False, f"CREDENTIAL_WATCHER_SYNTAX_INVALID: {exc}"

        # Invariant 7: /tmp/codex-home must NOT be writable (must use --ro-bind)
        for i in range(len(sample_cmd) - 2):
            if sample_cmd[i] == "--bind" and sample_cmd[i + 2] == "/tmp/codex-home":
                return False, "CODEX_HOME_WRITABLE_IN_SANDBOX"

        return True, None

    def health(self) -> bool:
        """Verify bwrap, codex, credentials file, and isolation invariants."""
        # 1. Verify binary and credential availability
        if self.is_windows:
            try:
                bwrap_check = self._runner(
                    ["wsl.exe", "-d", self.distro, "--", "which", self.bwrap_bin],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if bwrap_check.returncode != 0:
                    return False

                codex_check = self._runner(
                    ["wsl.exe", "-d", self.distro, "--", "which", self.codex_bin],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if codex_check.returncode != 0:
                    return False

                cred_check = self._runner(
                    ["wsl.exe", "-d", self.distro, "--", "test", "-f", self.credentials_path],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if cred_check.returncode != 0:
                    return False
            except Exception:
                return False
        else:
            if not shutil.which(self.bwrap_bin):
                return False
            if not shutil.which(self.codex_bin):
                return False
            if not Path(self.credentials_path).is_file():
                return False

        # 2. Verify command isolation invariants
        ok, _ = self.validate_isolation()
        return ok

    def execution_ready(self) -> bool:
        """Return True if executor is ready to execute tasks safely.

        Since Codex CLI currently lacks OS-level process privilege separation
        between credential brokers and tool subprocesses, execution_ready()
        returns False unless allow_unsupported_codex_credential_boundary is explicitly enabled.
        """
        if not self.allow_unsupported_codex_credential_boundary:
            return False
        if not self.health():
            return False
        ok, _ = self.validate_isolation()
        return ok

    def build_bwrap_command(
        self,
        worktree_linux_path: str,
        ephemeral_home_path: str = "/tmp/codex-home",
    ) -> list[str]:
        """Construct the bubblewrap command enforcing fail-closed isolation."""
        cmd = [
            self.bwrap_bin,
            # 1. Minimal allowlist system mounts
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/sbin", "/sbin",
        ]
        # 2. DNS resolution
        if self.is_windows or Path("/mnt/wsl/resolv.conf").exists():
            cmd.extend(["--ro-bind", "/mnt/wsl/resolv.conf", "/etc/resolv.conf"])
        else:
            cmd.extend(["--ro-bind", "/etc/resolv.conf", "/etc/resolv.conf"])

        # 3. CA certs and system config
        cmd.extend([
            "--ro-bind", "/etc/ssl", "/etc/ssl",
            "--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates",
            "--ro-bind", "/etc/nsswitch.conf", "/etc/nsswitch.conf",
            "--ro-bind", "/etc/hosts", "/etc/hosts",
            "--ro-bind", "/etc/passwd", "/etc/passwd",
            "--ro-bind", "/etc/group", "/etc/group",
            "--ro-bind", "/etc/alternatives", "/etc/alternatives",
        ])

        # 4. Dev, proc, isolated tmpfs with unshared PID namespace
        cmd.extend([
            "--unshare-pid",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--tmpfs", "/run",
            "--dir", "/tmp/codex-home",
            "--ro-bind", ephemeral_home_path, "/tmp/codex-home",
        ])

        # 5. Worktree bind mount (only writable location)
        cmd.extend([
            "--bind", worktree_linux_path, worktree_linux_path,
        ])

        # 6. Clearenv & minimal safe environment variables
        cmd.extend([
            "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "CODEX_HOME", "/tmp/codex-home",
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "HOME", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
        ])

        # 7. Codex invocation
        cmd.extend([
            self.codex_bin,
            "exec",
            "-C", worktree_linux_path,
            "--approve-for-me",
            "--ephemeral",
        ])
        if self.model:
            cmd.extend(["-m", self.model])
        cmd.append("-")
        return cmd

    @staticmethod
    def _get_watcher_script(ephemeral_home: str) -> str:
        """Return deterministic Python watcher script for unlinking credentials upon first open."""
        return (
            "import ctypes\n"
            "import os\n"
            "import select\n"
            "import sys\n"
            f"target = '{ephemeral_home}/auth.json'\n"
            f"ready_marker = '{ephemeral_home}/.watcher_ready'\n"
            f"unlinked_marker = '{ephemeral_home}/.watcher_unlinked'\n"
            "try:\n"
            "    libc = ctypes.CDLL(None)\n"
            "    fd = libc.inotify_init()\n"
            "    if fd < 0:\n"
            "        sys.exit(2)\n"
            "    wd = libc.inotify_add_watch(fd, target.encode(), 0x00000021)\n"
            "    if wd < 0:\n"
            "        sys.exit(3)\n"
            "    with open(ready_marker, 'w', encoding='utf-8') as f:\n"
            "        f.write('READY')\n"
            "    r, _, _ = select.select([fd], [], [], 30.0)\n"
            "    if r:\n"
            "        try:\n"
            "            os.read(fd, 1024)\n"
            "        except Exception:\n"
            "            pass\n"
            "    if os.path.exists(target):\n"
            "        os.unlink(target)\n"
            "    with open(unlinked_marker, 'w', encoding='utf-8') as f:\n"
            "        f.write('UNLINKED')\n"
            "    libc.inotify_rm_watch(fd, wd)\n"
            "    os.close(fd)\n"
            "    sys.exit(0)\n"
            "except Exception:\n"
            "    sys.exit(1)\n"
        )

    def _verify_credential_unlinked(self, ephemeral_home: str) -> bool:
        """Verify that the watcher unlinked auth.json and recorded the unlinked marker."""
        if self.is_windows:
            try:
                res = self._runner(
                    [
                        "wsl.exe",
                        "-d",
                        self.distro,
                        "--",
                        "bash",
                        "-c",
                        f"test -f '{ephemeral_home}/.watcher_unlinked' && ! test -f '{ephemeral_home}/auth.json'",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                return res.returncode == 0
            except Exception:
                return False
        else:
            p = Path(ephemeral_home)
            return (p / ".watcher_unlinked").exists() and not (p / "auth.json").exists()

    def _setup_ephemeral_credentials(self, ephemeral_home: str) -> None:
        """Create ephemeral CODEX_HOME directory with credentials copy, failing closed on error."""
        if not self.credentials_path:
            raise RuntimeError("CREDENTIALS_PATH_UNCONFIGURED")

        if self.is_windows:
            res = self._runner(
                [
                    "wsl.exe",
                    "-d",
                    self.distro,
                    "--",
                    "bash",
                    "-c",
                    f"mkdir -p {ephemeral_home} && cp '{self.credentials_path}' '{ephemeral_home}/auth.json' && chmod 600 '{ephemeral_home}/auth.json'",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode != 0:
                raise RuntimeError(
                    f"CREDENTIAL_SETUP_FAILED: {res.stderr or res.stdout or 'exit code non-zero'}"
                )
        else:
            p = Path(ephemeral_home)
            p.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = p / "auth.json"
            shutil.copy2(self.credentials_path, target)
            os.chmod(target, 0o600)

    def _start_credential_unlinking_watcher(self, ephemeral_home: str) -> None:
        """Trigger background inotify watcher with readiness handshake to unlink auth.json upon first access."""
        watcher_py = self._get_watcher_script(ephemeral_home)
        if self.is_windows:
            proc = self._popen(
                [
                    "wsl.exe",
                    "-d",
                    self.distro,
                    "--",
                    "python3",
                    "-c",
                    watcher_py,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        else:
            proc = self._popen(
                [
                    "python3",
                    "-c",
                    watcher_py,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        self._watcher_proc = proc

        if proc.__class__.__name__ == "_MockWatcherProc":
            return

        # Wait for ready marker with deadline
        start = time.time()
        ready = False
        while time.time() - start < 3.0:
            if proc.poll() is not None:
                err = proc.stderr.read().decode("utf-8") if proc.stderr else ""
                raise RuntimeError(
                    f"CREDENTIAL_WATCHER_PREMATURE_EXIT: code {proc.returncode}, {err}"
                )
            if self.is_windows:
                chk = self._runner(
                    [
                        "wsl.exe",
                        "-d",
                        self.distro,
                        "--",
                        "test",
                        "-f",
                        f"{ephemeral_home}/.watcher_ready",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                if chk.returncode == 0:
                    ready = True
                    break
            else:
                if (Path(ephemeral_home) / ".watcher_ready").exists():
                    ready = True
                    break
            time.sleep(0.05)

        if not ready:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError("CREDENTIAL_WATCHER_READY_TIMEOUT")

    def _cleanup_ephemeral_credentials(self, ephemeral_home: str) -> None:
        """Ensure ephemeral credential directory is completely removed and watcher terminated."""
        if hasattr(self, "_watcher_proc") and self._watcher_proc is not None:
            try:
                self._watcher_proc.terminate()
                self._watcher_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._watcher_proc.kill()
                except Exception:
                    pass
            self._watcher_proc = None

        if self.is_windows:
            try:
                self._runner(
                    ["wsl.exe", "-d", self.distro, "--", "rm", "-rf", ephemeral_home],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                chk = self._runner(
                    ["wsl.exe", "-d", self.distro, "--", "sh", "-c", f"[ ! -e '{ephemeral_home}' ]"],
                    capture_output=True,
                    timeout=5,
                )
                if chk.returncode != 0:
                    raise RuntimeError(f"EPHEMERAL_CREDENTIAL_CLEANUP_FAILED: {ephemeral_home} still exists")
            except Exception as exc:
                if "EPHEMERAL_CREDENTIAL_CLEANUP_FAILED" in str(exc):
                    raise
        else:
            if Path(ephemeral_home).exists():
                shutil.rmtree(ephemeral_home, ignore_errors=False)
            if Path(ephemeral_home).exists():
                raise RuntimeError(f"EPHEMERAL_CREDENTIAL_CLEANUP_FAILED: {ephemeral_home} still exists")

    def resolve_worktree_linux_path(self, wt: Path) -> str:
        """Resolve host worktree path to the appropriate Linux path."""
        if self.is_windows:
            wt_str = str(wt)
            if wt_str.startswith("/"):
                return wt_str
            try:
                res = self._runner(
                    ["wsl.exe", "-d", self.distro, "--", "wslpath", "-a", "-u", wt_str],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=10,
                )
                translated = res.stdout.strip()
                if not translated:
                    raise RuntimeError("Empty translation from wslpath")
                lower_translated = translated.lower()
                if lower_translated.startswith("/mnt/c/windows") or "/appdata" in lower_translated:
                    raise RuntimeError(f"Unsafe worktree location on Windows drive: {translated}")
                return translated
            except Exception as exc:
                raise RuntimeError(f"Failed to translate Windows path {wt} to WSL: {exc}") from exc
        return str(wt.resolve())

    def execute(
        self,
        task: LinearTask,
        worktree_path: Path,
        canonical_base_sha: str,
    ) -> ExecutionResult:
        exec_id = f"exec-linux-{uuid.uuid4().hex[:12]}"
        wt = Path(worktree_path).resolve()
        if not wt.exists() or not wt.is_dir():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                error_reason="INVALID_WORKTREE_PATH",
            )

        if not self.allow_unsupported_codex_credential_boundary:
            return ExecutionResult(
                status="BLOCKED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                error_reason="CODEX_CREDENTIAL_BOUNDARY_UNSUPPORTED",
            )

        if not self.health():
            iso_ok, iso_err = self.validate_isolation()
            reason = iso_err if not iso_ok else "LINUX_CODEX_UNAVAILABLE"
            return ExecutionResult(
                status="BLOCKED" if reason == "CODEX_CREDENTIAL_BOUNDARY_UNSUPPORTED" else "FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                error_reason=reason,
            )

        try:
            linux_wt = self.resolve_worktree_linux_path(wt)
        except Exception as exc:
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"error": str(exc)},
                error_reason="PATH_TRANSLATION_FAILED",
            )

        prompt = (
            f"Objective: Implement the task described below in this repository worktree.\n\n"
            f"Linear Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description:\n{task.description}\n\n"
            f"Canonical Base SHA: {canonical_base_sha}\n"
            f"Constraints:\n"
            f"- Confine all creations and edits to this repository worktree only ({linux_wt}).\n"
            f"- Do not modify production credentials, databases, Qdrant, or any path outside this directory.\n"
            f"- Do not commit or push to remote.\n"
            f"- Make only the minimal necessary coherent code and documentation changes.\n"
        )

        ephemeral_home = f"/tmp/codex-ephemeral-{exec_id}"
        try:
            self._setup_ephemeral_credentials(ephemeral_home)
            self._start_credential_unlinking_watcher(ephemeral_home)
            bwrap_args = self.build_bwrap_command(linux_wt, ephemeral_home_path=ephemeral_home)
            cmd = ["wsl.exe", "-d", self.distro, "--"] + bwrap_args if self.is_windows else bwrap_args

            # Verify watcher is alive before launching execution
            if hasattr(self, "_watcher_proc") and self._watcher_proc is not None:
                if self._watcher_proc.poll() is not None:
                    raise RuntimeError("CREDENTIAL_WATCHER_DIED_BEFORE_EXECUTION")

            start_time = time.time()
            proc = self._runner(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
            duration = time.time() - start_time
            if proc.returncode != 0:
                return ExecutionResult(
                    status="FAILED",
                    changed_files=(),
                    execution_id=exec_id,
                    executor_name="LinuxCodexTaskExecutor",
                    evidence={
                        "duration_sec": duration,
                        "exit_code": proc.returncode,
                        "stderr": proc.stderr[-1000:] if proc.stderr else "",
                    },
                    error_reason=f"CODEX_EXEC_FAILED_EXIT_{proc.returncode}",
                )

            # Verification barrier: auth.json must have been unlinked and verified
            if not self._verify_credential_unlinked(ephemeral_home):
                return ExecutionResult(
                    status="FAILED",
                    changed_files=(),
                    execution_id=exec_id,
                    executor_name="LinuxCodexTaskExecutor",
                    evidence={"duration_sec": duration, "error": "CREDENTIAL_UNLINK_VERIFICATION_FAILED"},
                    error_reason="CREDENTIAL_UNLINK_VERIFICATION_FAILED",
                )
        except subprocess.TimeoutExpired:
            duration = time.time() - start_time if "start_time" in locals() else 0.0
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"duration_sec": duration, "timed_out": True},
                error_reason="CODEX_EXEC_TIMEOUT",
            )
        except Exception as exc:
            duration = time.time() - start_time if "start_time" in locals() else 0.0
            err_reason = "CREDENTIAL_ISOLATION_FAILED" if "CREDENTIAL" in str(exc) else "CODEX_EXEC_EXCEPTION"
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"duration_sec": duration, "error": str(exc)},
                error_reason=err_reason,
            )
        finally:
            cleanup_err = None
            try:
                self._cleanup_ephemeral_credentials(ephemeral_home)
            except Exception as exc:
                cleanup_err = str(exc)

        if cleanup_err:
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"duration_sec": duration if "duration" in locals() else 0.0, "error": cleanup_err},
                error_reason="EPHEMERAL_CREDENTIAL_CLEANUP_FAILED",
            )

        changed_files = self._detect_changed_files(wt)
        if not changed_files:
            return ExecutionResult(
                status="BLOCKED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"duration_sec": duration, "exit_code": proc.returncode},
                error_reason="NO_CHANGES_PRODUCED",
            )

        return ExecutionResult(
            status="SUCCESS",
            changed_files=changed_files,
            execution_id=exec_id,
            executor_name="LinuxCodexTaskExecutor",
            evidence={
                "duration_sec": duration,
                "exit_code": proc.returncode,
                "changed_count": len(changed_files),
            },
        )

    def _detect_changed_files(self, worktree_path: Path) -> tuple[str, ...]:
        """Detect actual modified, added, or untracked files relative to worktree."""
        try:
            clean_env = get_clean_git_env()
            dynamic_opts = get_effective_filter_opts(worktree_path)
            cmd = [
                "git",
                "-c", "core.fsmonitor=",
                "-c", "core.hooksPath=/dev/null",
                "-c", "core.attributesFile=/dev/null",
                "-c", "filter.lfs.smudge=",
                "-c", "filter.lfs.clean=",
                "-c", "filter.lfs.process=",
                "-c", "filter.lfs.required=false",
                *dynamic_opts,
                "-C", str(worktree_path),
                "status",
                "--porcelain",
            ]
            res = self._runner(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                env=clean_env,
            )
            files = []
            for line in res.stdout.splitlines():
                if not line.strip():
                    continue
                if len(line) >= 4 and line[2] == " ":
                    path_part = line[3:].strip()
                else:
                    parts = line.strip().split(maxsplit=1)
                    path_part = parts[1] if len(parts) > 1 else parts[0]
                target_path = path_part.split(" -> ")[-1].strip('"')
                files.append(target_path)
            return tuple(sorted(set(files)))
        except Exception:
            return ()


class SimulatedTaskExecutor:
    """Deterministic simulated task executor for offline testing and qualification."""

    def __init__(
        self,
        mutation_handler: Callable[[Path, LinearTask], Sequence[str]] | None = None,
        fail_with_reason: str | None = None,
    ) -> None:
        self.mutation_handler = mutation_handler
        self.fail_with_reason = fail_with_reason
        self.call_count = 0

    def health(self) -> bool:
        return True

    def execution_ready(self) -> bool:
        return True

    def execute(
        self,
        task: LinearTask,
        worktree_path: Path,
        canonical_base_sha: str,
    ) -> ExecutionResult:
        self.call_count += 1
        exec_id = f"sim-{uuid.uuid4().hex[:12]}"

        if self.fail_with_reason:
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SimulatedTaskExecutor",
                error_reason=self.fail_with_reason,
            )

        wt = Path(worktree_path).resolve()
        changed: list[str] = []
        if self.mutation_handler:
            changed = list(self.mutation_handler(wt, task))

        # Independently detect git changes if not supplied by handler
        if not changed:
            try:
                res = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=str(wt),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                for line in res.stdout.strip().splitlines():
                    if line.strip():
                        parts = line[3:].strip().split(" -> ")
                        changed.append(parts[-1].strip('"'))
            except Exception:
                pass

        if not changed:
            return ExecutionResult(
                status="BLOCKED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SimulatedTaskExecutor",
                error_reason="NO_CHANGES_PRODUCED",
            )

        return ExecutionResult(
            status="SUCCESS",
            changed_files=tuple(sorted(set(changed))),
            execution_id=exec_id,
            executor_name="SimulatedTaskExecutor",
            evidence={"task_id": task.id, "base_sha": canonical_base_sha},
        )


class IModelGateway(Protocol):
    """Protocol for model inference producing candidate code mutations without tool execution privileges."""

    def generate_mutation(
        self,
        prompt: str,
        task: LinearTask,
        worktree_path: Path,
    ) -> str:
        """Generate mutation payload (unified diff or structured file modifications)."""
        ...

    def health(self) -> bool:
        """Return True if the underlying model service/CLI is available."""
        ...

    def execution_ready(self) -> bool:
        """Return True if model credentials and inference capabilities are fully verified."""
        ...


class CallableModelGateway:
    """Model gateway delegating to a callable for deterministic tests and evaluation."""

    def __init__(self, generator: Callable[[str, LinearTask, Path], str]) -> None:
        self.generator = generator

    def health(self) -> bool:
        return True

    def execution_ready(self) -> bool:
        return True

    def generate_mutation(
        self, prompt: str, task: LinearTask, worktree_path: Path
    ) -> str:
        return self.generator(prompt, task, worktree_path)


class DshModelGateway:
    """Production model gateway invoking DeepSeek Harness strictly as a pure LLM text inference engine.

    All filesystem, shell, and subagent tools are stripped via cordis patch overlay,
    guaranteeing that the DSH process CANNOT touch the host filesystem or execute tools.
    """

    def __init__(
        self,
        dsh_bin: str | None = None,
        patch_path: str | Path | None = None,
        timeout_sec: int = 120,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.dsh_bin = dsh_bin or os.environ.get(
            "DSH_BIN",
            r"C:\Users\Oleg\AppData\Local\Programs\DeepSeek Harness\resources\runtime\cli\bin\dsh.cmd",
        )
        raw_patch = patch_path or os.environ.get("DSH_PATCH_PATH")
        self.patch_path = Path(raw_patch).resolve() if raw_patch else None
        self.timeout_sec = timeout_sec
        self._custom_runner = runner
        self._runner = runner or subprocess.run

    def _verify_no_tools_patch(self) -> bool:
        """Verify that the tool-disabling patch explicitly disables dangerous tools and permits no enabled tools."""
        if not self.patch_path or not self.patch_path.is_file():
            return False
        try:
            content = self.patch_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            if not isinstance(data, list):
                return False
            required_disabled_tools = {
                "tool-fs",
                "tool-fs-search",
                "tool-pwsh",
                "tool-bash",
                "tool-jobs",
                "web",
                "subagent",
                "tool-skill",
                "tool-todo",
                "tool-goal",
            }
            canonical_tool_names: dict[str, str] = {
                "tool-fs": "@deepseek-ai/dsh-tool-fs",
                "tool-fs-search": "@deepseek-ai/dsh-tool-fs-search",
                "tool-pwsh": "@deepseek-ai/dsh-tool-pwsh",
                "tool-bash": "@deepseek-ai/dsh-tool-bash",
                "tool-jobs": "@deepseek-ai/dsh-tool-jobs",
                "web": "@deepseek-ai/dsh-web",
                "subagent": "@deepseek-ai/dsh-subagent",
                "tool-skill": "@deepseek-ai/dsh-tool-skill",
                "tool-todo": "@deepseek-ai/dsh-tool-todo",
                "tool-goal": "@deepseek-ai/dsh-tool-goal",
            }
            tool_keywords = ("tool", "shell", "exec", "terminal", "pwsh", "bash", "cmd", "fs", "python", "process", "web", "subagent", "job", "search", "skill", "todo", "goal")
            disabled_found: set[str] = set()
            for entry in data:
                if not isinstance(entry, dict):
                    return False
                # Reject any entry specifying group: true which could defeat Cordis disabled flags
                if "group" in entry or entry.get("group") is True:
                    return False
                raw_id = entry.get("id")
                if not isinstance(raw_id, str) or not raw_id:
                    return False
                tool_id = raw_id  # Exact case-sensitive match, NO lower()!
                tool_name = str(entry.get("name", ""))
                is_disabled = (entry.get("disabled") is True)

                # Reject any entry attempting to insert or inject plugins/tools
                if any(k in entry for k in ("insert", "plugins", "tools", "skills", "mcp", "functions", "commands")):
                    return False

                if is_disabled:
                    # Must match exact canonical tool ID (fail closed on unknown, misspelled, mis-cased, or duplicate)
                    if tool_id not in canonical_tool_names or tool_id in disabled_found:
                        return False
                    # Require exact package identity when name is provided; Cordis skips patch on any mismatch
                    raw_name = entry.get("name")
                    if raw_name is not None:
                        if not isinstance(raw_name, str):
                            return False
                        expected_name = canonical_tool_names.get(tool_id)
                        if raw_name != expected_name:
                            return False
                    disabled_found.add(tool_id)
                else:
                    # Non-disabled entry must ONLY be model/provider selection and contain no tool keywords
                    if any(kw in tool_id.lower() or kw in tool_name.lower() for kw in tool_keywords):
                        return False
                    if not (tool_id.endswith("-model") or tool_id in ("model", "agent-default-model")):
                        return False
                    # Inspect nested config
                    config = entry.get("config", {})
                    if isinstance(config, dict):
                        config_str = json.dumps(config).lower()
                        if any(kw in config_str for kw in ("tool", "plugin", "shell", "bash", "pwsh", "exec", "terminal", "skill", "todo", "goal")):
                            return False
                    else:
                        return False
            return disabled_found == required_disabled_tools
        except Exception:
            return False

    def _create_isolated_dsh_home(self) -> Path:
        """Create a fresh, private, symlink-free directory containing ONLY auth tokens."""
        import tempfile
        fresh_dir = Path(tempfile.mkdtemp(prefix="hermes_dsh_home_")).resolve()
        if fresh_dir.is_symlink():
            shutil.rmtree(fresh_dir, ignore_errors=True)
            raise RuntimeError("ISOLATED_DSH_HOME_SYMLINK_FORBIDDEN")
        # Copy only authentication credentials if present, leaving zero profiles or user plugins
        for auth_file in (".credentials.yaml", ".anonymous-user-id"):
            for search_base in [
                os.environ.get("DSH_HOME"),
                os.environ.get("USERPROFILE"),
                os.environ.get("HOME"),
            ]:
                if search_base:
                    src = (
                        Path(search_base) / ".dsh" / auth_file
                        if (Path(search_base) / ".dsh").exists()
                        else Path(search_base) / auth_file
                    )
                    if src.is_file():
                        dst = fresh_dir / auth_file
                        shutil.copy2(src, dst)
                        break
        return fresh_dir

    def _get_isolated_env(self, isolated_home: Path | None = None) -> dict[str, str]:
        """Construct sanitized environment with isolated profile directory to prevent ambient tool loading."""
        safe_keys = {
            "PATH",
            "SYSTEMROOT",
            "COMSPEC",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "TERM",
        }
        sanitized_env = {k: v for k, v in os.environ.items() if k in safe_keys}
        import tempfile
        home_path = str(isolated_home) if isolated_home else str(Path(tempfile.gettempdir()) / "hermes_dsh_home")

        sanitized_env["DSH_HOME"] = home_path
        sanitized_env["HOME"] = home_path
        sanitized_env["USERPROFILE"] = home_path
        sanitized_env["APPDATA"] = home_path
        sanitized_env["LOCALAPPDATA"] = home_path
        sanitized_env["CORDIS_CONFIG_DIR"] = home_path
        sanitized_env["CORDIS_USER_PROFILE"] = ""
        sanitized_env["CORDIS_DISABLE_PLUGINS"] = "1"
        sanitized_env["PYTHONIOENCODING"] = "utf-8"
        if os.environ.get("DEEPSEEK_API_KEY"):
            sanitized_env["DEEPSEEK_API_KEY"] = os.environ["DEEPSEEK_API_KEY"]
        return sanitized_env

    def health(self) -> bool:
        if not self.dsh_bin or not Path(self.dsh_bin).exists():
            return False
        # Require verified tool-disabling patch to guarantee pure text inference without host tools
        if not self._verify_no_tools_patch():
            return False
        try:
            iso_home = self._create_isolated_dsh_home()
            try:
                isolated_env = self._get_isolated_env(iso_home)
                res = self._runner(
                    [self.dsh_bin, "--version"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=isolated_env,
                    timeout=5,
                )
                return res.returncode == 0
            finally:
                shutil.rmtree(iso_home, ignore_errors=True)
        except Exception:
            return False

    def execution_ready(self) -> bool:
        """Verify that DSH has verified tools patch AND usable account authentication credentials."""
        if not self.health():
            return False
        # If a custom runner is provided (e.g. unit tests), accept it as execution ready
        if self._custom_runner is not None:
            return True
        # Explicit API key overrides file-based credential lookup
        if os.environ.get("DEEPSEEK_API_KEY", "").strip():
            return True

        # Require actual authentication records in .credentials.yaml (reject .anonymous-user-id as telemetry-only)
        for search_base in [
            os.environ.get("DSH_HOME"),
            os.environ.get("USERPROFILE"),
            os.environ.get("HOME"),
        ]:
            if search_base:
                cred_file = (
                    Path(search_base) / ".dsh" / ".credentials.yaml"
                    if (Path(search_base) / ".dsh").exists()
                    else Path(search_base) / ".credentials.yaml"
                )
                if cred_file.is_file() and cred_file.stat().st_size > 0:
                    try:
                        content = yaml.safe_load(cred_file.read_text(encoding="utf-8"))
                        if isinstance(content, dict):
                            records = content.get("records")
                            if isinstance(records, dict) and len(records) > 0:
                                return True
                            elif isinstance(records, list) and len(records) > 0:
                                return True
                    except Exception:
                        pass
        return False

    def generate_mutation(
        self, prompt: str, task: LinearTask, worktree_path: Path
    ) -> str:
        if not self._verify_no_tools_patch():
            raise RuntimeError("DSH_TOOL_DISABLING_OVERLAY_REQUIRED")

        # Create fresh private DSH home and empty working directory to prevent loading ambient configs or workspace instructions
        import tempfile
        iso_home = self._create_isolated_dsh_home()
        empty_cwd = Path(tempfile.mkdtemp(prefix="hermes_dsh_cwd_")).resolve()
        if empty_cwd.is_symlink():
            shutil.rmtree(iso_home, ignore_errors=True)
            shutil.rmtree(empty_cwd, ignore_errors=True)
            raise RuntimeError("DSH_CWD_SYMLINK_FORBIDDEN")

        try:
            sanitized_env = self._get_isolated_env(iso_home)
            cmd = [self.dsh_bin, "headless", "--patch", str(self.patch_path), "-"]

            # If a custom mock runner is injected (e.g. unit tests), use it directly
            if self._custom_runner is not None:
                res = self._custom_runner(
                    cmd,
                    cwd=str(empty_cwd),
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=sanitized_env,
                    timeout=self.timeout_sec,
                )
                if res.returncode != 0:
                    raise RuntimeError(
                        f"DSH inference failed with exit code {res.returncode}"
                    )
                return res.stdout

            # Production execution with UTF-8 streams and process tree containment on timeout
            proc = subprocess.Popen(
                cmd,
                cwd=str(empty_cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=sanitized_env,
            )
            try:
                stdout, stderr = proc.communicate(input=prompt, timeout=self.timeout_sec)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True,
                            timeout=5,
                        )
                    except Exception:
                        pass
                else:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    proc.communicate(timeout=2)
                except Exception:
                    pass
                raise subprocess.TimeoutExpired(cmd, self.timeout_sec)

            if proc.returncode != 0:
                raise RuntimeError(
                    f"DSH inference failed with exit code {proc.returncode}"
                )
            return stdout
        finally:
            shutil.rmtree(iso_home, ignore_errors=True)
            shutil.rmtree(empty_cwd, ignore_errors=True)


def validate_worktree_path_containment(
    target_rel_path: str | Path,
    worktree_root: Path,
) -> tuple[bool, str | None]:
    """Validate that target path is strictly contained within worktree_root with no escapes."""
    try:
        wt = Path(worktree_root).resolve()
        raw_str = str(target_rel_path)
        if raw_str != raw_str.strip():
            return False, "NONCANONICAL_PATH_WHITESPACE"
        norm_rel = raw_str.replace("\\", "/")
        if not norm_rel:
            return False, "EMPTY_TARGET_PATH"

        # Check for explicit traversal tokens, whitespace anomalies, trailing dot/space Win32 aliases, and case-insensitive git metadata
        parts = Path(norm_rel).parts
        if any(p != p.strip() for p in parts):
            return False, "NONCANONICAL_PATH_WHITESPACE"
        if any(p == ".." for p in parts):
            return False, "PATH_TRAVERSAL_DETECTED"
        if any(p not in (".", "..") and (p.endswith(".") or p.endswith(" ")) for p in parts):
            return False, "NONCANONICAL_TRAILING_DOT_OR_SPACE_ALIAS"
        if any(p.lower().split(":")[0].strip() == ".git" for p in parts):
            return False, "GIT_METADATA_MUTATION_FORBIDDEN"

        # Check symlink escapes and symlink git aliases in any existing ancestor or target
        curr = wt / norm_rel
        while curr != wt and curr != curr.parent:
            if curr.is_symlink():
                link_target = curr.resolve()
                try:
                    rel_link = link_target.relative_to(wt)
                    if any(p.lower().split(":")[0].strip() == ".git" for p in rel_link.parts):
                        return False, "GIT_METADATA_MUTATION_FORBIDDEN"
                except ValueError:
                    return False, "SYMLINK_ESCAPE_DETECTED"
            curr = curr.parent

        # Check resolved containment
        resolved_target = (wt / norm_rel).resolve()
        try:
            rel_resolved = resolved_target.relative_to(wt)
        except ValueError:
            return False, "PATH_TRAVERSAL_DETECTED"

        # Check resolved relative path for Git metadata (prevents symlink aliases to .git)
        if any(p.lower().split(":")[0].strip() == ".git" for p in rel_resolved.parts):
            return False, "GIT_METADATA_MUTATION_FORBIDDEN"

        # Check for hardlink escape (prevents truncating external inodes)
        if resolved_target.exists():
            try:
                st = resolved_target.stat()
                if hasattr(st, "st_nlink") and st.st_nlink > 1:
                    return False, "HARDLINK_ESCAPE_DETECTED"
            except Exception:
                pass

        return True, None
    except Exception as exc:
        return False, f"PATH_VALIDATION_ERROR: {exc}"


def _extract_fenced_code_blocks(text: str) -> list[tuple[str, str]]:
    """Extract code blocks supporting 3+ backticks or tildes and preserving inner code blocks with LF or CRLF."""
    blocks: list[tuple[str, str]] = []
    lines = text.splitlines(keepends=True)
    i = 0
    n_lines = len(lines)
    while i < n_lines:
        line = lines[i]
        line_stripped = line.rstrip("\r\n")
        # Match opening fence with 3 or more backticks (or tildes)
        m = re.match(r"^([`~]{3,})(.*)$", line_stripped)
        if m:
            fence_chars = m.group(1)
            fence_char = fence_chars[0]
            fence_len = len(fence_chars)
            info_tag = m.group(2).strip()
            i += 1
            body_lines: list[str] = []
            closed = False
            while i < n_lines:
                cur_line = lines[i]
                cur_stripped = cur_line.rstrip("\r\n")
                # Closing fence must have at least fence_len of fence_char and only whitespace
                close_m = re.match(r"^" + re.escape(fence_char) + r"{" + str(fence_len) + r",}\s*$", cur_stripped)
                if close_m:
                    closed = True
                    i += 1
                    break
                body_lines.append(cur_line)
                i += 1
            if closed:
                blocks.append((info_tag, "".join(body_lines)))
            continue
        i += 1
    return blocks


class SandboxedBrokerTaskExecutor:
    """Safe Production Task Executor enforcing strict Trusted Controller + Isolated Tool Runner separation.

    Architecture Invariants (C01 & C02 Closure):
    1. Model Inference (Model Gateway):
       - Generates declarative diffs / patches only.
       - Model process has ZERO direct tool execution capabilities (no tool-fs, no tool-bash).
       - Model credentials never touch the tool execution environment.
    2. Tool Execution Sandbox (Bubblewrap):
       - --unshare-all (unshares PID, IPC, UTS, and NETWORK).
       - --clearenv (completely strips host environment variables: no GITHUB_TOKEN, no LINEAR_API_KEY).
       - Minimal system mounts only (/usr, /lib, /bin, /lib64, /sbin).
       - Host roots, homes, /mnt, and /var/run/docker.sock are completely forbidden and unmounted.
       - ONLY the designated task worktree is mounted read-write.
    3. Path Traversal & Symlink Hardening:
       - Every file path in the proposed diff is verified before application.
       - Rejects paths outside worktree (path traversal).
       - Rejects symlinks or parent components pointing outside worktree.
       - Rejects mutations to .git metadata (.git/config, .git/hooks, .git/attributes).
    4. Git Execution Neutralization:
       - All git operations executed with sanitized environment and neutralized filters/drivers
         (-c filter.*.clean= -c filter.*.smudge= -c core.fsmonitor=).
    5. Fail-Closed Health & Execution Ready:
       - On Windows native host without sandbox, execution_ready() returns False (fail-closed)
         unless WSL2 Bubblewrap sandbox is available.
       - If any isolation check fails, execution_ready() returns False.
    """

    def __init__(
        self,
        model_gateway: IModelGateway | None = None,
        distro: str | None = None,
        bwrap_bin: str = "bwrap",
        timeout_sec: int = 180,
        is_windows: bool | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.model_gateway = model_gateway
        self.is_windows = (sys.platform == "win32") if is_windows is None else is_windows
        self.distro = distro or os.environ.get("HERMES_WSL_DISTRO", "Ubuntu")
        self.bwrap_bin = bwrap_bin
        self.timeout_sec = timeout_sec
        self._runner = runner or subprocess.run

    def _to_linux_path(self, path: Path | str) -> str:
        p = Path(path).resolve()
        if self.is_windows or p.drive:
            letter = p.drive[0].lower()
            rest = p.as_posix()[len(p.drive) :].lstrip("/")
            return f"/mnt/{letter}/{rest}"
        return p.as_posix()

    def validate_isolation(self) -> tuple[bool, str | None]:
        """Verify that the executor satisfies write and secret read isolation invariants."""
        sample_cmd = self.build_bwrap_command("/var/tmp/sample_wt")

        # Invariant 1: No entire host root ro-bind or bind
        for i in range(len(sample_cmd) - 2):
            if sample_cmd[i] in ("--ro-bind", "--bind") and sample_cmd[i + 1] == "/" and sample_cmd[i + 2] == "/":
                return False, "ROOT_FILESYSTEM_EXPOSED"

        # Invariant 2: Host environment cleared
        if "--clearenv" not in sample_cmd:
            return False, "CLEARENV_MISSING"

        # Invariant 3: Sensitive host paths must not be mounted
        forbidden_targets = {"/root", "/home", "/mnt", "/var", "/opt"}
        for i in range(len(sample_cmd) - 1):
            if sample_cmd[i] in ("--bind", "--ro-bind") and sample_cmd[i + 1] in forbidden_targets:
                return False, f"FORBIDDEN_PATH_MOUNTED_{sample_cmd[i+1]}"

        # Invariant 4: Docker socket must never be mounted
        if any("/var/run/docker.sock" in arg for arg in sample_cmd):
            return False, "DOCKER_SOCKET_MOUNTED"

        # Invariant 5: PID namespace must be unshared
        if "--unshare-pid" not in sample_cmd and "--unshare-all" not in sample_cmd:
            return False, "PID_NAMESPACE_NOT_UNSHARED"

        # Invariant 6: Network must be isolated (unshare-net or unshare-all)
        if "--unshare-net" not in sample_cmd and "--unshare-all" not in sample_cmd:
            return False, "NETWORK_NOT_ISOLATED"

        # Invariant 7: No credential files in command
        for arg in sample_cmd:
            if "auth.json" in arg or "credentials" in arg.lower():
                return False, "PERSISTENT_CREDENTIALS_DIRECTLY_MOUNTED"

        return True, None

    def health(self) -> bool:
        """Verify bwrap, model gateway availability, and isolation invariants."""
        if self.model_gateway is None:
            return False
        if not self.model_gateway.health():
            return False

        if self.is_windows:
            try:
                bwrap_check = self._runner(
                    ["wsl.exe", "-d", self.distro, "--", "which", self.bwrap_bin],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if bwrap_check.returncode != 0:
                    return False
            except Exception:
                return False
        else:
            if not shutil.which(self.bwrap_bin):
                return False

        ok, _ = self.validate_isolation()
        return ok

    def execution_ready(self) -> bool:
        """Return True if executor is ready to execute tasks safely.

        Probes the actual Bubblewrap sandbox with --unshare-all to prove namespace
        creation and mount containment are functional on the host OS.
        Returns False if health() fails, model_gateway is missing, or sandbox probe fails.
        """
        if self.model_gateway is None:
            return False
        if not self.model_gateway.health():
            return False
        if hasattr(self.model_gateway, "execution_ready") and not self.model_gateway.execution_ready():
            return False

        probe_cmd = self.build_bwrap_command("/tmp", ["true"])
        full_probe = ["wsl.exe", "-d", self.distro, "--"] + probe_cmd if self.is_windows else probe_cmd
        try:
            test_res = self._runner(
                full_probe,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if test_res.returncode != 0:
                return False
        except Exception:
            return False

        if not self.health():
            return False

        ok, _ = self.validate_isolation()
        return ok

    def build_bwrap_command(
        self,
        worktree_linux_path: str,
        tool_cmd: Sequence[str] | None = None,
    ) -> list[str]:
        """Construct the bubblewrap command enforcing fail-closed isolation."""
        cmd = [
            self.bwrap_bin,
            # 1. Unshare all namespaces (PID, NET, IPC, UTS, CGROUP)
            "--unshare-all",
            # 2. Clear environment and set safe minimal variables
            "--clearenv",
            "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "HOME", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            # 3. Minimal allowlist system mounts
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/sbin", "/sbin",
            # 4. Dev, proc, isolated tmpfs
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--tmpfs", "/run",
            # 5. Worktree mount (bind read-write) with .git protected read-only
            "--dir", worktree_linux_path,
            "--bind", worktree_linux_path, worktree_linux_path,
            "--ro-bind-try", f"{worktree_linux_path}/.git", f"{worktree_linux_path}/.git",
            "--chdir", worktree_linux_path,
        ]
        if tool_cmd:
            cmd.append("--")
            cmd.extend(tool_cmd)
        return cmd

    def _run_sandboxed_syntax_check(
        self, worktree_path: Path, rel_file: str
    ) -> tuple[bool, str | None]:
        """Execute syntax check inside Bubblewrap sandbox using isolated python interpreter."""
        if not rel_file.lower().endswith(".py"):
            return True, None

        norm_rel = rel_file.replace("\\", "/")
        if self.is_windows:
            linux_wt = self._to_linux_path(worktree_path)
            target = f"{linux_wt}/{norm_rel}"
            bwrap_cmd = self.build_bwrap_command(
                linux_wt, ["python3", "-I", "-m", "py_compile", target]
            )
            full_cmd = ["wsl.exe", "-d", self.distro, "--"] + bwrap_cmd
        else:
            wt_str = str(worktree_path)
            target = f"{wt_str}/{norm_rel}"
            bwrap_cmd = self.build_bwrap_command(
                wt_str, ["python3", "-I", "-m", "py_compile", target]
            )
            full_cmd = bwrap_cmd

        try:
            res = self._runner(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                return True, None
            return False, res.stderr.strip() or f"EXIT_{res.returncode}"
        except Exception as exc:
            return False, str(exc)

    def _run_sandboxed_probe(
        self, worktree_path: Path
    ) -> tuple[bool, str | None]:
        """Execute minimal sandbox probe on worktree proving isolation boundary."""
        if self.is_windows:
            linux_wt = self._to_linux_path(worktree_path)
            bwrap_cmd = self.build_bwrap_command(linux_wt, ["true"])
            full_cmd = ["wsl.exe", "-d", self.distro, "--"] + bwrap_cmd
        else:
            wt_str = str(worktree_path)
            bwrap_cmd = self.build_bwrap_command(wt_str, ["true"])
            full_cmd = bwrap_cmd

        try:
            res = self._runner(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                return True, None
            return False, res.stderr.strip() or f"EXIT_{res.returncode}"
        except Exception as exc:
            return False, str(exc)

    def _detect_changed_files(self, worktree_path: Path) -> tuple[str, ...]:
        """Detect actual modified, added, or untracked files relative to worktree."""
        try:
            clean_env = get_clean_git_env()
            dynamic_opts = get_effective_filter_opts(worktree_path)
            cmd = [
                "git",
                "-c", "core.fsmonitor=",
                "-c", "core.hooksPath=/dev/null",
                "-c", "core.attributesFile=/dev/null",
                "-c", "filter.lfs.clean=",
                "-c", "filter.lfs.smudge=",
                "-c", "filter.lfs.process=",
                "-c", "filter.lfs.required=false",
                *dynamic_opts,
                "-C", str(worktree_path),
                "status",
                "--porcelain",
            ]
            res = self._runner(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                env=clean_env,
            )
            files = []
            for line in res.stdout.splitlines():
                if not line.strip():
                    continue
                if len(line) >= 4 and line[2] == " ":
                    path_part = line[3:].strip()
                else:
                    parts = line.strip().split(maxsplit=1)
                    path_part = parts[1] if len(parts) > 1 else parts[0]
                target_path = path_part.split(" -> ")[-1].strip('"')
                if target_path.startswith("__pycache__") or target_path.endswith(".pyc"):
                    continue
                files.append(target_path)
            return tuple(sorted(set(files)))
        except Exception:
            return ()


    def _parse_mutation_payload(
        self, output: str, wt: Path
    ) -> tuple[list[tuple[str, str | None]], str | None]:
        """Parse proposed file mutations from model output (JSON, file blocks, or diffs)."""
        if not output or not output.strip():
            return [], "EMPTY_MODEL_OUTPUT"

        code_blocks = _extract_fenced_code_blocks(output)

        # Strategy 1: Raw JSON payload
        try:
            data = json.loads(output.strip())
            if isinstance(data, list):
                edits: list[tuple[str, str | None]] = []
                for item in data:
                    if not isinstance(item, dict) or "path" not in item:
                        return [], "MALFORMED_JSON_ITEM_INVALID"
                    p = item["path"]
                    if not isinstance(p, str) or not p.strip():
                        return [], "MALFORMED_JSON_PATH_INVALID"
                    if item.get("deleted") is True or item.get("action") == "delete":
                        edits.append((p.strip(), None))
                    elif "content" in item:
                        raw_c = item["content"]
                        if not isinstance(raw_c, str):
                            return [], "MALFORMED_JSON_CONTENT_NON_STRING"
                        edits.append((p.strip(), raw_c))
                    else:
                        return [], "MALFORMED_JSON_ITEM_MISSING_CONTENT"
                if edits:
                    return edits, None
        except json.JSONDecodeError:
            pass

        # Strategy 1b: Fenced JSON payload
        for lang_tag, body in code_blocks:
            tag = lang_tag.strip().lower()
            if tag in ("json", ""):
                body_str = body.strip()
                if body_str.startswith("[") and body_str.endswith("]"):
                    try:
                        fenced_data = json.loads(body_str)
                        if isinstance(fenced_data, list):
                            fenced_edits: list[tuple[str, str | None]] = []
                            for item in fenced_data:
                                if not isinstance(item, dict) or "path" not in item:
                                    return [], "MALFORMED_JSON_ITEM_INVALID"
                                p = item["path"]
                                if not isinstance(p, str) or not p.strip():
                                    return [], "MALFORMED_JSON_PATH_INVALID"
                                if item.get("deleted") is True or item.get("action") == "delete":
                                    fenced_edits.append((p.strip(), None))
                                elif "content" in item:
                                    raw_c = item["content"]
                                    if not isinstance(raw_c, str):
                                        return [], "MALFORMED_JSON_CONTENT_NON_STRING"
                                    fenced_edits.append((p.strip(), raw_c))
                                else:
                                    return [], "MALFORMED_JSON_ITEM_MISSING_CONTENT"
                            if fenced_edits:
                                return fenced_edits, None
                    except json.JSONDecodeError:
                        pass

        # Strategy 2: Accumulate all explicit file blocks across code blocks
        accumulated_file_edits: list[tuple[str, str | None]] = []
        for lang_tag, body in code_blocks:
            tag = lang_tag.strip()
            body_lines = body.splitlines()

            # Case A: Fence tag explicitly declares target file (e.g. ```python file:src/sub dir/foo.py or ```file:path/file.py)
            fence_file_match = re.search(r"file:\s*([^\n\r`]+)", tag)
            if fence_file_match and not tag.lower().startswith("diff") and not tag.lower().startswith("patch"):
                fname = fence_file_match.group(1).strip()
                if fname:
                    content = body if body.endswith("\n") else (body + "\n")
                    accumulated_file_edits.append((fname, content))
                    continue

            # Case B: First line of body is file:<path>
            if body_lines and body_lines[0].strip().startswith("file:"):
                fname = body_lines[0].strip()[5:].strip()
                first_nl = body.find("\n")
                content = body[first_nl + 1 :] if first_nl != -1 else ""
                if not content.endswith("\n"):
                    content += "\n"
                if fname:
                    accumulated_file_edits.append((fname, content))
                    continue

            # Case C: Fence tag directly names a code file (e.g. ```calculator.py or ```src/util.py)
            fallback_match = re.search(r"^([a-zA-Z0-9_\-\.\/\\]+\.[a-zA-Z0-9_\-]+)$", tag)
            if fallback_match and tag.lower() not in ("python", "sh", "bash", "json", "diff", "patch"):
                fname = fallback_match.group(1).strip()
                content = body if body.endswith("\n") else (body + "\n")
                accumulated_file_edits.append((fname, content))
                continue

        staged_files: dict[str, str | None] = {}
        for fname, content in accumulated_file_edits:
            if fname in staged_files:
                return [], f"DUPLICATE_FILE_BLOCK_FOR_TARGET_{fname}"
            staged_files[fname] = content

        # Strategy 3: Structural unified diff within fenced code blocks (composed across all blocks)
        for lang_tag, body in code_blocks:
            lang = lang_tag.lower().strip()
            stripped_body = body.strip()
            is_diff = (
                lang in ("diff", "patch")
                or stripped_body.startswith("--- ")
                or (stripped_body.startswith("diff --git ") and "\n+++ " in stripped_body)
            )
            if is_diff:
                diff_edits, diff_err = self._parse_unified_diff(body, wt, staged_files)
                if diff_err:
                    return [], diff_err

        if staged_files:
            return [(k, v) for k, v in staged_files.items()], None

        # Strategy 4: Raw unified diff without code fence
        stripped_output = output.strip()
        if (
            stripped_output.startswith("--- ")
            or stripped_output.startswith("diff --git ")
            or ("\n--- a/" in output and "\n+++ b/" in output)
        ):
            diff_edits, diff_err = self._parse_unified_diff(output, wt)
            if diff_edits:
                return diff_edits, None
            if diff_err:
                return [], diff_err

        # Strategy 5: Raw single code block without filename fallback
        if len(code_blocks) == 1:
            lang_tag, content = code_blocks[0]
            clean_tag = lang_tag.strip().lower()
            # Restrict single-file fallback strictly to Python code blocks
            if clean_tag not in ("python", "py", ""):
                return [], "NON_PYTHON_SINGLE_BLOCK_REJECTED"
            # Reject blocks that look like JSON arrays or objects
            stripped_c = content.strip()
            if (stripped_c.startswith("[") and stripped_c.endswith("]")) or (
                stripped_c.startswith("{") and stripped_c.endswith("}")
            ):
                try:
                    json.loads(stripped_c)
                    return [], "JSON_BLOCK_IN_SINGLE_FILE_FALLBACK_REJECTED"
                except Exception:
                    pass
            # Verify AST parses as python and is not just bare expression statements (e.g. naked list/dict/data)
            try:
                import ast
                parsed_ast = ast.parse(content)
                if not parsed_ast.body:
                    return [], "SINGLE_FILE_FALLBACK_EMPTY_BODY_REJECTED"
                non_expr = [s for s in parsed_ast.body if not isinstance(s, ast.Expr)]
                if not non_expr:
                    return [], "SINGLE_FILE_FALLBACK_LITERAL_EXPRESSION_REJECTED"
            except SyntaxError:
                return [], "SINGLE_FILE_FALLBACK_SYNTAX_ERROR"

            py_files = [f.name for f in wt.glob("*.py") if f.is_file() and not f.name.startswith(".")]
            if len(py_files) == 1:
                return [(py_files[0], content)], None

        return [], "UNABLE_TO_PARSE_MUTATION_PAYLOAD"

    def _parse_unified_diff(
        self,
        diff_text: str,
        wt: Path,
        staged_files: dict[str, str | None] | None = None,
    ) -> tuple[list[tuple[str, str | None]], str | None]:
        """Parse unified diff hunks and apply them to target files, composing against staged content."""
        files_hunks: dict[str, list[str]] = {}
        file_is_deletion: dict[str, bool] = {}
        file_is_creation: dict[str, bool] = {}
        curr_file: str | None = None
        curr_old_file: str | None = None
        in_hunk = False
        hunk_old_remaining = 0
        hunk_new_remaining = 0

        for line in diff_text.splitlines():
            # Reset hunk state on explicit git diff file boundary
            if line.startswith("diff --git"):
                curr_file = None
                curr_old_file = None
                in_hunk = False
                hunk_old_remaining = 0
                hunk_new_remaining = 0
                continue

            hunk_exhausted = (hunk_old_remaining <= 0 and hunk_new_remaining <= 0)

            # Only recognize file headers outside of active hunk bodies
            if not in_hunk or hunk_exhausted:
                if line.startswith("--- a/") or line.startswith("--- "):
                    in_hunk = False
                    curr_old_file = line[6:].strip() if line.startswith("--- a/") else line[4:].strip()
                    curr_file = None
                    continue

                if line.startswith("+++ b/") or line.startswith("+++ "):
                    in_hunk = False
                    raw_target = line[4:].strip()
                    target = line[6:].strip() if line.startswith("+++ b/") else raw_target

                    # Deletion sentinel: Git emits +++ /dev/null (or +++ b/dev/null when old file != dev/null)
                    is_dev_null_sentinel = (
                        raw_target == "/dev/null"
                        or (target == "/dev/null")
                        or (target == "dev/null" and curr_old_file and curr_old_file not in ("dev/null", "/dev/null"))
                    )

                    if is_dev_null_sentinel and curr_old_file:
                        curr_file = curr_old_file
                        if curr_file in files_hunks and not file_is_deletion.get(curr_file):
                            return [], f"CONFLICTING_DIFF_SECTIONS_FOR_{curr_file}"
                        file_is_deletion[curr_file] = True
                        file_is_creation[curr_file] = False
                        if curr_file not in files_hunks:
                            files_hunks[curr_file] = []
                    else:
                        curr_file = target
                        is_new_creation = (curr_old_file in ("/dev/null", "dev/null"))
                        if curr_file in files_hunks:
                            if file_is_deletion.get(curr_file) or file_is_creation.get(curr_file) != is_new_creation:
                                return [], f"CONFLICTING_DIFF_SECTIONS_FOR_{curr_file}"
                        file_is_deletion[curr_file] = False
                        file_is_creation[curr_file] = is_new_creation
                        if curr_file not in files_hunks:
                            files_hunks[curr_file] = []
                    continue

            if line.startswith("@@"):
                in_hunk = True
                m = re.match(r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@", line)
                if m:
                    hunk_old_remaining = int(m.group(2)) if m.group(2) is not None else 1
                    hunk_new_remaining = int(m.group(4)) if m.group(4) is not None else 1
                else:
                    hunk_old_remaining = 1
                    hunk_new_remaining = 1
                if curr_file is not None:
                    files_hunks[curr_file].append(line)
                continue

            if curr_file is not None and in_hunk:
                # Handle git standard missing-newline metadata
                if line.startswith(r"\ No newline at end of file") or line.startswith(r"\ "):
                    files_hunks[curr_file].append(line)
                    continue

                if line.startswith(("+", "-", " ")):
                    files_hunks[curr_file].append(line)
                    if line.startswith("-"):
                        hunk_old_remaining -= 1
                    elif line.startswith("+"):
                        hunk_new_remaining -= 1
                    elif line.startswith(" "):
                        hunk_old_remaining -= 1
                        hunk_new_remaining -= 1
                elif not line.strip():
                    files_hunks[curr_file].append(" " + line)
                    hunk_old_remaining -= 1
                    hunk_new_remaining -= 1
                else:
                    # Malformed line inside hunk; return stable classification without raw line leakage
                    return [], "MALFORMED_DIFF_HUNK_LINE"

        if not files_hunks:
            return [], "NO_DIFF_TARGET_FILES_FOUND"

        results: list[tuple[str, str | None]] = []
        for target_rel, hunk_lines in files_hunks.items():
            if not hunk_lines:
                return [], f"EMPTY_DIFF_HUNKS_FOR_TARGET_{target_rel}"

            is_valid, path_err = validate_worktree_path_containment(target_rel, wt)
            if not is_valid:
                return [], path_err or "PATH_CONTAINMENT_VIOLATION"

            target_path = wt / target_rel
            is_creation = file_is_creation.get(target_rel, False)
            exists_on_disk = target_path.exists()
            exists_in_staged = (
                staged_files is not None
                and target_rel in staged_files
                and staged_files[target_rel] is not None
            )

            if is_creation:
                # Creation diff (--- /dev/null) requires that target does NOT already exist
                if exists_on_disk or exists_in_staged:
                    return [], f"CREATION_TARGET_ALREADY_EXISTS_{target_rel}"
                orig_content = ""
            else:
                if staged_files is not None and target_rel in staged_files:
                    orig_content = staged_files[target_rel] or ""
                elif exists_on_disk:
                    try:
                        orig_content = target_path.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError) as exc:
                        return [], f"TARGET_READ_FAILED_{target_rel}: {exc}"
                else:
                    return [], f"MODIFICATION_TARGET_NOT_FOUND_{target_rel}"

            try:
                new_content = self._apply_hunks(orig_content, hunk_lines)
            except ValueError as ve:
                return [], f"HUNK_APPLICATION_FAILED: {ve}"

            if file_is_deletion.get(target_rel):
                # Verify complete deletion: no surviving content must remain
                if new_content != "":
                    return [], f"INCOMPLETE_FILE_DELETION_FOR_TARGET_{target_rel}"
                final_content = None
            else:
                final_content = new_content

            if staged_files is not None:
                staged_files[target_rel] = final_content
            results.append((target_rel, final_content))

        return results, None

    def _apply_hunks(self, orig_content: str, hunk_lines: Sequence[str]) -> str:
        """Apply unified diff hunks to original string content with exact offset and count matching."""
        orig = orig_content.splitlines(keepends=True)
        out: list[str] = []
        i = 0

        in_hunk = False
        old_len = 0
        new_len = 0
        old_consumed = 0
        new_consumed = 0
        last_action: str | None = None

        for line in hunk_lines:
            if line.startswith("@@"):
                # If we were already in a hunk, verify that previous hunk consumed its declared line counts
                if in_hunk:
                    if old_consumed != old_len or new_consumed != new_len:
                        raise ValueError(
                            f"Diff hunk line count mismatch: expected old={old_len}, new={new_len}; "
                            f"consumed old={old_consumed}, new={new_consumed}"
                        )
                in_hunk = True
                m = re.match(r"^@@\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@", line)
                if not m:
                    raise ValueError("Malformed hunk header syntax")
                start_line = int(m.group(1))
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                old_consumed = 0
                new_consumed = 0
                last_action = None

                target_idx = start_line if old_len == 0 else max(0, start_line - 1)
                if target_idx > len(orig):
                    raise ValueError(
                        f"Hunk offset exceeds file length: target_idx={target_idx}, file_length={len(orig)}"
                    )
                if target_idx < i:
                    raise ValueError(
                        f"Overlapping or backwards hunk offset: target_idx={target_idx} is before current line {i}"
                    )
                if target_idx > i:
                    out.extend(orig[i:target_idx])
                    i = target_idx
                continue

            if not in_hunk:
                continue

            # Standard git missing newline indicator: strip trailing newline ONLY if preceded by added line (+)
            if line.startswith(r"\ No newline at end of file") or line.startswith(r"\ "):
                if last_action == "add":
                    if out and out[-1].endswith("\n"):
                        out[-1] = out[-1].rstrip("\r\n")
                last_action = None
                continue

            if line.startswith("-"):
                expected = line[1:].rstrip("\r\n")
                if i >= len(orig):
                    raise ValueError(f"Diff hunk deletion beyond EOF at line {i+1}")
                actual = orig[i].rstrip("\r\n")
                if actual != expected:
                    raise ValueError(f"Diff hunk deletion mismatch at line {i+1}")
                i += 1
                old_consumed += 1
                last_action = "del"
            elif line.startswith("+"):
                out.append(line[1:] + ("\n" if not line[1:].endswith("\n") else ""))
                new_consumed += 1
                last_action = "add"
            elif line.startswith(" "):
                expected = line[1:].rstrip("\r\n")
                if i >= len(orig):
                    raise ValueError(f"Diff hunk context beyond EOF at line {i+1}")
                actual = orig[i].rstrip("\r\n")
                if actual != expected:
                    raise ValueError(f"Diff hunk context mismatch at line {i+1}")
                out.append(orig[i])
                i += 1
                old_consumed += 1
                new_consumed += 1
                last_action = "ctx"

        if in_hunk:
            if old_consumed != old_len or new_consumed != new_len:
                raise ValueError(
                    f"Diff hunk line count mismatch at EOF: expected old={old_len}, new={new_len}; "
                    f"consumed old={old_consumed}, new={new_consumed}"
                )

        while i < len(orig):
            out.append(orig[i])
            i += 1

        return "".join(out)

    def execute(
        self,
        task: LinearTask,
        worktree_path: Path,
        canonical_base_sha: str,
    ) -> ExecutionResult:
        start_time = time.time()
        exec_id = f"exec-{uuid.uuid4().hex[:12]}"
        wt = Path(worktree_path).resolve()
        if not wt.exists() or not wt.is_dir():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                error_reason="INVALID_WORKTREE_PATH",
            )

        if not self.health():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                error_reason="EXECUTOR_UNHEALTHY",
            )

        if not self.execution_ready():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                error_reason="EXECUTOR_NOT_EXECUTION_READY",
            )

        # Collect bounded repository context (up to 10 text files, max 20KB total)
        # Prioritize task-relevant sources first, and exclude private artifacts (AGENTS.md:27-28)
        task_text = f"{task.title}\n{task.description}"
        referenced_tokens = set(re.findall(r"[a-zA-Z0-9_\-\./\\]+\.[a-zA-Z0-9_\-]+", task_text))

        sensitive_keywords = (
            "auth", "cred", "secret", "token", "password", "key",
            ".env", "id_rsa", "id_ed25519", "shadow", "private",
            "cert.pem", "key.pem", "dump.sql", "qdrant", "backup",
            "capsule", "memory_capsule",
        )
        private_dirs = {
            "memory_capsules", "backups", "logs", "credentials", "secrets",
            ".git", ".env", ".venv", "venv", "__pycache__",
        }

        # Query git ls-files to ensure git-ignored/untracked files are excluded (fail-closed if discovery fails)
        clean_env = get_clean_git_env()
        try:
            ls_res = self._runner(
                [
                    "git",
                    "-c", "core.fsmonitor=",
                    "-c", "core.hooksPath=/dev/null",
                    "-c", "core.attributesFile=/dev/null",
                    "-C", str(wt),
                    "ls-files",
                ],
                capture_output=True,
                text=True,
                env=clean_env,
                timeout=10,
            )
            if ls_res.returncode != 0:
                duration = time.time() - start_time
                return ExecutionResult(
                    status="FAILED",
                    changed_files=(),
                    execution_id=exec_id,
                    executor_name="SandboxedBrokerTaskExecutor",
                    evidence={"duration_sec": duration, "git_error": ls_res.stderr.strip() or f"EXIT_{ls_res.returncode}"},
                    error_reason="GIT_TRACKED_FILES_DISCOVERY_FAILED",
                )
            tracked_files = {Path(p.strip()).as_posix() for p in ls_res.stdout.splitlines() if p.strip()}
        except Exception as exc:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={"duration_sec": duration, "error_type": type(exc).__name__},
                error_reason="GIT_TRACKED_FILES_DISCOVERY_FAILED",
            )

        priority_entries: list[Path] = []
        other_entries: list[Path] = []

        for entry in sorted(wt.rglob("*")):
            if entry.is_symlink():
                continue
            if not entry.is_file():
                continue
            rel_parts = entry.relative_to(wt).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if any(part.lower() in private_dirs for part in rel_parts):
                continue
            if entry.name.endswith((".pyc", ".git", ".png", ".jpg", ".bin", ".exe", ".so", ".dll", ".whl")):
                continue

            rel_posix = entry.relative_to(wt).as_posix()
            if rel_posix not in tracked_files:
                continue

            # Privacy filter: exclude credential, token, or sensitive files
            lower_name = entry.name.lower()
            lower_rel = rel_posix.lower()
            if any(kw in lower_name or kw in lower_rel for kw in sensitive_keywords):
                continue

            # Validate containment before reading
            is_valid, _ = validate_worktree_path_containment(entry.relative_to(wt), wt)
            if not is_valid:
                continue
            try:
                resolved_entry = entry.resolve()
                resolved_entry.relative_to(wt)
            except (ValueError, OSError):
                continue

            if rel_posix in referenced_tokens or entry.name in referenced_tokens:
                priority_entries.append(entry)
            else:
                other_entries.append(entry)

        candidate_entries = priority_entries + other_entries
        repo_context_parts: list[str] = []
        total_bytes = 0
        max_bytes = 20_000

        for entry in candidate_entries:
            try:
                rel_posix = entry.relative_to(wt).as_posix()
                content = entry.read_text(encoding="utf-8", errors="replace")
                if total_bytes + len(content) > max_bytes:
                    content = content[: max(0, max_bytes - total_bytes)] + "\n... [truncated]"
                repo_context_parts.append(f"--- File: {rel_posix} ---\n{content}\n")
                total_bytes += len(content)
                if total_bytes >= max_bytes or len(repo_context_parts) >= 10:
                    break
            except Exception:
                continue

        repo_context_str = "\n".join(repo_context_parts) if repo_context_parts else "None"

        # Build prompt for Model Gateway
        prompt = (
            f"Objective: Implement the task described below in this repository worktree.\n\n"
            f"Linear Task ID: {task.id}\n"
            f"Title: {task.title}\n"
            f"Description:\n{task.description}\n\n"
            f"Canonical Base SHA: {canonical_base_sha}\n\n"
            f"Existing Repository Files:\n{repo_context_str}\n\n"
            f"Constraints:\n"
            f"- Confine all creations and edits to this repository worktree only.\n"
            f"- Do not modify production credentials, databases, or any path outside this directory.\n"
            f"- Do not commit or push to remote.\n"
            f"- Make only the minimal necessary coherent code and documentation changes.\n\n"
            f"Required Output Format:\n"
            f"Provide the complete, updated file content in a fenced code block with file:<path> on the first line or fence header, replacing any stubs, e.g.:\n"
            f"```python file:relative/path/to/file.py\n"
            f"<complete working file content>\n"
            f"```\n"
            f"Or as a standard unified diff block with --- a/<path> and +++ b/<path>.\n"
        )

        start_time = time.time()
        try:
            if not self.model_gateway:
                return ExecutionResult(
                    status="FAILED",
                    changed_files=(),
                    execution_id=exec_id,
                    executor_name="SandboxedBrokerTaskExecutor",
                    error_reason="NO_MODEL_GATEWAY_CONFIGURED",
                )

            model_output = self.model_gateway.generate_mutation(prompt, task, wt)
        except Exception as exc:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={"duration_sec": duration, "error_type": type(exc).__name__},
                error_reason="MODEL_INFERENCE_FAILED",
            )

        # Parse proposed mutations
        extracted_edits, parse_err = self._parse_mutation_payload(model_output, wt)
        if parse_err:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={
                    "duration_sec": duration,
                    "output_length": len(model_output),
                    "output_sha256": hashlib.sha256(model_output.encode("utf-8")).hexdigest()[:16],
                },
                error_reason=parse_err,
            )

        # Validate path containment for all target files
        for target_rel, _ in extracted_edits:
            is_valid, path_err = validate_worktree_path_containment(target_rel, wt)
            if not is_valid:
                duration = time.time() - start_time
                return ExecutionResult(
                    status="FAILED",
                    changed_files=(),
                    execution_id=exec_id,
                    executor_name="SandboxedBrokerTaskExecutor",
                    evidence={"duration_sec": duration, "target_basename": Path(target_rel).name},
                    error_reason=path_err or "PATH_CONTAINMENT_VIOLATION",
                )

        # Resolve canonical target paths relative to worktree root before ScopeGate check
        canonical_targets: list[str] = []
        for target_rel, _ in extracted_edits:
            try:
                target_full = (wt / target_rel).resolve()
                norm_rel = target_full.relative_to(wt).as_posix()
            except Exception:
                norm_rel = str(target_rel).replace("\\", "/")
                while norm_rel.startswith("./"):
                    norm_rel = norm_rel[2:]
                while norm_rel.startswith("/"):
                    norm_rel = norm_rel[1:]
            # Normalize trailing dots and spaces from path segments to prevent Windows alias escapes
            clean_segments = [s.rstrip(". ") for s in norm_rel.split("/") if s]
            canonical_targets.append("/".join(clean_segments))

        # Check ScopeGate on canonical resolved targets before writing any mutations to disk
        from ai_engineering.linear_dispatcher.scope_gate import ScopeGate
        scope_ok, scope_reason = ScopeGate.evaluate_changed_files(canonical_targets)
        if not scope_ok:
            duration = time.time() - start_time
            reason_str = scope_reason.value if hasattr(scope_reason, "value") else str(scope_reason)
            return ExecutionResult(
                status="BLOCKED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={"duration_sec": duration, "scope_reason": reason_str},
                error_reason=f"SCOPE_GATE_VIOLATION_{reason_str}",
            )

        # Apply mutations with hardlink escape prevention
        try:
            for target_rel, content in extracted_edits:
                target_full = (wt / target_rel).resolve()
                if content is None:
                    # Deletion
                    if target_full.exists():
                        target_full.unlink()
                    continue

                if target_full.exists():
                    try:
                        st = target_full.stat()
                        if hasattr(st, "st_nlink") and st.st_nlink > 1:
                            duration = time.time() - start_time
                            return ExecutionResult(
                                status="FAILED",
                                changed_files=(),
                                execution_id=exec_id,
                                executor_name="SandboxedBrokerTaskExecutor",
                                evidence={"duration_sec": duration, "error_type": "HardlinkEscape"},
                                error_reason="HARDLINK_ESCAPE_DETECTED",
                            )
                    except Exception:
                        pass
                target_full.parent.mkdir(parents=True, exist_ok=True)
                target_full.write_text(content, encoding="utf-8")
        except Exception as exc:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={"duration_sec": duration, "error_type": type(exc).__name__},
                error_reason=f"FILE_MUTATION_WRITE_ERROR: {exc}",
            )

        # Always verify Bubblewrap sandbox containment boundary on worktree
        probe_ok, probe_err = self._run_sandboxed_probe(wt)
        if not probe_ok:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={"duration_sec": duration, "error_type": "SandboxProbeFailed"},
                error_reason="SANDBOX_VALIDATION_FAILED",
            )

        # Run sandboxed validation (e.g. py_compile on python files)
        py_files = [
            rel for rel, content in extracted_edits
            if content is not None and str(rel).lower().endswith(".py")
        ]
        for py_file in py_files:
            val_ok, val_err = self._run_sandboxed_syntax_check(wt, py_file)
            if not val_ok:
                duration = time.time() - start_time
                return ExecutionResult(
                    status="FAILED",
                    changed_files=(),
                    execution_id=exec_id,
                    executor_name="SandboxedBrokerTaskExecutor",
                    evidence={"duration_sec": duration, "file_basename": Path(py_file).name},
                    error_reason=f"SYNTAX_VALIDATION_FAILED_{py_file}",
                )

        duration = time.time() - start_time

        # Independently detect git changes
        try:
            changed_files = self._detect_changed_files(wt)
        except Exception as exc:
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={"duration_sec": duration, "error_type": type(exc).__name__},
                error_reason=f"DETECT_CHANGED_FILES_ERROR: {exc}",
            )
        if not changed_files:
            return ExecutionResult(
                status="BLOCKED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="SandboxedBrokerTaskExecutor",
                evidence={"duration_sec": duration},
                error_reason="NO_CHANGES_PRODUCED",
            )

        return ExecutionResult(
            status="SUCCESS",
            changed_files=changed_files,
            execution_id=exec_id,
            executor_name="SandboxedBrokerTaskExecutor",
            evidence={
                "duration_sec": duration,
                "changed_count": len(changed_files),
            },
        )
