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
    """Production task executor running Codex inside an OS-level Bubblewrap sandbox.

    Enforces strict filesystem isolation:
    - Root filesystem mounted read-only (--ro-bind / /)
    - Host Windows drives masked (--tmpfs /mnt)
    - Host daemons / Docker socket masked (--tmpfs /run)
    - Isolated tmpfs for temporary files (--tmpfs /tmp)
    - Ephemeral CODEX_HOME with auth.json mounted read-only (--ro-bind <creds> /tmp/codex-home/auth.json)
    - ONLY the designated task worktree mounted read-write (--bind <wt> <wt>)
    - Supports running natively on Linux or from Windows via WSL (wsl.exe -d <distro>)
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

    def health(self) -> bool:
        """Verify bwrap, codex, and credentials file are available in the target Linux environment."""
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
                return cred_check.returncode == 0
            except Exception:
                return False
        else:
            if not shutil.which(self.bwrap_bin):
                return False
            if not shutil.which(self.codex_bin):
                return False
            if not Path(self.credentials_path).is_file():
                return False
            return True

    def build_bwrap_command(self, worktree_linux_path: str) -> list[str]:
        """Construct the bubblewrap command enforcing fail-closed isolation."""
        cmd = [
            self.bwrap_bin,
            "--ro-bind", "/", "/",
            "--tmpfs", "/mnt",
        ]
        if self.is_windows or Path("/mnt/wsl/resolv.conf").exists():
            cmd.extend(["--dir", "/mnt/wsl", "--ro-bind", "/mnt/wsl/resolv.conf", "/mnt/wsl/resolv.conf"])

        cmd.extend([
            "--tmpfs", "/run",
            "--tmpfs", "/tmp",
            "--dir", "/tmp/codex-home",
            "--ro-bind", self.credentials_path, "/tmp/codex-home/auth.json",
            "--dev", "/dev",
            "--proc", "/proc",
            "--bind", worktree_linux_path, worktree_linux_path,
            "--setenv", "CODEX_HOME", "/tmp/codex-home",
            "--setenv", "TMPDIR", "/tmp",
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
                if translated.startswith(("/mnt/c/Windows", "/mnt/c/Users/Oleg/AppData")):
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

        bwrap_args = self.build_bwrap_command(linux_wt)
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
