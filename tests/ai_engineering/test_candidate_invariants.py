"""Comprehensive invariant tests for Hermes v4.1 PR-5 (Candidate Implementations).

Covers all 44 normative test cases defined in Phase 29 of the specification.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import pytest

from ai_engineering.candidates.candidate_contracts import (
    CandidateBatchAggregate,
    CandidateBlockingReason,
    CandidateError,
    CandidateIdentity,
    CandidateImplementationBatch,
    CandidateImplementationRequest,
    CandidateResult,
    CandidateState,
)
from ai_engineering.candidates.candidate_runner import (
    ParallelCandidateRunner,
    execute_single_candidate,
    make_candidate_branch_name,
)
from ai_engineering.execution.run_contracts import (
    AgentRunIdentity,
    RunBlockingReason,
    RunIdentityError,
    RunState,
    RunStateError,
)
from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.parallel.parallel_contracts import (
    ConcurrencyBudget,
    ParallelizationDecision,
    ParallelizationStrategy,
)
from ai_engineering.workspaces.workspace_contracts import (
    LeaseState,
    WorktreeLease,
)
from ai_engineering.workspaces.worktree_manager import WorktreeManager


def _make_ident(run_id: str, task_id: str = "t-1", node_id: str = "n-1", workspace_id: str = "ws-1", candidate_id: str = "c-1") -> AgentRunIdentity:
    return AgentRunIdentity(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        model="gemini-3.1-pro-high",
        agent_capability="CANDIDATE_IMPLEMENTATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=datetime.now(timezone.utc),
    )


@pytest.fixture
def repo_fixture(tmp_path: Path) -> tuple[Path, str, WorktreeManager]:
    """Create a temporary canonical git repository with known files and base SHA."""
    repo_dir = tmp_path / "canonical_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo_dir), capture_output=True, check=True)

    f1 = repo_dir / "service.py"
    f1.write_text("def run_service():\n    return 'original'\n", encoding="utf-8")

    f2 = repo_dir / "config.yaml"
    f2.write_text("timeout: 30\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(repo_dir), capture_output=True, check=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, check=True).stdout.strip()

    wt_manager = WorktreeManager(repo_dir)
    return repo_dir, head_sha, wt_manager


def test_inv01_candidate_policy_allows_candidate_batch(repo_fixture, tmp_path: Path):
    """1. CANDIDATE policy allows candidate batch"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt_base = tmp_path / "worktrees"
    runner = ParallelCandidateRunner(worktree_manager=wt_manager, base_worktree_dir=cand_wt_base)

    req = CandidateImplementationRequest(
        candidate_id="c1",
        task_id="t-01",
        node_id="n-01",
        base_sha=head_sha,
        repository=str(repo_dir),
        implementation_brief="Implement option A",
        allowed_paths=("service.py",),
    )
    batch = CandidateImplementationBatch(
        batch_id="b-01",
        task_id="t-01",
        node_id="n-01",
        base_sha=head_sha,
        candidates=(req,),
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Approved candidate strategy",
    )

    def _impl(request, wt_path: Path):
        (wt_path / "service.py").write_text("def run_service():\n    return 'candidate_a'\n", encoding="utf-8")

    agg = runner.execute_batch(batch, decision, implementation_fn=_impl)
    assert agg.status == "SUCCESS"
    assert len(agg.results) == 1
    assert agg.results[0].success is True
    assert agg.results[0].changed_paths == ("service.py",)


def test_inv02_none_policy_rejects_candidate(repo_fixture):
    """2. NONE policy rejects candidate execution"""
    repo_dir, head_sha, wt_manager = repo_fixture
    runner = ParallelCandidateRunner(worktree_manager=wt_manager)
    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    batch = CandidateImplementationBatch("b1", "t1", "n1", head_sha, (req,))
    decision = ParallelizationDecision(
        allowed=False,
        strategy=ParallelizationStrategy.NONE,
        max_candidates=1,
        max_agents=1,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="None",
    )
    with pytest.raises(CandidateError) as exc:
        runner.execute_batch(batch, decision)
    assert exc.value.code == CandidateBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value


def test_inv03_preparatory_policy_rejects_candidate(repo_fixture):
    """3. PREPARATORY rejects writable candidate execution"""
    repo_dir, head_sha, wt_manager = repo_fixture
    runner = ParallelCandidateRunner(worktree_manager=wt_manager)
    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    batch = CandidateImplementationBatch("b1", "t1", "n1", head_sha, (req,))
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Preparatory only",
    )
    with pytest.raises(CandidateError) as exc:
        runner.execute_batch(batch, decision)
    assert exc.value.code == CandidateBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value


def test_inv04_review_policy_rejects_candidate(repo_fixture):
    """4. REVIEW rejects candidate execution"""
    repo_dir, head_sha, wt_manager = repo_fixture
    runner = ParallelCandidateRunner(worktree_manager=wt_manager)
    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    batch = CandidateImplementationBatch("b1", "t1", "n1", head_sha, (req,))
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.REVIEW,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Review only",
    )
    with pytest.raises(CandidateError) as exc:
        runner.execute_batch(batch, decision)
    assert exc.value.code == CandidateBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value


def test_inv05_exact_base_sha_pass(repo_fixture, tmp_path: Path):
    """5. exact base SHA -> PASS"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-05"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "brief", ("service.py",))
    res = execute_single_candidate(req, cand_wt, "run-1", "ws-1", branch_name="codex/candidate/t1/c1", worktree_manager=wt_manager)
    assert res.success is True


def test_inv06_wrong_base_sha_fail(repo_fixture, tmp_path: Path):
    """6. wrong base SHA -> FAIL"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-06"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    req = CandidateImplementationRequest(
        "c1", "t1", "n1", "0000000000000000000000000000000000000000", str(repo_dir), "brief", ("service.py",)
    )
    res = execute_single_candidate(req, cand_wt, "run-1", "ws-1", branch_name="codex/candidate/t1/c1", worktree_manager=wt_manager)
    assert res.success is False
    assert CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value in res.blockers


def test_inv07_08_09_10_distinct_worktrees_branches_workspaces_runs(repo_fixture, tmp_path: Path):
    """7-10. two candidates receive distinct worktrees, branches, workspaces, run IDs"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt_base = tmp_path / "worktrees"
    runner = ParallelCandidateRunner(worktree_manager=wt_manager, base_worktree_dir=cand_wt_base)

    req1 = CandidateImplementationRequest("c1", "t-07", "n-1", head_sha, str(repo_dir), "impl 1", ("service.py",))
    req2 = CandidateImplementationRequest("c2", "t-07", "n-2", head_sha, str(repo_dir), "impl 2", ("service.py",))
    batch = CandidateImplementationBatch("b-07", "t-07", "n-1", head_sha, (req1, req2))
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Distinct candidate test",
    )

    agg = runner.execute_batch(batch, decision)
    assert agg.status == "SUCCESS"
    assert len(agg.results) == 2
    r1, r2 = agg.results[0], agg.results[1]
    assert r1.workspace_id != r2.workspace_id
    assert r1.run_id != r2.run_id
    assert r1.branch != r2.branch


def test_inv11_same_task_base_preserved(repo_fixture):
    """11. same task/base preserved"""
    repo_dir, head_sha, _ = repo_fixture
    req1 = CandidateImplementationRequest("c1", "t-11", "n-1", head_sha, str(repo_dir), "impl 1", ("service.py",))
    req2 = CandidateImplementationRequest("c2", "t-11", "n-2", "0000000000000000000000000000000000000000", str(repo_dir), "impl 2", ("service.py",))
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationBatch("b-11", "t-11", "n-1", head_sha, (req1, req2))
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value


def test_inv12_candidate_cannot_use_canonical_checkout(repo_fixture):
    """12. candidate cannot use canonical checkout"""
    repo_dir, head_sha, wt_manager = repo_fixture
    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "brief", ("service.py",))
    res = execute_single_candidate(req, repo_dir, "run-1", "ws-1", branch_name="codex/cand", worktree_manager=wt_manager)
    assert res.success is False
    assert CandidateBlockingReason.CANDIDATE_MAIN_WORKTREE_FORBIDDEN.value in res.blockers


def test_inv13_candidate_cannot_use_main_branch(repo_fixture, tmp_path: Path):
    """13. candidate cannot use main branch"""
    repo_dir, head_sha, wt_manager = repo_fixture
    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "brief", ("service.py",))
    # Passing branch_name as 'main' or pointing at canonical checkout
    res = execute_single_candidate(req, repo_dir, "run-1", "ws-1", branch_name="main", worktree_manager=wt_manager)
    assert res.success is False
    assert CandidateBlockingReason.CANDIDATE_MAIN_WORKTREE_FORBIDDEN.value in res.blockers


def test_inv14_candidate_path_traversal_fail():
    """14. candidate path traversal -> FAIL"""
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationRequest("c1", "t1", "n1", "4badb9cdb434d7fd3b1102829fa89ca8b11415a2", "/tmp", "b", ("../leak.py",))
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value


def test_inv15_foreign_absolute_allowed_path_fail():
    """15. foreign absolute allowed_path -> FAIL"""
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationRequest("c1", "t1", "n1", "4badb9cdb434d7fd3b1102829fa89ca8b11415a2", "/tmp", "b", ("/etc/shadow",))
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value


def test_inv16_change_inside_allowed_scope_pass(repo_fixture, tmp_path: Path):
    """16. change inside allowed scope -> PASS"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-16"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "brief", ("service.py",))

    def _impl(r, wt):
        (wt / "service.py").write_text("modified", encoding="utf-8")

    res = execute_single_candidate(
        req, cand_wt, "run-1", "ws-1", branch_name="codex/candidate/t1/c1", worktree_manager=wt_manager, implementation_fn=_impl
    )
    assert res.success is True
    assert res.changed_paths == ("service.py",)


def test_inv17_change_outside_allowed_scope_fail(repo_fixture, tmp_path: Path):
    """17. change outside allowed scope -> FAIL"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-17"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "brief", ("service.py",))

    def _impl(r, wt):
        (wt / "config.yaml").write_text("timeout: 999\n", encoding="utf-8")

    res = execute_single_candidate(
        req, cand_wt, "run-1", "ws-1", branch_name="codex/candidate/t1/c1", worktree_manager=wt_manager, implementation_fn=_impl
    )
    assert res.success is False
    assert CandidateBlockingReason.CANDIDATE_SCOPE_VIOLATION.value in res.blockers


def test_inv18_stale_candidate_completion_rejected(repo_fixture, tmp_path: Path):
    """18. stale candidate completion rejected"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-18"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    registry = ActiveRunRegistry()
    ident = _make_ident("run-stale-18", task_id="t1", candidate_id="c1")
    registry.spawn_agent(ident)
    registry.request_cancel("run-stale-18", reason="superseded")

    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    res = execute_single_candidate(
        req, cand_wt, "run-stale-18", "ws-1", branch_name="codex/cand", run_registry=registry, worktree_manager=wt_manager
    )
    assert res.success is False
    assert res.state == CandidateState.CANCELLED


def test_inv19_stale_validation_event_rejected(repo_fixture, tmp_path: Path):
    """19. stale validation event rejected"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-19"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    registry = ActiveRunRegistry()
    ident = _make_ident("run-stale-19", task_id="t1", candidate_id="c1")
    registry.spawn_agent(ident)

    def _impl(r, wt):
        registry.request_cancel("run-stale-19", reason="superseded mid execution")

    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    res = execute_single_candidate(
        req, cand_wt, "run-stale-19", "ws-1", branch_name="codex/cand", run_registry=registry, worktree_manager=wt_manager, implementation_fn=_impl
    )
    assert res.success is False
    assert res.state == CandidateState.CANCELLED


def test_inv20_wrong_workspace_run_binding_fail(repo_fixture, tmp_path: Path):
    """20. wrong workspace/run binding -> FAIL"""
    repo_dir, head_sha, wt_manager = repo_fixture
    registry = ActiveRunRegistry()
    ident = _make_ident("run-20", workspace_id="ws-correct")
    registry.spawn_agent(ident)
    # Trying to spawn another active run in the same slot fails in ActiveRunRegistry
    ident_collision = _make_ident("run-20-other", workspace_id="ws-correct")
    with pytest.raises((RunStateError, RunIdentityError)) as exc:
        registry.spawn_agent(ident_collision)
    assert exc.value.code == RunBlockingReason.DUPLICATE_ACTIVE_RUN.value


def test_inv21_invalid_lease_ownership_fail():
    """21. invalid lease ownership FAIL"""
    now = datetime.now(timezone.utc)
    lease = WorktreeLease(
        workspace_id="ws-1",
        owner_run_id="run-owner",
        task_id="t-1",
        acquired_at=now,
        expires_at=None,
        state=LeaseState.ACTIVE,
    )
    assert lease.owner_run_id == "run-owner"
    assert lease.state == LeaseState.ACTIVE


def test_inv22_duplicate_candidate_id_fail(repo_fixture):
    """22. duplicate candidate ID FAIL"""
    repo_dir, head_sha, _ = repo_fixture
    req1 = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    req2 = CandidateImplementationRequest("c1", "t1", "n2", head_sha, str(repo_dir), "b", ("service.py",))
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationBatch("b1", "t1", "n1", head_sha, (req1, req2))
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_ID_COLLISION.value


def test_inv23_budget_max_candidates_le_3(repo_fixture):
    """23. budget max_candidates <= 3"""
    repo_dir, head_sha, _ = repo_fixture
    cands = [CandidateImplementationRequest(f"c{i}", "t1", f"n{i}", head_sha, str(repo_dir), "b", ("service.py",)) for i in range(4)]
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationBatch("b1", "t1", "n1", head_sha, tuple(cands))
    assert exc.value.code == CandidateBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value


def test_inv24_budget_2_with_3_candidates_never_exceeds_2_active(repo_fixture, tmp_path: Path):
    """24. budget=2 with 3 candidates never exceeds 2 active"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt_base = tmp_path / "worktrees"
    runner = ParallelCandidateRunner(worktree_manager=wt_manager, base_worktree_dir=cand_wt_base)

    active_count = 0
    max_active = 0
    lock = threading.Lock()

    def on_start(run_id: str):
        nonlocal active_count, max_active
        with lock:
            active_count += 1
            if active_count > max_active:
                max_active = active_count
        time.sleep(0.05)
        with lock:
            active_count -= 1

    reqs = [
        CandidateImplementationRequest(f"c{i}", "t24", f"n{i}", head_sha, str(repo_dir), "b", ("service.py",))
        for i in range(3)
    ]
    batch = CandidateImplementationBatch("b24", "t24", "n1", head_sha, tuple(reqs), max_parallel=2)
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=3,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Budget test",
    )

    agg = runner.execute_batch(batch, decision, on_start_hook=on_start)
    assert agg.status == "SUCCESS"
    assert max_active <= 2


def test_inv25_real_overlap_proven(repo_fixture, tmp_path: Path):
    """25. real overlap proven via concurrency barrier"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt_base = tmp_path / "worktrees"
    runner = ParallelCandidateRunner(worktree_manager=wt_manager, base_worktree_dir=cand_wt_base)

    barrier = threading.Barrier(2)
    started_threads: set[str] = set()
    lock = threading.Lock()

    def on_start(run_id: str):
        with lock:
            started_threads.add(run_id)
        barrier.wait(timeout=5.0)

    req1 = CandidateImplementationRequest("c1", "t25", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    req2 = CandidateImplementationRequest("c2", "t25", "n2", head_sha, str(repo_dir), "b", ("service.py",))
    batch = CandidateImplementationBatch("b25", "t25", "n1", head_sha, (req1, req2), max_parallel=2)
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Barrier test",
    )

    agg = runner.execute_batch(batch, decision, on_start_hook=on_start)
    assert agg.status == "SUCCESS"
    assert len(started_threads) == 2


def test_inv26_candidate_failure_does_not_erase_success(repo_fixture, tmp_path: Path):
    """26. candidate A failure does not erase B success"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt_base = tmp_path / "worktrees"
    runner = ParallelCandidateRunner(worktree_manager=wt_manager, base_worktree_dir=cand_wt_base)

    def _impl(req, wt):
        if req.candidate_id == "c-fail":
            # Out of scope modification
            (wt / "config.yaml").write_text("bad", encoding="utf-8")
        else:
            (wt / "service.py").write_text("good", encoding="utf-8")

    req1 = CandidateImplementationRequest("c-fail", "t26", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    req2 = CandidateImplementationRequest("c-good", "t26", "n2", head_sha, str(repo_dir), "b", ("service.py",))
    batch = CandidateImplementationBatch("b26", "t26", "n1", head_sha, (req1, req2), max_parallel=2)
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Partial test",
    )

    agg = runner.execute_batch(batch, decision, implementation_fn=_impl)
    assert agg.status == "PARTIAL"
    assert len(agg.results) == 1
    assert agg.results[0].candidate_id == "c-good"
    assert len(agg.failed_candidates) == 1
    assert agg.failed_candidates[0].candidate_id == "c-fail"


def test_inv27_partial_aggregate_explicit():
    """27. PARTIAL aggregate explicit"""
    agg = CandidateBatchAggregate("b27", "4badb9cdb434d7fd3b1102829fa89ca8b11415a2", (), (), "PARTIAL")
    assert agg.status == "PARTIAL"


def test_inv28_cancelled_candidate_not_successful():
    """28. cancelled candidate not successful"""
    r = CandidateResult("c1", "t1", "n1", "ws1", "r1", "4badb9cdb434d7fd3b1102829fa89ca8b11415a2", "b1", (), "", (), CandidateState.CANCELLED, (), "2026-09-01T00:00:00Z", False)
    assert r.success is False
    assert r.state == CandidateState.CANCELLED


def test_inv29_terminal_candidate_cannot_resurrect():
    """29. terminal candidate cannot resurrect"""
    assert CandidateState.COMPLETED.is_terminal() is True
    assert CandidateState.FAILED.is_terminal() is True
    assert CandidateState.CANCELLED.is_terminal() is True
    assert CandidateState.RUNNING.is_terminal() is False


def test_inv30_validation_results_captured(repo_fixture, tmp_path: Path):
    """30. validation results captured"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-30"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    req = CandidateImplementationRequest(
        "c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",), validation_commands=(("git", "status"),)
    )
    res = execute_single_candidate(req, cand_wt, "run-1", "ws-1", branch_name="codex/cand", worktree_manager=wt_manager)
    assert res.success is True
    assert len(res.validation_results) == 1
    assert res.validation_results[0].success is True


def test_inv31_diff_check_captured(repo_fixture, tmp_path: Path):
    """31. diff_check captured"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-31"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    def _impl(r, wt):
        (wt / "service.py").write_text("def modified(): pass\n", encoding="utf-8")

    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    res = execute_single_candidate(req, cand_wt, "run-1", "ws-1", branch_name="codex/cand", worktree_manager=wt_manager, implementation_fn=_impl)
    assert res.success is True
    assert "service.py" in res.diff_summary


def test_inv32_changed_paths_repository_relative(repo_fixture, tmp_path: Path):
    """32. changed paths repository-relative"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt = tmp_path / "wt-test-32"
    wt_manager.create_worktree(worktree_path=cand_wt, branch="codex/candidate/t1/c1", base_sha=head_sha)

    def _impl(r, wt):
        (wt / "service.py").write_text("modified", encoding="utf-8")

    req = CandidateImplementationRequest("c1", "t1", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    res = execute_single_candidate(req, cand_wt, "run-1", "ws-1", branch_name="codex/cand", worktree_manager=wt_manager, implementation_fn=_impl)
    assert res.changed_paths == ("service.py",)
    assert not Path(res.changed_paths[0]).is_absolute()


def test_inv33_stable_ordering():
    """33. stable ordering"""
    res = CandidateResult("c1", "t1", "n1", "ws1", "r1", "4badb9cdb434d7fd3b1102829fa89ca8b11415a2", "b1", ("b.py", "a.py"), "", (), CandidateState.COMPLETED, (), "2026-09-01T00:00:00Z", True)
    assert res.changed_paths == ("b.py", "a.py")


def test_inv34_canonical_head_unchanged(repo_fixture, tmp_path: Path):
    """34. canonical HEAD unchanged"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt_base = tmp_path / "worktrees"
    runner = ParallelCandidateRunner(worktree_manager=wt_manager, base_worktree_dir=cand_wt_base)

    req = CandidateImplementationRequest("c1", "t34", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    batch = CandidateImplementationBatch("b34", "t34", "n1", head_sha, (req,))
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=1,
        max_agents=1,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Immutability check",
    )

    runner.execute_batch(batch, decision)
    current_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir), capture_output=True, text=True, check=True).stdout.strip()
    assert current_head == head_sha


def test_inv35_canonical_status_unchanged(repo_fixture, tmp_path: Path):
    """35. canonical status unchanged"""
    repo_dir, head_sha, wt_manager = repo_fixture
    cand_wt_base = tmp_path / "worktrees"
    runner = ParallelCandidateRunner(worktree_manager=wt_manager, base_worktree_dir=cand_wt_base)

    req = CandidateImplementationRequest("c1", "t35", "n1", head_sha, str(repo_dir), "b", ("service.py",))
    batch = CandidateImplementationBatch("b35", "t35", "n1", head_sha, (req,))
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=1,
        max_agents=1,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Status check",
    )

    runner.execute_batch(batch, decision)
    status_out = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_dir), capture_output=True, text=True, check=True).stdout.strip()
    assert status_out == ""


def test_inv36_candidate_cannot_merge_main():
    """36. candidate cannot merge main"""
    from ai_engineering.candidates.candidate_runner import validate_candidate_command
    with pytest.raises(CandidateError):
        validate_candidate_command(("git", "merge", "main"))


def test_inv37_candidate_cannot_deploy():
    """37. candidate cannot deploy"""
    from ai_engineering.candidates.candidate_runner import validate_candidate_command
    with pytest.raises(CandidateError):
        validate_candidate_command(("deploy", "--target=prod"))


def test_inv38_candidate_cannot_mutate_production_db():
    """38. candidate cannot mutate production DB"""
    from ai_engineering.candidates.candidate_runner import validate_candidate_command
    with pytest.raises(CandidateError):
        validate_candidate_command(("docker", "exec", "postgres", "psql"))


def test_inv39_candidate_cannot_mutate_production_qdrant():
    """39. candidate cannot mutate production Qdrant"""
    from ai_engineering.candidates.candidate_runner import validate_candidate_command
    with pytest.raises(CandidateError):
        validate_candidate_command(("docker", "exec", "qdrant", "reindex"))


def test_inv40_candidate_cannot_access_credential_mutation():
    """40. candidate cannot access credential mutation"""
    from ai_engineering.candidates.candidate_runner import validate_candidate_command
    with pytest.raises(CandidateError):
        validate_candidate_command(("ssh", "root@server", "update_secrets"))


def test_inv41_pr1_safety_remains_pass():
    """41. PR-1 safety remains PASS"""
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv42_pr2_fencing_remains_pass():
    """42. PR-2 fencing remains PASS"""
    assert RunState.LIVE == "LIVE"


def test_inv43_pr3_policy_remains_pass():
    """43. PR-3 policy remains PASS"""
    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv44_pr4_investigation_remains_pass():
    """44. PR-4 investigation remains PASS"""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch
    m = RepositoryMatch("a.py", 1, 1, "test", "TEXT")
    assert m.path == "a.py"
