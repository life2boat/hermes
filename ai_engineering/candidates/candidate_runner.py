"""Parallel candidate implementation execution engine and scope fencing."""

from __future__ import annotations

import concurrent.futures
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai_engineering.candidates.candidate_contracts import (
    CandidateBatchAggregate,
    CandidateBlockingReason,
    CandidateError,
    CandidateIdentity,
    CandidateImplementationBatch,
    CandidateImplementationRequest,
    CandidateResult,
    CandidateState,
    ValidationCommandResult,
)
from ai_engineering.execution.run_contracts import (
    AgentRunIdentity,
    RunBlockingReason,
    RunState,
)
from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)
from ai_engineering.workspaces.workspace_contracts import (
    LeaseState,
    WorkspaceBlockingReason,
    WorkspaceIdentity,
    WorktreeLease,
)
from ai_engineering.workspaces.worktree_manager import WorktreeManager

_BRANCH_CHAR_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

_FORBIDDEN_COMMAND_SUBSTRINGS = (
    "docker",
    "systemctl",
    "kubectl",
    "deploy",
    "rollback",
    "ssh",
    "git push",
    "gh pr merge",
    "git merge main",
    "git reset --hard origin/main",
)


def make_candidate_branch_name(task_id: str, candidate_id: str) -> str:
    """Generate a bounded, sanitized branch name for a candidate implementation."""
    if not _BRANCH_CHAR_RE.match(task_id) or ".." in task_id:
        raise CandidateError(
            CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
            f"Invalid task_id for branch naming: {task_id!r}",
        )
    if not _BRANCH_CHAR_RE.match(candidate_id) or ".." in candidate_id:
        raise CandidateError(
            CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
            f"Invalid candidate_id for branch naming: {candidate_id!r}",
        )
    return f"codex/candidate/{task_id}/{candidate_id}"


def validate_candidate_command(cmd: tuple[str, ...]) -> None:
    """Validate that candidate commands do not violate production or git security boundaries."""
    if not cmd:
        raise CandidateError(
            CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
            "Empty command",
        )
    cmd_str = " ".join(cmd).lower()
    for forb in _FORBIDDEN_COMMAND_SUBSTRINGS:
        if forb in cmd_str:
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_NOT_AUTHORIZED.value,
                f"Forbidden candidate command token {forb!r} in {cmd!r}",
            )


def execute_single_candidate(
    request: CandidateImplementationRequest,
    workspace_dir: Path,
    run_id: str,
    workspace_id: str,
    *,
    branch_name: str,
    run_registry: ActiveRunRegistry | None = None,
    worktree_manager: WorktreeManager | None = None,
    implementation_fn: Callable[[CandidateImplementationRequest, Path], None] | None = None,
    on_start_hook: Callable[[str], None] | None = None,
) -> CandidateResult:
    """Execute a single candidate implementation step inside its isolated worktree."""
    resolved_ws = Path(workspace_dir).resolve()
    now_str = datetime.now(timezone.utc).isoformat()

    # 1. Main branch and Canonical checkout protection
    if worktree_manager is not None and worktree_manager.is_canonical_checkout(resolved_ws):
        return CandidateResult(
            candidate_id=request.candidate_id,
            task_id=request.task_id,
            node_id=request.node_id,
            workspace_id=workspace_id,
            run_id=run_id,
            base_sha=request.base_sha,
            branch=branch_name,
            changed_paths=(),
            diff_summary="Execution in canonical checkout is strictly forbidden",
            validation_results=(),
            state=CandidateState.FAILED,
            blockers=(CandidateBlockingReason.CANDIDATE_MAIN_WORKTREE_FORBIDDEN.value,),
            completed_at=now_str,
            success=False,
        )

    # Check worktree git branch
    try:
        proc_br = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(resolved_ws),
            capture_output=True,
            text=True,
            check=True,
        )
        curr_branch = proc_br.stdout.strip()
        if curr_branch in ("main", "master"):
            return CandidateResult(
                candidate_id=request.candidate_id,
                task_id=request.task_id,
                node_id=request.node_id,
                workspace_id=workspace_id,
                run_id=run_id,
                base_sha=request.base_sha,
                branch=curr_branch,
                changed_paths=(),
                diff_summary="Execution on main branch is strictly forbidden",
                validation_results=(),
                state=CandidateState.FAILED,
                blockers=(CandidateBlockingReason.CANDIDATE_MAIN_WORKTREE_FORBIDDEN.value,),
                completed_at=now_str,
                success=False,
            )
    except Exception as exc:
        return CandidateResult(
            candidate_id=request.candidate_id,
            task_id=request.task_id,
            node_id=request.node_id,
            workspace_id=workspace_id,
            run_id=run_id,
            base_sha=request.base_sha,
            branch=branch_name,
            changed_paths=(),
            diff_summary=f"Failed to inspect worktree branch: {exc}",
            validation_results=(),
            state=CandidateState.FAILED,
            blockers=(CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,),
            completed_at=now_str,
            success=False,
        )

    # 2. Base SHA verification
    try:
        proc_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(resolved_ws),
            capture_output=True,
            text=True,
            check=True,
        )
        curr_head = proc_head.stdout.strip()
        if curr_head.lower() != request.base_sha.lower():
            return CandidateResult(
                candidate_id=request.candidate_id,
                task_id=request.task_id,
                node_id=request.node_id,
                workspace_id=workspace_id,
                run_id=run_id,
                base_sha=request.base_sha,
                branch=branch_name,
                changed_paths=(),
                diff_summary=f"Base SHA mismatch: expected {request.base_sha}, got {curr_head}",
                validation_results=(),
                state=CandidateState.FAILED,
                blockers=(CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,),
                completed_at=now_str,
                success=False,
            )
    except Exception as exc:
        return CandidateResult(
            candidate_id=request.candidate_id,
            task_id=request.task_id,
            node_id=request.node_id,
            workspace_id=workspace_id,
            run_id=run_id,
            base_sha=request.base_sha,
            branch=branch_name,
            changed_paths=(),
            diff_summary=f"Failed to inspect worktree HEAD: {exc}",
            validation_results=(),
            state=CandidateState.FAILED,
            blockers=(CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,),
            completed_at=now_str,
            success=False,
        )

    # 3. Check for stale / cancelled run
    if run_registry is not None:
        rec = run_registry.get_run(run_id)
        if rec is not None and rec.state in (RunState.CANCEL_REQUESTED, RunState.EXITED, RunState.FAILED):
            return CandidateResult(
                candidate_id=request.candidate_id,
                task_id=request.task_id,
                node_id=request.node_id,
                workspace_id=workspace_id,
                run_id=run_id,
                base_sha=request.base_sha,
                branch=branch_name,
                changed_paths=(),
                diff_summary="Candidate execution was cancelled before start",
                validation_results=(),
                state=CandidateState.CANCELLED,
                blockers=(CandidateBlockingReason.STALE_RUN_EVENT.value,),
                completed_at=now_str,
                success=False,
            )

    if on_start_hook is not None:
        on_start_hook(run_id)

    # 4. Execute candidate implementation function
    if implementation_fn is not None:
        try:
            implementation_fn(request, resolved_ws)
        except Exception as exc:
            return CandidateResult(
                candidate_id=request.candidate_id,
                task_id=request.task_id,
                node_id=request.node_id,
                workspace_id=workspace_id,
                run_id=run_id,
                base_sha=request.base_sha,
                branch=branch_name,
                changed_paths=(),
                diff_summary=f"Implementation function failed: {exc}",
                validation_results=(),
                state=CandidateState.FAILED,
                blockers=(CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,),
                completed_at=now_str,
                success=False,
            )

    # 5. Inspect changed paths and enforce Scope Fencing
    changed_paths_set: set[str] = set()
    diff_summary = ""
    try:
        # Changed tracked files against base SHA
        proc_diff_names = subprocess.run(
            ["git", "diff", "--name-only", request.base_sha],
            cwd=str(resolved_ws),
            capture_output=True,
            text=True,
            check=True,
        )
        for line in proc_diff_names.stdout.strip().splitlines():
            if line.strip():
                changed_paths_set.add(line.strip())

        # Untracked files
        proc_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(resolved_ws),
            capture_output=True,
            text=True,
            check=True,
        )
        for line in proc_status.stdout.strip().splitlines():
            if line.startswith("?? "):
                changed_paths_set.add(line[3:].strip())

        # Diff stat
        proc_stat = subprocess.run(
            ["git", "diff", "--stat", request.base_sha],
            cwd=str(resolved_ws),
            capture_output=True,
            text=True,
            check=True,
        )
        diff_summary = proc_stat.stdout.strip()
    except Exception as exc:
        return CandidateResult(
            candidate_id=request.candidate_id,
            task_id=request.task_id,
            node_id=request.node_id,
            workspace_id=workspace_id,
            run_id=run_id,
            base_sha=request.base_sha,
            branch=branch_name,
            changed_paths=(),
            diff_summary=f"Failed to inspect git diff: {exc}",
            validation_results=(),
            state=CandidateState.FAILED,
            blockers=(CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,),
            completed_at=now_str,
            success=False,
        )

    changed_paths = tuple(sorted(changed_paths_set))

    # Scope Fencing check
    scope_violations: list[str] = []
    for cp in changed_paths:
        matched = False
        for ap in request.allowed_paths:
            if cp == ap or cp.startswith(ap.rstrip("/") + "/"):
                matched = True
                break
        if not matched:
            scope_violations.append(cp)

    if scope_violations:
        return CandidateResult(
            candidate_id=request.candidate_id,
            task_id=request.task_id,
            node_id=request.node_id,
            workspace_id=workspace_id,
            run_id=run_id,
            base_sha=request.base_sha,
            branch=branch_name,
            changed_paths=changed_paths,
            diff_summary=f"Scope violations detected: {scope_violations}",
            validation_results=(),
            state=CandidateState.FAILED,
            blockers=(CandidateBlockingReason.CANDIDATE_SCOPE_VIOLATION.value,),
            completed_at=now_str,
            success=False,
        )

    # 6. Run validation commands inside candidate worktree
    validation_results: list[ValidationCommandResult] = []
    validation_failed = False
    for val_cmd in request.validation_commands:
        validate_candidate_command(val_cmd)
        try:
            res_val = subprocess.run(
                list(val_cmd),
                cwd=str(resolved_ws),
                capture_output=True,
                text=True,
            )
            v_res = ValidationCommandResult(
                command=val_cmd,
                return_code=res_val.returncode,
                stdout=res_val.stdout,
                stderr=res_val.stderr,
                success=(res_val.returncode == 0),
            )
            validation_results.append(v_res)
            if res_val.returncode != 0:
                validation_failed = True
        except Exception as exc:
            v_res = ValidationCommandResult(
                command=val_cmd,
                return_code=1,
                stdout="",
                stderr=str(exc),
                success=False,
            )
            validation_results.append(v_res)
            validation_failed = True

    if validation_failed:
        return CandidateResult(
            candidate_id=request.candidate_id,
            task_id=request.task_id,
            node_id=request.node_id,
            workspace_id=workspace_id,
            run_id=run_id,
            base_sha=request.base_sha,
            branch=branch_name,
            changed_paths=changed_paths,
            diff_summary=diff_summary,
            validation_results=tuple(validation_results),
            state=CandidateState.FAILED,
            blockers=(CandidateBlockingReason.CANDIDATE_VALIDATION_FAILED.value,),
            completed_at=now_str,
            success=False,
        )

    # 7. Final stale check before success return
    if run_registry is not None:
        rec = run_registry.get_run(run_id)
        if rec is not None and rec.state in (RunState.CANCEL_REQUESTED, RunState.EXITED, RunState.FAILED):
            return CandidateResult(
                candidate_id=request.candidate_id,
                task_id=request.task_id,
                node_id=request.node_id,
                workspace_id=workspace_id,
                run_id=run_id,
                base_sha=request.base_sha,
                branch=branch_name,
                changed_paths=changed_paths,
                diff_summary="Run was cancelled or superseded during execution",
                validation_results=tuple(validation_results),
                state=CandidateState.CANCELLED,
                blockers=(CandidateBlockingReason.STALE_RUN_EVENT.value,),
                completed_at=now_str,
                success=False,
            )

    return CandidateResult(
        candidate_id=request.candidate_id,
        task_id=request.task_id,
        node_id=request.node_id,
        workspace_id=workspace_id,
        run_id=run_id,
        base_sha=request.base_sha,
        branch=branch_name,
        changed_paths=changed_paths,
        diff_summary=diff_summary,
        validation_results=tuple(validation_results),
        state=CandidateState.COMPLETED,
        blockers=(),
        completed_at=now_str,
        success=True,
    )


class ParallelCandidateRunner:
    """Orchestrator for managing isolated worktrees and concurrent candidate execution."""

    def __init__(
        self,
        *,
        worktree_manager: WorktreeManager | None = None,
        run_registry: ActiveRunRegistry | None = None,
        base_worktree_dir: Path | None = None,
    ) -> None:
        self.worktree_manager = worktree_manager
        self.run_registry = run_registry
        self.base_worktree_dir = base_worktree_dir

    def execute_batch(
        self,
        batch: CandidateImplementationBatch,
        decision: ParallelizationDecision,
        *,
        implementation_fn: Callable[[CandidateImplementationRequest, Path], None] | None = None,
        on_start_hook: Callable[[str], None] | None = None,
    ) -> CandidateBatchAggregate:
        """Execute a batch of candidate implementations within approved policy and budget boundaries."""
        # 1. Policy Gate: must be allowed with strategy CANDIDATE
        if not decision.allowed or decision.strategy != ParallelizationStrategy.CANDIDATE:
            raise CandidateError(
                CandidateBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value,
                f"Candidate batch requires approved CANDIDATE decision, got: allowed={decision.allowed}, strategy={decision.strategy}",
            )

        # 2. Concurrency budget clamping
        effective_max_parallel = min(batch.max_parallel, decision.max_agents, decision.max_candidates)
        if effective_max_parallel < 1:
            raise CandidateError(
                CandidateBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"Effective concurrency budget must be >= 1, got {effective_max_parallel}",
            )

        successful_results: list[CandidateResult] = []
        failed_results: list[CandidateResult] = []

        now = datetime.now(timezone.utc)
        candidate_meta: list[tuple[CandidateImplementationRequest, Path, str, str, str]] = []

        # 3. Setup worktrees, workspace identities, leases, and run identities
        for idx, cand in enumerate(batch.candidates, start=1):
            run_id = f"run-cand-{cand.candidate_id}"
            workspace_id = f"ws-{cand.task_id}-{cand.candidate_id}"
            branch_name = make_candidate_branch_name(cand.task_id, cand.candidate_id)

            if self.base_worktree_dir is not None:
                wt_path = self.base_worktree_dir / f"wt-{cand.task_id}-{cand.candidate_id}"
            else:
                wt_path = Path(cand.repository).parent / f"wt-{cand.task_id}-{cand.candidate_id}"

            # Create worktree if WorktreeManager is provided
            if self.worktree_manager is not None:
                self.worktree_manager.create_worktree(
                    worktree_path=wt_path,
                    branch=branch_name,
                    base_sha=cand.base_sha,
                )

            # Register run identity
            if self.run_registry is not None:
                ident = AgentRunIdentity(
                    run_id=run_id,
                    task_id=cand.task_id,
                    node_id=cand.node_id,
                    workspace_id=workspace_id,
                    candidate_id=cand.candidate_id,
                    model="gemini-3.1-pro-high",
                    agent_capability="CANDIDATE_IMPLEMENTATION",
                    execution_host_id="local",
                    execution_epoch=1,
                    start_time=now,
                )
                self.run_registry.register_run(ident)

            candidate_meta.append((cand, wt_path, run_id, workspace_id, branch_name))

        # 4. Worker function for concurrency
        def _worker(meta: tuple[CandidateImplementationRequest, Path, str, str, str]) -> CandidateResult:
            cand_req, wt_dir, r_id, ws_id, br_name = meta
            try:
                return execute_single_candidate(
                    cand_req,
                    wt_dir,
                    r_id,
                    ws_id,
                    branch_name=br_name,
                    run_registry=self.run_registry,
                    worktree_manager=self.worktree_manager,
                    implementation_fn=implementation_fn,
                    on_start_hook=on_start_hook,
                )
            except Exception as exc:
                return CandidateResult(
                    candidate_id=cand_req.candidate_id,
                    task_id=cand_req.task_id,
                    node_id=cand_req.node_id,
                    workspace_id=ws_id,
                    run_id=r_id,
                    base_sha=cand_req.base_sha,
                    branch=br_name,
                    changed_paths=(),
                    diff_summary=f"Unhandled exception during candidate execution: {exc}",
                    validation_results=(),
                    state=CandidateState.FAILED,
                    blockers=(getattr(exc, "code", CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value),),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    success=False,
                )

        # 5. Execute concurrently via ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=effective_max_parallel) as executor:
            future_to_cand = {executor.submit(_worker, m): m for m in candidate_meta}
            for future in concurrent.futures.as_completed(future_to_cand):
                res = future.result()
                if res.success:
                    successful_results.append(res)
                else:
                    failed_results.append(res)

        # 6. Sort deterministically
        successful_results.sort(key=lambda r: r.candidate_id)
        failed_results.sort(key=lambda r: r.candidate_id)

        if not failed_results:
            status = "SUCCESS"
        elif not successful_results:
            status = "FAILED"
        else:
            status = "PARTIAL"

        return CandidateBatchAggregate(
            batch_id=batch.batch_id,
            base_sha=batch.base_sha,
            results=tuple(successful_results),
            failed_candidates=tuple(failed_results),
            status=status,
        )
