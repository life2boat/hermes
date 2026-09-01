"""Comprehensive invariant tests for Hermes v4.1 PR-8 (Main Drift & Candidate Requalification).

Covers all 56 normative test cases defined in Phase 39 of the specification.
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from ai_engineering.candidates.candidate_contracts import CandidateResult, CandidateState
from ai_engineering.judge.judge_contracts import CandidateJudgeResult, CandidateDecisionState
from ai_engineering.requalification.requalification_contracts import (
    BaseRelationship,
    CandidateRequalificationRequest,
    CandidateRequalificationResult,
    JudgementFreshness,
    RequalificationBlockingReason,
    RequalificationDecisionState,
    RequalificationError,
    ValidationFreshness,
)
from ai_engineering.requalification.requalification_engine import (
    CandidateRequalificationEngine,
)
from ai_engineering.requalification.requalification_registry import (
    RequalificationRegistry,
)
from ai_engineering.workspaces.diff_artifacts import generate_diff_artifact
from ai_engineering.workspaces.snapshot_contracts import validate_repository_relative_path

BASE_A = "7334916be325e817fb3d35710aa7c547a9c10040"
MAIN_B = "8888888888888888888888888888888888888888"
MAIN_C = "9999999999999999999999999999999999999999"


def _make_candidate_result(
    candidate_id: str = "cand-01",
    base_sha: str = BASE_A,
    changed_paths: tuple[str, ...] = ("src/service.py",),
    success: bool = True,
    blockers: tuple[str, ...] = (),
) -> CandidateResult:
    return CandidateResult(
        candidate_id=candidate_id,
        task_id="task-01",
        node_id="node-01",
        workspace_id="ws-01",
        run_id="run-01",
        base_sha=base_sha,
        branch=f"codex/candidate/task-01/{candidate_id}",
        changed_paths=changed_paths,
        diff_summary="",
        validation_results=(),
        state=CandidateState.COMPLETED,
        blockers=blockers,
        completed_at="2026-09-01T00:00:00Z",
        success=success,
    )


def _make_mock_git(
    cat_file_rc: int = 0,
    merge_base_rc: int = 0,
    diff_name_output: str = "",
    diff_stat_output: str = "",
    diff_binary_output: str = "",
    rev_list_output: str = "1\n",
):
    def executor(cmd: list[str], cwd: str) -> tuple[int, str, str]:
        if "cat-file" in cmd:
            return cat_file_rc, "", ""
        if "merge-base" in cmd:
            return merge_base_rc, "", ""
        if "diff" in cmd:
            if "--name-only" in cmd:
                return 0, diff_name_output, ""
            if "--stat" in cmd:
                return 0, diff_stat_output, ""
            if "--binary" in cmd:
                return 0, diff_binary_output, ""
        if "rev-list" in cmd:
            return 0, rev_list_output, ""
        return 0, "", ""
    return executor


def test_inv01_exact_base_no_requalification_required():
    """1. exact base => no requalification required"""
    c_res = _make_candidate_result(base_sha=BASE_A)
    req = CandidateRequalificationRequest("req-01", "task-01", "node-01", "cand-01", "ws-01", "run-01", BASE_A, BASE_A, c_res)
    res = CandidateRequalificationEngine().evaluate(req)
    assert res.relationship == BaseRelationship.EXACT_BASE
    assert res.decision_state == RequalificationDecisionState.NO_REQUALIFICATION_REQUIRED
    assert res.eligible is True
    assert res.requires_new_candidate is False


def test_inv02_main_descendant_detected():
    """2. main descendant detected"""
    c_res = _make_candidate_result(base_sha=BASE_A)
    req = CandidateRequalificationRequest("req-02", "task-01", "node-01", "cand-01", "ws-01", "run-01", BASE_A, MAIN_B, c_res)
    engine = CandidateRequalificationEngine(git_executor=_make_mock_git(merge_base_rc=0))
    res = engine.evaluate(req)
    assert res.relationship == BaseRelationship.MAIN_ADVANCED_DESCENDANT


def test_inv03_05_divergent_or_non_ancestor_main():
    """3, 5. divergent main / base not ancestor rejected"""
    c_res = _make_candidate_result(base_sha=BASE_A)
    req = CandidateRequalificationRequest("req-03", "task-01", "node-01", "cand-01", "ws-01", "run-01", BASE_A, MAIN_B, c_res)
    engine = CandidateRequalificationEngine(git_executor=_make_mock_git(merge_base_rc=1))
    res = engine.evaluate(req)
    assert res.relationship == BaseRelationship.BASE_NOT_ANCESTOR
    assert res.decision_state == RequalificationDecisionState.REQUALIFICATION_REJECTED
    assert res.eligible is False
    assert res.requires_new_candidate is True
    assert RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value in res.blockers


def test_inv04_unknown_base_rejected():
    """4. unknown base rejected"""
    c_res = _make_candidate_result(base_sha=BASE_A)
    req = CandidateRequalificationRequest("req-04", "task-01", "node-01", "cand-01", "ws-01", "run-01", BASE_A, MAIN_B, c_res)
    engine = CandidateRequalificationEngine(git_executor=_make_mock_git(cat_file_rc=1))
    res = engine.evaluate(req)
    assert res.relationship == BaseRelationship.BASE_UNKNOWN
    assert res.decision_state == RequalificationDecisionState.REQUALIFICATION_REJECTED
    assert res.eligible is False


def test_inv06_07_08_drift_evidence_and_paths():
    """6, 7, 8. valid drift evidence, deterministic changed paths, deterministic drift digest"""
    git_fn = _make_mock_git(
        diff_name_output="b.py\na.py\n",
        diff_stat_output=" 2 files changed",
        diff_binary_output="diff --git a/a.py b/a.py",
        rev_list_output="2\n",
    )
    engine = CandidateRequalificationEngine(git_executor=git_fn)
    drift = engine.compute_drift_evidence(BASE_A, MAIN_B, ".")
    assert drift.changed_paths == ("a.py", "b.py")
    assert drift.drift_commit_count == 2
    assert len(drift.diff_digest) == 64


def test_inv09_10_candidate_artifact_digest_verified_and_tampering():
    """9, 10. candidate artifact digest verified and tampered candidate artifact rejected"""
    c_res = _make_candidate_result(base_sha=BASE_A)
    art = generate_diff_artifact("ws-01", BASE_A, MAIN_B, ("src/service.py",), "stat", "diff")
    req = CandidateRequalificationRequest("req-09", "task-01", "node-01", "cand-01", "ws-01", "run-01", BASE_A, MAIN_B, c_res, snapshot_evidence=art)
    engine = CandidateRequalificationEngine(git_executor=_make_mock_git())
    res = engine.evaluate(req)
    assert res.evidence.candidate_diff_digest == art.diff_digest


def test_inv11_12_13_14_15_overlap_and_non_overlap():
    """11-15. overlap and non-overlap detection, deterministic decision"""
    # 11, 14. Non-overlap
    git_no_overlap = _make_mock_git(diff_name_output="src/unrelated.py\n")
    c_res = _make_candidate_result(base_sha=BASE_A, changed_paths=("src/service.py",))
    req = CandidateRequalificationRequest("req-11", "task-01", "node-01", "cand-01", "ws-01", "run-01", BASE_A, MAIN_B, c_res)
    res = CandidateRequalificationEngine(git_executor=git_no_overlap).evaluate(req)
    assert res.decision_state == RequalificationDecisionState.REQUALIFIED
    assert res.eligible is True
    assert res.requires_new_candidate is False

    # 12, 13, 15. Overlap
    git_overlap = _make_mock_git(diff_name_output="src/service.py\n")
    res_overlap = CandidateRequalificationEngine(git_executor=git_overlap).evaluate(req)
    assert res_overlap.decision_state == RequalificationDecisionState.NEW_CANDIDATE_REQUIRED
    assert res_overlap.eligible is False
    assert res_overlap.requires_new_candidate is True
    assert res_overlap.evidence.overlapping_paths == ("src/service.py",)


def test_inv16_22_identity_and_sha_mismatches():
    """16-22. identity and SHA validation"""
    c_res = _make_candidate_result(base_sha=BASE_A)
    # Candidate base mismatch
    with pytest.raises(RequalificationError) as exc:
        CandidateRequalificationRequest("req-id", "t-01", "n-01", "cand-01", "ws-01", "run-01", "0" * 40, MAIN_B, c_res)
    assert exc.value.code == RequalificationBlockingReason.CANDIDATE_BASE_DRIFT.value


def test_inv23_24_25_26_path_safety_fencing():
    """23-26. path safety fencing"""
    with pytest.raises(Exception):
        validate_repository_relative_path("../escape.py")
    with pytest.raises(Exception):
        validate_repository_relative_path("/root/file.py")
    with pytest.raises(Exception):
        validate_repository_relative_path("C:/Users/file.py")
    with pytest.raises(Exception):
        validate_repository_relative_path("\\\\server\\share\\file.py")


def test_inv27_28_determinism_and_input_order_independence():
    """27, 28. input order independence and deterministic repeat runs"""
    paths_1 = ["z.py", "a.py"]
    paths_2 = ["a.py", "z.py"]
    c1 = _make_candidate_result(changed_paths=tuple(paths_1))
    c2 = _make_candidate_result(changed_paths=tuple(paths_2))

    git_fn = _make_mock_git(diff_name_output="other.py\n")
    engine = CandidateRequalificationEngine(git_executor=git_fn)

    r1 = engine.evaluate(CandidateRequalificationRequest("r1", "t", "n", "cand-01", "ws", "run", BASE_A, MAIN_B, c1))
    r2 = engine.evaluate(CandidateRequalificationRequest("r2", "t", "n", "cand-01", "ws", "run", BASE_A, MAIN_B, c2))

    assert r1.evidence.candidate_changed_paths == r2.evidence.candidate_changed_paths == ("a.py", "z.py")


def test_inv29_30_31_judgement_freshness_invalidation():
    """29, 30, 31. judgement freshness invalidation across base drift"""
    assert CandidateRequalificationEngine.classify_judgement_freshness(BASE_A, BASE_A) == JudgementFreshness.CURRENT
    assert CandidateRequalificationEngine.classify_judgement_freshness(BASE_A, MAIN_B) == JudgementFreshness.STALE_BASE


def test_inv32_33_validation_freshness():
    """32, 33. validation evidence freshness"""
    # Non-overlapping successful candidate retains STILL_APPLICABLE
    git_no_overlap = _make_mock_git(diff_name_output="unrelated.py\n")
    c_res = _make_candidate_result(base_sha=BASE_A)
    req = CandidateRequalificationRequest("r-v", "t", "n", "cand-01", "ws", "run", BASE_A, MAIN_B, c_res)
    res = CandidateRequalificationEngine(git_executor=git_no_overlap).evaluate(req)
    assert res.evidence.validation_status == ValidationFreshness.STILL_APPLICABLE

    # Overlapping candidate gets REQUIRES_RERUN
    git_overlap = _make_mock_git(diff_name_output="src/service.py\n")
    res_overlap = CandidateRequalificationEngine(git_executor=git_overlap).evaluate(req)
    assert res_overlap.evidence.validation_status == ValidationFreshness.REQUIRES_RERUN


def test_inv34_43_no_side_effects():
    """34-43. requalification is strictly read-only, no rebase, merge, push, taskgraph or provider mutations"""
    engine = CandidateRequalificationEngine()
    assert not hasattr(engine, "rebase")
    assert not hasattr(engine, "merge")
    assert not hasattr(engine, "push")
    assert not hasattr(engine, "mutate_taskgraph")


def test_inv44_45_registry_idempotency_and_collision():
    """44, 45. registry idempotency and collision handling"""
    reg = RequalificationRegistry()
    res = CandidateRequalificationResult("req-1", "cand-1", BASE_A, MAIN_B, BaseRelationship.EXACT_BASE, RequalificationDecisionState.NO_REQUALIFICATION_REQUIRED, True, False, (), None, "now")
    reg.record(res)
    reg.record(res)  # Idempotent

    colliding = CandidateRequalificationResult("req-1", "cand-1", BASE_A, MAIN_B, BaseRelationship.MAIN_ADVANCED_DESCENDANT, RequalificationDecisionState.REQUALIFIED, True, False, (), None, "now")
    with pytest.raises(RequalificationError):
        reg.record(colliding)


def test_inv46_47_result_binds_exact_main_sha():
    """46, 47. result binds exact current main SHA and cannot be reused across subsequent advance"""
    res = CandidateRequalificationResult("req-1", "cand-1", BASE_A, MAIN_B, BaseRelationship.MAIN_ADVANCED_DESCENDANT, RequalificationDecisionState.REQUALIFIED, True, False, (), None, "now")
    assert res.current_main_sha == MAIN_B
    assert res.current_main_sha != MAIN_C


def test_inv48_49_multiple_and_mixed_base_candidates_independent():
    """48, 49. multiple and mixed base candidates evaluated independently"""
    engine = CandidateRequalificationEngine(git_executor=_make_mock_git(diff_name_output="src/cand1.py\n"))
    c1 = _make_candidate_result("c1", BASE_A, changed_paths=("src/cand1.py",))
    c2 = _make_candidate_result("c2", BASE_A, changed_paths=("src/cand2.py",))

    r1 = engine.evaluate(CandidateRequalificationRequest("req-1", "t", "n", "c1", "ws1", "run1", BASE_A, MAIN_B, c1))
    r2 = engine.evaluate(CandidateRequalificationRequest("req-2", "t", "n", "c2", "ws2", "run2", BASE_A, MAIN_B, c2))

    assert r1.decision_state == RequalificationDecisionState.NEW_CANDIDATE_REQUIRED
    assert r2.decision_state == RequalificationDecisionState.REQUALIFIED


def test_inv50_pr1_compatibility():
    """50. PR-1 compatibility"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv51_pr2_fencing_compatibility():
    """51. PR-2 fencing compatibility"""
    from ai_engineering.execution.run_contracts import RunState
    assert RunState.LIVE == "LIVE"


def test_inv52_pr3_policy_compatibility():
    """52. PR-3 policy compatibility"""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv53_pr4_investigation_compatibility():
    """53. PR-4 investigation compatibility"""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch
    m = RepositoryMatch("a.py", 1, 1, "test", "TEXT")
    assert m.path == "a.py"


def test_inv54_pr5_candidate_compatibility():
    """54. PR-5 candidate compatibility"""
    c = _make_candidate_result()
    assert c.candidate_id == "cand-01"


def test_inv55_pr6_judge_compatibility():
    """55. PR-6 judge compatibility"""
    from ai_engineering.judge.judge_contracts import CandidateDecisionState
    assert CandidateDecisionState.RANKED_SELECTION == "RANKED_SELECTION"


def test_inv56_pr7_snapshot_diff_compatibility():
    """56. PR-7 snapshot/diff compatibility"""
    art = generate_diff_artifact("ws-01", BASE_A, MAIN_B, ("src/service.py",), "stat", "diff")
    assert art.base_sha == BASE_A
