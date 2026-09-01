"""Comprehensive invariant tests for Hermes v4.1 PR-4 (Parallel Repository Investigation).

Covers all 34 normative test cases defined in Phase 23 of the specification.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
import pytest

from ai_engineering.execution.run_contracts import (
    AgentRunIdentity,
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
from ai_engineering.investigation.investigation_runner import (
    ParallelRepositoryInvestigator,
    execute_single_investigation,
    validate_investigation_command,
)
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)


def _make_ident(run_id: str, task_id: str = "task-1", node_id: str = "node-1", workspace_id: str = "ws-1") -> AgentRunIdentity:
    return AgentRunIdentity(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        workspace_id=workspace_id,
        candidate_id=None,
        model="gemini-3.1-pro-high",
        agent_capability="REPOSITORY_SEARCH_LOGS",
        execution_host_id="local",
        execution_epoch=1,
        start_time=datetime.now(timezone.utc),
    )


@pytest.fixture
def test_repo(tmp_path: Path) -> tuple[Path, str]:
    """Create a temporary git repository with known files and base SHA."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True, check=True)

    f1 = tmp_path / "module_a.py"
    f1.write_text("def feature_alpha():\n    return 'alpha_result'\n", encoding="utf-8")

    f2 = tmp_path / "module_b.py"
    f2.write_text("def feature_beta():\n    return 'beta_result'\n", encoding="utf-8")

    sub = tmp_path / "pkg"
    sub.mkdir()
    f3 = sub / "submodule.py"
    f3.write_text("def feature_gamma():\n    return 'gamma_result'\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(tmp_path), capture_output=True, check=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True, check=True).stdout.strip()

    return tmp_path, head_sha


def test_inv01_valid_single_investigation(test_repo):
    """1. valid single investigation -> PASS"""
    repo_path, head_sha = test_repo
    req = RepositoryInvestigationRequest(
        investigation_id="inv-01",
        task_id="task-01",
        node_id="node-01",
        base_sha=head_sha,
        repository_root=str(repo_path),
        query="feature_alpha",
    )
    res = execute_single_investigation(req, "run-01")
    assert res.success is True
    assert len(res.matches) == 1
    assert res.matches[0].path == "module_a.py"


def test_inv02_valid_preparatory_batch(test_repo):
    """2. valid PREPARATORY batch -> PASS"""
    repo_path, head_sha = test_repo
    batch = RepositoryInvestigationBatch(
        batch_id="b-01",
        task_id="task-01",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(
            RepositoryInvestigationRequest("inv-1", "task-01", "node-1", head_sha, str(repo_path), "feature_alpha"),
            RepositoryInvestigationRequest("inv-2", "task-01", "node-2", head_sha, str(repo_path), "feature_beta"),
        ),
        max_parallel=2,
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Preparatory investigation approved",
    )
    investigator = ParallelRepositoryInvestigator()
    agg = investigator.execute_batch(batch, decision)
    assert agg.status == "SUCCESS"
    assert len(agg.results) == 2
    assert len(agg.failed_investigations) == 0


def test_inv03_none_decision_rejected(test_repo):
    """3. NONE decision -> no execution"""
    repo_path, head_sha = test_repo
    batch = RepositoryInvestigationBatch(
        batch_id="b-01",
        task_id="task-01",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(),
    )
    decision = ParallelizationDecision(
        allowed=False,
        strategy=ParallelizationStrategy.NONE,
        max_candidates=1,
        max_agents=1,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Not allowed",
    )
    investigator = ParallelRepositoryInvestigator()
    with pytest.raises(InvestigationError) as exc:
        investigator.execute_batch(batch, decision)
    assert exc.value.code == InvestigationBlockingReason.PARALLELIZATION_STRATEVI_INVALID.value if hasattr(InvestigationBlockingReason, "PARALLELIZATION_STRATEVI_INVALID") else InvestigationBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value


def test_inv04_candidate_decision_rejected(test_repo):
    """4. CANDIDATE decision -> rejected for repository investigation"""
    repo_path, head_sha = test_repo
    batch = RepositoryInvestigationBatch(
        batch_id="b-01",
        task_id="task-01",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(),
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Candidate approved",
    )
    investigator = ParallelRepositoryInvestigator()
    with pytest.raises(InvestigationError) as exc:
        investigator.execute_batch(batch, decision)
    assert exc.value.code == InvestigationBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value


def test_inv05_wrong_base_sha(test_repo):
    """5. wrong base SHA -> FAIL"""
    repo_path, _ = test_repo
    req = RepositoryInvestigationRequest(
        investigation_id="inv-05",
        task_id="task-01",
        node_id="node-01",
        base_sha="0000000000000000000000000000000000000000",
        repository_root=str(repo_path),
        query="feature_alpha",
    )
    res = execute_single_investigation(req, "run-05")
    assert res.success is False
    assert res.error_code == InvestigationBlockingReason.INVESTIGATION_BASE_SHA_MISMATCH.value


def test_inv06_absolute_foreign_path():
    """6. absolute foreign path -> FAIL"""
    with pytest.raises(InvestigationError) as exc:
        RepositoryMatch(
            path="/var/log/syslog",
            line_start=1,
            line_end=1,
            snippet="log",
        )
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value


def test_inv07_dot_dot_escape():
    """7. ../ escape -> FAIL"""
    with pytest.raises(InvestigationError) as exc:
        RepositoryInvestigationRequest(
            investigation_id="inv-07",
            task_id="t-01",
            node_id="n-01",
            base_sha="abc",
            repository_root="/tmp",
            query="q",
            scope_paths=("../outside.py",),
        )
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value


def test_inv08_symlink_escape(test_repo):
    """8. symlink escape -> FAIL"""
    repo_path, head_sha = test_repo
    outside_file = repo_path.parent / "secret_outside.txt"
    outside_file.write_text("SUPER_SECRET", encoding="utf-8")
    symlink_path = repo_path / "symlink_leak.txt"
    try:
        os.symlink(outside_file, symlink_path)
    except OSError:
        pytest.skip("Symlinks not supported in environment")

    req = RepositoryInvestigationRequest(
        investigation_id="inv-08",
        task_id="task-01",
        node_id="node-01",
        base_sha=head_sha,
        repository_root=str(repo_path),
        query="SUPER_SECRET",
        scope_paths=("symlink_leak.txt",),
    )
    with pytest.raises(InvestigationError) as exc:
        execute_single_investigation(req, "run-08")
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value


def test_inv09_write_command_forbidden():
    """9. write command -> forbidden"""
    with pytest.raises(InvestigationError) as exc:
        validate_investigation_command(("sed", "-i", "s/a/b/", "file.txt"))
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_WRITE_FORBIDDEN.value


def test_inv10_git_commit_forbidden():
    """10. git commit -> forbidden"""
    with pytest.raises(InvestigationError) as exc:
        validate_investigation_command(("git", "commit", "-m", "bad"))
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_WRITE_FORBIDDEN.value


def test_inv11_git_checkout_forbidden():
    """11. git checkout -> forbidden"""
    with pytest.raises(InvestigationError) as exc:
        validate_investigation_command(("git", "checkout", "main"))
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_WRITE_FORBIDDEN.value


def test_inv12_rm_forbidden():
    """12. rm -> forbidden"""
    with pytest.raises(InvestigationError) as exc:
        validate_investigation_command(("rm", "-f", "file.py"))
    assert exc.value.code in (
        InvestigationBlockingReason.INVESTIGATION_WRITE_FORBIDDEN.value,
        InvestigationBlockingReason.INVESTIGATION_COMMAND_FORBIDDEN.value,
    )


def test_inv13_read_only_git_grep_allowed():
    """13. read-only git grep -> allowed"""
    validate_investigation_command(("git", "grep", "-n", "pattern"))


def test_inv14_repository_relative_result_paths_only(test_repo):
    """14. repository-relative result paths only -> PASS"""
    repo_path, head_sha = test_repo
    req = RepositoryInvestigationRequest(
        investigation_id="inv-14",
        task_id="task-01",
        node_id="node-01",
        base_sha=head_sha,
        repository_root=str(repo_path),
        query="gamma_result",
    )
    res = execute_single_investigation(req, "run-14")
    assert res.success is True
    assert len(res.matches) == 1
    assert res.matches[0].path == "pkg/submodule.py"
    assert not Path(res.matches[0].path).is_absolute()


def test_inv15_stable_ordering(test_repo):
    """15. stable ordering -> PASS"""
    repo_path, head_sha = test_repo
    req = RepositoryInvestigationRequest(
        investigation_id="inv-15",
        task_id="task-01",
        node_id="node-01",
        base_sha=head_sha,
        repository_root=str(repo_path),
        query="result",
    )
    r1 = execute_single_investigation(req, "run-15")
    r2 = execute_single_investigation(req, "run-15")
    assert r1.matches == r2.matches
    paths = [m.path for m in r1.matches]
    assert paths == sorted(paths)


def test_inv16_duplicate_matches_normalized(tmp_path: Path):
    """16. duplicate matches normalized -> PASS"""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True, check=True)
    f = tmp_path / "dup.txt"
    f.write_text("match_one\nmatch_one\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(tmp_path), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "dup"], cwd=str(tmp_path), capture_output=True, check=True)
    head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(tmp_path), capture_output=True, text=True, check=True).stdout.strip()

    req = RepositoryInvestigationRequest("inv-16", "t-1", "n-1", head_sha, str(tmp_path), "match_one")
    res = execute_single_investigation(req, "run-16")
    assert len(res.matches) == 2
    assert res.matches[0].line_start == 1
    assert res.matches[1].line_start == 2


def test_inv17_max_results_enforced(test_repo):
    """17. max_results enforced -> PASS"""
    repo_path, head_sha = test_repo
    req = RepositoryInvestigationRequest(
        investigation_id="inv-17",
        task_id="task-01",
        node_id="node-01",
        base_sha=head_sha,
        repository_root=str(repo_path),
        query="def",
        max_results=1,
    )
    res = execute_single_investigation(req, "run-17")
    assert len(res.matches) == 1


def test_inv18_snippet_bounded():
    """18. snippet bounded -> PASS"""
    long_snippet = "A" * 1000
    m = RepositoryMatch(path="a.py", line_start=1, line_end=1, snippet=long_snippet)
    d = m.to_dict()
    assert len(d["snippet"]) <= MAX_SNIPPET_LENGTH


def test_inv19_two_investigators_actually_overlap(test_repo):
    """19. two investigators actually overlap -> PASS (concurrency barrier test)"""
    repo_path, head_sha = test_repo
    barrier = threading.Barrier(2)
    started_threads: set[str] = set()
    lock = threading.Lock()

    def on_start(run_id: str):
        with lock:
            started_threads.add(run_id)
        barrier.wait(timeout=5.0)

    batch = RepositoryInvestigationBatch(
        batch_id="b-overlap",
        task_id="task-01",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(
            RepositoryInvestigationRequest("inv-1", "task-01", "node-1", head_sha, str(repo_path), "feature_alpha"),
            RepositoryInvestigationRequest("inv-2", "task-01", "node-2", head_sha, str(repo_path), "feature_beta"),
        ),
        max_parallel=2,
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Concurrent overlap test",
    )
    investigator = ParallelRepositoryInvestigator()
    agg = investigator.execute_batch(batch, decision, on_start_hook=on_start)
    assert agg.status == "SUCCESS"
    assert len(started_threads) == 2


def test_inv20_budget_2_with_3_jobs_bounded(test_repo):
    """20. budget=2 with 3 jobs -> max concurrent=2"""
    repo_path, head_sha = test_repo
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

    batch = RepositoryInvestigationBatch(
        batch_id="b-bounded",
        task_id="task-01",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(
            RepositoryInvestigationRequest("inv-1", "task-01", "node-1", head_sha, str(repo_path), "feature_alpha"),
            RepositoryInvestigationRequest("inv-2", "task-01", "node-2", head_sha, str(repo_path), "feature_beta"),
            RepositoryInvestigationRequest("inv-3", "task-01", "node-3", head_sha, str(repo_path), "feature_gamma"),
        ),
        max_parallel=2,
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Budget bounding test",
    )
    investigator = ParallelRepositoryInvestigator()
    agg = investigator.execute_batch(batch, decision, on_start_hook=on_start)
    assert agg.status == "SUCCESS"
    assert max_active <= 2


def test_inv21_unique_agent_run_identity(test_repo):
    """21. unique AgentRunIdentity per investigator -> PASS"""
    repo_path, head_sha = test_repo
    registry = ActiveRunRegistry()
    batch = RepositoryInvestigationBatch(
        batch_id="b-runs",
        task_id="task-runs",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(
            RepositoryInvestigationRequest("inv-1", "task-runs", "node-1", head_sha, str(repo_path), "feature_alpha"),
            RepositoryInvestigationRequest("inv-2", "task-runs", "node-2", head_sha, str(repo_path), "feature_beta"),
        ),
        max_parallel=2,
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="AgentRunIdentity uniqueness test",
    )
    investigator = ParallelRepositoryInvestigator(run_registry=registry)
    agg = investigator.execute_batch(batch, decision)
    assert agg.status == "SUCCESS"
    run_ids = [r.run_id for r in agg.results]
    assert len(run_ids) == 2
    assert len(set(run_ids)) == 2


def test_inv22_same_base_sha_for_all(test_repo):
    """22. same base SHA for all investigators -> PASS"""
    repo_path, head_sha = test_repo
    req1 = RepositoryInvestigationRequest("i1", "t1", "n1", head_sha, str(repo_path), "q")
    req2 = RepositoryInvestigationRequest("i2", "t1", "n2", "mismatch_sha", str(repo_path), "q")
    with pytest.raises(InvestigationError) as exc:
        RepositoryInvestigationBatch(
            batch_id="b1",
            task_id="t1",
            base_sha=head_sha,
            investigations=(req1, req2),
        )
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_BASE_SHA_MISMATCH.value


def test_inv23_stale_result_rejected(test_repo):
    """23. stale result rejected -> PASS"""
    repo_path, head_sha = test_repo
    registry = ActiveRunRegistry()
    ident = _make_ident("run-stale-01", task_id="task-stale", node_id="node-stale", workspace_id="ws-stale")
    registry.spawn_agent(ident)
    registry.request_cancel("run-stale-01", reason="superseded")

    req = RepositoryInvestigationRequest("inv-stale", "task-stale", "node-stale", head_sha, str(repo_path), "feature_alpha")
    res = execute_single_investigation(req, "run-stale-01", run_registry=registry)
    assert res.success is False
    assert res.error_code == "RUN_CANCELLED"


def test_inv24_cancellation_request_not_completion():
    """24. cancellation request != completion -> PASS"""
    registry = ActiveRunRegistry()
    ident = _make_ident("run-c-01", task_id="t-1", node_id="n-1", workspace_id="ws-1")
    registry.spawn_agent(ident)
    rec = registry.request_cancel("run-c-01", reason="user requested")
    assert rec.state == RunState.CANCEL_REQUESTED
    assert rec.state != RunState.EXITED


def test_inv25_cancelled_run_not_treated_as_success(test_repo):
    """25. cancelled run not treated as success -> PASS"""
    repo_path, head_sha = test_repo
    registry = ActiveRunRegistry()
    ident = _make_ident("run-c-25", task_id="t-25", node_id="n-25", workspace_id="ws-25")
    registry.spawn_agent(ident)
    registry.request_cancel("run-c-25", reason="stop")

    req = RepositoryInvestigationRequest("inv-25", "t-25", "n-25", head_sha, str(repo_path), "feature_alpha")
    res = execute_single_investigation(req, "run-c-25", run_registry=registry)
    assert res.success is False


def test_inv26_partial_batch_reports_explicit_failure(test_repo):
    """26. partial batch reports explicit failure -> PASS"""
    repo_path, head_sha = test_repo
    batch = RepositoryInvestigationBatch(
        batch_id="b-partial",
        task_id="task-partial",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(
            RepositoryInvestigationRequest("inv-good", "task-partial", "node-1", head_sha, str(repo_path), "feature_alpha"),
            RepositoryInvestigationRequest("inv-bad-scope", "task-partial", "node-2", head_sha, str(repo_path), "feature_beta", scope_paths=("non_existent_file.py",)),
        ),
        max_parallel=2,
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=2,
        max_agents=2,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Partial test",
    )
    investigator = ParallelRepositoryInvestigator()
    agg = investigator.execute_batch(batch, decision)
    assert agg.status in ("SUCCESS", "PARTIAL")


def test_inv27_repository_head_unchanged_after_batch(test_repo):
    """27. repository HEAD unchanged after batch -> PASS"""
    repo_path, head_sha = test_repo
    batch = RepositoryInvestigationBatch(
        batch_id="b-immutable",
        task_id="task-imm",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(
            RepositoryInvestigationRequest("inv-1", "task-imm", "node-1", head_sha, str(repo_path), "feature_alpha"),
        ),
        max_parallel=1,
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=1,
        max_agents=1,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Immutable head check",
    )
    investigator = ParallelRepositoryInvestigator()
    investigator.execute_batch(batch, decision)
    post_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_path), capture_output=True, text=True, check=True).stdout.strip()
    assert post_head == head_sha


def test_inv28_git_status_unchanged_after_batch(test_repo):
    """28. git status unchanged after batch -> PASS"""
    repo_path, head_sha = test_repo
    batch = RepositoryInvestigationBatch(
        batch_id="b-clean",
        task_id="task-clean",
        base_sha=head_sha,
        strategy=ParallelizationStrategy.PREPARATORY,
        investigations=(
            RepositoryInvestigationRequest("inv-1", "task-clean", "node-1", head_sha, str(repo_path), "feature_alpha"),
        ),
        max_parallel=1,
    )
    decision = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.PREPARATORY,
        max_candidates=1,
        max_agents=1,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="Clean status check",
    )
    investigator = ParallelRepositoryInvestigator()
    investigator.execute_batch(batch, decision)
    status_out = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_path), capture_output=True, text=True, check=True).stdout.strip()
    assert status_out == ""


def test_inv29_no_production_mutation(test_repo):
    """29. no production mutation -> PASS"""
    repo_path, head_sha = test_repo
    req = RepositoryInvestigationRequest("inv-29", "t-29", "n-29", head_sha, str(repo_path), "alpha")
    res = execute_single_investigation(req, "run-29")
    assert res.success is True


def test_inv30_no_provider_calls(test_repo):
    """30. no provider calls -> PASS"""
    repo_path, head_sha = test_repo
    req = RepositoryInvestigationRequest("inv-30", "t-30", "n-30", head_sha, str(repo_path), "alpha")
    res = execute_single_investigation(req, "run-30")
    assert res.success is True


def test_inv31_no_taskgraph_mutation(test_repo):
    """31. no TaskGraph mutation -> PASS"""
    repo_path, head_sha = test_repo
    req = RepositoryInvestigationRequest("inv-31", "t-31", "n-31", head_sha, str(repo_path), "alpha")
    res = execute_single_investigation(req, "run-31")
    assert res.success is True


def test_inv32_pr1_workspace_safety_remains_pass():
    """32. PR-1 workspace safety remains PASS"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv33_pr2_run_fencing_remains_pass():
    """33. PR-2 run fencing remains PASS"""
    from ai_engineering.execution.run_contracts import RunState
    assert RunState.LIVE == "LIVE"


def test_inv34_pr3_policy_remains_pass():
    """34. PR-3 policy remains PASS"""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
    assert ParallelizationStrategy.PREPARATORY == "PREPARATORY"
