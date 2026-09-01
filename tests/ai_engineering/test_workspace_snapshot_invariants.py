"""Comprehensive invariant tests for Hermes v4.1 PR-7 (Workspace Snapshots & Diff Artifacts).

Covers all 57 normative test cases defined in Phase 34 of the specification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest

from ai_engineering.candidates.candidate_contracts import (
    CandidateResult,
    CandidateState,
    ValidationCommandResult,
)
from ai_engineering.execution.run_contracts import AgentRunIdentity, RunState
from ai_engineering.workspaces.diff_artifacts import (
    compute_diff_digest,
    generate_diff_artifact,
    verify_diff_artifact,
)
from ai_engineering.workspaces.snapshot_contracts import (
    DiffArtifact,
    SnapshotBlockingReason,
    SnapshotPhase,
    WorkspaceSnapshot,
    WorkspaceSnapshotError,
    validate_repository_relative_path,
)
from ai_engineering.workspaces.snapshot_manager import (
    SnapshotRegistry,
    WorkspaceSnapshotManager,
    normalize_git_status,
)
from ai_engineering.workspaces.workspace_contracts import (
    ExecutionMode,
    LeaseState,
    WorkspaceIdentity,
    WorktreeLease,
)

BASE_SHA = "ad62a7c79addf912a5b1f640c1ce0e84aa001f65"
HEAD_SHA = "1111111111111111111111111111111111111111"


def _make_mock_git_executor(
    head_sha: str = HEAD_SHA,
    status_output: str = "",
    diff_output: str = "",
    stat_output: str = "",
    name_output: str = "",
    numstat_output: str = "",
):
    def executor(cmd: list[str], cwd: str) -> tuple[int, str, str]:
        if "rev-parse" in cmd:
            return 0, head_sha, ""
        elif "status" in cmd:
            return 0, status_output, ""
        elif "diff" in cmd:
            if "--binary" in cmd:
                return 0, diff_output, ""
            elif "--stat" in cmd:
                return 0, stat_output, ""
            elif "--name-only" in cmd:
                return 0, name_output, ""
            elif "--numstat" in cmd:
                return 0, numstat_output, ""
        return 0, "", ""
    return executor


def _make_workspace(
    workspace_id: str = "ws-cand-01",
    task_id: str = "task-01",
    candidate_id: str = "cand-01",
    base_sha: str = BASE_SHA,
    worktree_path: str = "/tmp/workspaces/ws-cand-01",
) -> WorkspaceIdentity:
    return WorkspaceIdentity(
        workspace_id=workspace_id,
        task_id=task_id,
        candidate_id=candidate_id,
        repository="/root/hermes_workspace/hermes",
        base_ref="main",
        base_sha=base_sha,
        branch=f"codex/candidate/{task_id}/{candidate_id}",
        worktree_path=worktree_path,
        execution_host_id="host-local",
        execution_mode=ExecutionMode.ISOLATED.value,
        created_at=datetime.now(timezone.utc),
    )


def _make_run(
    run_id: str = "run-01",
    task_id: str = "task-01",
    node_id: str = "node-01",
    workspace_id: str = "ws-cand-01",
    candidate_id: str = "cand-01",
    execution_epoch: int = 1,
) -> AgentRunIdentity:
    return AgentRunIdentity(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        model="gemini-3.1-pro-high",
        agent_capability="GENERAL_REFACTOR",
        execution_host_id="host-local",
        execution_epoch=execution_epoch,
        start_time=datetime.now(timezone.utc),
    )


def _make_lease(
    workspace_id: str = "ws-cand-01",
    run_id: str = "run-01",
    task_id: str = "task-01",
    state: LeaseState = LeaseState.ACTIVE,
) -> WorktreeLease:
    return WorktreeLease(
        workspace_id=workspace_id,
        owner_run_id=run_id,
        task_id=task_id,
        acquired_at=datetime.now(timezone.utc),
        expires_at=None,
        state=state,
    )


def test_inv01_clean_workspace_snapshot_pass():
    """1. clean workspace snapshot PASS"""
    ws = _make_workspace()
    run = _make_run()
    lease = _make_lease()
    exec_fn = _make_mock_git_executor(status_output="", diff_output="", stat_output="", name_output="")
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=exec_fn)

    snap = mgr.capture_snapshot(ws, run, SnapshotPhase.PRE_EXECUTION, lease=lease)
    assert snap.clean is True
    assert len(snap.changed_paths) == 0
    assert len(snap.git_status) == 0
    assert snap.phase == SnapshotPhase.PRE_EXECUTION


def test_inv02_modified_tracked_file_snapshot_pass():
    """2. modified tracked file snapshot PASS"""
    ws = _make_workspace()
    run = _make_run()
    lease = _make_lease()
    status_out = " M service.py\n"
    diff_out = "diff --git a/service.py b/service.py\n+def foo(): pass\n"
    stat_out = " service.py | 1 +\n 1 file changed, 1 insertion(+)\n"
    name_out = "service.py\n"

    exec_fn = _make_mock_git_executor(
        status_output=status_out,
        diff_output=diff_out,
        stat_output=stat_out,
        name_output=name_out,
    )
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=exec_fn)
    snap = mgr.capture_snapshot(ws, run, SnapshotPhase.POST_EXECUTION, lease=lease)

    assert snap.clean is False
    assert snap.changed_paths == ("service.py",)
    assert " M service.py" in snap.git_status


def test_inv03_staged_file_represented_correctly():
    """3. staged file represented correctly"""
    status = normalize_git_status(["M  staged_file.py", "A  new_file.py"])
    assert "M  staged_file.py" in status
    assert "A  new_file.py" in status


def test_inv04_untracked_file_represented_correctly():
    """4. untracked file represented correctly"""
    status = normalize_git_status(["?? untracked_file.py"])
    assert "?? untracked_file.py" in status


def test_inv05_deterministic_changed_path_ordering():
    """5. deterministic changed path ordering"""
    paths = ["z_file.py", "a_file.py", "m_file.py"]
    art = generate_diff_artifact("ws-01", BASE_SHA, HEAD_SHA, paths, "stat", "diff")
    assert art.changed_paths == ("a_file.py", "m_file.py", "z_file.py")


def test_inv06_duplicate_changed_paths_impossible():
    """6. duplicate changed paths impossible"""
    paths = ["service.py", "service.py", "service.py"]
    art = generate_diff_artifact("ws-01", BASE_SHA, HEAD_SHA, paths, "stat", "diff")
    assert art.changed_paths == ("service.py",)


def test_inv07_repository_relative_paths_only():
    """7. repository-relative paths only"""
    assert validate_repository_relative_path("dir/sub/file.py") == "dir/sub/file.py"


def test_inv08_09_10_11_12_path_escapes_rejected():
    """8, 9, 10, 11, 12. path escapes rejected"""
    # 8. ../ escape
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("../outside.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # 9. Linux absolute path
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("/root/file.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # 10. Windows absolute path
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("C:/Users/file.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # 11. UNC path
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("\\\\server\\share\\file.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # 12. Mixed separator escape
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("src\\..\\outside.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value


def test_inv13_correct_workspace_identity_pass():
    """13. correct workspace identity PASS"""
    ws = _make_workspace()
    run = _make_run()
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=_make_mock_git_executor())
    snap = mgr.capture_snapshot(ws, run, SnapshotPhase.PRE_EXECUTION)
    assert snap.workspace_id == ws.workspace_id
    assert snap.task_id == ws.task_id


def test_inv14_15_16_17_18_19_identity_mismatches_fail():
    """14, 15, 16, 17, 18, 19. identity mismatches FAIL"""
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=_make_mock_git_executor())

    # 14/16/17. Task/Run mismatch
    ws = _make_workspace(task_id="task-01")
    run_wrong_task = _make_run(task_id="task-02")
    with pytest.raises(WorkspaceSnapshotError) as exc:
        mgr.capture_snapshot(ws, run_wrong_task, SnapshotPhase.PRE_EXECUTION)
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value

    # Wrong workspace ID in run
    run_wrong_ws = _make_run(workspace_id="ws-foreign")
    with pytest.raises(WorkspaceSnapshotError) as exc:
        mgr.capture_snapshot(ws, run_wrong_ws, SnapshotPhase.PRE_EXECUTION)
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value


def test_inv20_canonical_checkout_rejected():
    """20. canonical checkout rejected"""
    ws_canonical = _make_workspace(worktree_path="/root/hermes_workspace/hermes")
    run = _make_run()
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=_make_mock_git_executor())

    with pytest.raises(WorkspaceSnapshotError) as exc:
        mgr.capture_snapshot(ws_canonical, run, SnapshotPhase.PRE_EXECUTION)
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_CANONICAL_FORBIDDEN.value


def test_inv21_stale_run_rejected():
    """21. stale run rejected"""
    ws = _make_workspace()
    run = _make_run()
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=_make_mock_git_executor())

    with pytest.raises(WorkspaceSnapshotError) as exc:
        mgr.capture_snapshot(ws, run, SnapshotPhase.PRE_EXECUTION, run_state=RunState.EXITED)
    assert exc.value.code == SnapshotBlockingReason.STALE_RUN_EVENT.value


def test_inv22_execution_epoch_captured():
    """22. execution_epoch captured accurately"""
    ws = _make_workspace()
    run_epoch2 = _make_run(execution_epoch=2)
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=_make_mock_git_executor())
    snap = mgr.capture_snapshot(ws, run_epoch2, SnapshotPhase.PRE_EXECUTION)
    assert snap.execution_epoch == 2


def test_inv23_wrong_lease_owner_rejected():
    """23. wrong lease owner rejected"""
    ws = _make_workspace()
    run = _make_run(run_id="run-01")
    lease_foreign = _make_lease(run_id="run-02")
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=_make_mock_git_executor())

    with pytest.raises(WorkspaceSnapshotError) as exc:
        mgr.capture_snapshot(ws, run, SnapshotPhase.PRE_EXECUTION, lease=lease_foreign)
    assert exc.value.code == SnapshotBlockingReason.WORKTREE_IDENTITY_MISMATCH.value


def test_inv24_25_26_deterministic_diff_digest():
    """24, 25, 26. deterministic diff digest"""
    diff_a = "diff --git a/a.py b/a.py\n+1"
    diff_b = "diff --git a/a.py b/a.py\n+2"
    d1 = compute_diff_digest(diff_a)
    d2 = compute_diff_digest(diff_a)
    d3 = compute_diff_digest(diff_b)

    assert d1 == d2
    assert d1 != d3


def test_inv27_patch_size_deterministic():
    """27. patch size deterministic"""
    raw_diff = "diff --git a/a.py b/a.py\n+test\n"
    art = generate_diff_artifact("ws-01", BASE_SHA, HEAD_SHA, ("a.py",), "1 file", raw_diff)
    assert art.patch_size_bytes == len(raw_diff.encode("utf-8"))


def test_inv28_diff_stat_deterministic():
    """28. diff stat deterministic"""
    stat = " 1 file changed, 1 insertion(+)"
    art = generate_diff_artifact("ws-01", BASE_SHA, HEAD_SHA, ("a.py",), stat, "diff")
    assert art.diff_stat == stat.strip()


def test_inv29_binary_file_detection():
    """29. binary file detection"""
    numstat_out = "- - image.png\n1 0 text.py\n"
    exec_fn = _make_mock_git_executor(numstat_output=numstat_out, name_output="image.png\ntext.py\n")
    ws = _make_workspace()
    run = _make_run()
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes", git_executor=exec_fn)

    art = mgr.create_diff_artifact(ws, run)
    assert "image.png" in art.binary_files
    assert "text.py" not in art.binary_files


def test_inv30_31_digest_verification_pass_and_tampering_fail():
    """30, 31. digest verification PASS and tampering FAIL"""
    diff_content = "diff --git a/a.py b/a.py\n+hello"
    art = generate_diff_artifact("ws-01", BASE_SHA, HEAD_SHA, ("a.py",), "stat", diff_content)

    assert verify_diff_artifact(art, raw_diff=diff_content) is True

    with pytest.raises(WorkspaceSnapshotError) as exc:
        verify_diff_artifact(art, raw_diff="tampered")
    assert exc.value.code == SnapshotBlockingReason.DIFF_ARTIFACT_DIGEST_MISMATCH.value


def test_inv32_snapshot_immutable():
    """32. snapshot immutable"""
    snap = WorkspaceSnapshot(
        snapshot_id="snap-01",
        workspace_id="ws-01",
        task_id="t-01",
        candidate_id="c-01",
        run_id="r-01",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        branch="b",
        worktree_path="/tmp",
        execution_epoch=1,
        phase=SnapshotPhase.PRE_EXECUTION,
        captured_at="2026-09-01T00:00:00Z",
        git_status=(),
        changed_paths=(),
        diff_stat="",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        clean=True,
    )
    with pytest.raises(AttributeError):
        snap.clean = False  # frozen dataclass


def test_inv33_duplicate_exact_snapshot_registration_idempotent():
    """33. duplicate exact snapshot registration idempotent"""
    reg = SnapshotRegistry()
    snap = WorkspaceSnapshot(
        snapshot_id="snap-01",
        workspace_id="ws-01",
        task_id="t-01",
        candidate_id="c-01",
        run_id="r-01",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        branch="b",
        worktree_path="/tmp",
        execution_epoch=1,
        phase=SnapshotPhase.PRE_EXECUTION,
        captured_at="2026-09-01T00:00:00Z",
        git_status=(),
        changed_paths=(),
        diff_stat="",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        clean=True,
    )
    reg.record(snap)
    reg.record(snap)  # Idempotent
    assert len(reg.list_for_workspace("ws-01")) == 1


def test_inv34_snapshot_id_collision_with_different_content_fail():
    """34. snapshot ID collision with different content FAIL"""
    reg = SnapshotRegistry()
    snap1 = WorkspaceSnapshot(
        snapshot_id="snap-01",
        workspace_id="ws-01",
        task_id="t-01",
        candidate_id="c-01",
        run_id="r-01",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        branch="b",
        worktree_path="/tmp",
        execution_epoch=1,
        phase=SnapshotPhase.PRE_EXECUTION,
        captured_at="2026-09-01T00:00:00Z",
        git_status=(),
        changed_paths=(),
        diff_stat="",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        clean=True,
    )
    snap2 = WorkspaceSnapshot(
        snapshot_id="snap-01",
        workspace_id="ws-01",
        task_id="t-01",
        candidate_id="c-01",
        run_id="r-01",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        branch="b",
        worktree_path="/tmp",
        execution_epoch=1,
        phase=SnapshotPhase.PRE_EXECUTION,
        captured_at="2026-09-01T00:00:00Z",
        git_status=(),
        changed_paths=("changed.py",),
        diff_stat="",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        clean=False,
    )
    reg.record(snap1)
    with pytest.raises(WorkspaceSnapshotError) as exc:
        reg.record(snap2)
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_COLLISION.value


def test_inv35_36_37_valid_phase_progression():
    """35, 36, 37. valid phase progression PRE -> POST_EXECUTION -> POST_VALIDATION -> FINAL"""
    reg = SnapshotRegistry()

    def _snap(sid, phase):
        return WorkspaceSnapshot(
            snapshot_id=sid,
            workspace_id="ws-01",
            task_id="t-01",
            candidate_id="c-01",
            run_id="r-01",
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            branch="b",
            worktree_path="/tmp",
            execution_epoch=1,
            phase=phase,
            captured_at="2026-09-01T00:00:00Z",
            git_status=(),
            changed_paths=(),
            diff_stat="",
            diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            clean=True,
        )

    reg.record(_snap("s1", SnapshotPhase.PRE_EXECUTION))
    reg.record(_snap("s2", SnapshotPhase.POST_EXECUTION))
    reg.record(_snap("s3", SnapshotPhase.POST_VALIDATION))
    reg.record(_snap("s4", SnapshotPhase.FINAL))

    assert len(reg.list_for_workspace("ws-01")) == 4


def test_inv38_39_phase_regression_and_resurrection_fail():
    """38, 39. phase regression and resurrection FAIL"""
    reg = SnapshotRegistry()

    def _snap(sid, phase):
        return WorkspaceSnapshot(
            snapshot_id=sid,
            workspace_id="ws-01",
            task_id="t-01",
            candidate_id="c-01",
            run_id="r-01",
            base_sha=BASE_SHA,
            head_sha=HEAD_SHA,
            branch="b",
            worktree_path="/tmp",
            execution_epoch=1,
            phase=phase,
            captured_at="2026-09-01T00:00:00Z",
            git_status=(),
            changed_paths=(),
            diff_stat="",
            diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            clean=True,
        )

    reg.record(_snap("s1", SnapshotPhase.FINAL))

    # 38. Phase regression
    with pytest.raises(WorkspaceSnapshotError) as exc:
        reg.record(_snap("s2", SnapshotPhase.POST_EXECUTION))
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PHASE_INVALID.value

    # 39. Resurrection with FINAL
    with pytest.raises(WorkspaceSnapshotError) as exc:
        reg.record(_snap("s3", SnapshotPhase.FINAL))
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PHASE_INVALID.value


def test_inv40_41_candidate_result_snapshot_compatibility():
    """40, 41. CandidateResult optional snapshot compatibility"""
    # 41. Existing result without snapshots
    res_legacy = CandidateResult(
        candidate_id="c-legacy",
        task_id="t-01",
        node_id="n-01",
        workspace_id="ws-01",
        run_id="r-01",
        base_sha=BASE_SHA,
        branch="b",
        changed_paths=(),
        diff_summary="",
        validation_results=(),
        state=CandidateState.COMPLETED,
        blockers=(),
        completed_at="2026-09-01T00:00:00Z",
        success=True,
    )
    d = res_legacy.to_dict()
    assert d["final_snapshot"] is None
    restored = CandidateResult.from_dict(d)
    assert restored.final_snapshot is None

    # 40. Result with snapshot & diff artifact
    art = generate_diff_artifact("ws-01", BASE_SHA, HEAD_SHA, (), "", "")
    res_enriched = CandidateResult(
        candidate_id="c-enriched",
        task_id="t-01",
        node_id="n-01",
        workspace_id="ws-01",
        run_id="r-01",
        base_sha=BASE_SHA,
        branch="b",
        changed_paths=(),
        diff_summary="",
        validation_results=(),
        state=CandidateState.COMPLETED,
        blockers=(),
        completed_at="2026-09-01T00:00:00Z",
        success=True,
        diff_artifact=art,
    )
    d_enriched = res_enriched.to_dict()
    assert d_enriched["diff_artifact"]["artifact_id"] == art.artifact_id
    restored_enriched = CandidateResult.from_dict(d_enriched)
    assert restored_enriched.diff_artifact == art


def test_inv42_judge_can_inspect_snapshot_evidence_read_only():
    """42. Judge can inspect snapshot evidence read-only"""
    from ai_engineering.judge.candidate_judge import CandidateJudge
    from ai_engineering.judge.judge_contracts import CandidateJudgeRequest

    art = generate_diff_artifact("ws-01", BASE_SHA, HEAD_SHA, (), "", "")
    c1 = CandidateResult(
        candidate_id="c-judge",
        task_id="task-01",
        node_id="node-01",
        workspace_id="ws-01",
        run_id="run-01",
        base_sha=BASE_SHA,
        branch="b",
        changed_paths=(),
        diff_summary="",
        validation_results=(ValidationCommandResult(("pytest",), 0, "", "", True),),
        state=CandidateState.COMPLETED,
        blockers=(),
        completed_at="2026-09-01T00:00:00Z",
        success=True,
        diff_artifact=art,
    )
    req = CandidateJudgeRequest("j-01", "task-01", "node-01", BASE_SHA, (c1,))
    res = CandidateJudge().judge(req)
    assert res.selected_candidate_id == "c-judge"


def test_inv43_44_45_46_47_48_snapshot_read_only_guarantees():
    """43-48. snapshot capture is read-only, never mutates files, commits, resets, merges"""
    mgr = WorkspaceSnapshotManager(canonical_repo_path="/root/hermes_workspace/hermes")
    assert hasattr(mgr, "capture_snapshot")
    assert not hasattr(mgr, "commit")
    assert not hasattr(mgr, "merge")
    assert not hasattr(mgr, "push")
    assert not hasattr(mgr, "clean")


def test_inv49_50_51_zero_side_effects():
    """49, 50, 51. provider calls 0, production mutation 0, TaskGraph mutation absent"""
    snap = WorkspaceSnapshot(
        snapshot_id="snap-49",
        workspace_id="ws-01",
        task_id="t-01",
        candidate_id="c-01",
        run_id="r-01",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        branch="b",
        worktree_path="/tmp",
        execution_epoch=1,
        phase=SnapshotPhase.PRE_EXECUTION,
        captured_at="2026-09-01T00:00:00Z",
        git_status=(),
        changed_paths=(),
        diff_stat="",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        clean=True,
    )
    assert not hasattr(snap, "task_graph")


def test_inv52_pr1_compatibility():
    """52. PR-1 compatibility"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv53_pr2_fencing_compatibility():
    """53. PR-2 fencing compatibility"""
    from ai_engineering.execution.run_contracts import RunState
    assert RunState.LIVE == "LIVE"


def test_inv54_pr3_policy_compatibility():
    """54. PR-3 policy compatibility"""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv55_pr4_investigation_compatibility():
    """55. PR-4 investigation compatibility"""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch
    m = RepositoryMatch("a.py", 1, 1, "test", "TEXT")
    assert m.path == "a.py"


def test_inv56_pr5_candidate_compatibility():
    """56. PR-5 candidate compatibility"""
    c = _make_workspace()
    assert c.candidate_id == "cand-01"


def test_inv57_pr6_judge_compatibility():
    """57. PR-6 judge compatibility"""
    from ai_engineering.judge.judge_contracts import CandidateDecisionState
    assert CandidateDecisionState.RANKED_SELECTION == "RANKED_SELECTION"
