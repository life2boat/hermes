"""Task execution contracts and adapters for Hermes Linear Dispatcher."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
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
    ) -> None:
        self.codex_bin = codex_bin or shutil.which("codex")
        self.timeout_sec = timeout_sec
        self.model = model

    def health(self) -> bool:
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
        if not self.health():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                error_reason="CODEX_EXECUTOR_UNAVAILABLE",
            )

        wt = Path(worktree_path).resolve()
        if not wt.exists() or not wt.is_dir():
            return ExecutionResult(
                status="FAILED",
                changed_files=(),
                execution_id=exec_id,
                executor_name="CodexTaskExecutor",
                error_reason="INVALID_WORKTREE_PATH",
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
            for line in res.stdout.strip().splitlines():
                if not line.strip():
                    continue
                # Format: XY <file> or XY <old> -> <new>
                parts = line[3:].strip().split(" -> ")
                path_str = parts[-1].strip('"')
                files.append(path_str)
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
