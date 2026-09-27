"""GitHub service for PR creation, CI monitoring, and exact-head attestation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Protocol, runtime_checkable

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    LinearTask,
)


@dataclass(frozen=True, slots=True)
class PRCreationResult:
    pr_number: int
    pr_url: str
    base_sha: str
    head_sha: str
    is_draft: bool


@dataclass(frozen=True, slots=True)
class CIStatusResult:
    overall_status: str  # "PASS", "FAIL", "PENDING", "BLOCKED"
    head_sha: str
    runs: list[dict[str, Any]]
    details: dict[str, str]


@runtime_checkable
class IGitHubService(Protocol):
    def create_draft_pr(
        self,
        task: LinearTask,
        branch_name: str,
        base_sha: str,
        head_sha: str,
        cwd: Path | str,
    ) -> tuple[bool, PRCreationResult | None, str | None]:
        ...

    def get_ci_status(
        self,
        repo: str,
        expected_head_sha: str,
        pr_number: int,
    ) -> tuple[bool, CIStatusResult | None, BlockReasonCode | None]:
        ...


class GitHubService(IGitHubService):
    """Production GitHub client using gh CLI."""

    def __init__(self, repo: str = "life2boat/hermes") -> None:
        self._repo = repo

    def create_draft_pr(
        self,
        task: LinearTask,
        branch_name: str,
        base_sha: str,
        head_sha: str,
        cwd: Path | str,
    ) -> tuple[bool, PRCreationResult | None, str | None]:
        pr_title = f"feat({task.id.lower()}): {task.title}"
        pr_body = f"""## Purpose & Scope
Autonomous task dispatch for Linear issue **[{task.id}]({task.url})**.

- **Linear Issue:** [{task.id}]({task.url})
- **Exact Base SHA:** `{base_sha}`
- **Exact Head SHA:** `{head_sha}`
- **Branch:** `{branch_name}`
- **Scope Classification:** Controlled shadow task

## Production Boundary
Production untouched (0 mutations, 0 deployments, 0 DB/Qdrant changes).
"""
        cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            self._repo,
            "--draft",
            "--base",
            "main",
            "--head",
            branch_name,
            "--title",
            pr_title,
            "--body",
            pr_body,
        ]
        res = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
        if res.returncode != 0:
            return False, None, res.stderr or res.stdout

        pr_url = res.stdout.strip()
        # Parse PR number from URL
        try:
            pr_num = int(pr_url.rstrip("/").split("/")[-1])
        except (ValueError, IndexError):
            pr_num = 0

        return True, PRCreationResult(
            pr_number=pr_num,
            pr_url=pr_url,
            base_sha=base_sha,
            head_sha=head_sha,
            is_draft=True,
        ), None

    def get_ci_status(
        self,
        repo: str,
        expected_head_sha: str,
        pr_number: int,
    ) -> tuple[bool, CIStatusResult | None, BlockReasonCode | None]:
        cmd = [
            "gh",
            "run",
            "list",
            "--commit",
            expected_head_sha,
            "--repo",
            repo or self._repo,
            "--json",
            "headSha,conclusion,status,name,databaseId",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            return False, None, BlockReasonCode.CI_FAILED

        try:
            runs = json.loads(res.stdout)
        except json.JSONDecodeError:
            return False, None, BlockReasonCode.CI_FAILED

        if not runs:
            # No runs yet for this commit
            return False, CIStatusResult(
                overall_status="PENDING",
                head_sha=expected_head_sha,
                runs=[],
                details={},
            ), None

        details: dict[str, str] = {}
        for r in runs:
            run_sha = r.get("headSha", "")
            if run_sha != expected_head_sha:
                return False, None, BlockReasonCode.CI_SHA_MISMATCH

            name = r.get("name", "unknown")
            conclusion = r.get("conclusion")
            status = r.get("status")

            if status != "completed":
                details[name] = f"in_progress ({status})"
                return False, CIStatusResult(
                    overall_status="PENDING",
                    head_sha=expected_head_sha,
                    runs=runs,
                    details=details,
                ), None

            if conclusion not in ("success", "skipped", "neutral"):
                details[name] = f"failed ({conclusion})"
                return False, CIStatusResult(
                    overall_status="FAIL",
                    head_sha=expected_head_sha,
                    runs=runs,
                    details=details,
                ), BlockReasonCode.CI_FAILED

            details[name] = conclusion or "success"

        return True, CIStatusResult(
            overall_status="PASS",
            head_sha=expected_head_sha,
            runs=runs,
            details=details,
        ), None
