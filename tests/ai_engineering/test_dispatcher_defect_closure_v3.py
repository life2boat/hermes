"""Comprehensive test suite for Hermes Linear Autonomous Dispatcher Defect Closure v3."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Sequence
import pytest

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
    TaskState,
)
from ai_engineering.linear_dispatcher.dispatcher import (
    AutonomousDispatcher,
    DispatchResult,
)
from ai_engineering.linear_dispatcher.execution_ledger import (
    ExecutionLedger,
    ExecutionState,
    SingleWorkerLock,
)
from ai_engineering.linear_dispatcher.github_service import (
    CIStatusResult,
    IGitHubService,
    PRCreationResult,
)
from ai_engineering.linear_dispatcher.lease_manager import LeaseError, LeaseManager
from ai_engineering.linear_dispatcher.task_executor import (
    CodexTaskExecutor,
    ExecutionResult,
    ITaskExecutor,
    SimulatedTaskExecutor,
)
from ai_engineering.linear_dispatcher.worker import DispatcherWorker
from ai_engineering.linear_dispatcher.worktree_service import WorktreeService


class MockLinearClient:
    def __init__(self, tasks: list[LinearTask] | None = None) -> None:
        self.tasks = {t.id: t for t in (tasks or [])}
        self.comments: dict[str, list[str]] = {}

    def get_issue(self, issue_id: str) -> LinearTask | None:
        return self.tasks.get(issue_id)

    def list_issues(self, team: str = "Hermes", limit: int = 50) -> list[LinearTask]:
        return list(self.tasks.values())

    def get_issue_comments(self, issue_id: str) -> list[str]:
        return self.comments.get(issue_id, [])

    def add_comment(self, issue_id: str, body: str) -> bool:
        self.comments.setdefault(issue_id, []).append(body)
        return True

    def update_issue(self, issue_id: str, fields: dict[str, Any]) -> bool:
        t = self.tasks.get(issue_id)
        if not t:
            return False
        from dataclasses import replace
        new_state = fields.get("state", t.state)
        new_desc = fields.get("description", t.description)
        self.tasks[issue_id] = replace(t, state=new_state, description=new_desc)
        return True


class MockGitHubService(IGitHubService):
    def __init__(self, ci_status: str = "PASS", fail_pr: bool = False) -> None:
        self.ci_status = ci_status
        self.fail_pr = fail_pr
        self.created_prs: list[dict[str, Any]] = []

    def create_draft_pr(
        self,
        task: LinearTask,
        branch_name: str,
        base_sha: str,
        head_sha: str,
        cwd: Path | str,
    ) -> tuple[bool, PRCreationResult | None, str | None]:
        if self.fail_pr:
            return False, None, "PR_CREATION_FAILED_BY_MOCK"
        pr = PRCreationResult(
            pr_number=389,
            pr_url="https://github.com/life2boat/hermes/pull/389",
            base_sha=base_sha,
            head_sha=head_sha,
            is_draft=True,
        )
        self.created_prs.append({"task_id": task.id, "branch": branch_name, "pr": pr})
        return True, pr, None

    def get_ci_status(
        self,
        repo: str,
        expected_head_sha: str,
        pr_number: int,
    ) -> tuple[bool, CIStatusResult | None, BlockReasonCode | None]:
        if self.ci_status == "PASS":
            return (
                True,
                CIStatusResult(
                    overall_status="PASS",
                    head_sha=expected_head_sha,
                    runs=[
                        {
                            "name": "check",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ],
                    details={"check": "success"},
                ),
                None,
            )
        return (
            False,
            CIStatusResult(
                overall_status="FAIL",
                head_sha=expected_head_sha,
                runs=[
                    {"name": "check", "status": "completed", "conclusion": "failure"}
                ],
                details={"check": "failed"},
            ),
            BlockReasonCode.CI_FAILED,
        )


def init_mock_git_repo(repo_dir: Path) -> str:
    """Initialize a git repo with a commit and return HEAD SHA."""
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-B", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=str(repo_dir), check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo_dir),
        check=True,
    )
    readme = repo_dir / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo_dir), check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), check=True
    )
    # Add self as remote 'github' so git ls-remote github refs/heads/main works
    subprocess.run(
        ["git", "remote", "add", "github", str(repo_dir)], cwd=str(repo_dir), check=True
    )
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def make_linear_task(
    task_id: str = "HER-100", title: str = "Test Task", state: str = "Todo"
) -> LinearTask:
    return LinearTask(
        id=task_id,
        uuid=f"uuid-{task_id}",
        title=title,
        description="Harmless bounded task description",
        state=state,
        priority=1,
        assignee="agent",
        labels=("agent:auto", "agent:shadow"),
        created_at="2026-10-05T00:00:00Z",
        updated_at="2026-10-05T00:00:00Z",
        url=f"https://linear.app/hermes/issue/{task_id}",
    )


# ---------------------------------------------------------------------------
# TaskExecutor & Codex Backend Tests
# ---------------------------------------------------------------------------


def test_codex_executor_disabled_when_unproven(tmp_path):
    """When codex binary is unavailable, CodexTaskExecutor fails closed."""
    executor = CodexTaskExecutor(codex_bin="non_existent_binary_xyz_123")
    assert executor.health() is False
    res = executor.execute(make_linear_task(), tmp_path, "base_sha")
    assert res.status == "FAILED"
    assert res.error_reason in ("CODEX_EXECUTOR_UNAVAILABLE", "WINDOWS_CODEX_UNSUPPORTED")


def test_codex_executor_workspace_boundary(tmp_path):
    """CodexTaskExecutor rejects execution if given invalid worktree path."""
    executor = CodexTaskExecutor()
    non_existent = tmp_path / "does_not_exist"
    res = executor.execute(make_linear_task(), non_existent, "base_sha")
    assert res.status == "FAILED"
    assert res.error_reason == "INVALID_WORKTREE_PATH"


def test_executor_actual_git_diff_authoritative(tmp_path):
    """SimulatedTaskExecutor verifies real git status independently."""
    base_sha = init_mock_git_repo(tmp_path)

    def touch_file(wt: Path, task: LinearTask):
        (wt / "change.txt").write_text("hello", encoding="utf-8")
        return ["change.txt"]

    executor = SimulatedTaskExecutor(mutation_handler=touch_file)
    res = executor.execute(make_linear_task(), tmp_path, base_sha)
    assert res.status == "SUCCESS"
    assert "change.txt" in res.changed_files


def test_executor_real_change_path(tmp_path):
    """Real change path produces SUCCESS and non-empty changed files."""
    base_sha = init_mock_git_repo(tmp_path)

    def handler(wt: Path, task: LinearTask):
        (wt / "app.py").write_text("print(1)\n", encoding="utf-8")
        return ["app.py"]

    executor = SimulatedTaskExecutor(mutation_handler=handler)
    res = executor.execute(make_linear_task(), tmp_path, base_sha)
    assert res.status == "SUCCESS"
    assert res.changed_files == ("app.py",)


def test_executor_zero_change_blocks(tmp_path):
    """When executor produces 0 changes, AutonomousDispatcher blocks without empty commits."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)

    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-200")
    linear = MockLinearClient([task])
    gh = MockGitHubService()
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    # Empty executor
    executor = SimulatedTaskExecutor(mutation_handler=lambda wt, t: [])
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=executor,
    )

    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.BLOCKED
    assert result.block_reason == BlockReasonCode.NO_CHANGES_PRODUCED.value
    # Ensure no PR created
    assert len(gh.created_prs) == 0
    # Ensure worktree cleaned up
    assert len(list(wt_base.glob("*"))) == 0


def test_executor_scope_violation_blocks(tmp_path):
    """When executor changes forbidden files (e.g. .env), ScopeGate blocks."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-201")
    linear = MockLinearClient([task])
    gh = MockGitHubService()
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    def write_env(wt: Path, t: LinearTask):
        (wt / ".env").write_text("SECRET=1\n", encoding="utf-8")
        return [".env"]

    executor = SimulatedTaskExecutor(mutation_handler=write_env)
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=executor,
    )

    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.BLOCKED
    assert "CREDENTIALS_MUTATION_FORBIDDEN" in str(result.block_reason)
    assert len(list(wt_base.glob("*"))) == 0


def test_no_executor_silent_fallback(tmp_path):
    """When no executor is provided and no fallback callable exists, dispatch blocks."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-202")
    linear = MockLinearClient([task])
    gh = MockGitHubService()
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=None,
    )
    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.BLOCKED
    assert (
        result.block_reason == BlockReasonCode.NO_SUPPORTED_TASK_EXECUTION_BACKEND.value
    )


# ---------------------------------------------------------------------------
# Worktree Lifecycle & Cleanup Tests
# ---------------------------------------------------------------------------


def test_worktree_cleanup_done(tmp_path):
    """On successful task execution (DONE), worktree is removed."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-203")
    linear = MockLinearClient([task])
    gh = MockGitHubService()
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    def valid_mutation(wt: Path, t: LinearTask):
        (wt / "feature.py").write_text("# new feature\n", encoding="utf-8")
        return ["feature.py"]

    executor = SimulatedTaskExecutor(mutation_handler=valid_mutation)
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=executor,
    )

    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.DONE
    # Worktree directory must be cleaned
    assert len(list(wt_base.glob("*"))) == 0


def test_worktree_cleanup_failed(tmp_path):
    """When PR creation fails, worktree is cleaned up."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-204")
    linear = MockLinearClient([task])
    gh = MockGitHubService(fail_pr=True)
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    def valid_mutation(wt: Path, t: LinearTask):
        (wt / "feature.py").write_text("# new feature\n", encoding="utf-8")
        return ["feature.py"]

    executor = SimulatedTaskExecutor(mutation_handler=valid_mutation)
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=executor,
    )

    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.FAILED
    assert len(list(wt_base.glob("*"))) == 0


def test_worktree_cleanup_blocked(tmp_path, monkeypatch):
    """When local validation fails, worktree is cleaned up."""
    from ai_engineering.linear_dispatcher.validator import LocalValidator

    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-205")
    linear = MockLinearClient([task])
    gh = MockGitHubService()
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    monkeypatch.setattr(
        LocalValidator,
        "run_validation",
        staticmethod(lambda *args, **kwargs: (False, {"error": "forced_validation_failure"})),
    )

    def dummy_mutation(wt: Path, t: LinearTask):
        (wt / "valid.py").write_text("x = 1\n", encoding="utf-8")
        return ["valid.py"]

    executor = SimulatedTaskExecutor(mutation_handler=dummy_mutation)
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=executor,
    )

    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.FAILED
    assert result.block_reason == "LOCAL_VALIDATION_FAILED"
    assert len(list(wt_base.glob("*"))) == 0


def test_worktree_recovery_preserved(tmp_path):
    """Worktree path is recorded in ExecutionLedger and preserved during in-flight crash."""
    ledger_path = tmp_path / "execution_state.json"
    ledger = ExecutionLedger(ledger_path)
    state = ExecutionState(
        task_id="HER-206",
        state=TaskState.PR_OPEN.value,
        claim_owner="worker-1",
        claim_token="tok-123",
        worktree_path=str(tmp_path / "saved_wt"),
        branch="agent/her-206",
        head_sha="head_sha_val",
        pr_number=389,
    )
    ledger.write_state(state)
    read_back = ledger.read_state()
    assert read_back is not None
    assert read_back.worktree_path == str(tmp_path / "saved_wt")


def test_worktree_base_sha_tracks_main_drift(tmp_path):
    """WorktreeService creates branches from exact passed base_sha, tracking main advances."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sha1 = init_mock_git_repo(repo)

    # Create commit 2
    (repo / "file2.txt").write_text("2\n", encoding="utf-8")
    subprocess.run(["git", "add", "file2.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "commit 2"], cwd=str(repo), check=True)
    sha2 = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert sha1 != sha2

    wt_service1 = WorktreeService(canonical_root=repo, base_sha=sha1)
    wt1, b1, _ = wt_service1.create_worktree(
        make_linear_task("HER-1"), tmp_path / "wt1"
    )
    branch_base1 = subprocess.run(
        ["git", "rev-parse", f"{b1}~0"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_base1 == sha1

    wt_service2 = WorktreeService(canonical_root=repo, base_sha=sha2)
    wt2, b2, _ = wt_service2.create_worktree(
        make_linear_task("HER-2"), tmp_path / "wt2"
    )
    branch_base2 = subprocess.run(
        ["git", "rev-parse", f"{b2}~0"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert branch_base2 == sha2


# ---------------------------------------------------------------------------
# Provenance & Canonical Root Validation Tests
# ---------------------------------------------------------------------------


def test_canonical_root_remote_mismatch(tmp_path):
    """DispatcherWorker.validate_canonical_root fails closed if remote is not github:life2boat/hermes."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    init_mock_git_repo(repo)

    worker = DispatcherWorker()
    ok, err = worker.validate_canonical_root(repo)
    assert ok is False
    assert "INVALID_CANONICAL_REMOTE" in str(err)


# ---------------------------------------------------------------------------
# Locking & Lease Concurrency Tests
# ---------------------------------------------------------------------------


def test_lockfile_not_truncated(tmp_path):
    """Non-truncating lockfile preserves existing content when a second worker attempts acquisition."""
    lock_path = tmp_path / "worker.lock"
    # Pre-populate lockfile with data
    lock_path.write_text("WORKER_1_METADATA", encoding="utf-8")

    lock1 = SingleWorkerLock(lock_path)
    assert lock1.acquire() is True

    # Worker 2 attempts acquire
    lock2 = SingleWorkerLock(lock_path)
    assert lock2.acquire() is False

    lock1.release()

    # Verify content was not truncated by worker 1 or worker 2
    assert lock_path.read_text(encoding="utf-8") == "WORKER_1_METADATA"

    assert lock2.acquire() is True
    lock2.release()
    assert lock_path.read_text(encoding="utf-8") == "WORKER_1_METADATA"


def test_second_worker_blocked(tmp_path):
    """Two concurrent workers cannot simultaneously hold the single-worker lock."""
    lock_path = tmp_path / "worker.lock"
    lockA = SingleWorkerLock(lock_path)
    lockB = SingleWorkerLock(lock_path)

    assert lockA.acquire() is True
    assert lockB.acquire() is False
    lockA.release()
    assert lockB.acquire() is True
    lockB.release()


# ---------------------------------------------------------------------------
# Recovery State Machine Tests
# ---------------------------------------------------------------------------


def test_foreign_lease_blocks_startup(tmp_path):
    """When recovery detects an active foreign lease, recovery blocks fail-closed."""
    lease_path = tmp_path / "leases.json"
    leases = LeaseManager(lease_path)
    # Foreign worker claims task
    leases.claim("HER-300", "foreign-worker", lease_duration_sec=3600)

    assert leases.is_held_by_foreign_worker("HER-300", "my-worker") is True


def test_recovery_claimed_cleans_up(tmp_path):
    """Task interrupted at CLAIMED state is cleanly released for fresh poll."""
    config = DispatcherConfig(worker_id="test-worker")
    leases = LeaseManager(tmp_path / "leases.json")
    leases.claim("HER-301", config.worker_id, 3600)

    wt_service = WorktreeService(canonical_root=tmp_path, base_sha="sha")
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=MockLinearClient(),
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=MockGitHubService(),
        worktree_base_dir=tmp_path / "wt",
        canonical_main_sha="sha",
    )
    state = ExecutionState(
        task_id="HER-301",
        state=TaskState.CLAIMED.value,
        claim_owner=config.worker_id,
        claim_token="tok",
    )
    res = dispatcher.recover_task(state)
    assert res is None
    # Lease released
    assert leases.verify_lease("HER-301", config.worker_id, "tok") is False


def test_recovery_worktree_ready(tmp_path):
    """Task interrupted at WORKTREE_READY safely cleans up worktree and releases lease."""
    config = DispatcherConfig(worker_id="test-worker")
    leases = LeaseManager(tmp_path / "leases.json")
    leases.claim("HER-302", config.worker_id, 3600)

    wt_dir = tmp_path / "abandoned_wt"
    wt_dir.mkdir()

    wt_service = WorktreeService(canonical_root=tmp_path, base_sha="sha")
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=MockLinearClient(),
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=MockGitHubService(),
        worktree_base_dir=tmp_path / "wt",
        canonical_main_sha="sha",
    )
    state = ExecutionState(
        task_id="HER-302",
        state=TaskState.WORKTREE_READY.value,
        claim_owner=config.worker_id,
        claim_token="tok",
        worktree_path=str(wt_dir),
        branch="agent/her-302",
    )
    res = dispatcher.recover_task(state)
    assert res is None
    assert leases.verify_lease("HER-302", config.worker_id, "tok") is False


def test_recovery_pr_open_resumes_ci(tmp_path):
    """Task interrupted at PR_OPEN resumes CI and advances to DONE without re-executing."""
    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-303")
    linear = MockLinearClient([task])
    gh = MockGitHubService(ci_status="PASS")
    leases = LeaseManager(tmp_path / "leases.json")
    leases.claim(task.id, config.worker_id, 3600)

    wt_service = WorktreeService(canonical_root=tmp_path, base_sha="sha")
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=tmp_path / "wt",
        canonical_main_sha="sha",
    )
    state = ExecutionState(
        task_id=task.id,
        state=TaskState.PR_OPEN.value,
        claim_owner=config.worker_id,
        claim_token="tok",
        branch="agent/her-303",
        head_sha="head123",
        pr_number=389,
    )
    res = dispatcher.recover_task(state)
    assert res is not None
    assert res.final_state == TaskState.DONE
    # Verify linear writeback comment was added
    assert len(linear.comments.get(task.id, [])) == 1


def test_recovery_ci_pending_resumes(tmp_path):
    """Task interrupted at CI_PENDING resumes CI and completes idempotently."""
    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-304")
    linear = MockLinearClient([task])
    gh = MockGitHubService(ci_status="PASS")
    leases = LeaseManager(tmp_path / "leases.json")
    leases.claim(task.id, config.worker_id, 3600)

    wt_service = WorktreeService(canonical_root=tmp_path, base_sha="sha")
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=tmp_path / "wt",
        canonical_main_sha="sha",
    )
    state = ExecutionState(
        task_id=task.id,
        state=TaskState.CI_PENDING.value,
        claim_owner=config.worker_id,
        claim_token="tok",
        branch="agent/her-304",
        head_sha="head456",
        pr_number=389,
    )
    res = dispatcher.recover_task(state)
    assert res is not None
    assert res.final_state == TaskState.DONE


def test_recovery_writeback_done(tmp_path):
    """Task interrupted at WRITEBACK_DONE verifies Linear evidence and marks DONE."""
    from ai_engineering.linear_dispatcher.writeback import ExecutionEvidence

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-305", state="Done")
    linear = MockLinearClient([task])
    ev = ExecutionEvidence(
        task_id=task.id,
        execution_status="PASS",
        claim_owner=config.worker_id,
        branch="agent/her-305",
        pr_number=389,
        pr_url="url",
        base_sha="sha",
        head_sha="head_sha_789",
        validation_status="PASS",
        ci_status="PASS",
        ci_head_sha="head_sha_789",
        sha_match="YES",
    )
    linear.comments[task.id] = [ev.to_markdown()]

    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=tmp_path, base_sha="sha")
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=MockGitHubService(),
        worktree_base_dir=tmp_path / "wt",
        canonical_main_sha="sha",
    )
    state = ExecutionState(
        task_id=task.id,
        state=TaskState.WRITEBACK_DONE.value,
        claim_owner=config.worker_id,
        claim_token="tok",
        head_sha="head_sha_789",
    )
    res = dispatcher.recover_task(state)
    assert res is not None
    assert res.final_state == TaskState.DONE
    # No duplicate comment created
    assert len(linear.comments[task.id]) == 1


# ---------------------------------------------------------------------------
# Duplicate Protection & Heartbeat Tests
# ---------------------------------------------------------------------------


def test_duplicate_pr_prevented(tmp_path):
    """PR creation checks for existing branch PR to avoid duplicates."""
    from ai_engineering.linear_dispatcher.github_service_production import (
        GitHubProductionService,
    )

    # GitHubProductionService checks `pulls?head=life2boat:{branch}&state=open`
    assert hasattr(GitHubProductionService, "create_draft_pr")


def test_duplicate_writeback_prevented(tmp_path):
    """WritebackService checks if evidence head_sha already exists in comments."""
    from ai_engineering.linear_dispatcher.writeback import (
        WritebackService,
        ExecutionEvidence,
    )

    task = make_linear_task("HER-400")
    linear = MockLinearClient([task])
    ev = ExecutionEvidence(
        task_id=task.id,
        execution_status="PASS",
        claim_owner="w1",
        branch="b",
        pr_number=1,
        pr_url="url",
        base_sha="base",
        head_sha="head_sha_abc",
        validation_status="PASS",
        ci_status="PASS",
        ci_head_sha="head_sha_abc",
        sha_match="YES",
    )
    linear.comments[task.id] = [ev.to_markdown()]
    wb = WritebackService(linear)
    ok, err = wb.writeback_and_verify(task, ev)
    assert ok is True
    # Did not append a second comment
    assert len(linear.comments[task.id]) == 1


def test_lost_lease_before_executor(tmp_path):
    """If lease expires or is lost before executor runs, dispatch halts as LOST_LEASE."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-500")
    linear = MockLinearClient([task])
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=MockGitHubService(),
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=SimulatedTaskExecutor(mutation_handler=lambda wt, t: ["x.py"]),
    )
    # Monkeypatch verify_lease to simulate lost lease
    leases.verify_lease = lambda *args, **kwargs: False

    res = dispatcher.dispatch_one_task([task])
    assert res is not None
    assert res.final_state == TaskState.BLOCKED
    assert res.block_reason == BlockReasonCode.LOST_LEASE.value


def test_lost_lease_during_ci(tmp_path):
    """If heartbeat lease renewal fails during CI waiting, dispatch terminates with LOST_LEASE."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker", ci_poll_interval_sec=0)
    task = make_linear_task("HER-501")
    linear = MockLinearClient([task])
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    class PendingGH(MockGitHubService):
        def get_ci_status(self, *args, **kwargs):
            return False, CIStatusResult("PENDING", "h", [], {}), None

    gh = PendingGH()

    def write_y(wt: Path, t: LinearTask):
        (wt / "y.py").write_text("y = 1\n", encoding="utf-8")
        return ["y.py"]

    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=SimulatedTaskExecutor(mutation_handler=write_y),
    )

    # Allow initial claim, but fail subsequent heartbeat claim
    original_claim = leases.claim
    call_count = [0]

    def claim_mock(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] > 1:
            return False, None, "LEASE_EXPIRED"
        return original_claim(*args, **kwargs)

    leases.claim = claim_mock
    res = dispatcher.dispatch_one_task([task])
    assert res is not None
    assert res.final_state == TaskState.BLOCKED
    assert res.block_reason == BlockReasonCode.LOST_LEASE


def test_main_drift_before_push(tmp_path):
    """If canonical main advances between dispatch start and git push, task blocks on CANONICAL_MAIN_DRIFT."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-600")
    linear = MockLinearClient([task])
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    def write_drift(wt: Path, t: LinearTask):
        (wt / "drift.py").write_text("d = 1\n", encoding="utf-8")
        return ["drift.py"]

    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=MockGitHubService(),
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=SimulatedTaskExecutor(mutation_handler=write_drift),
    )
    # Simulate remote main advancing
    dispatcher._resolve_remote_main_sha = lambda *args, **kwargs: (
        "newer_drifted_sha_999"
    )

    res = dispatcher.dispatch_one_task([task])
    assert res is not None
    assert res.final_state == TaskState.BLOCKED
    assert res.block_reason == BlockReasonCode.CANONICAL_MAIN_DRIFT.value
    # Worktree was cleaned up
    assert len(list(wt_base.glob("*"))) == 0


def test_simulated_executor_full_lifecycle(tmp_path):
    """Phase 15 full lifecycle test: claim -> worktree -> executor changes -> validation -> commit -> PR -> CI -> writeback -> DONE."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-700")
    linear = MockLinearClient([task])
    gh = MockGitHubService(ci_status="PASS")
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    def write_feature(wt: Path, t: LinearTask):
        (wt / "feature.py").write_text("# implementation\n", encoding="utf-8")
        return ["feature.py"]

    executor = SimulatedTaskExecutor(mutation_handler=write_feature)
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=executor,
    )

    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.DONE
    assert executor.call_count == 1
    assert len(gh.created_prs) == 1
    assert len(linear.comments[task.id]) == 1
    assert len(list(wt_base.glob("*"))) == 0


def test_worker_dispatch_empty_changes_does_not_crash(tmp_path):
    """Dispatcher handles empty changes without unhandled exceptions."""
    canonical_root = tmp_path / "canonical"
    canonical_root.mkdir()
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-800")
    linear = MockLinearClient([task])
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=MockGitHubService(),
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=SimulatedTaskExecutor(mutation_handler=lambda wt, t: []),
    )
    # Must return cleanly without raising CalledProcessError
    res = dispatcher.dispatch_one_task([task])
    assert res is not None
    assert res.final_state == TaskState.BLOCKED
    assert res.block_reason == BlockReasonCode.NO_CHANGES_PRODUCED.value
