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
    def __init__(
        self,
        repo: str = "life2boat/hermes",
        token: str | None = None,
        required_checks: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> None:
        self._repo = repo
        self._token = token or os.environ.get("GITHUB_TOKEN")
        if not self._token:
            raise ValueError("GITHUB_TOKEN environment variable is required")
        self.base_url = "https://api.github.com"
        self._required_checks = tuple(required_checks) if required_checks is not None else ("tests",)

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
            # Do not silently fall through on search errors
            return False, None, f"PR_SEARCH_FAILED: {e}"

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
            pr_num = res.get("number", 0)
            actual_head = res.get("head", {}).get("sha", "")
            actual_base = res.get("base", {}).get("ref", "")
            is_draft = res.get("draft", False)
            if not is_draft:
                return False, None, "POST_PR_NOT_DRAFT"
            if actual_head != head_sha:
                return False, None, f"POST_PR_HEAD_MISMATCH_{actual_head}_VS_{head_sha}"
            if actual_base != "main":
                return False, None, f"POST_PR_BASE_MISMATCH_{actual_base}"

            # Reread PR via GET to verify actual state from API
            reread = self._request("GET", f"pulls/{pr_num}")
            if not reread.get("draft", False):
                return False, None, "REREAD_PR_NOT_DRAFT"
            if reread.get("head", {}).get("sha", "") != head_sha:
                return False, None, "REREAD_PR_HEAD_MISMATCH"
            if reread.get("base", {}).get("ref", "") != "main":
                return False, None, "REREAD_PR_BASE_MISMATCH"

            return True, PRCreationResult(
                pr_number=pr_num,
                pr_url=reread.get("html_url", res.get("html_url", "")),
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
        expected_base_sha: str | None = None,
        required_checks: tuple[str, ...] | list[str] | set[str] | None = None,
    ) -> tuple[bool, CIStatusResult | None, BlockReasonCode | None]:
        # Step 1: Verify current PR identity, draft state, head SHA, and base ref/SHA
        if pr_number:
            try:
                pr_res = self._request("GET", f"pulls/{pr_number}")
                if not pr_res.get("draft", False):
                    return False, None, BlockReasonCode.REREAD_VERIFICATION_FAILED
                if pr_res.get("head", {}).get("sha", "") != expected_head_sha:
                    return False, None, BlockReasonCode.CI_SHA_MISMATCH
                if pr_res.get("base", {}).get("ref", "") != "main":
                    return False, None, BlockReasonCode.REREAD_VERIFICATION_FAILED
                if expected_base_sha:
                    observed_base_sha = pr_res.get("base", {}).get("sha", "")
                    if observed_base_sha and observed_base_sha != expected_base_sha:
                        return False, None, BlockReasonCode.CANONICAL_MAIN_DRIFT
            except Exception:
                return False, None, BlockReasonCode.CI_FAILED

        # Step 2: Retrieve all check runs using pagination with fail-closed truncated page check
        check_runs = []
        page = 1
        per_page = 100
        try:
            while True:
                res = self._request("GET", f"commits/{expected_head_sha}/check-runs?per_page={per_page}&page={page}")
                runs_page = res.get("check_runs", [])
                total_count = res.get("total_count", len(runs_page))
                if not runs_page and len(check_runs) < total_count:
                    # Pagination terminated prematurely before total_count reached
                    return False, None, BlockReasonCode.CI_FAILED
                check_runs.extend(runs_page)
                if len(check_runs) >= total_count or not runs_page:
                    break
                page += 1
        except Exception:
            return False, None, BlockReasonCode.CI_FAILED

        if not check_runs:
            return False, CIStatusResult(
                overall_status="PENDING",
                head_sha=expected_head_sha,
                runs=[],
                details={},
            ), None

        # Step 3: Exact head SHA and producer verification on every check run
        for r in check_runs:
            run_head = r.get("head_sha")
            if not run_head or run_head != expected_head_sha:
                return False, CIStatusResult(
                    overall_status="FAIL",
                    head_sha=expected_head_sha,
                    runs=check_runs,
                    details={"error": f"CHECK_RUN_HEAD_MISMATCH_{run_head}_VS_{expected_head_sha}"},
                ), BlockReasonCode.CI_SHA_MISMATCH

            app = r.get("app")
            if isinstance(app, dict):
                app_slug = app.get("slug")
                if app_slug and app_slug not in ("github-actions", "hermes-release-gate", "ci"):
                    return False, CIStatusResult(
                        overall_status="FAIL",
                        head_sha=expected_head_sha,
                        runs=check_runs,
                        details={"error": f"UNTRUSTED_CHECK_PRODUCER_{app_slug}"},
                    ), BlockReasonCode.CI_FAILED

        # Step 4: Verify mandatory required check presence
        req_checks = set(required_checks) if required_checks is not None else set(self._required_checks)
        present_names = {r.get("name", "") for r in check_runs}
        missing_reqs = [
            req for req in req_checks
            if not any(
                name == req
                or name.startswith(f"{req} ")
                or name.startswith(f"{req}/")
                or name.startswith(f"{req}:")
                for name in present_names
            )
        ]
        if missing_reqs:
            return False, CIStatusResult(
                overall_status="PENDING",
                head_sha=expected_head_sha,
                runs=check_runs,
                details={"missing_required_checks": ", ".join(missing_reqs)},
            ), None

        # Step 5: Check run statuses and outcomes
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

            # Required checks must not be skipped or neutral: they must succeed
            is_req = any(
                name == req
                or name.startswith(f"{req} ")
                or name.startswith(f"{req}/")
                or name.startswith(f"{req}:")
                for req in req_checks
            )
            if is_req and conclusion != "success":
                details[name] = f"required_check_not_successful ({conclusion})"
                return False, CIStatusResult(
                    overall_status="FAIL",
                    head_sha=expected_head_sha,
                    runs=check_runs,
                    details=details,
                ), BlockReasonCode.CI_FAILED

            details[name] = conclusion or "success"
            if conclusion == "success":
                completed_success += 1

        if completed_success < 1:
            return False, CIStatusResult(
                overall_status="PENDING",
                head_sha=expected_head_sha,
                runs=check_runs,
                details={"status": "NO_SUCCESSFUL_CHECK_RUNS"},
            ), None

        return True, CIStatusResult(
            overall_status="PASS",
            head_sha=expected_head_sha,
            runs=check_runs,
            details=details,
        ), None

    def get_pr(self, pr_number: int) -> tuple[bool, PRCreationResult | None, str | None]:
        try:
            pr = self._request("GET", f"pulls/{pr_number}")
            if not pr:
                return False, None, "PR_NOT_FOUND"
            is_draft = pr.get("draft", False)
            head_sha = pr.get("head", {}).get("sha", "")
            base_ref = pr.get("base", {}).get("ref", "")
            base_sha = pr.get("base", {}).get("sha", "")
            pr_url = pr.get("html_url", "")
            if not is_draft:
                return False, None, "EXISTING_PR_NOT_DRAFT"
            if base_ref != "main":
                return False, None, f"EXISTING_PR_BASE_MISMATCH_{base_ref}"
            return True, PRCreationResult(
                pr_number=pr_number,
                pr_url=pr_url,
                base_sha=base_sha,
                head_sha=head_sha,
                is_draft=is_draft,
            ), None
        except Exception as e:
            return False, None, f"PR_GET_FAILED: {e}"
