"""Real GitHub CI Status Provider for Hermes Autonomous Supervisor (Task 7.7).

Status / Observation-only:
- Observes GitHub CI check-runs for an exact commit SHA.
- Strict SHA binding: requested_sha == observed_sha, else CI_SHA_MISMATCH.
- Required technical checks: Tests, Lint, Typecheck, Nix, Agent Release Gate, Supply Chain Audit, History Check.
- Contributor Attribution: Strictly governance-only, never blocks technical CI completion.
- Produces hermes.ci-status-snapshot.v1 and hermes.ci-status-receipt.v1.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from ai_engineering.supervisor.autonomous_run import CIStatusProvider


class CIProviderError(Exception):
    """Base error for CI Status Provider."""
    code: str = "CI_PROVIDER_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class CIProviderUnavailableError(CIProviderError):
    code: str = "CI_PROVIDER_UNAVAILABLE"


class CIShaMismatchError(CIProviderError):
    code: str = "CI_SHA_MISMATCH"


class CIRequiredCheckMissingError(CIProviderError):
    code: str = "CI_REQUIRED_CHECK_MISSING"


class CITimeoutError(CIProviderError):
    code: str = "CI_TIMEOUT"


REQUIRED_TECHNICAL_CHECKS: tuple[str, ...] = (
    "Tests",
    "Lint",
    "Typecheck",
    "Nix",
    "Agent Release Gate",
    "Supply Chain Audit",
    "History Check",
)

GOVERNANCE_ONLY_CHECKS: tuple[str, ...] = (
    "Contributor Attribution",
    "attribution",
)


@dataclass(frozen=True, slots=True)
class CICheckResult:
    name: str
    status: str  # "queued", "in_progress", "completed"
    conclusion: str  # "success", "failure", "cancelled", "skipped", "neutral", "timed_out"
    details_url: str = ""


@dataclass(frozen=True, slots=True)
class CIStatusSnapshot:
    schema_version: str  # "hermes.ci-status-snapshot.v1"
    sha: str
    overall_status: str  # "SUCCESS", "FAILURE", "IN_PROGRESS", "TIMED_OUT", "MISSING", "SHA_MISMATCH"
    checks: tuple[CICheckResult, ...]
    required_checks: tuple[str, ...]
    observed_at_utc: str


@dataclass(frozen=True, slots=True)
class CIStatusReceipt:
    schema_version: str  # "hermes.ci-status-receipt.v1"
    receipt_id: str
    run_id: str
    sha: str
    overall_status: str
    checks_summary: dict[str, str]
    created_at_utc: str


def compute_ci_receipt_digest(receipt_data: Mapping[str, Any]) -> str:
    canonical = json.dumps(receipt_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_ci_checks(
    requested_sha: str,
    observed_sha: str,
    checks: Sequence[CICheckResult],
    required_checks: Sequence[str] = REQUIRED_TECHNICAL_CHECKS,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Evaluates checks list against required checks.

    Returns:
        (overall_status, passed_checks, pending_or_failed_checks)
    """
    if observed_sha != requested_sha:
        return "SHA_MISMATCH", (), ()

    checks_by_name: dict[str, CICheckResult] = {}
    for c in checks:
        checks_by_name[c.name.strip().lower()] = c

    # Filter out governance-only checks from blocking requirements
    def is_governance(name: str) -> bool:
        low = name.strip().lower()
        return any(gov.lower() in low for gov in GOVERNANCE_ONLY_CHECKS)

    has_pending = False
    has_failure = False
    has_missing = False

    passed: list[str] = []
    issues: list[str] = []

    all_completed = True
    for c in checks:
        if c.status.lower() in ("queued", "in_progress"):
            all_completed = False

    for req in required_checks:
        if is_governance(req):
            continue

        req_low = req.strip().lower()
        matched: CICheckResult | None = None
        for name_low, c in checks_by_name.items():
            if req_low in name_low or name_low in req_low:
                matched = c
                break

        if not matched:
            if all_completed:
                has_missing = True
                issues.append(f"MISSING: {req}")
            else:
                has_pending = True
                issues.append(f"NOT_YET_QUEUED: {req}")
            continue

        st = matched.status.lower()
        cc = (matched.conclusion or "").lower()

        if st in ("queued", "in_progress"):
            has_pending = True
            issues.append(f"PENDING: {matched.name} ({st})")
        elif cc in ("success", "skipped", "neutral"):
            passed.append(matched.name)
        elif cc in ("failure", "cancelled", "timed_out", "action_required"):
            has_failure = True
            issues.append(f"FAILED: {matched.name} ({cc})")
        else:
            # Unknown conclusion
            has_failure = True
            issues.append(f"UNKNOWN_CONCLUSION: {matched.name} ({cc})")

    if has_failure:
        return "FAILURE", tuple(passed), tuple(issues)
    if has_missing:
        return "MISSING", tuple(passed), tuple(issues)
    if has_pending:
        return "IN_PROGRESS", tuple(passed), tuple(issues)

    return "SUCCESS", tuple(passed), ()


class GitHubCIStatusProvider(CIStatusProvider):
    """Real GitHub CI Status Provider supporting CLI and custom runner."""

    def __init__(
        self,
        repository: str = "life2boat/hermes",
        required_checks: Sequence[str] = REQUIRED_TECHNICAL_CHECKS,
        timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 10.0,
        runner: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]]]]] | None = None,
    ) -> None:
        self.repository = repository
        self.required_checks = tuple(required_checks)
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.runner = runner
        self.latest_receipt: CIStatusReceipt | None = None

    async def _fetch_checks_default(self, repo: str, sha: str) -> tuple[str, list[dict[str, Any]]]:
        cmd = [
            "gh", "api", f"repos/{repo}/commits/{sha}/check-runs",
            "--jq", "{sha: .check_runs[0].head_sha, check_runs: [.check_runs[] | {name: .name, status: .status, conclusion: .conclusion, details_url: .details_url}]}"
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except Exception as e:
            raise CIProviderUnavailableError(f"Failed to query gh api for {sha}: {e}") from e

        if proc.returncode != 0:
            err = stderr_bytes.decode("utf-8", errors="replace")
            raise CIProviderUnavailableError(f"gh api returned {proc.returncode}: {err}")

        out_str = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not out_str:
            return sha, []

        try:
            data = json.loads(out_str)
            observed_sha = data.get("sha") or sha
            runs = data.get("check_runs", [])
            return observed_sha, runs
        except Exception as e:
            raise CIProviderUnavailableError(f"Failed to parse gh api output: {e}") from e

    async def wait_for_ci(
        self,
        run_id: str,
        sha: str,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
    ) -> CIStatusSnapshot:
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        interval = poll_interval_seconds if poll_interval_seconds is not None else self.poll_interval_seconds

        start_time = time.monotonic()

        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout:
                snapshot = CIStatusSnapshot(
                    schema_version="hermes.ci-status-snapshot.v1",
                    sha=sha,
                    overall_status="TIMED_OUT",
                    checks=(),
                    required_checks=self.required_checks,
                    observed_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                self._record_receipt(run_id, sha, snapshot)
                return snapshot

            try:
                if self.runner is not None:
                    observed_sha, raw_checks = await self.runner(self.repository, sha)
                else:
                    observed_sha, raw_checks = await self._fetch_checks_default(self.repository, sha)
            except CIProviderError:
                raise
            except Exception as e:
                raise CIProviderUnavailableError(f"Error fetching CI status: {e}") from e

            checks = tuple(
                CICheckResult(
                    name=str(c.get("name", "")),
                    status=str(c.get("status", "")),
                    conclusion=str(c.get("conclusion") or ""),
                    details_url=str(c.get("details_url", "")),
                )
                for c in raw_checks
            )

            overall_status, passed, issues = evaluate_ci_checks(
                requested_sha=sha,
                observed_sha=observed_sha,
                checks=checks,
                required_checks=self.required_checks,
            )

            snapshot = CIStatusSnapshot(
                schema_version="hermes.ci-status-snapshot.v1",
                sha=observed_sha,
                overall_status=overall_status,
                checks=checks,
                required_checks=self.required_checks,
                observed_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            )
            self._record_receipt(run_id, sha, snapshot)

            if overall_status in ("SUCCESS", "FAILURE", "SHA_MISMATCH", "MISSING"):
                return snapshot

            await asyncio.sleep(interval)

    def _record_receipt(self, run_id: str, requested_sha: str, snapshot: CIStatusSnapshot) -> None:
        created_at_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        checks_summary = {
            c.name: f"{c.status}/{c.conclusion}"
            for c in snapshot.checks
        }
        receipt_data = {
            "schema_version": "hermes.ci-status-receipt.v1",
            "run_id": run_id,
            "sha": snapshot.sha,
            "overall_status": snapshot.overall_status,
            "checks_summary": checks_summary,
            "created_at_utc": created_at_utc,
        }
        receipt_id = compute_ci_receipt_digest(receipt_data)
        self.latest_receipt = CIStatusReceipt(
            schema_version="hermes.ci-status-receipt.v1",
            receipt_id=receipt_id,
            run_id=run_id,
            sha=snapshot.sha,
            overall_status=snapshot.overall_status,
            checks_summary=checks_summary,
            created_at_utc=created_at_utc,
        )
