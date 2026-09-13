"""Real GitHub CI Status Provider for Hermes Autonomous Supervisor (Task 7.7 Corrective Closure).

Workflow-Run Level Source of Truth:
- Queries GitHub Actions workflow runs for the exact SHA (/repos/{repo}/actions/runs?head_sha={sha}&per_page=100).
- Reason at workflow-run level (e.g. Tests, Lint (ruff + ty), Typecheck, Nix, Agent Release Gate, Supply Chain Audit, History Check).
- Exact SHA binding: workflow_run.head_sha == requested_sha, else CI_SHA_MISMATCH -> BLOCK.
- Required success semantics: status == 'completed' AND conclusion == 'success'.
- Governance: Contributor Attribution Check is observed separately as governance metadata and does NOT block technical CI.
- Produces hermes.ci-status-snapshot.v1 and hermes.ci-status-receipt.v1.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import time
from dataclasses import dataclass

from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence


class CIStatusProvider(Protocol):
    """Protocol for CI status providers. Defined locally to avoid circular import with autonomous_run."""

    async def wait_for_ci(self, run_id: str, sha: str, **kwargs: Any) -> Any:
        ...


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


REQUIRED_TECHNICAL_WORKFLOWS: tuple[str, ...] = (
    "Tests",
    "Lint (ruff + ty)",
    "Typecheck",
    "Nix",
    "Agent Release Gate",
    "Supply Chain Audit",
    "History Check",
)

GOVERNANCE_WORKFLOWS: tuple[str, ...] = (
    "Contributor Attribution Check",
)

# Backward compatibility alias
REQUIRED_TECHNICAL_CHECKS = REQUIRED_TECHNICAL_WORKFLOWS


@dataclass(frozen=True, slots=True)
class WorkflowObservation:
    workflow_name: str
    run_id: int
    head_sha: str
    status: str       # "completed", "in_progress", "queued"
    conclusion: str   # "success", "failure", "skipped", "cancelled", etc.
    event: str = "pull_request"

    @property
    def name(self) -> str:
        return self.workflow_name


@dataclass(frozen=True, slots=True)
class CICheckResult:
    name: str
    status: str
    conclusion: str
    details_url: str = ""
    run_id: int = 0
    head_sha: str = ""
    event: str = "pull_request"

    @property
    def workflow_name(self) -> str:
        return self.name



@dataclass(frozen=True, slots=True)
class CIStatusSnapshot:
    schema_version: str  # "hermes.ci-status-snapshot.v1"
    repository: str
    requested_sha: str
    observed_sha: str
    checked_at_utc: str
    overall_status: str  # "SUCCESS", "FAILURE", "IN_PROGRESS", "TIMED_OUT", "MISSING", "SHA_MISMATCH"
    required_workflow_observations: tuple[WorkflowObservation, ...]
    all_required_completed: bool
    all_required_success: bool
    governance_observations: tuple[WorkflowObservation, ...]
    snapshot_digest: str

    @property
    def checks(self) -> tuple[WorkflowObservation, ...]:
        """Backward compatibility property returning all observations."""
        return self.required_workflow_observations + self.governance_observations

    @property
    def sha(self) -> str:
        return self.observed_sha or self.requested_sha

    @property
    def required_checks(self) -> tuple[str, ...]:
        return tuple(obs.workflow_name for obs in self.required_workflow_observations)


@dataclass(frozen=True, slots=True)
class CIStatusReceipt:
    schema_version: str  # "hermes.ci-status-receipt.v1"
    receipt_id: str
    run_id: str
    repository: str
    requested_sha: str
    observed_sha: str
    overall_status: str
    workflows_summary: dict[str, str]
    snapshot_digest: str
    created_at_utc: str

    @property
    def checks_summary(self) -> dict[str, str]:
        return self.workflows_summary

    @property
    def sha(self) -> str:
        return self.observed_sha or self.requested_sha


def compute_snapshot_digest(snapshot_data: Mapping[str, Any]) -> str:
    canonical = json.dumps(snapshot_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_ci_receipt_digest(receipt_data: Mapping[str, Any]) -> str:
    canonical = json.dumps(receipt_data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _match_workflow(req_low: str, runs_by_name: Mapping[str, Any]) -> Any | None:
    return runs_by_name.get(req_low.strip().lower())


def evaluate_workflow_runs(
    repository: str,
    requested_sha: str,
    raw_runs: Sequence[Mapping[str, Any]],
    required_workflows: Sequence[str] = REQUIRED_TECHNICAL_WORKFLOWS,
    governance_workflows: Sequence[str] = GOVERNANCE_WORKFLOWS,
    checked_at_utc: str | None = None,
    observed_sha: str | None = None,
) -> CIStatusSnapshot:
    """Evaluates GitHub Actions workflow runs against exact SHA, pull_request event, and required technical workflows."""
    ts = checked_at_utc or datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Index observed runs by workflow name (case-insensitive, exact name only)
    runs_by_name: dict[str, dict[str, Any]] = {}
    observed_shas: set[str] = set()

    for r in raw_runs:
        head_sha = str(r.get("head_sha", "")).strip()
        if head_sha:
            observed_shas.add(head_sha)

        # PR-Event binding: Every selected run must satisfy event == pull_request.
        # Do not allow push/workflow_dispatch/schedule runs to satisfy PR CI.
        ev = str(r.get("event") or "pull_request").strip().lower()
        if ev != "pull_request":
            continue

        name = str(r.get("name", "")).strip()
        if name:
            # If multiple runs for same workflow, take highest ID (latest)
            existing = runs_by_name.get(name.lower())
            if existing is None or int(r.get("id", 0)) > int(existing.get("id", 0)):
                runs_by_name[name.lower()] = dict(r)

    # 1. Exact SHA binding
    eff_observed_sha = observed_sha or requested_sha
    if (observed_sha and observed_sha != requested_sha) or (observed_shas and any(s != requested_sha for s in observed_shas)):
        mismatched = observed_sha if (observed_sha and observed_sha != requested_sha) else next(s for s in observed_shas if s != requested_sha)
        snap_data = {
            "schema_version": "hermes.ci-status-snapshot.v1",
            "repository": repository,
            "requested_sha": requested_sha,
            "observed_sha": mismatched,
            "overall_status": "SHA_MISMATCH",
            "required_workflows": [],
            "governance_workflows": [],
        }
        return CIStatusSnapshot(
            schema_version="hermes.ci-status-snapshot.v1",
            repository=repository,
            requested_sha=requested_sha,
            observed_sha=mismatched,
            checked_at_utc=ts,
            overall_status="SHA_MISMATCH",
            required_workflow_observations=(),
            all_required_completed=False,
            all_required_success=False,
            governance_observations=(),
            snapshot_digest=compute_snapshot_digest(snap_data),
        )

    # 2. Evaluate required technical workflows
    required_obs: list[WorkflowObservation] = []
    has_pending = False
    has_failure = False
    has_missing = False

    all_observed_completed = True
    for r in raw_runs:
        ev = str(r.get("event") or "pull_request").strip().lower()
        if ev == "pull_request":
            st = str(r.get("status", "")).lower()
            if st in ("queued", "in_progress", "waiting", "requested", "pending"):
                all_observed_completed = False

    for req in required_workflows:
        req_low = req.strip().lower()
        matched = _match_workflow(req_low, runs_by_name)

        if not matched:
            if all_observed_completed and len(raw_runs) > 0:
                has_missing = True
            else:
                has_pending = True
            continue

        run_id = int(matched.get("id", 0))
        run_head_sha = str(matched.get("head_sha", requested_sha))
        st = str(matched.get("status", "")).lower()
        cc = str(matched.get("conclusion") or "").lower()
        ev = str(matched.get("event") or "pull_request").strip().lower()

        obs = WorkflowObservation(
            workflow_name=req,
            run_id=run_id,
            head_sha=run_head_sha,
            status=st,
            conclusion=cc,
            event=ev,
        )
        required_obs.append(obs)

        if st in ("queued", "in_progress", "waiting", "requested", "pending"):
            has_pending = True
        elif cc == "success":
            pass  # Required technical check passed!
        else:
            # skipped, neutral, cancelled, timed_out, action_required, failure
            # Do NOT treat skipped or neutral as success for required technical checks!
            has_failure = True

    # 3. Evaluate governance-only workflows (Attribution)
    governance_obs: list[WorkflowObservation] = []
    for gov in governance_workflows:
        gov_low = gov.strip().lower()
        matched = _match_workflow(gov_low, runs_by_name)
        if matched:
            obs = WorkflowObservation(
                workflow_name=gov,
                run_id=int(matched.get("id", 0)),
                head_sha=str(matched.get("head_sha", requested_sha)),
                status=str(matched.get("status", "")).lower(),
                conclusion=str(matched.get("conclusion") or "").lower(),
                event=str(matched.get("event") or "pull_request").strip().lower(),
            )
            governance_obs.append(obs)

    # Resolve overall status
    if has_failure:
        overall_status = "FAILURE"
    elif has_missing:
        overall_status = "MISSING"
    elif has_pending:
        overall_status = "IN_PROGRESS"
    elif len(required_obs) == len(required_workflows) and all(obs.status == "completed" and obs.conclusion == "success" for obs in required_obs):
        overall_status = "SUCCESS"
    else:
        overall_status = "IN_PROGRESS"

    all_completed = (
        len(required_obs) == len(required_workflows)
        and all(obs.status == "completed" for obs in required_obs)
    )
    all_success = (
        len(required_obs) == len(required_workflows)
        and all(obs.status == "completed" and obs.conclusion == "success" for obs in required_obs)
    )

    snap_dict = {
        "schema_version": "hermes.ci-status-snapshot.v1",
        "repository": repository,
        "requested_sha": requested_sha,
        "observed_sha": eff_observed_sha,
        "overall_status": overall_status,
        "all_required_completed": all_completed,
        "all_required_success": all_success,
        "required_workflows": [
            {"name": o.workflow_name, "id": o.run_id, "status": o.status, "conclusion": o.conclusion}
            for o in required_obs
        ],
        "governance_workflows": [
            {"name": o.workflow_name, "id": o.run_id, "status": o.status, "conclusion": o.conclusion}
            for o in governance_obs
        ],
    }
    digest = compute_snapshot_digest(snap_dict)

    return CIStatusSnapshot(
        schema_version="hermes.ci-status-snapshot.v1",
        repository=repository,
        requested_sha=requested_sha,
        observed_sha=eff_observed_sha,
        checked_at_utc=ts,
        overall_status=overall_status,
        required_workflow_observations=tuple(required_obs),
        all_required_completed=all_completed,
        all_required_success=all_success,
        governance_observations=tuple(governance_obs),
        snapshot_digest=digest,
    )


# Backward compatibility wrapper for old evaluate_ci_checks
def evaluate_ci_checks(
    requested_sha: str,
    observed_sha: str,
    checks: Sequence[Any],
    required_checks: Sequence[str] = REQUIRED_TECHNICAL_CHECKS,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    if requested_sha != observed_sha:
        return "SHA_MISMATCH", (), (f"SHA_MISMATCH: requested {requested_sha} != observed {observed_sha}",)

    checks_by_name: dict[str, Any] = {}
    for c in checks:
        if isinstance(c, dict):
            ev = str(c.get("event", "pull_request") or "pull_request").strip().lower()
            c_name = str(c.get("name", c.get("workflow_name", ""))).strip().lower()
        else:
            ev = str(getattr(c, "event", "pull_request") or "pull_request").strip().lower()
            c_name = str(getattr(c, "name", getattr(c, "workflow_name", ""))).strip().lower()
        if ev != "pull_request":
            continue
        checks_by_name[c_name] = c

    def is_governance(name: str) -> bool:
        low = name.strip().lower()
        return any(gov.lower() in low for gov in GOVERNANCE_WORKFLOWS)

    has_pending = False
    has_failure = False
    has_missing = False

    passed: list[str] = []
    issues: list[str] = []

    all_completed = True
    for c in checks:
        st = getattr(c, "status", "").lower()
        if st in ("queued", "in_progress", "waiting", "requested", "pending"):
            all_completed = False

    for req in required_checks:
        if is_governance(req):
            continue

        matched = _match_workflow(req.strip().lower(), checks_by_name)

        if not matched:
            if all_completed and len(checks) > 0:
                has_missing = True
                issues.append(f"MISSING: {req}")
            else:
                has_pending = True
                issues.append(f"NOT_YET_QUEUED: {req}")
            continue

        c_name = getattr(matched, "name", getattr(matched, "workflow_name", req))
        st = getattr(matched, "status", "").lower()
        cc = (getattr(matched, "conclusion", "") or "").lower()

        if st in ("queued", "in_progress", "waiting", "requested", "pending"):
            has_pending = True
            issues.append(f"PENDING: {c_name} ({st})")
        elif cc == "success":
            passed.append(c_name)
        else:
            has_failure = True
            issues.append(f"FAILED: {c_name} ({cc})")

    if has_failure:
        return "FAILURE", tuple(passed), tuple(issues)
    if has_missing:
        return "MISSING", tuple(passed), tuple(issues)
    if has_pending:
        return "IN_PROGRESS", tuple(passed), tuple(issues)

    return "SUCCESS", tuple(passed), ()


class GitHubCIStatusProvider(CIStatusProvider):
    """Real GitHub CI Status Provider supporting workflow-run level inspection, pagination, and bounded polling."""

    def __init__(
        self,
        repository: str = "life2boat/hermes",
        required_workflows: Sequence[str] = REQUIRED_TECHNICAL_WORKFLOWS,
        governance_workflows: Sequence[str] = GOVERNANCE_WORKFLOWS,
        timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 10.0,
        max_poll_attempts: int = 60,
        runner: Callable[[str, str], Awaitable[tuple[str, list[dict[str, Any]]]]] | None = None,
        # Backward compatibility kwargs:
        required_checks: Sequence[str] | None = None,
    ) -> None:
        self.repository = repository
        self.required_workflows = tuple(required_checks if required_checks is not None else required_workflows)
        self.governance_workflows = tuple(governance_workflows)
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_attempts = max_poll_attempts
        self.runner = runner
        self.latest_receipt: CIStatusReceipt | None = None

    @property
    def required_checks(self) -> tuple[str, ...]:
        return self.required_workflows

    async def _fetch_runs_default(self, repo: str, sha: str) -> tuple[str, list[dict[str, Any]]]:
        """Fetches workflow runs for exact SHA using GitHub Actions runs API with pagination."""
        all_runs: list[dict[str, Any]] = []
        page = 1
        per_page = 100

        while True:
            cmd = [
                "gh", "api",
                f"repos/{repo}/actions/runs?head_sha={sha}&event=pull_request&per_page={per_page}&page={page}",
                "--jq", "{total_count: .total_count, workflow_runs: [.workflow_runs[] | {id: .id, name: .name, head_sha: .head_sha, status: .status, conclusion: .conclusion, event: .event}]}"
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
                break

            try:
                data = json.loads(out_str)
            except Exception as e:
                raise CIProviderUnavailableError(f"Failed to parse gh api output: {e}") from e

            page_runs = data.get("workflow_runs", [])
            all_runs.extend(page_runs)

            total_count = data.get("total_count", 0)
            if len(all_runs) >= total_count or len(page_runs) < per_page:
                break
            page += 1

        return sha, all_runs

    async def check_ci(self, run_id: str, sha: str) -> CIStatusSnapshot:
        """Performs a single observation pass of GitHub CI for sha."""
        try:
            if self.runner is not None:
                observed_sha, raw_runs = await self.runner(self.repository, sha)
            else:
                observed_sha, raw_runs = await self._fetch_runs_default(self.repository, sha)
        except CIProviderError:
            raise
        except Exception as e:
            raise CIProviderUnavailableError(f"Error fetching CI status: {e}") from e

        snapshot = evaluate_workflow_runs(
            repository=self.repository,
            requested_sha=sha,
            raw_runs=raw_runs,
            required_workflows=self.required_workflows,
            governance_workflows=self.governance_workflows,
            observed_sha=observed_sha,
        )
        self._record_receipt(run_id, sha, snapshot)
        return snapshot

    async def wait_for_ci(
        self,
        run_id: str,
        sha: str,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        max_poll_attempts: int | None = None,
        on_observation: Callable[[CIStatusSnapshot], Awaitable[None]] | None = None,
    ) -> CIStatusSnapshot:
        """Bounded polling loop checking workflow runs until completion or budget exhaustion."""
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        interval = poll_interval_seconds if poll_interval_seconds is not None else self.poll_interval_seconds
        max_attempts = max_poll_attempts if max_poll_attempts is not None else self.max_poll_attempts

        start_time = time.monotonic()
        attempt = 0

        while True:
            attempt += 1
            elapsed = time.monotonic() - start_time
            if elapsed >= timeout or attempt > max_attempts:
                snap_dict = {
                    "schema_version": "hermes.ci-status-snapshot.v1",
                    "repository": self.repository,
                    "requested_sha": sha,
                    "observed_sha": sha,
                    "overall_status": "TIMED_OUT",
                    "required_workflows": [],
                    "governance_workflows": [],
                }
                snapshot = CIStatusSnapshot(
                    schema_version="hermes.ci-status-snapshot.v1",
                    repository=self.repository,
                    requested_sha=sha,
                    observed_sha=sha,
                    checked_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    overall_status="TIMED_OUT",
                    required_workflow_observations=(),
                    all_required_completed=False,
                    all_required_success=False,
                    governance_observations=(),
                    snapshot_digest=compute_snapshot_digest(snap_dict),
                )
                self._record_receipt(run_id, sha, snapshot)
                if on_observation is not None:
                    await on_observation(snapshot)
                return snapshot

            snapshot = await self.check_ci(run_id, sha)
            if on_observation is not None:
                await on_observation(snapshot)

            if snapshot.overall_status in ("SUCCESS", "FAILURE", "SHA_MISMATCH", "MISSING"):
                return snapshot

            await asyncio.sleep(interval)

    def _record_receipt(self, run_id: str, requested_sha: str, snapshot: CIStatusSnapshot) -> None:
        created_at_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        workflows_summary = {
            o.workflow_name: f"{o.status}/{o.conclusion}"
            for o in snapshot.required_workflow_observations
        }
        for g in snapshot.governance_observations:
            workflows_summary[g.workflow_name] = f"{g.status}/{g.conclusion}"

        receipt_data = {
            "schema_version": "hermes.ci-status-receipt.v1",
            "run_id": run_id,
            "repository": self.repository,
            "requested_sha": requested_sha,
            "observed_sha": snapshot.observed_sha,
            "overall_status": snapshot.overall_status,
            "workflows_summary": workflows_summary,
            "snapshot_digest": snapshot.snapshot_digest,
            "created_at_utc": created_at_utc,
        }
        receipt_id = compute_ci_receipt_digest(receipt_data)
        self.latest_receipt = CIStatusReceipt(
            schema_version="hermes.ci-status-receipt.v1",
            receipt_id=receipt_id,
            run_id=run_id,
            repository=self.repository,
            requested_sha=requested_sha,
            observed_sha=snapshot.observed_sha,
            overall_status=snapshot.overall_status,
            workflows_summary=workflows_summary,
            snapshot_digest=snapshot.snapshot_digest,
            created_at_utc=created_at_utc,
        )
