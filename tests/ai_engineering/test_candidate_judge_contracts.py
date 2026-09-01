"""Unit tests for Candidate Judge contracts, serialization, and input validations."""

from __future__ import annotations

import math
import pytest

from ai_engineering.candidates.candidate_contracts import (
    CandidateResult,
    CandidateState,
    ValidationCommandResult,
)
from ai_engineering.judge.judge_contracts import (
    CandidateDecisionState,
    CandidateHardGateResult,
    CandidateJudgeError,
    CandidateJudgeRequest,
    CandidateJudgeResult,
    CandidateJudgement,
    CandidateSemanticScore,
    JudgeBlockingReason,
)


def _make_candidate_result(
    candidate_id: str,
    base_sha: str = "2e239d4688ee235d1d3e62781d9607f4742713b4",
    success: bool = True,
    state: CandidateState = CandidateState.COMPLETED,
    blockers: tuple[str, ...] = (),
    failed_validations: bool = False,
) -> CandidateResult:
    val_results = (
        ValidationCommandResult(("pytest",), 0 if not failed_validations else 1, "", "", not failed_validations),
    )
    return CandidateResult(
        candidate_id=candidate_id,
        task_id="task-01",
        node_id="node-01",
        workspace_id=f"ws-{candidate_id}",
        run_id=f"run-{candidate_id}",
        base_sha=base_sha,
        branch=f"codex/candidate/task-01/{candidate_id}",
        changed_paths=("service.py",),
        diff_summary="diff --git a/service.py b/service.py",
        validation_results=val_results,
        state=state,
        blockers=blockers,
        completed_at="2026-09-01T00:00:00Z",
        success=success,
    )


def test_candidate_semantic_score_validation():
    # Valid
    score = CandidateSemanticScore("cand-1", 0.85, "Good", "eval-1")
    assert score.score == 0.85
    assert score.to_dict()["score"] == 0.85

    # Out of bounds
    with pytest.raises(CandidateJudgeError) as exc:
        CandidateSemanticScore("cand-1", -0.1, "Negative", "eval-1")
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_SEMANTIC_SCORE_INVALID.value

    with pytest.raises(CandidateJudgeError) as exc:
        CandidateSemanticScore("cand-1", 1.5, "Too high", "eval-1")
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_SEMANTIC_SCORE_INVALID.value

    # NaN / Inf
    with pytest.raises(CandidateJudgeError):
        CandidateSemanticScore("cand-1", float("nan"), "NaN", "eval-1")

    with pytest.raises(CandidateJudgeError):
        CandidateSemanticScore("cand-1", float("inf"), "Inf", "eval-1")


def test_candidate_hard_gate_result_serialization():
    gate = CandidateHardGateResult("cand-1", "REQUIRED_VALIDATIONS_PASSED", True)
    d = gate.to_dict()
    assert d["passed"] is True
    restored = CandidateHardGateResult.from_dict(d)
    assert restored == gate

    # Failed gate requires blocker
    with pytest.raises(CandidateJudgeError):
        CandidateHardGateResult("cand-1", "GATE_FAIL", False, blocker=None)


def test_candidate_judgement_invariants():
    gate = CandidateHardGateResult("cand-1", "GATE_1", True)
    score = CandidateSemanticScore("cand-1", 0.9, "Great", "eval-1")

    # Hard gate pass + eligible
    j = CandidateJudgement("cand-1", True, True, score, 1, (), "Rank 1", (gate,))
    assert j.eligible is True
    assert j.to_dict()["rank"] == 1

    # Invariant: hard gate fail CANNOT be eligible
    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgement("cand-1", False, True, None, 1, ("FAIL",), "Bad", (gate,))
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_HARD_VALIDATION_FAILED.value

    # Invariant: hard gate fail CANNOT have semantic score
    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgement("cand-1", False, False, score, None, ("FAIL",), "Bad", (gate,))
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_HARD_VALIDATION_FAILED.value


def test_candidate_judge_request_base_drift():
    c1 = _make_candidate_result("c1", base_sha="2e239d4688ee235d1d3e62781d9607f4742713b4")
    c2 = _make_candidate_result("c2", base_sha="0000000000000000000000000000000000000000")

    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgeRequest(
            judge_id="j-1",
            task_id="task-01",
            node_id="node-01",
            base_sha="2e239d4688ee235d1d3e62781d9607f4742713b4",
            candidates=(c1, c2),
        )
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_BASE_DRIFT.value


def test_candidate_judge_request_duplicate_candidate_id():
    c1 = _make_candidate_result("c1")
    c2 = _make_candidate_result("c1")

    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgeRequest(
            judge_id="j-1",
            task_id="task-01",
            node_id="node-01",
            base_sha="2e239d4688ee235d1d3e62781d9607f4742713b4",
            candidates=(c1, c2),
        )
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_ID_COLLISION.value


def test_candidate_judge_result_serialization():
    gate = CandidateHardGateResult("c1", "REQUIRED_VALIDATIONS_PASSED", True)
    score = CandidateSemanticScore("c1", 0.92, "High quality", "eval-1")
    j = CandidateJudgement("c1", True, True, score, 1, (), "Selected", (gate,))
    res = CandidateJudgeResult(
        judge_id="j-1",
        task_id="t-1",
        node_id="n-1",
        base_sha="2e239d4688ee235d1d3e62781d9607f4742713b4",
        judgements=(j,),
        selected_candidate_id="c1",
        decision_state=CandidateDecisionState.SINGLE_ELIGIBLE,
        rationale="Single eligible",
    )
    d = res.to_dict()
    assert d["selected_candidate_id"] == "c1"
    assert d["decision_state"] == "SINGLE_ELIGIBLE"

    restored = CandidateJudgeResult.from_dict(d)
    assert restored.selected_candidate_id == "c1"
    assert restored.decision_state == CandidateDecisionState.SINGLE_ELIGIBLE
    assert len(restored.judgements) == 1
    assert restored.judgements[0].candidate_id == "c1"


def test_candidate_judge_result_selected_must_be_eligible():
    gate = CandidateHardGateResult("c1", "GATE_1", False, blocker="FAIL")
    j = CandidateJudgement("c1", False, False, None, None, ("FAIL",), "Failed", (gate,))

    with pytest.raises(CandidateJudgeError) as exc:
        CandidateJudgeResult(
            judge_id="j-1",
            task_id="t-1",
            node_id="n-1",
            base_sha="2e239d4688ee235d1d3e62781d9607f4742713b4",
            judgements=(j,),
            selected_candidate_id="c1",
            decision_state=CandidateDecisionState.SINGLE_ELIGIBLE,
        )
    assert exc.value.code == JudgeBlockingReason.CANDIDATE_JUDGE_INPUT_INVALID.value


def test_candidate_judge_event_sink_emission():
    from ai_engineering.judge.candidate_judge import CandidateJudge
    from ai_engineering.judge.semantic_evaluator import DeterministicSemanticEvaluator

    events = []

    def sink(event_type, details):
        events.append((event_type, details))

    c1 = _make_candidate_result("c1")
    req = CandidateJudgeRequest(
        judge_id="j-events",
        task_id="task-01",
        node_id="node-01",
        base_sha="2e239d4688ee235d1d3e62781d9607f4742713b4",
        candidates=(c1,),
    )
    judge = CandidateJudge(semantic_evaluator=DeterministicSemanticEvaluator(), event_sink=sink)
    res = judge.judge(req)

    assert res.decision_state == CandidateDecisionState.SINGLE_ELIGIBLE
    assert len(events) >= 3
    event_names = [e[0] for e in events]
    assert "CANDIDATE_JUDGE_STARTED" in event_names
    assert "CANDIDATE_HARD_GATE_PASSED" in event_names
    assert "CANDIDATE_SELECTED" in event_names
