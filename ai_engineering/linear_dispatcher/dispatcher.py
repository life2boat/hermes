"""Hermes Linear Autonomous Dispatcher orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
    TaskState,
)
from ai_engineering.linear_dispatcher.github_service import IGitHubService
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.scope_gate import ScopeGate
from ai_engineering.linear_dispatcher.state_machine import TaskStateMachine
from ai_engineering.linear_dispatcher.task_filter import (
    is_eligible_task,
    select_next_task,
)
from ai_engineering.linear_dispatcher.validator import LocalValidator
from ai_engineering.linear_dispatcher.worktree_service import WorktreeService
from ai_engineering.linear_dispatcher.writeback import (
    ExecutionEvidence,
    ILinearClient,
    WritebackService,
)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    task_id: str
    final_state: TaskState
    branch: str | None
    pr_number: int | None
    pr_url: str | None
    head_sha: str | None
    block_reason: str | None
    transitions: tuple[Any, ...]


class AutonomousDispatcher:
    """End-to-end safe autonomous dispatcher for Linear tasks."""

    def __init__(
        self,
        config: DispatcherConfig,
        linear_client: ILinearClient,
        lease_manager: LeaseManager,
        worktree_service: WorktreeService,
        github_service: IGitHubService,
        worktree_base_dir: Path | str,
        canonical_main_sha: str,
    ) -> None:
        self._config = config
        self._linear_client = linear_client
        self._lease_manager = lease_manager
        self._worktree_service = worktree_service
        self._github_service = github_service
        self._worktree_base_dir = Path(worktree_base_dir).resolve()
        self._writeback_service = WritebackService(linear_client)
        self._canonical_main_sha = canonical_main_sha

    def dispatch_one_task(
        self,
        tasks: Sequence[LinearTask],
        apply_task_changes: Callable[[Path, LinearTask], Sequence[str]] | None = None,
        now_iso: str | None = None,
    ) -> DispatchResult | None:
        """Select, claim, execute, validate, PR, and write back exactly ONE task."""
        # Phase 1: Select next eligible task
        task = select_next_task(tasks, self._lease_manager, self._config, now_iso=now_iso)
        if not task:
            return None

        sm = TaskStateMachine(task.id, TaskState.DISCOVERED)

        # Pre-check forbidden task scope
        is_allowed, block_reason = ScopeGate.evaluate_task_intent(task)
        if not is_allowed:
            sm.transition(TaskState.BLOCKED, reason=str(block_reason), now_iso=now_iso)
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=None,
                pr_number=None,
                pr_url=None,
                head_sha=None,
                block_reason=str(block_reason),
                transitions=sm.history,
            )

        # Phase 2: Claim lease
        claimed, claim_record, claim_err = self._lease_manager.claim(
            task.id,
            self._config.worker_id,
            self._config.lease_duration_sec,
            now_iso=now_iso,
        )
        if not claimed or not claim_record:
            sm.transition(TaskState.BLOCKED, reason=claim_err or "CLAIM_FAILED", now_iso=now_iso)
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=None,
                pr_number=None,
                pr_url=None,
                head_sha=None,
                block_reason=claim_err or "CLAIM_FAILED",
                transitions=sm.history,
            )

        sm.transition(TaskState.CLAIMED, evidence={"claim_token": claim_record.claim_token}, now_iso=now_iso)

        # Phase 3: Setup isolated worktree
        wt_path, branch_name, wt_err = self._worktree_service.create_worktree(
            task,
            self._worktree_base_dir,
            fail_if_dirty=True,
        )
        if not wt_path or not branch_name:
            sm.transition(TaskState.BLOCKED, reason=str(wt_err), now_iso=now_iso)
            self._lease_manager.release(task.id, self._config.worker_id, claim_record.claim_token)
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=None,
                pr_number=None,
                pr_url=None,
                head_sha=None,
                block_reason=str(wt_err),
                transitions=sm.history,
            )

        sm.transition(
            TaskState.WORKTREE_READY,
            evidence={"worktree_path": str(wt_path), "branch": branch_name},
            now_iso=now_iso,
        )

        try:
            # Stale worker / lost lease check before applying mutations
            if not self._lease_manager.verify_lease(task.id, self._config.worker_id, claim_record.claim_token, now_iso=now_iso):
                sm.transition(TaskState.BLOCKED, reason=BlockReasonCode.LOST_LEASE.value, now_iso=now_iso)
                return DispatchResult(
                    task_id=task.id,
                    final_state=TaskState.BLOCKED,
                    branch=branch_name,
                    pr_number=None,
                    pr_url=None,
                    head_sha=None,
                    block_reason=BlockReasonCode.LOST_LEASE.value,
                    transitions=sm.history,
                )

            # Phase 4: Apply task changes
            sm.transition(TaskState.IMPLEMENTING, now_iso=now_iso)
            changed_files: list[str] = []
            if apply_task_changes:
                changed_files = list(apply_task_changes(wt_path, task))

            # Phase 5: Scope gate evaluation on changed files
            files_ok, files_block = ScopeGate.evaluate_changed_files(changed_files)
            if not files_ok:
                sm.transition(TaskState.BLOCKED, reason=str(files_block), now_iso=now_iso)
                return DispatchResult(
                    task_id=task.id,
                    final_state=TaskState.BLOCKED,
                    branch=branch_name,
                    pr_number=None,
                    pr_url=None,
                    head_sha=None,
                    block_reason=str(files_block),
                    transitions=sm.history,
                )

            # Git stage changes
            import subprocess
            subprocess.run(["git", "add", "-A"], cwd=str(wt_path), check=True)

            # Phase 6: Local validation
            sm.transition(TaskState.VALIDATING, now_iso=now_iso)
            val_ok, val_details = LocalValidator.run_validation(wt_path)
            if not val_ok:
                sm.transition(TaskState.FAILED, evidence=val_details, reason="LOCAL_VALIDATION_FAILED", now_iso=now_iso)
                return DispatchResult(
                    task_id=task.id,
                    final_state=TaskState.FAILED,
                    branch=branch_name,
                    pr_number=None,
                    pr_url=None,
                    head_sha=None,
                    block_reason="LOCAL_VALIDATION_FAILED",
                    transitions=sm.history,
                )

            commit_msg = f"feat({task.id.lower()}): {task.title}"
            subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(wt_path), check=True)
            head_sha_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
                check=True,
            )
            head_sha = head_sha_proc.stdout.strip()

            # Push branch
            subprocess.run(
                ["git", "push", self._config.canonical_remote, branch_name],
                cwd=str(wt_path),
                capture_output=True,
                text=True,
            )

            # Phase 7: Create Draft PR
            pr_ok, pr_result, pr_err = self._github_service.create_draft_pr(
                task,
                branch_name,
                self._canonical_main_sha,
                head_sha,
                wt_path,
            )
            if not pr_ok or not pr_result:
                sm.transition(TaskState.FAILED, reason=pr_err or "PR_CREATION_FAILED", now_iso=now_iso)
                return DispatchResult(
                    task_id=task.id,
                    final_state=TaskState.FAILED,
                    branch=branch_name,
                    pr_number=None,
                    pr_url=None,
                    head_sha=head_sha,
                    block_reason=pr_err or "PR_CREATION_FAILED",
                    transitions=sm.history,
                )

            sm.transition(
                TaskState.PR_OPEN,
                evidence={"pr_number": pr_result.pr_number, "pr_url": pr_result.pr_url, "head_sha": head_sha},
                now_iso=now_iso,
            )

            # Phase 8: CI Check (Exact-head)
            sm.transition(TaskState.CI_PENDING, now_iso=now_iso)
            import time
            start_wait = time.time()
            ci_ok = False
            ci_result = None
            ci_reason = None
            while True:
                ci_ok, ci_result, ci_reason = self._github_service.get_ci_status(
                    self._config.canonical_repo,
                    head_sha,
                    pr_result.pr_number,
                )
                if ci_ok and ci_result and ci_result.overall_status == "PASS":
                    break
                if ci_reason == BlockReasonCode.CI_SHA_MISMATCH or (ci_result and ci_result.overall_status == "FAIL"):
                    break
                if (time.time() - start_wait) >= self._config.ci_max_wait_sec:
                    ci_reason = BlockReasonCode.CI_FAILED
                    break
                time.sleep(self._config.ci_poll_interval_sec)

            if not ci_ok or not ci_result or ci_result.overall_status != "PASS":
                reason_code = ci_reason.value if ci_reason else "CI_INCOMPLETE_OR_FAILED"
                target_state = TaskState.BLOCKED if ci_reason == BlockReasonCode.CI_SHA_MISMATCH else TaskState.FAILED
                sm.transition(target_state, reason=reason_code, now_iso=now_iso)
                return DispatchResult(
                    task_id=task.id,
                    final_state=target_state,
                    branch=branch_name,
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                    head_sha=head_sha,
                    block_reason=reason_code,
                    transitions=sm.history,
                )

            sm.transition(
                TaskState.CI_PASS,
                evidence={"ci_head_sha": ci_result.head_sha, "runs": len(ci_result.runs)},
                now_iso=now_iso,
            )

            # Phase 9: Linear Writeback & Reread confirmation
            evidence = ExecutionEvidence(
                task_id=task.id,
                execution_status="PASS",
                claim_owner=self._config.worker_id,
                branch=branch_name,
                pr_number=pr_result.pr_number,
                pr_url=pr_result.pr_url,
                base_sha=self._canonical_main_sha,
                head_sha=head_sha,
                validation_status="PASS",
                ci_status="PASS",
                ci_head_sha=ci_result.head_sha,
                sha_match="YES",
            )
            wb_ok, wb_reason = self._writeback_service.writeback_and_verify(task, evidence)
            if not wb_ok:
                sm.transition(TaskState.BLOCKED, reason=str(wb_reason), now_iso=now_iso)
                return DispatchResult(
                    task_id=task.id,
                    final_state=TaskState.BLOCKED,
                    branch=branch_name,
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                    head_sha=head_sha,
                    block_reason=str(wb_reason),
                    transitions=sm.history,
                )

            sm.transition(TaskState.WRITEBACK_DONE, now_iso=now_iso)
            sm.transition(TaskState.DONE, now_iso=now_iso)

            # Cleanup
            self._lease_manager.release(task.id, self._config.worker_id, claim_record.claim_token)
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.DONE,
                branch=branch_name,
                pr_number=pr_result.pr_number,
                pr_url=pr_result.pr_url,
                head_sha=head_sha,
                block_reason=None,
                transitions=sm.history,
            )

        finally:
            pass
