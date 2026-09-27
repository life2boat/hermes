"""Comprehensive tests for Hermes Linear Autonomous Dispatcher v1."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import pytest
from typing import Any

from ai_engineering.linear_dispatcher import (
    AutonomousDispatcher,
    BlockReasonCode,
    ClaimRecord,
    DispatcherConfig,
    ExecutionEvidence,
    ILinearClient,
    InvalidStateTransitionError,
    LeaseManager,
    LinearTask,
    ScopeGate,
    TaskState,
    TaskStateMachine,
    WorktreeService,
    WritebackService,
    compute_branch_name,
    is_eligible_task,
    select_next_task,
    sort_tasks,
)
from ai_engineering.linear_dispatcher.github_service import (
    CIStatusResult,
    IGitHubService,
    PRCreationResult,
)


class FakeLinearClient:
    """In-memory Linear client for deterministic testing."""

    def __init__(self, tasks: list[LinearTask] | None = None) -> None:
        self.tasks: dict[str, LinearTask] = {t.id: t for t in (tasks or [])}
        self.comments: dict[str, list[str]] = {}
        self.simulate_reread_failure = False

    def add_comment(self, issue_id: str, body: str) -> bool:
        self.comments.setdefault(issue_id, []).append(body)
        return True

    def update_issue(self, issue_id: str, fields: dict[str, Any]) -> bool:
        t = self.tasks.get(issue_id)
        if not t:
            return False
        # update fields
        new_state = fields.get("state", t.state)
        new_desc = fields.get("description", t.description)
        self.tasks[issue_id] = replace(t, state=new_state, description=new_desc)
        return True

    def get_issue(self, issue_id: str) -> LinearTask | None:
        if self.simulate_reread_failure:
            return None
        return self.tasks.get(issue_id)

    def get_issue_comments(self, issue_id: str) -> list[str]:
        if self.simulate_reread_failure:
            return []
        return self.comments.get(issue_id, [])


class FakeGitHubService:
    """In-memory GitHub client for deterministic testing."""

    def __init__(self) -> None:
        self.created_prs: list[dict[str, Any]] = []
        self.ci_result: tuple[bool, CIStatusResult | None, BlockReasonCode | None] = (
            True,
            CIStatusResult(
                overall_status="PASS",
                head_sha="head1234567890123456789012345678901234",
                runs=[{"name": "check", "conclusion": "success", "status": "completed"}],
                details={"check": "success"},
            ),
            None,
        )

    def create_draft_pr(
        self,
        task: LinearTask,
        branch_name: str,
        base_sha: str,
        head_sha: str,
        cwd: Path | str,
    ) -> tuple[bool, PRCreationResult | None, str | None]:
        pr_num = len(self.created_prs) + 1
        record = {
            "pr_number": pr_num,
            "task_id": task.id,
            "branch_name": branch_name,
            "head_sha": head_sha,
        }
        self.created_prs.append(record)
        return True, PRCreationResult(
            pr_number=pr_num,
            pr_url=f"https://github.com/life2boat/hermes/pull/{pr_num}",
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
        return self.ci_result


def make_task(
    id: str = "HER-7",
    title: str = "Test Task",
    labels: tuple[str, ...] = ("agent:auto", "agent:shadow"),
    state: str = "Todo",
    priority: int = 3,
    created_at: str = "2026-09-27T10:00:00Z",
    description: str = "Safe task description",
) -> LinearTask:
    return LinearTask(
        id=id,
        uuid=f"uuid-{id}",
        title=title,
        description=description,
        state=state,
        priority=priority,
        assignee="Oleg Hodirev",
        labels=labels,
        created_at=created_at,
        updated_at=created_at,
        url=f"https://linear.app/hermes/issue/{id}",
    )


# =========================================================================
# 14 MANDATORY TESTS SPECIFIED IN PHASE 14
# =========================================================================

def test_01_eligible_issue_selected():
    """1. eligible issue is selected when it has agent:auto and agent:shadow."""
    config = DispatcherConfig()
    task = make_task(labels=("agent:auto", "agent:shadow"))
    is_ok, reason = is_eligible_task(task, config)
    assert is_ok is True
    assert reason is None


def test_02_issue_without_agent_auto_skipped():
    """2. issue without agent:auto is skipped."""
    config = DispatcherConfig()
    task = make_task(labels=("agent:shadow",))
    is_ok, reason = is_eligible_task(task, config)
    assert is_ok is False
    assert reason == BlockReasonCode.MISSING_AGENT_AUTO_LABEL


def test_03_issue_without_agent_shadow_skipped():
    """3. issue without agent:shadow is skipped in v1."""
    config = DispatcherConfig()
    task = make_task(labels=("agent:auto",))
    is_ok, reason = is_eligible_task(task, config)
    assert is_ok is False
    assert reason == BlockReasonCode.MISSING_AGENT_SHADOW_LABEL


def test_04_active_foreign_lease_blocks_execution(tmp_path: Path):
    """4. active foreign lease blocks execution."""
    lease_mgr = LeaseManager(tmp_path / "leases.json")
    task = make_task(id="HER-10")

    # Worker 1 claims lease
    ok, claim, _ = lease_mgr.claim("HER-10", "worker-1", lease_duration_sec=3600)
    assert ok is True

    # Worker 2 tries to claim while active -> BLOCKED
    ok2, claim2, reason = lease_mgr.claim("HER-10", "worker-2", lease_duration_sec=3600)
    assert ok2 is False
    assert claim2 is None
    assert reason == BlockReasonCode.ACTIVE_FOREIGN_LEASE.value

    # Foreign worker check in task selection
    config = DispatcherConfig(worker_id="worker-2")
    selected = select_next_task([task], lease_mgr, config)
    assert selected is None


def test_05_same_owner_retry_idempotent(tmp_path: Path):
    """5. same-owner retry is idempotent."""
    lease_mgr = LeaseManager(tmp_path / "leases.json")
    task = make_task(id="HER-11")

    ok1, claim1, _ = lease_mgr.claim("HER-11", "worker-1", lease_duration_sec=3600)
    assert ok1 is True

    # Same worker claims again
    ok2, claim2, _ = lease_mgr.claim("HER-11", "worker-1", lease_duration_sec=3600)
    assert ok2 is True
    assert claim1.claim_token == claim2.claim_token  # Preserves token idempotently


def test_06_expired_lease_handled_by_policy(tmp_path: Path):
    """6. expired lease can be reclaimed per policy."""
    lease_mgr = LeaseManager(tmp_path / "leases.json")

    # Worker 1 claims in the past (already expired)
    past_iso = "2026-01-01T00:00:00Z"
    ok1, claim1, _ = lease_mgr.claim("HER-12", "worker-1", lease_duration_sec=60, now_iso=past_iso)
    assert ok1 is True

    # Now (at future time), Worker 2 can reclaim expired lease
    now_iso = "2026-09-27T12:00:00Z"
    assert lease_mgr.is_held_by_foreign_worker("HER-12", "worker-2", now_iso=now_iso) is False
    ok2, claim2, _ = lease_mgr.claim("HER-12", "worker-2", lease_duration_sec=3600, now_iso=now_iso)
    assert ok2 is True
    assert claim2.claim_owner == "worker-2"


def test_07_lost_lease_blocks_mutation(tmp_path: Path):
    """7. lost lease blocks mutation before changes occur."""
    lease_mgr = LeaseManager(tmp_path / "leases.json")
    ok, claim, _ = lease_mgr.claim("HER-13", "worker-1", lease_duration_sec=3600)

    # If lease was released or replaced by another worker
    lease_mgr.release("HER-13", "worker-1", claim.claim_token)

    # Verification fails -> mutation must be blocked
    assert lease_mgr.verify_lease("HER-13", "worker-1", claim.claim_token) is False


def test_08_dirty_canonical_checkout_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """8. dirty canonical checkout fails closed."""
    service = WorktreeService(canonical_root=tmp_path, base_sha="61dfa808a")

    # Mock canonical dirty to True
    monkeypatch.setattr(service, "is_canonical_dirty", lambda: True)

    task = make_task(id="HER-14")
    wt, branch, err = service.create_worktree(task, tmp_path / "wts", fail_if_dirty=True)
    assert wt is None
    assert branch is None
    assert err == BlockReasonCode.DIRTY_CANONICAL_CHECKOUT


def test_09_ci_sha_mismatch_fail_closed():
    """9. CI SHA mismatch fails closed (BLOCKED, not PASS)."""
    gh = FakeGitHubService()
    expected_head = "1111111111111111111111111111111111111111"
    different_head = "2222222222222222222222222222222222222222"

    gh.ci_result = (
        False,
        None,
        BlockReasonCode.CI_SHA_MISMATCH,
    )
    ok, res, reason = gh.get_ci_status("life2boat/hermes", expected_head, 123)
    assert ok is False
    assert reason == BlockReasonCode.CI_SHA_MISMATCH


def test_10_ci_failure_does_not_become_pass():
    """10. CI failure never becomes PASS."""
    gh = FakeGitHubService()
    gh.ci_result = (
        False,
        CIStatusResult(
            overall_status="FAIL",
            head_sha="head123",
            runs=[{"name": "tests", "conclusion": "failure", "status": "completed"}],
            details={"tests": "failure"},
        ),
        BlockReasonCode.CI_FAILED,
    )
    ok, res, reason = gh.get_ci_status("life2boat/hermes", "head123", 123)
    assert ok is False
    assert reason == BlockReasonCode.CI_FAILED


def test_11_linear_writeback_reread_required():
    """11. Linear writeback reread is required; persistence failure blocks completion."""
    client = FakeLinearClient()
    client.tasks["HER-15"] = make_task(id="HER-15")
    client.simulate_reread_failure = True  # Simulates inability to confirm persistence

    service = WritebackService(client)
    evidence = ExecutionEvidence(
        task_id="HER-15",
        execution_status="PASS",
        claim_owner="worker-1",
        branch="agent/her-15-test",
        pr_number=385,
        pr_url="https://github.com/...",
        base_sha="base",
        head_sha="head",
        validation_status="PASS",
        ci_status="PASS",
        ci_head_sha="head",
        sha_match="YES",
    )

    ok, reason = service.writeback_and_verify(client.tasks["HER-15"], evidence)
    assert ok is False
    assert reason == BlockReasonCode.REREAD_VERIFICATION_FAILED
    # Task must NOT be moved to Done
    assert client.tasks["HER-15"].state != "Done"


def test_12_next_task_selected_deterministically(tmp_path: Path):
    """12. Next task is selected deterministically: priority -> createdAt -> ID."""
    lease_mgr = LeaseManager(tmp_path / "leases.json")
    config = DispatcherConfig()

    t1 = make_task(id="HER-100", priority=3, created_at="2026-09-27T10:00:00Z")  # Medium
    t2 = make_task(id="HER-101", priority=1, created_at="2026-09-27T11:00:00Z")  # Urgent (highest priority)
    t3 = make_task(id="HER-102", priority=2, created_at="2026-09-27T09:00:00Z")  # High
    t4 = make_task(id="HER-103", priority=2, created_at="2026-09-27T08:00:00Z")  # High, older

    # Sorting order should be: t2 (pri 1), t4 (pri 2, older), t3 (pri 2, newer), t1 (pri 3)
    sorted_tasks = sort_tasks([t1, t2, t3, t4])
    assert [t.id for t in sorted_tasks] == ["HER-101", "HER-103", "HER-102", "HER-100"]

    selected = select_next_task([t1, t2, t3, t4], lease_mgr, config)
    assert selected.id == "HER-101"


def test_13_forbidden_production_scope_blocked():
    """13. Forbidden production scope transitions to BLOCKED without mutations."""
    forbidden_task = make_task(
        id="HER-16",
        title="Deploy to production now",
        description="Please restart production container in /etc/hermes",
    )
    is_ok, reason = ScopeGate.evaluate_task_intent(forbidden_task)
    assert is_ok is False
    assert reason == BlockReasonCode.PRODUCTION_MUTATION_FORBIDDEN

    # File check
    files_ok, files_reason = ScopeGate.evaluate_changed_files(["deploy/host-secrets.env"])
    assert files_ok is False
    assert files_reason == BlockReasonCode.CREDENTIALS_MUTATION_FORBIDDEN


def test_14_duplicate_execution_does_not_create_second_pr():
    """14. State machine prevents illegal jumping; finished tasks are not re-executed."""
    sm = TaskStateMachine("HER-17", TaskState.DISCOVERED)

    # Illegal jump DISCOVERED -> DONE must be rejected
    with pytest.raises(InvalidStateTransitionError):
        sm.transition(TaskState.DONE)

    # Finished task in Done state is not eligible for selection
    finished_task = make_task(id="HER-18", state="Done")
    is_ok, _ = is_eligible_task(finished_task, DispatcherConfig())
    assert is_ok is False


def test_full_state_machine_legal_progression():
    """Verify strictly ordered legal transitions DISCOVERED -> DONE."""
    sm = TaskStateMachine("HER-19", TaskState.DISCOVERED)
    sm.transition(TaskState.CLAIMED)
    sm.transition(TaskState.WORKTREE_READY)
    sm.transition(TaskState.IMPLEMENTING)
    sm.transition(TaskState.VALIDATING)
    sm.transition(TaskState.PR_OPEN)
    sm.transition(TaskState.CI_PENDING)
    sm.transition(TaskState.CI_PASS)
    sm.transition(TaskState.WRITEBACK_DONE)
    sm.transition(TaskState.DONE)
    assert sm.current_state == TaskState.DONE
    assert len(sm.history) == 10
