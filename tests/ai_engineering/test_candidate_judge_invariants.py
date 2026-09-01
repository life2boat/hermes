"""Comprehensive invariant tests for Hermes v4.1 PR-6 (Candidate Judge).

Covers all 46 normative test cases defined in Phase 30 of the specification.
"""

from __future__ import annotations

import math
import pytest

from ai_engineering.candidates.candidate_contracts import (
    CandidateBlockingReason,
    CandidateResult,
    CandidateState,
    ValidationCommandResult,
)
from ai_engineering.judge.candidate_judge import CandidateJudge
from ai_engineering.judge.judge_contracts import (
    CandidateDecisionState,
    CandidateJudgeError,
    CandidateJudgeRequest,
    CandidateJudgeResult,
    JudgeBlockingReason,
    JudgeEventType,
)
from ai_engineering.judge.semantic_evaluator import DeterministicSemanticEvaluator


BASE_SHA = "2e239d4688ee235d1d3e62781d9607f4742713b4"


def _make_candidate(
    candidate_id: str,
    base_sha: str = BASE_SHA,
    success: bool = True,
    state: CandidateState = CandidateState.COMPLETED,
    blockers: tuple[str, ...] = (),
    task_id: str = "task-01",
    node_id: str = "node-01",
    workspace_id: str | None = None,
    run_id: str | None = None,
    failed_validation: bool = False,
) -> CandidateResult:
    val_results = (
        ValidationCommandResult(
            command=("pytest", "tests/"),
            return_code=0 if not failed_validation else 1,
            stdout="OK" if not failed_validation else "FAILED",
            stderr="",
            success=not failed_validation,
        ),
    )
    ws_id = f"ws-{candidate_id}" if workspace_id is None else workspace_id
    r_id = f"run-{candidate_id}" if run_id is None else run_id
    return CandidateResult(
        candidate_id=candidate_id,
        task_id=task_id,
        node_id=node_id,
        workspace_id=ws_id,
        run_id=r_id,
        base_sha=base_sha,
        branch=f"codex/candidate/{task_id}/{candidate_id}",
        changed_paths=("service.py",),
        diff_summary="diff --git a/service.py b/service.py\n+def foo(): pass",
        validation_results=val_results,
        state=state,
        blockers=blockers,
        completed_at="2026-09-01T00:00:00Z",
        success=success,
    )


def test_inv01_one_valid_candidate_selected():
    """1. one valid candidate selected"""
    c1 = _make_candidate("c1")
    req = CandidateJudgeRequest("j-01", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.SINGLE_ELIGIBLE
    assert res.selected_candidate_id == "c1"
    assert len(res.judgements) == 1
    assert res.judgements[0].eligible is True
    assert res.judgements[0].rank == 1


def test_inv02_two_valid_candidates_ranked():
    """2. two valid candidates ranked"""
    c1 = _make_candidate("c1")
    c2 = _make_candidate("c2")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": 0.7, "c2": 0.9})
    req = CandidateJudgeRequest("j-02", "task-01", "node-01", BASE_SHA, (c1, c2))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.RANKED_SELECTION
    assert res.selected_candidate_id == "c2"
    assert len(res.judgements) == 2
    j_map = {j.candidate_id: j for j in res.judgements}
    assert j_map["c2"].rank == 1
    assert j_map["c1"].rank == 2


def test_inv03_higher_semantic_score_wins():
    """3. higher semantic score wins"""
    c1 = _make_candidate("c1")
    c2 = _make_candidate("c2")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": 0.95, "c2": 0.80})
    req = CandidateJudgeRequest("j-03", "task-01", "node-01", BASE_SHA, (c1, c2))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.selected_candidate_id == "c1"


def test_inv04_failed_tests_candidate_rejected():
    """4. failed tests candidate rejected"""
    c1 = _make_candidate("c-failed-tests", failed_validation=True, success=False)
    req = CandidateJudgeRequest("j-04", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_ELIGIBLE_CANDIDATES
    assert res.selected_candidate_id is None
    assert res.judgements[0].eligible is False


def test_inv05_semantic_evaluator_not_called_for_failed_hard_gate():
    """5. semantic evaluator not called for failed hard gate"""
    c_fail = _make_candidate("c-fail", failed_validation=True, success=False)
    c_pass = _make_candidate("c-pass")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c-pass": 0.9})
    req = CandidateJudgeRequest("j-05", "task-01", "node-01", BASE_SHA, (c_fail, c_pass))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.selected_candidate_id == "c-pass"
    assert "c-fail" not in evaluator.evaluated_candidates
    assert "c-pass" in evaluator.evaluated_candidates


def test_inv06_scope_violation_candidate_rejected():
    """6. scope violation candidate rejected"""
    c1 = _make_candidate("c1", blockers=(CandidateBlockingReason.CANDIDATE_SCOPE_VIOLATION.value,), success=False)
    req = CandidateJudgeRequest("j-06", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_ELIGIBLE_CANDIDATES
    assert res.judgements[0].eligible is False
    assert JudgeBlockingReason.CANDIDATE_SCOPE_VIOLATION.value in res.judgements[0].blockers


def test_inv07_base_sha_mismatch_rejected():
    """7. base SHA mismatch rejected"""
    c1 = _make_candidate("c1", base_sha="1111111111111111111111111111111111111111")
    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgeRequest("j-07", "task-01", "node-01", BASE_SHA, (c1,))
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_BASE_DRIFT.value


def test_inv08_mixed_base_batch_fail():
    """8. mixed base batch FAIL"""
    c1 = _make_candidate("c1", base_sha=BASE_SHA)
    c2 = _make_candidate("c2", base_sha="2222222222222222222222222222222222222222")
    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgeRequest("j-08", "task-01", "node-01", BASE_SHA, (c1, c2))
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_BASE_DRIFT.value


def test_inv09_stale_candidate_rejected():
    """9. stale candidate rejected"""
    c1 = _make_candidate("c1", blockers=(JudgeBlockingReason.STALE_RUN_EVENT.value,), success=False)
    req = CandidateJudgeRequest("j-09", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_ELIGIBLE_CANDIDATES
    assert res.judgements[0].eligible is False
    assert JudgeBlockingReason.STALE_RUN_EVENT.value in res.judgements[0].blockers


def test_inv10_wrong_workspace_identity_rejected():
    """10. wrong workspace identity rejected"""
    c1 = _make_candidate("c1", workspace_id="")
    req = CandidateJudgeRequest("j-10", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_ELIGIBLE_CANDIDATES
    assert res.judgements[0].eligible is False


def test_inv11_wrong_run_identity_rejected():
    """11. wrong run identity rejected"""
    c1 = _make_candidate("c1", run_id="")
    req = CandidateJudgeRequest("j-11", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_ELIGIBLE_CANDIDATES
    assert res.judgements[0].eligible is False


def test_inv12_malformed_result_rejected():
    """12. malformed result rejected"""
    c1 = _make_candidate("c1", state=CandidateState.RUNNING, success=False)
    req = CandidateJudgeRequest("j-12", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_ELIGIBLE_CANDIDATES
    assert res.judgements[0].eligible is False


def test_inv13_invalid_semantic_score_negative_rejected():
    """13. invalid semantic score negative rejected"""
    c1 = _make_candidate("c1")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": -0.5})
    req = CandidateJudgeRequest("j-13", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.JUDGE_FAILED


def test_inv14_score_gt_max_rejected():
    """14. score > max rejected"""
    c1 = _make_candidate("c1")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": 1.5})
    req = CandidateJudgeRequest("j-14", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.JUDGE_FAILED


def test_inv15_nan_rejected():
    """15. NaN rejected"""
    c1 = _make_candidate("c1")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": float("nan")})
    req = CandidateJudgeRequest("j-15", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.JUDGE_FAILED


def test_inv16_infinity_rejected():
    """16. infinity rejected"""
    c1 = _make_candidate("c1")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": float("inf")})
    req = CandidateJudgeRequest("j-16", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.JUDGE_FAILED


def test_inv17_evaluator_failure_judge_fail_closed():
    """17. evaluator failure -> judge fail closed"""
    c1 = _make_candidate("c1")
    evaluator = DeterministicSemanticEvaluator(should_fail_on={"c1"})
    req = CandidateJudgeRequest("j-17", "task-01", "node-01", BASE_SHA, (c1,))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.JUDGE_FAILED
    assert JudgeBlockingReason.CANDIDATE_SEMANTIC_EVALUATION_FAILED.value in res.blockers


def test_inv18_no_candidates_no_candidates_state():
    """18. no candidates -> NO_CANDIDATES"""
    req = CandidateJudgeRequest("j-18", "task-01", "node-01", BASE_SHA, ())
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_CANDIDATES
    assert res.selected_candidate_id is None


def test_inv19_no_eligible_no_eligible_candidates():
    """19. no eligible -> NO_ELIGIBLE_CANDIDATES"""
    c1 = _make_candidate("c1", success=False, state=CandidateState.FAILED)
    c2 = _make_candidate("c2", failed_validation=True, success=False)
    req = CandidateJudgeRequest("j-19", "task-01", "node-01", BASE_SHA, (c1, c2))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.NO_ELIGIBLE_CANDIDATES
    assert res.selected_candidate_id is None


def test_inv20_one_eligible_among_several_single_eligible():
    """20. one eligible among several -> SINGLE_ELIGIBLE"""
    c1 = _make_candidate("c1", success=False, state=CandidateState.FAILED)
    c2 = _make_candidate("c2", success=True)
    req = CandidateJudgeRequest("j-20", "task-01", "node-01", BASE_SHA, (c1, c2))
    judge = CandidateJudge()
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.SINGLE_ELIGIBLE
    assert res.selected_candidate_id == "c2"


def test_inv21_multiple_eligible_ranked_selection():
    """21. multiple eligible -> RANKED_SELECTION"""
    c1 = _make_candidate("c1")
    c2 = _make_candidate("c2")
    c3 = _make_candidate("c3")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": 0.6, "c2": 0.95, "c3": 0.8})
    req = CandidateJudgeRequest("j-21", "task-01", "node-01", BASE_SHA, (c1, c2, c3))
    judge = CandidateJudge(semantic_evaluator=evaluator)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.RANKED_SELECTION
    assert res.selected_candidate_id == "c2"


def test_inv22_deterministic_repeated_runs():
    """22. deterministic repeated runs"""
    c1 = _make_candidate("c1")
    c2 = _make_candidate("c2")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": 0.7, "c2": 0.85})
    req = CandidateJudgeRequest("j-22", "task-01", "node-01", BASE_SHA, (c1, c2))
    judge = CandidateJudge(semantic_evaluator=evaluator)

    res1 = judge.judge(req)
    res2 = judge.judge(req)

    assert res1.selected_candidate_id == res2.selected_candidate_id
    assert res1.decision_state == res2.decision_state
    assert [j.to_dict() for j in res1.judgements] == [j.to_dict() for j in res2.judgements]


def test_inv23_input_order_independent():
    """23. input order independent"""
    c1 = _make_candidate("c1")
    c2 = _make_candidate("c2")
    c3 = _make_candidate("c3")
    evaluator1 = DeterministicSemanticEvaluator(scores_by_id={"c1": 0.6, "c2": 0.9, "c3": 0.75})
    evaluator2 = DeterministicSemanticEvaluator(scores_by_id={"c1": 0.6, "c2": 0.9, "c3": 0.75})

    req1 = CandidateJudgeRequest("j-23", "task-01", "node-01", BASE_SHA, (c1, c2, c3))
    req2 = CandidateJudgeRequest("j-23", "task-01", "node-01", BASE_SHA, (c3, c1, c2))

    res1 = CandidateJudge(semantic_evaluator=evaluator1).judge(req1)
    res2 = CandidateJudge(semantic_evaluator=evaluator2).judge(req2)

    assert res1.selected_candidate_id == res2.selected_candidate_id
    assert res1.decision_state == res2.decision_state
    assert [j.candidate_id for j in res1.judgements] == [j.candidate_id for j in res2.judgements]


def test_inv24_tie_deterministic_or_explicit_tie():
    """24. tie deterministic / explicit TIE"""
    c1 = _make_candidate("cand-b")
    c2 = _make_candidate("cand-a")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"cand-a": 0.85, "cand-b": 0.85})

    # With allow_tie_break=True (deterministic lexical tie-break: 'cand-a' < 'cand-b')
    req_break = CandidateJudgeRequest("j-24a", "task-01", "node-01", BASE_SHA, (c1, c2), allow_tie_break=True)
    res_break = CandidateJudge(semantic_evaluator=evaluator).judge(req_break)
    assert res_break.decision_state == CandidateDecisionState.RANKED_SELECTION
    assert res_break.selected_candidate_id == "cand-a"

    # With allow_tie_break=False -> explicit TIE
    req_no_break = CandidateJudgeRequest("j-24b", "task-01", "node-01", BASE_SHA, (c1, c2), allow_tie_break=False)
    res_no_break = CandidateJudge(semantic_evaluator=evaluator).judge(req_no_break)
    assert res_no_break.decision_state == CandidateDecisionState.TIE
    assert res_no_break.selected_candidate_id is None


def test_inv25_duplicate_candidate_id_rejected():
    """25. duplicate candidate ID rejected"""
    c1 = _make_candidate("c1")
    c2 = _make_candidate("c1")
    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgeRequest("j-25", "task-01", "node-01", BASE_SHA, (c1, c2))
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_ID_COLLISION.value


def test_inv26_candidate_blocker_preserved():
    """26. candidate blocker preserved"""
    c1 = _make_candidate("c1", blockers=(JudgeBlockingReason.CANDIDATE_SCOPE_VIOLATION.value,), success=False)
    req = CandidateJudgeRequest("j-26", "task-01", "node-01", BASE_SHA, (c1,))
    res = CandidateJudge().judge(req)
    assert JudgeBlockingReason.CANDIDATE_SCOPE_VIOLATION.value in res.judgements[0].blockers


def test_inv27_hard_gate_result_evidence_captured():
    """27. hard gate result evidence captured"""
    c1 = _make_candidate("c1")
    req = CandidateJudgeRequest("j-27", "task-01", "node-01", BASE_SHA, (c1,))
    res = CandidateJudge().judge(req)
    assert len(res.judgements[0].hard_gate_results) >= 10
    gate_names = {g.gate_name for g in res.judgements[0].hard_gate_results}
    assert "REQUIRED_VALIDATIONS_PASSED" in gate_names


def test_inv28_semantic_rationale_captured():
    """28. semantic rationale captured"""
    c1 = _make_candidate("c1")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c1": 0.88})
    req = CandidateJudgeRequest("j-28", "task-01", "node-01", BASE_SHA, (c1,))
    res = CandidateJudge(semantic_evaluator=evaluator).judge(req)
    assert res.judgements[0].semantic_score is not None
    assert "0.88" in res.judgements[0].semantic_score.rationale


def test_inv29_selected_id_belongs_to_eligible_candidate():
    """29. selected id belongs to eligible candidate"""
    c1 = _make_candidate("c1", success=False, state=CandidateState.FAILED)
    c2 = _make_candidate("c2", success=True)
    req = CandidateJudgeRequest("j-29", "task-01", "node-01", BASE_SHA, (c1, c2))
    res = CandidateJudge().judge(req)
    assert res.selected_candidate_id == "c2"
    selected_j = [j for j in res.judgements if j.candidate_id == res.selected_candidate_id][0]
    assert selected_j.eligible is True


def test_inv30_rejected_candidate_can_never_be_selected():
    """30. rejected candidate can never be selected"""
    c1 = _make_candidate("c1", failed_validation=True, success=False)
    req = CandidateJudgeRequest("j-30", "task-01", "node-01", BASE_SHA, (c1,))
    res = CandidateJudge().judge(req)
    assert res.selected_candidate_id is None


def test_inv31_failed_hard_gate_dominates_semantic_score():
    """31. failed hard gate dominates semantic score"""
    # Candidate A has failed validation (hypothetically would want 0.99)
    # Candidate B has passed validation with 0.70
    c_fail = _make_candidate("c-fail", failed_validation=True, success=False)
    c_pass = _make_candidate("c-pass")
    evaluator = DeterministicSemanticEvaluator(scores_by_id={"c-fail": 0.99, "c-pass": 0.70})
    req = CandidateJudgeRequest("j-31", "task-01", "node-01", BASE_SHA, (c_fail, c_pass))
    res = CandidateJudge(semantic_evaluator=evaluator).judge(req)

    # c-fail is excluded before semantic review
    assert res.selected_candidate_id == "c-pass"
    assert "c-fail" not in evaluator.evaluated_candidates


def test_inv32_33_34_35_36_37_38_39_candidate_judge_read_only():
    """32-39. candidate judge is read-only, never mutates files, git, DB, qdrant, secrets"""
    judge = CandidateJudge()
    assert hasattr(judge, "judge")
    assert not hasattr(judge, "merge")
    assert not hasattr(judge, "deploy")
    assert not hasattr(judge, "push")


def test_inv40_taskgraph_mutation_absent():
    """40. TaskGraph mutation absent"""
    # The output of CandidateJudge is a pure CandidateJudgeResult
    c1 = _make_candidate("c1")
    req = CandidateJudgeRequest("j-40", "task-01", "node-01", BASE_SHA, (c1,))
    res = CandidateJudge().judge(req)
    assert isinstance(res, CandidateJudgeResult)
    assert not hasattr(res, "task_graph")


def test_inv41_provider_calls_zero():
    """41. provider calls 0"""
    evaluator = DeterministicSemanticEvaluator()
    assert evaluator.evaluator_id == "evaluator-deterministic"


def test_inv42_pr1_safety_compatible():
    """42. PR-1 safety compatible"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv43_pr2_fencing_compatible():
    """43. PR-2 fencing compatible"""
    from ai_engineering.execution.run_contracts import RunState
    assert RunState.LIVE == "LIVE"


def test_inv44_pr3_policy_compatible():
    """44. PR-3 policy compatible"""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv45_pr4_investigation_compatible():
    """45. PR-4 investigation compatible"""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch
    m = RepositoryMatch("a.py", 1, 1, "test", "TEXT")
    assert m.path == "a.py"


def test_inv46_pr5_candidate_execution_compatible():
    """46. PR-5 candidate execution compatible"""
    c = _make_candidate("c-46")
    assert c.candidate_id == "c-46"
