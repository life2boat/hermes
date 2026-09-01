"""Unit tests for Candidate Requalification contracts and serialization."""

from __future__ import annotations

import pytest

from ai_engineering.candidates.candidate_contracts import CandidateResult, CandidateState
from ai_engineering.requalification.requalification_contracts import (
    REQUALIFICATION_CONTRACT_VERSION,
    BaseRelationship,
    CandidateRequalificationRequest,
    CandidateRequalificationResult,
    DriftEvidence,
    JudgementFreshness,
    RequalificationDecisionState,
    RequalificationError,
    RequalificationEvidence,
    ValidationFreshness,
)

BASE_A = "7334916be325e817fb3d35710aa7c547a9c10040"
MAIN_B = "8888888888888888888888888888888888888888"


def _make_candidate_result(
    candidate_id: str = "cand-01",
    base_sha: str = BASE_A,
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
        changed_paths=("src/service.py",),
        diff_summary="",
        validation_results=(),
        state=CandidateState.COMPLETED,
        blockers=(),
        completed_at="2026-09-01T00:00:00Z",
        success=success,
    )


def test_drift_evidence_serialization():
    ev = DriftEvidence(
        candidate_base_sha=BASE_A,
        current_main_sha=MAIN_B,
        changed_paths=("src/models.py",),
        diff_stat="1 file changed, 1 insertion(+)",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        drift_commit_count=1,
    )
    d = ev.to_dict()
    assert d["candidate_base_sha"] == BASE_A
    assert d["current_main_sha"] == MAIN_B
    restored = DriftEvidence.from_dict(d)
    assert restored == ev


def test_requalification_evidence_serialization():
    ev = RequalificationEvidence(
        candidate_base_sha=BASE_A,
        current_main_sha=MAIN_B,
        drift_changed_paths=("src/models.py",),
        candidate_changed_paths=("src/service.py",),
        overlapping_paths=(),
        drift_diff_digest="1" * 64,
        candidate_diff_digest="2" * 64,
        validation_status=ValidationFreshness.STILL_APPLICABLE,
    )
    d = ev.to_dict()
    assert d["validation_status"] == "STILL_APPLICABLE"
    restored = RequalificationEvidence.from_dict(d)
    assert restored == ev


def test_requalification_result_serialization():
    res = CandidateRequalificationResult(
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
    d = res.to_dict()
    assert d["schema_version"] == REQUALIFICATION_CONTRACT_VERSION
    assert d["relationship"] == "MAIN_ADVANCED_DESCENDANT"
    assert d["decision_state"] == "REQUALIFIED"

    restored = CandidateRequalificationResult.from_dict(d)
    assert restored == res
    assert CandidateRequalificationResult.from_json(res.to_json()) == res
