"""Hermes Linear Autonomous Dispatcher orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable, Sequence

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
    TaskState,
)
from ai_engineering.linear_dispatcher.execution_ledger import ExecutionState
from ai_engineering.linear_dispatcher.github_service import IGitHubService
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.scope_gate import ScopeGate
from ai_engineering.linear_dispatcher.state_machine import TaskStateMachine
from ai_engineering.linear_dispatcher.task_executor import (
    ITaskExecutor,
    SimulatedTaskExecutor,
)
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
        execution_ledger: Any = None,
        task_executor: ITaskExecutor | None = None,
    ) -> None:
        self._config = config
        self._linear_client = linear_client
        self._lease_manager = lease_manager
        self._worktree_service = worktree_service
        self._github_service = github_service
        self._worktree_base_dir = Path(worktree_base_dir).resolve()
        self._writeback_service = WritebackService(linear_client)
        self._canonical_main_sha = canonical_main_sha
        self._ledger = execution_ledger
        self._task_executor = task_executor
        self._canonical_root = getattr(worktree_service, "canonical_root", Path("."))

    @staticmethod
    def _safe_git_run(
        args: list[str], cwd: Path | str, **kwargs
    ) -> subprocess.CompletedProcess:
        safe_opts = [
            "-c", "core.fsmonitor=",
            "-c", "core.hooksPath=/dev/null",
            "-c", "core.whitespace=cr-at-eol",
            "-c", "core.autocrlf=false",
            "-c", "filter.lfs.smudge=",
            "-c", "filter.lfs.clean=",
            "-c", "filter.lfs.process=",
            "-c", "filter.lfs.required=false",
        ]
        env = dict(kwargs.pop("env", os.environ))
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        cmd = ["git"] + safe_opts + ["-C", str(cwd)] + args
        return subprocess.run(cmd, env=env, **kwargs)

    def dispatch_one_task(
        self,
        tasks: Sequence[LinearTask],
        apply_task_changes: Callable[[Path, LinearTask], Sequence[str]] | None = None,
        now_iso: str | None = None,
    ) -> DispatchResult | None:
        """Select, claim, execute, validate, PR, and write back exactly ONE task."""
        # Phase 1: Select next eligible task
        task = select_next_task(
            tasks, self._lease_manager, self._config, now_iso=now_iso
        )
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
            sm.transition(
                TaskState.BLOCKED, reason=claim_err or "CLAIM_FAILED", now_iso=now_iso
            )
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

        sm.transition(
            TaskState.CLAIMED,
            evidence={"claim_token": claim_record.claim_token},
            now_iso=now_iso,
        )
        if self._ledger:
            self._ledger.write_state(
                ExecutionState(
                    task_id=task.id,
                    state=TaskState.CLAIMED.value,
                    claim_owner=self._config.worker_id,
                    claim_token=claim_record.claim_token,
                    base_sha=self._canonical_main_sha,
                )
            )

        # Phase 3: Setup isolated worktree
        wt_path, branch_name, wt_err = self._worktree_service.create_worktree(
            task,
            self._worktree_base_dir,
            fail_if_dirty=True,
        )
        if not wt_path or not branch_name:
            sm.transition(TaskState.BLOCKED, reason=str(wt_err), now_iso=now_iso)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
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
        if self._ledger:
            self._ledger.write_state(
                ExecutionState(
                    task_id=task.id,
                    state=TaskState.WORKTREE_READY.value,
                    claim_owner=self._config.worker_id,
                    claim_token=claim_record.claim_token,
                    worktree_path=str(wt_path),
                    branch=branch_name,
                    base_sha=self._canonical_main_sha,
                )
            )

        # Heartbeat check 1: Before task execution
        if not self._lease_manager.verify_lease(
            task.id, self._config.worker_id, claim_record.claim_token, now_iso=now_iso
        ):
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.LOST_LEASE.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            if self._ledger:
                self._ledger.clear()
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

        # Phase 4: Apply task changes via TaskExecutor
        sm.transition(TaskState.IMPLEMENTING, now_iso=now_iso)
        executor = self._task_executor
        if not executor and apply_task_changes:
            executor = SimulatedTaskExecutor(mutation_handler=apply_task_changes)

        if not executor:
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.NO_SUPPORTED_TASK_EXECUTION_BACKEND.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=None,
                block_reason=BlockReasonCode.NO_SUPPORTED_TASK_EXECUTION_BACKEND.value,
                transitions=sm.history,
            )

        exec_res = executor.execute(task, wt_path, self._canonical_main_sha)
        if exec_res.status != "SUCCESS":
            target_state = (
                TaskState.BLOCKED if exec_res.status == "BLOCKED" else TaskState.FAILED
            )
            reason = exec_res.error_reason or "EXECUTOR_FAILED"
            sm.transition(target_state, reason=reason, now_iso=now_iso)
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=target_state,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=None,
                block_reason=reason,
                transitions=sm.history,
            )

        # Heartbeat check 2: After executor completion
        if not self._lease_manager.verify_lease(
            task.id, self._config.worker_id, claim_record.claim_token, now_iso=now_iso
        ):
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.LOST_LEASE.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            if self._ledger:
                self._ledger.clear()
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

        # Independently detect actual modified/untracked files
        actual_changes = self._get_git_changes(wt_path)
        if not actual_changes:
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.NO_CHANGES_PRODUCED.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=None,
                block_reason=BlockReasonCode.NO_CHANGES_PRODUCED.value,
                transitions=sm.history,
            )

        # Phase 5: Scope gate evaluation on actual changed files
        files_ok, files_block = ScopeGate.evaluate_changed_files(actual_changes)
        if not files_ok:
            sm.transition(TaskState.BLOCKED, reason=str(files_block), now_iso=now_iso)
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
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
        self._safe_git_run(["add", "-A"], cwd=wt_path, check=True)

        # Re-verify staged files against ScopeGate
        staged_res = self._safe_git_run(
            ["diff", "--cached", "--name-only"],
            cwd=wt_path,
            capture_output=True,
            text=True,
            check=True,
        )
        staged_files = [f.strip() for f in staged_res.stdout.splitlines() if f.strip()]
        staged_allowed, staged_block = ScopeGate.evaluate_changed_files(staged_files)
        if not staged_allowed:
            sm.transition(
                TaskState.BLOCKED,
                reason=str(staged_block),
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=None,
                block_reason=str(staged_block),
                transitions=sm.history,
            )

        # Phase 6: Local validation
        sm.transition(TaskState.VALIDATING, now_iso=now_iso)
        val_ok, val_details = LocalValidator.run_validation(
            wt_path, trusted_root=self._canonical_root
        )
        if not val_ok:
            sm.transition(
                TaskState.FAILED,
                evidence=val_details,
                reason="LOCAL_VALIDATION_FAILED",
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
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
        self._safe_git_run(
            ["commit", "-m", commit_msg], cwd=wt_path, check=True
        )
        head_sha_proc = self._safe_git_run(
            ["rev-parse", "HEAD"],
            cwd=wt_path,
            capture_output=True,
            text=True,
            check=True,
        )
        head_sha = head_sha_proc.stdout.strip()

        sm.transition(
            TaskState.COMMITTED,
            evidence={"head_sha": head_sha},
            now_iso=now_iso,
        )
        if self._ledger:
            self._ledger.write_state(
                ExecutionState(
                    task_id=task.id,
                    state=TaskState.COMMITTED.value,
                    claim_owner=self._config.worker_id,
                    claim_token=claim_record.claim_token,
                    worktree_path=str(wt_path),
                    branch=branch_name,
                    head_sha=head_sha,
                    base_sha=self._canonical_main_sha,
                )
            )

        # Phase 7: Main Drift Barrier (before push / PR)
        current_main_sha = self._resolve_remote_main_sha(wt_path)
        if not current_main_sha:
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.CANONICAL_MAIN_LOOKUP_FAILED.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=head_sha,
                block_reason=BlockReasonCode.CANONICAL_MAIN_LOOKUP_FAILED.value,
                transitions=sm.history,
            )
        elif current_main_sha != self._canonical_main_sha:
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.CANONICAL_MAIN_DRIFT.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=head_sha,
                block_reason=BlockReasonCode.CANONICAL_MAIN_DRIFT.value,
                transitions=sm.history,
            )

        # Heartbeat check 3: Before git push
        if not self._lease_manager.verify_lease(
            task.id, self._config.worker_id, claim_record.claim_token, now_iso=now_iso
        ):
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.LOST_LEASE.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=head_sha,
                block_reason=BlockReasonCode.LOST_LEASE.value,
                transitions=sm.history,
            )

        # Push branch
        chk_remote = self._safe_git_run(
            ["remote", "get-url", self._config.canonical_remote],
            cwd=wt_path,
            capture_output=True,
            text=True,
        )
        if chk_remote.returncode == 0:
            push_res = self._safe_git_run(
                ["push", self._config.canonical_remote, branch_name],
                cwd=wt_path,
                capture_output=True,
                text=True,
            )
            if push_res.returncode != 0:
                sm.transition(
                    TaskState.FAILED,
                    reason="GIT_PUSH_FAILED",
                    evidence={"stderr": push_res.stderr or push_res.stdout},
                    now_iso=now_iso,
                )
                self._worktree_service.remove_worktree(wt_path, branch_name)
                self._lease_manager.release(
                    task.id, self._config.worker_id, claim_record.claim_token
                )
                if self._ledger:
                    self._ledger.clear()
                return DispatchResult(
                    task_id=task.id,
                    final_state=TaskState.FAILED,
                    branch=branch_name,
                    pr_number=None,
                    pr_url=None,
                    head_sha=head_sha,
                    block_reason="GIT_PUSH_FAILED",
                    transitions=sm.history,
                )

            sm.transition(
                TaskState.BRANCH_PUSHED,
                evidence={"branch": branch_name, "head_sha": head_sha},
                now_iso=now_iso,
            )
            if self._ledger:
                self._ledger.write_state(
                    ExecutionState(
                        task_id=task.id,
                        state=TaskState.BRANCH_PUSHED.value,
                        claim_owner=self._config.worker_id,
                        claim_token=claim_record.claim_token,
                        worktree_path=str(wt_path),
                        branch=branch_name,
                        head_sha=head_sha,
                        base_sha=self._canonical_main_sha,
                    )
                )

        # Heartbeat check 4: Before PR creation
        if not self._lease_manager.verify_lease(
            task.id, self._config.worker_id, claim_record.claim_token, now_iso=now_iso
        ):
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.LOST_LEASE.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=None,
                pr_url=None,
                head_sha=head_sha,
                block_reason=BlockReasonCode.LOST_LEASE.value,
                transitions=sm.history,
            )

        # Create Draft PR
        pr_ok, pr_result, pr_err = self._github_service.create_draft_pr(
            task,
            branch_name,
            self._canonical_main_sha,
            head_sha,
            wt_path,
        )
        if not pr_ok or not pr_result:
            sm.transition(
                TaskState.FAILED, reason=pr_err or "PR_CREATION_FAILED", now_iso=now_iso
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
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
            evidence={
                "pr_number": pr_result.pr_number,
                "pr_url": pr_result.pr_url,
                "head_sha": head_sha,
            },
            now_iso=now_iso,
        )
        if self._ledger:
            self._ledger.write_state(
                ExecutionState(
                    task_id=task.id,
                    state=TaskState.PR_OPEN.value,
                    claim_owner=self._config.worker_id,
                    claim_token=claim_record.claim_token,
                    worktree_path=str(wt_path),
                    branch=branch_name,
                    head_sha=head_sha,
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                    base_sha=self._canonical_main_sha,
                )
            )

        # Phase 8: CI Check (Exact-head)
        sm.transition(TaskState.CI_PENDING, now_iso=now_iso)
        if self._ledger:
            self._ledger.write_state(
                ExecutionState(
                    task_id=task.id,
                    state=TaskState.CI_PENDING.value,
                    claim_owner=self._config.worker_id,
                    claim_token=claim_record.claim_token,
                    worktree_path=str(wt_path),
                    branch=branch_name,
                    head_sha=head_sha,
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                    base_sha=self._canonical_main_sha,
                )
            )

        ci_ok, ci_result, ci_reason = self._wait_for_ci(
            task.id, head_sha, pr_result.pr_number, now_iso=now_iso
        )
        if not ci_ok or not ci_result or ci_result.overall_status != "PASS":
            reason_code = (
                ci_reason.value
                if hasattr(ci_reason, "value")
                else (str(ci_reason) if ci_reason else "CI_INCOMPLETE_OR_FAILED")
            )
            target_state = (
                TaskState.BLOCKED
                if ci_reason == BlockReasonCode.CI_SHA_MISMATCH
                or ci_reason == BlockReasonCode.LOST_LEASE
                else TaskState.FAILED
            )
            sm.transition(target_state, reason=reason_code, now_iso=now_iso)
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
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
        if self._ledger:
            self._ledger.write_state(
                ExecutionState(
                    task_id=task.id,
                    state=TaskState.CI_PASS.value,
                    claim_owner=self._config.worker_id,
                    claim_token=claim_record.claim_token,
                    worktree_path=str(wt_path),
                    branch=branch_name,
                    head_sha=head_sha,
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                    base_sha=self._canonical_main_sha,
                )
            )

        # Heartbeat check 6: Before Linear writeback
        if not self._lease_manager.verify_lease(
            task.id, self._config.worker_id, claim_record.claim_token, now_iso=now_iso
        ):
            sm.transition(
                TaskState.BLOCKED,
                reason=BlockReasonCode.LOST_LEASE.value,
                now_iso=now_iso,
            )
            self._worktree_service.remove_worktree(wt_path, branch_name)
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task.id,
                final_state=TaskState.BLOCKED,
                branch=branch_name,
                pr_number=pr_result.pr_number,
                pr_url=pr_result.pr_url,
                head_sha=head_sha,
                block_reason=BlockReasonCode.LOST_LEASE.value,
                transitions=sm.history,
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
            self._worktree_service.remove_worktree(wt_path, branch_name)
            self._lease_manager.release(
                task.id, self._config.worker_id, claim_record.claim_token
            )
            if self._ledger:
                self._ledger.clear()
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
        if self._ledger:
            self._ledger.write_state(
                ExecutionState(
                    task_id=task.id,
                    state=TaskState.WRITEBACK_DONE.value,
                    claim_owner=self._config.worker_id,
                    claim_token=claim_record.claim_token,
                    worktree_path=str(wt_path),
                    branch=branch_name,
                    head_sha=head_sha,
                    pr_number=pr_result.pr_number,
                    pr_url=pr_result.pr_url,
                    base_sha=self._canonical_main_sha,
                )
            )
        sm.transition(TaskState.DONE, now_iso=now_iso)

        # Cleanup on DONE
        self._worktree_service.remove_worktree(wt_path, branch_name)
        self._lease_manager.release(
            task.id, self._config.worker_id, claim_record.claim_token
        )
        if self._ledger:
            self._ledger.clear()

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

    def recover_task(
        self, recovery_state: ExecutionState, now_iso: str | None = None
    ) -> DispatchResult | None:
        """Safely resume an interrupted task from a proven idempotent boundary."""
        task_id = recovery_state.task_id
        owner = recovery_state.claim_owner
        token = recovery_state.claim_token
        state_val = recovery_state.state

        # Invariant H06: Exclusive lease fencing MUST precede all effects and cleanup
        if not self._lease_manager.verify_lease(task_id, owner, token, now_iso=now_iso):
            claim_ok, claim_record, claim_reason = self._lease_manager.claim(
                task_id, self._config.worker_id, self._config.lease_duration_sec, now_iso=now_iso
            )
            if not claim_ok or not claim_record:
                reason = (
                    claim_reason.value
                    if hasattr(claim_reason, "value")
                    else (str(claim_reason) if claim_reason else BlockReasonCode.LOST_LEASE.value)
                )
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.BLOCKED,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason=reason,
                    transitions=(),
                )
            owner = self._config.worker_id
            token = claim_record.claim_token

        # Early abortable states (before git commit)
        if state_val in (TaskState.CLAIMED.value, TaskState.WORKTREE_READY.value):
            if recovery_state.worktree_path and recovery_state.branch:
                self._worktree_service.remove_worktree(
                    recovery_state.worktree_path, recovery_state.branch
                )
            self._lease_manager.release(task_id, owner, token)
            if self._ledger:
                self._ledger.clear()
            return None

        # Reconcile COMMITTED or BRANCH_PUSHED states
        if state_val in (TaskState.COMMITTED.value, TaskState.BRANCH_PUSHED.value):
            if self._github_service and recovery_state.branch:
                try:
                    search_res = self._github_service._request(
                        "GET", f"pulls?head=life2boat:{recovery_state.branch}&state=open"
                    )
                    if search_res and len(search_res) == 1:
                        pr_num = search_res[0].get("number", 0)
                        pr_url = search_res[0].get("html_url", "")
                        recovery_state.pr_number = pr_num
                        recovery_state.pr_url = pr_url
                        state_val = TaskState.PR_OPEN.value
                except Exception:
                    pass

            if state_val != TaskState.PR_OPEN.value:
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.BLOCKED,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason=BlockReasonCode.RECOVERY_BLOCKED.value,
                    transitions=(),
                )

        # Recovery for PR_OPEN or CI_PENDING or CI_PASS
        if state_val in (
            TaskState.PR_OPEN.value,
            TaskState.CI_PENDING.value,
            TaskState.CI_PASS.value,
        ):
            if (
                not recovery_state.pr_number
                or not recovery_state.head_sha
                or not recovery_state.branch
            ):
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.BLOCKED,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason=BlockReasonCode.RECOVERY_BLOCKED.value,
                    transitions=(),
                )

            # If CI was pending, resume CI waiting
            if state_val in (TaskState.PR_OPEN.value, TaskState.CI_PENDING.value):
                ci_ok, ci_result, ci_reason = self._wait_for_ci(
                    task_id,
                    recovery_state.head_sha,
                    recovery_state.pr_number,
                    now_iso=now_iso,
                )
                if not ci_ok or not ci_result or ci_result.overall_status != "PASS":
                    reason = (
                        ci_reason.value
                        if hasattr(ci_reason, "value")
                        else str(ci_reason)
                    )
                    if recovery_state.worktree_path:
                        self._worktree_service.remove_worktree(
                            recovery_state.worktree_path, recovery_state.branch
                        )
                    self._lease_manager.release(task_id, owner, token)
                    if self._ledger:
                        self._ledger.clear()
                    return DispatchResult(
                        task_id=task_id,
                        final_state=TaskState.FAILED,
                        branch=recovery_state.branch,
                        pr_number=recovery_state.pr_number,
                        pr_url=recovery_state.pr_url,
                        head_sha=recovery_state.head_sha,
                        block_reason=reason,
                        transitions=(),
                    )

            # Invariant H06: For CI_PASS recovery, re-verify with GitHub that CI is actually PASS
            if state_val == TaskState.CI_PASS.value:
                if not self._github_service:
                    return DispatchResult(
                        task_id=task_id,
                        final_state=TaskState.BLOCKED,
                        branch=recovery_state.branch,
                        pr_number=recovery_state.pr_number,
                        pr_url=recovery_state.pr_url,
                        head_sha=recovery_state.head_sha,
                        block_reason="CI_PASS_REVALIDATION_FAILED",
                        transitions=(),
                    )
                ci_ok, ci_result, ci_reason = self._github_service.get_ci_status(
                    self._config.canonical_repo,
                    recovery_state.head_sha,
                    recovery_state.pr_number or 0,
                )
                if (
                    not ci_ok
                    or not ci_result
                    or ci_result.overall_status != "PASS"
                    or ci_result.head_sha != recovery_state.head_sha
                ):
                    return DispatchResult(
                        task_id=task_id,
                        final_state=TaskState.BLOCKED,
                        branch=recovery_state.branch,
                        pr_number=recovery_state.pr_number,
                        pr_url=recovery_state.pr_url,
                        head_sha=recovery_state.head_sha,
                        block_reason="CI_PASS_REVALIDATION_FAILED",
                        transitions=(),
                    )

            # Proceed to writeback: issue must exist
            task = self._linear_client.get_issue(task_id)
            if not task:
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.BLOCKED,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason="LINEAR_TASK_NOT_FOUND",
                    transitions=(),
                )

            evidence = ExecutionEvidence(
                task_id=task_id,
                execution_status="PASS",
                claim_owner=owner,
                branch=recovery_state.branch or "",
                pr_number=recovery_state.pr_number or 0,
                pr_url=recovery_state.pr_url or "",
                base_sha=recovery_state.base_sha or self._canonical_main_sha,
                head_sha=recovery_state.head_sha or "",
                validation_status="PASS",
                ci_status="PASS",
                ci_head_sha=recovery_state.head_sha or "",
                sha_match="YES",
                execution_id=recovery_state.head_sha or task_id,
            )
            wb_ok, wb_reason = self._writeback_service.writeback_and_verify(
                task, evidence
            )
            if not wb_ok:
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.BLOCKED,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason=str(wb_reason),
                    transitions=(),
                )

            if recovery_state.worktree_path:
                self._worktree_service.remove_worktree(
                    recovery_state.worktree_path, recovery_state.branch
                )
            self._lease_manager.release(task_id, owner, token)
            if self._ledger:
                self._ledger.clear()
            return DispatchResult(
                task_id=task_id,
                final_state=TaskState.DONE,
                branch=recovery_state.branch,
                pr_number=recovery_state.pr_number,
                pr_url=recovery_state.pr_url,
                head_sha=recovery_state.head_sha,
                block_reason=None,
                transitions=(),
            )

        if state_val == TaskState.WRITEBACK_DONE.value:
            # Re-read Linear evidence and verify terminal state and authentic receipt
            comments = self._linear_client.get_issue_comments(task_id)
            refetched = self._linear_client.get_issue(task_id)
            if not refetched:
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.BLOCKED,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason="LINEAR_TASK_NOT_FOUND",
                    transitions=(),
                )

            if refetched.state.lower() not in ("done", "completed", "closed"):
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.BLOCKED,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason=BlockReasonCode.REREAD_VERIFICATION_FAILED.value,
                    transitions=(),
                )

            header_marker = "### Autonomous Task Execution Evidence"
            sha_marker = f"HEAD_SHA:** `{recovery_state.head_sha}`" if recovery_state.head_sha else ""
            has_receipt = (
                any(header_marker in c and sha_marker in c for c in comments)
                or (header_marker in refetched.description and sha_marker in refetched.description)
            )

            if has_receipt:
                if recovery_state.worktree_path:
                    self._worktree_service.remove_worktree(
                        recovery_state.worktree_path, recovery_state.branch
                    )
                self._lease_manager.release(task_id, owner, token)
                if self._ledger:
                    self._ledger.clear()
                return DispatchResult(
                    task_id=task_id,
                    final_state=TaskState.DONE,
                    branch=recovery_state.branch,
                    pr_number=recovery_state.pr_number,
                    pr_url=recovery_state.pr_url,
                    head_sha=recovery_state.head_sha,
                    block_reason=None,
                    transitions=(),
                )

            return DispatchResult(
                task_id=task_id,
                final_state=TaskState.BLOCKED,
                branch=recovery_state.branch,
                pr_number=recovery_state.pr_number,
                pr_url=recovery_state.pr_url,
                head_sha=recovery_state.head_sha,
                block_reason=BlockReasonCode.RECOVERY_BLOCKED.value,
                transitions=(),
            )

        return None

    def _wait_for_ci(
        self,
        task_id: str,
        head_sha: str,
        pr_number: int,
        now_iso: str | None = None,
    ) -> tuple[bool, Any, Any]:
        start_wait = time.time()
        ci_ok = False
        ci_result = None
        ci_reason = None
        while True:
            ci_ok, ci_result, ci_reason = self._github_service.get_ci_status(
                self._config.canonical_repo,
                head_sha,
                pr_number,
            )
            if ci_ok and ci_result and ci_result.overall_status == "PASS":
                break
            if ci_reason == BlockReasonCode.CI_SHA_MISMATCH or (
                ci_result and ci_result.overall_status == "FAIL"
            ):
                break
            if (time.time() - start_wait) >= self._config.ci_max_wait_sec:
                ci_reason = BlockReasonCode.CI_FAILED
                break

            # Heartbeat renewal during CI polling
            renewed, _, _ = self._lease_manager.claim(
                task_id,
                self._config.worker_id,
                self._config.lease_duration_sec,
                now_iso=now_iso,
            )
            if not renewed:
                ci_reason = BlockReasonCode.LOST_LEASE
                break

            time.sleep(self._config.ci_poll_interval_sec)

        return ci_ok, ci_result, ci_reason

    def _resolve_remote_main_sha(self, wt_path: Path | str | None = None) -> str:
        try:
            remote = self._config.canonical_remote
            cwd = wt_path or self._canonical_root

            # Check if remote is configured in this repository
            chk_remote = self._safe_git_run(
                ["remote", "get-url", remote],
                cwd=cwd,
                capture_output=True,
                text=True,
            )
            if chk_remote.returncode != 0:
                # Canonical remote must be configured; fail closed if missing
                return ""

            drift_proc = self._safe_git_run(
                ["ls-remote", remote, "refs/heads/main"],
                cwd=cwd,
                capture_output=True,
                text=True,
            )
            if drift_proc.returncode == 0 and drift_proc.stdout.strip():
                return drift_proc.stdout.split()[0]
            return ""
        except Exception:
            return ""

    def _get_git_changes(self, worktree_path: Path) -> tuple[str, ...]:
        try:
            res = self._safe_git_run(
                ["status", "--porcelain=v1"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                check=True,
            )
            files: list[str] = []
            for line in res.stdout.splitlines():
                if len(line) < 4:
                    continue
                raw_path = line[3:].strip()
                if " -> " in raw_path:
                    old_path, new_path = raw_path.split(" -> ", 1)
                    files.append(old_path.strip('"'))
                    files.append(new_path.strip('"'))
                else:
                    files.append(raw_path.strip('"'))
            return tuple(sorted(set(files)))
        except Exception:
            return ()
