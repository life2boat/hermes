"""Task execution contracts and adapters for Hermes Linear Dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Protocol, Sequence
import uuid

from ai_engineering.linear_dispatcher.contracts import LinearTask


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
            res = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                check=True,
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
        self._runner = runner or subprocess.run

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
            "--bind", ephemeral_home_path, "/tmp/codex-home",
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
            "        os.read(fd, 1024)\n"
            "    if os.path.exists(target):\n"
            "        os.unlink(target)\n"
            "    libc.inotify_rm_watch(fd, wd)\n"
            "    os.close(fd)\n"
            "    sys.exit(0)\n"
            "except Exception:\n"
            "    sys.exit(1)\n"
        )

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
            proc = subprocess.Popen(
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
            proc = subprocess.Popen(
                [
                    "python3",
                    "-c",
                    watcher_py,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        self._watcher_proc = proc

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
            except Exception:
                pass
        else:
            shutil.rmtree(ephemeral_home, ignore_errors=True)

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

        if not self.health():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                error_reason="LINUX_CODEX_UNAVAILABLE",
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
        except Exception as exc:
            self._cleanup_ephemeral_credentials(ephemeral_home)
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"error": str(exc)},
                error_reason="CREDENTIAL_ISOLATION_FAILED",
            )

        bwrap_args = self.build_bwrap_command(linux_wt, ephemeral_home_path=ephemeral_home)
        cmd = ["wsl.exe", "-d", self.distro, "--"] + bwrap_args if self.is_windows else bwrap_args

        start_time = time.time()
        try:
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
        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"duration_sec": duration, "timed_out": True},
                error_reason="CODEX_EXEC_TIMEOUT",
            )
        except Exception as exc:
            duration = time.time() - start_time
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="LinuxCodexTaskExecutor",
                evidence={"duration_sec": duration, "error": str(exc)},
                error_reason="CODEX_EXEC_EXCEPTION",
            )
        finally:
            self._cleanup_ephemeral_credentials(ephemeral_home)

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
            res = self._runner(
                ["git", "-C", str(worktree_path), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
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
