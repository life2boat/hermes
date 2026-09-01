"""Safe, deterministic read-only repository investigator and parallel batch runner."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_engineering.execution.run_contracts import (
    AgentRunIdentity,
    RunBlockingReason,
    RunState,
)
from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.investigation.investigation_contracts import (
    MAX_SNIPPET_LENGTH,
    InvestigationBlockingReason,
    InvestigationError,
    RepositoryInvestigationAggregate,
    RepositoryInvestigationBatch,
    RepositoryInvestigationRequest,
    RepositoryInvestigationResult,
    RepositoryMatch,
)
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)

_ALLOWED_COMMAND_PREFIXES = (
    ("git", "rev-parse"),
    ("git", "show"),
    ("git", "grep"),
    ("git", "ls-files"),
    ("git", "log"),
    ("rg",),
    ("grep",),
    ("find",),
    ("sed",),
    ("head",),
    ("tail",),
)

_MUTATION_COMMANDS = (
    "commit",
    "checkout",
    "switch",
    "reset",
    "clean",
    "merge",
    "rebase",
    "add",
    "push",
    "tag",
    "branch",
    "rm",
    "mv",
    "cp",
    "-i",
    "--in-place",
    "write",
    "truncate",
)


def validate_investigation_command(cmd: tuple[str, ...]) -> None:
    """Validate that command tokens strictly conform to the read-only allowlist."""
    if not cmd:
        raise InvestigationError(
            InvestigationBlockingReason.INVESTIGATION_COMMAND_FORBIDDEN.value,
            "Empty command",
        )

    # Check for forbidden mutations first
    for token in cmd:
        token_lower = token.lower()
        for forb in _MUTATION_COMMANDS:
            if token_lower == forb or (forb in token_lower and forb not in ("git", "log", "show", "rev-parse")):
                # Special case: allow git log/show/rev-parse with commit hash
                if forb == "commit" and (len(cmd) >= 2 and cmd[0] == "git" and cmd[1] in ("log", "show", "rev-parse")):
                    continue
                if forb == "add" and (len(cmd) >= 2 and (cmd[0] != "git" or cmd[1] != "add")):
                    continue
                raise InvestigationError(
                    InvestigationBlockingReason.INVESTIGATION_WRITE_FORBIDDEN.value,
                    f"Forbidden mutation command or token {forb!r} in: {cmd!r}",
                )

    # Check prefix allowlist
    allowed = False
    for prefix in _ALLOWED_COMMAND_PREFIXES:
        if len(cmd) >= len(prefix) and tuple(cmd[:len(prefix)]) == prefix:
            allowed = True
            break

    if not allowed:
        raise InvestigationError(
            InvestigationBlockingReason.INVESTIGATION_COMMAND_FORBIDDEN.value,
            f"Command {cmd!r} is not in read-only allowlist",
        )


def _get_repository_head_sha(repo_root: Path) -> str:
    """Get current HEAD SHA of repository."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception as exc:
        raise InvestigationError(
            InvestigationBlockingReason.INVESTIGATION_BASE_SHA_MISMATCH.value,
            f"Failed to resolve repository HEAD SHA: {exc}",
        ) from exc


def execute_single_investigation(
    request: RepositoryInvestigationRequest,
    run_id: str,
    *,
    run_registry: ActiveRunRegistry | None = None,
    on_start_hook: Callable[[str], None] | None = None,
) -> RepositoryInvestigationResult:
    """Execute a single bounded read-only investigation branch."""
    repo_root = Path(request.repository_root).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        raise InvestigationError(
            InvestigationBlockingReason.INVESTIGATION_SCOPE_INVALID.value,
            f"Repository root does not exist: {repo_root}",
        )

    # 1. Base SHA verification
    current_head = _get_repository_head_sha(repo_root)
    if current_head != request.base_sha:
        return RepositoryInvestigationResult(
            investigation_id=request.investigation_id,
            run_id=run_id,
            base_sha=request.base_sha,
            matches=(),
            summary="Base SHA mismatch",
            success=False,
            error_code=InvestigationBlockingReason.INVESTIGATION_BASE_SHA_MISMATCH.value,
            error_message=f"Expected base SHA {request.base_sha}, found {current_head}",
        )

    # 2. Check for run cancellation / stale state before starting
    if run_registry is not None:
        rec = run_registry.get_run(run_id)
        if rec is not None and rec.state in (RunState.CANCEL_REQUESTED, RunState.EXITED, RunState.FAILED):
            return RepositoryInvestigationResult(
                investigation_id=request.investigation_id,
                run_id=run_id,
                base_sha=request.base_sha,
                matches=(),
                summary="Investigation cancelled before start",
                success=False,
                error_code=RunBlockingReason.RUN_CANCELLED.value if hasattr(RunBlockingReason, "RUN_CANCELLED") else "RUN_CANCELLED",
                error_message="Run was cancelled",
            )

    if on_start_hook is not None:
        on_start_hook(run_id)

    # 3. Path Fencing: resolve and validate scope paths
    target_files: list[Path] = []
    if request.scope_paths:
        for sp in request.scope_paths:
            sp_path = Path(sp)
            if sp_path.is_absolute() or sp.startswith(("/", "\\")) or ":" in sp or ".." in sp_path.parts:
                raise InvestigationError(
                    InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value,
                    f"Scope path escapes repository: {sp}",
                )
            resolved = (repo_root / sp_path).resolve()
            if not resolved.is_relative_to(repo_root):
                raise InvestigationError(
                    InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value,
                    f"Scope path escapes repository root: {sp}",
                )
            if resolved.is_file():
                target_files.append(resolved)
            elif resolved.is_dir():
                for r, _, files in os.walk(resolved):
                    for f in files:
                        target_files.append(Path(r) / f)
    else:
        # Default: scan repository files, ignoring .git
        for r, dirs, files in os.walk(repo_root):
            if ".git" in dirs:
                dirs.remove(".git")
            for f in files:
                target_files.append(Path(r) / f)

    # 4. Search execution (Pure read-only)
    query = request.query
    raw_matches: list[RepositoryMatch] = []
    seen_matches: set[tuple[str, int, int, str]] = set()

    for file_path in target_files:
        if not file_path.is_file():
            continue
        try:
            rel_path = file_path.relative_to(repo_root).as_posix()
        except ValueError:
            continue

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line_idx, line in enumerate(f, start=1):
                    if query in line:
                        snippet = line.strip()[:MAX_SNIPPET_LENGTH]
                        match_key = (rel_path, line_idx, line_idx, snippet)
                        if match_key not in seen_matches:
                            seen_matches.add(match_key)
                            raw_matches.append(
                                RepositoryMatch(
                                    path=rel_path,
                                    line_start=line_idx,
                                    line_end=line_idx,
                                    snippet=snippet,
                                    match_kind="TEXT",
                                )
                            )
        except (OSError, UnicodeDecodeError):
            continue

    # 5. Normalization: sort deterministically by (path, line_start, line_end)
    raw_matches.sort(key=lambda m: (m.path, m.line_start, m.line_end, m.snippet))
    bounded_matches = tuple(raw_matches[:request.max_results])

    # 6. Check for stale fencing before returning
    if run_registry is not None:
        rec = run_registry.get_run(run_id)
        if rec is not None and rec.state in (RunState.CANCEL_REQUESTED, RunState.EXITED, RunState.FAILED):
            return RepositoryInvestigationResult(
                investigation_id=request.investigation_id,
                run_id=run_id,
                base_sha=request.base_sha,
                matches=(),
                summary="Investigation cancelled during execution",
                success=False,
                error_code="RUN_CANCELLED",
                error_message="Run was cancelled or superseded",
            )

    return RepositoryInvestigationResult(
        investigation_id=request.investigation_id,
        run_id=run_id,
        base_sha=request.base_sha,
        matches=bounded_matches,
        summary=f"Found {len(bounded_matches)} matches for query {request.query!r}",
        success=True,
    )


class ParallelRepositoryInvestigator:
    """Orchestrator for concurrent read-only repository investigations."""

    def __init__(self, run_registry: ActiveRunRegistry | None = None) -> None:
        self.run_registry = run_registry

    def execute_batch(
        self,
        batch: RepositoryInvestigationBatch,
        decision: ParallelizationDecision,
        *,
        on_start_hook: Callable[[str], None] | None = None,
    ) -> RepositoryInvestigationAggregate:
        """Execute a batch of repository investigations concurrently within policy and budget limits."""
        # 1. PR-3 Policy Gate: Decision must be allowed and strategy PREPARATORY
        if not decision.allowed or decision.strategy != ParallelizationStrategy.PREPARATORY:
            raise InvestigationError(
                InvestigationBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value,
                f"Batch execution requires approved PREPARATORY decision, got: allowed={decision.allowed}, strategy={decision.strategy}",
            )

        # 2. Concurrency budget clamping
        effective_max_parallel = min(batch.max_parallel, decision.max_agents)
        if effective_max_parallel < 1:
            raise InvestigationError(
                InvestigationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"Effective concurrency must be >= 1, got {effective_max_parallel}",
            )

        successful_results: list[RepositoryInvestigationResult] = []
        failed_results: list[RepositoryInvestigationResult] = []

        # 3. Create unique AgentRunIdentity per investigation if run_registry is provided
        run_map: dict[str, str] = {}
        now = datetime.now(timezone.utc)
        for idx, inv in enumerate(batch.investigations, start=1):
            run_id = f"run-inv-{idx}"
            run_map[inv.investigation_id] = run_id
            if self.run_registry is not None:
                ident = AgentRunIdentity(
                    run_id=run_id,
                    task_id=inv.task_id,
                    node_id=inv.node_id,
                    workspace_id=f"ws-{inv.task_id}-readonly",
                    candidate_id=None,
                    model="gemini-3.1-pro-high",
                    agent_capability="REPOSITORY_SEARCH_LOGS",
                    execution_host_id="local",
                    execution_epoch=1,
                    start_time=now,
                )
                self.run_registry.register_run(ident)

        # 4. Concurrent execution via ThreadPoolExecutor
        def _worker(inv_req: RepositoryInvestigationRequest) -> RepositoryInvestigationResult:
            assigned_run_id = run_map[inv_req.investigation_id]
            try:
                return execute_single_investigation(
                    inv_req,
                    assigned_run_id,
                    run_registry=self.run_registry,
                    on_start_hook=on_start_hook,
                )
            except Exception as exc:
                return RepositoryInvestigationResult(
                    investigation_id=inv_req.investigation_id,
                    run_id=assigned_run_id,
                    base_sha=inv_req.base_sha,
                    matches=(),
                    summary=f"Investigation failed with exception: {exc}",
                    success=False,
                    error_code=getattr(exc, "code", InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value),
                    error_message=str(exc),
                )

        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_max_parallel) as executor:
            future_to_req = {executor.submit(_worker, inv): inv for inv in batch.investigations}
            for future in concurrent.futures.as_completed(future_to_req):
                res = future.result()
                if res.success:
                    successful_results.append(res)
                else:
                    failed_results.append(res)

        # 5. Deterministic sorting of aggregate results
        successful_results.sort(key=lambda r: r.investigation_id)
        failed_results.sort(key=lambda r: r.investigation_id)

        if not failed_results:
            status = "SUCCESS"
        elif not successful_results:
            status = "FAILED"
        else:
            status = "PARTIAL"

        return RepositoryInvestigationAggregate(
            batch_id=batch.batch_id,
            base_sha=batch.base_sha,
            results=tuple(successful_results),
            failed_investigations=tuple(failed_results),
            status=status,
        )
