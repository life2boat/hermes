"""Unit tests for CandidateRequalificationEngine and RequalificationRegistry."""

from __future__ import annotations

import pytest

from ai_engineering.candidates.candidate_contracts import CandidateResult, CandidateState
from ai_engineering.requalification.requalification_contracts import (
    BaseRelationship,
    CandidateRequalificationRequest,
    CandidateRequalificationResult,
    JudgementFreshness,
    RequalificationBlockingReason,
    RequalificationDecisionState,
    RequalificationError,
)
from ai_engineering.requalification.requalification_engine import (
    CandidateRequalificationEngine,
)
from ai_engineering.requalification.requalification_registry import (
    RequalificationRegistry,
)
from ai_engineering.workspaces.diff_artifacts import generate_diff_artifact

BASE_A = "7334916be325e817fb3d35710aa7c547a9c10040"
MAIN_B = "8888888888888888888888888888888888888888"


def _make_candidate_result(
    candidate_id: str = "cand-01",
    base_sha: str = BASE_A,
    changed_paths: tuple[str, ...] = ("src/service.py",),
    success: bool = True,
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
        blockers=(),
        completed_at="2026-09-01T00:00:00Z",
        success=success,
    )


def test_requalification_exact_base_no_requalification_required():
    c_res = _make_candidate_result(base_sha=BASE_A)
    req = CandidateRequalificationRequest(
        requalification_id="req-01",
        task_id="task-01",
        node_id="node-01",
        candidate_id="cand-01",
        workspace_id="ws-01",
        run_id="run-01",
        candidate_base_sha=BASE_A,
        current_main_sha=BASE_A,
        candidate_result=c_res,
    )
    engine = CandidateRequalificationEngine()
    res = engine.evaluate(req)
    assert res.relationship == BaseRelationship.EXACT_BASE
    assert res.decision_state == RequalificationDecisionState.NO_REQUALIFICATION_REQUIRED
    assert res.eligible is True
    assert res.requires_new_candidate is False


def test_requalification_non_overlapping_requalified():
    def mock_git(cmd: list[str], cwd: str) -> tuple[int, str, str]:
        if "cat-file" in cmd:
            return 0, "", ""
        if "merge-base" in cmd:
            return 0, "", ""
        if "diff" in cmd:
            if "--name-only" in cmd:
                return 0, "src/other.py\n", ""
            if "--stat" in cmd:
                return 0, " 1 file changed", ""
            if "--binary" in cmd:
                return 0, "diff --git a/src/other.py b/src/other.py", ""
        if "rev-list" in cmd:
            return 0, "1\n", ""
        return 0, "", ""

    c_res = _make_candidate_result(base_sha=BASE_A, changed_paths=("src/service.py",))
    art = generate_diff_artifact("ws-01", BASE_A, MAIN_B, ("src/service.py",), "stat", "diff")
    req = CandidateRequalificationRequest(
        requalification_id="req-02",
        task_id="task-01",
        node_id="node-01",
        candidate_id="cand-01",
        workspace_id="ws-01",
        run_id="run-01",
        candidate_base_sha=BASE_A,
        current_main_sha=MAIN_B,
        candidate_result=c_res,
        snapshot_evidence=art,
    )
    engine = CandidateRequalificationEngine(git_executor=mock_git)
    res = engine.evaluate(req)
    assert res.relationship == BaseRelationship.MAIN_ADVANCED_DESCENDANT
    assert res.decision_state == RequalificationDecisionState.REQUALIFIED
    assert res.eligible is True
    assert res.requires_new_candidate is False
    assert res.evidence is not None
    assert len(res.evidence.overlapping_paths) == 0


def test_requalification_overlapping_requires_new_candidate():
    def mock_git(cmd: list[str], cwd: str) -> tuple[int, str, str]:
        if "cat-file" in cmd:
            return 0, "", ""
        if "merge-base" in cmd:
            return 0, "", ""
        if "diff" in cmd:
            if "--name-only" in cmd:
                return 0, "src/service.py\n", ""
            if "--stat" in cmd:
                return 0, " 1 file changed", ""
            if "--binary" in cmd:
                return 0, "diff --git a/src/service.py b/src/service.py", ""
        if "rev-list" in cmd:
            return 0, "1\n", ""
        return 0, "", ""

    c_res = _make_candidate_result(base_sha=BASE_A, changed_paths=("src/service.py",))
    req = CandidateRequalificationRequest(
        requalification_id="req-03",
        task_id="task-01",
        node_id="node-01",
        candidate_id="cand-01",
        workspace_id="ws-01",
        run_id="run-01",
        candidate_base_sha=BASE_A,
        current_main_sha=MAIN_B,
        candidate_result=c_res,
    )
    engine = CandidateRequalificationEngine(git_executor=mock_git)
    res = engine.evaluate(req)
    assert res.relationship == BaseRelationship.MAIN_ADVANCED_DESCENDANT
    assert res.decision_state == RequalificationDecisionState.NEW_CANDIDATE_REQUIRED
    assert res.eligible is False
    assert res.requires_new_candidate is True
    assert RequalificationBlockingReason.CANDIDATE_DRIFT_OVERLAP.value in res.blockers


def test_judgement_freshness_classification():
    assert CandidateRequalificationEngine.classify_judgement_freshness(BASE_A, BASE_A) == JudgementFreshness.CURRENT
    assert CandidateRequalificationEngine.classify_judgement_freshness(BASE_A, MAIN_B) == JudgementFreshness.STALE_BASE


def test_requalification_registry_idempotent_and_collision():
    reg = RequalificationRegistry()
    r1 = CandidateRequalificationResult(
        requalification_id="req-01",
        candidate_id="cand-01",
        candidate_base_sha=BASE_A,
        current_main_sha=MAIN_B,
        relationship=BaseRelationship.MAIN_ADVANCED_DESCENDANT,
        decision_state=RequalificationDecisionState.REQUALIFIED,
        eligible=True,
        requires_new_candidate=False,
        blockers=(),
        evidence=None,
        completed_at="2026-09-01T00:00:00Z",
    )
    reg.record(r1)
    reg.record(r1)  # Idempotent
    assert reg.get("req-01") == r1

    r2 = CandidateRequalificationResult(
        requalification_id="req-01",
        candidate_id="cand-01",
        candidate_base_sha=BASE_A,
        current_main_sha=MAIN_B,
        relationship=BaseRelationship.MAIN_ADVANCED_DESCENDANT,
        decision_state=RequalificationDecisionState.NEW_CANDIDATE_REQUIRED,
        eligible=False,
        requires_new_candidate=True,
        blockers=(),
        evidence=None,
        completed_at="2026-09-01T00:00:00Z",
    )
    with pytest.raises(RequalificationError) as exc:
        reg.record(r2)
    assert exc.value.code == RequalificationBlockingReason.REQUALIFICATION_COLLISION.value
