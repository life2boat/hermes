"""Production GitHub client using standard API and repository credentials."""

import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any

from ai_engineering.linear_dispatcher.contracts import BlockReasonCode, LinearTask
from ai_engineering.linear_dispatcher.github_service import IGitHubService, PRCreationResult, CIStatusResult

class GitHubProductionService(IGitHubService):
    def __init__(self, repo: str = "life2boat/hermes", token: str | None = None) -> None:
        self._repo = repo
        self._token = token or os.environ.get("GITHUB_TOKEN")
        if not self._token:
            raise ValueError("GITHUB_TOKEN environment variable is required")
        self.base_url = "https://api.github.com"

    def _request(self, method: str, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/repos/{self._repo}/{endpoint}"
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {self._token}",
            "User-Agent": "Hermes-Dispatcher-Worker"
        }
        body = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub API request failed: {e}")

    def create_draft_pr(
        self,
        task: LinearTask,
        branch_name: str,
        base_sha: str,
        head_sha: str,
        cwd: Path | str,
    ) -> tuple[bool, PRCreationResult | None, str | None]:
        
        # PR Idempotency check
        try:
            search_res = self._request("GET", f"pulls?head=life2boat:{branch_name}&state=open")
            if search_res and len(search_res) == 1:
                pr = search_res[0]
                actual_head = pr.get("head", {}).get("sha", "")
                actual_base = pr.get("base", {}).get("ref", "")
                is_draft = pr.get("draft", False)
                if not is_draft:
                    return False, None, "EXISTING_PR_NOT_DRAFT"
                if actual_head != head_sha:
                    return False, None, f"EXISTING_PR_HEAD_MISMATCH_{actual_head}_VS_{head_sha}"
                if actual_base != "main":
                    return False, None, f"EXISTING_PR_BASE_MISMATCH_{actual_base}"
                return True, PRCreationResult(
                    pr_number=pr.get("number", 0),
                    pr_url=pr.get("html_url", ""),
                    base_sha=base_sha,
                    head_sha=head_sha,
                    is_draft=True,
                ), None
            elif search_res and len(search_res) > 1:
                return False, None, "MULTIPLE_CONFLICTING_PRS"
        except Exception as e:
            if "EXISTING_PR" in str(e):
                return False, None, str(e)
            pass

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
        try:
            res = self._request("POST", "pulls", {
                "title": pr_title,
                "body": pr_body,
                "head": branch_name,
                "base": "main",
                "draft": True
            })
            return True, PRCreationResult(
                pr_number=res.get("number", 0),
                pr_url=res.get("html_url", ""),
                base_sha=base_sha,
                head_sha=head_sha,
                is_draft=True
            ), None
        except Exception as e:
            return False, None, str(e)

    def get_ci_status(
        self,
        repo: str,
        expected_head_sha: str,
        pr_number: int,
    ) -> tuple[bool, CIStatusResult | None, BlockReasonCode | None]:
        # Using GitHub check-runs API with exact head verification and required check suite
        try:
            res = self._request("GET", f"commits/{expected_head_sha}/check-runs")
            check_runs = res.get("check_runs", [])
        except Exception:
            return False, None, BlockReasonCode.CI_FAILED

        if not check_runs:
            return False, CIStatusResult(
                overall_status="PENDING",
                head_sha=expected_head_sha,
                runs=[],
                details={},
            ), None

        # Verify head SHA scoping on returned check runs
        for r in check_runs:
            run_head = r.get("head_sha", "")
            if run_head and run_head != expected_head_sha:
                return False, CIStatusResult(
                    overall_status="FAIL",
                    head_sha=expected_head_sha,
                    runs=check_runs,
                    details={"error": f"CHECK_RUN_HEAD_MISMATCH_{run_head}_VS_{expected_head_sha}"},
                ), BlockReasonCode.CI_SHA_MISMATCH

        details: dict[str, str] = {}
        completed_success = 0
        for r in check_runs:
            name = r.get("name", "unknown")
            status = r.get("status")
            conclusion = r.get("conclusion")

            if status != "completed":
                details[name] = f"in_progress ({status})"
                return False, CIStatusResult(
                    overall_status="PENDING",
                    head_sha=expected_head_sha,
                    runs=check_runs,
                    details=details,
                ), None

            if conclusion not in ("success", "skipped", "neutral"):
                details[name] = f"failed ({conclusion})"
                return False, CIStatusResult(
                    overall_status="FAIL",
                    head_sha=expected_head_sha,
                    runs=check_runs,
                    details=details,
                ), BlockReasonCode.CI_FAILED

            details[name] = conclusion or "success"
            if conclusion == "success":
                completed_success += 1

        # Must have completed checks to prove test suite ran
        if len(check_runs) < 2 and completed_success < 1:
            return False, CIStatusResult(
                overall_status="PENDING",
                head_sha=expected_head_sha,
                runs=check_runs,
                details={"status": "INSUFFICIENT_CHECK_RUNS"},
            ), None

        return True, CIStatusResult(
            overall_status="PASS",
            head_sha=expected_head_sha,
            runs=check_runs,
            details=details,
        ), None
