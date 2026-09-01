"""Unit tests for Candidate Implementation contracts, serialization, and validations."""

from __future__ import annotations

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
    ValidationCommandResult,
)
from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy


def test_candidate_identity_valid_and_serialization():
    ident = CandidateIdentity(
        candidate_id="cand-01",
        task_id="task-01",
        node_id="node-01",
        base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
        workspace_id="ws-cand-01",
        run_id="run-cand-01",
    )
    d = ident.to_dict()
    assert d["candidate_id"] == "cand-01"
    assert d["base_sha"] == "4badb9cdb434d7fd3b1102829fa89ca8b11415a2"

    restored = CandidateIdentity.from_dict(d)
    assert restored == ident


def test_candidate_identity_invalid_sha_rejected():
    with pytest.raises(CandidateError) as exc:
        CandidateIdentity(
            candidate_id="cand-01",
            task_id="task-01",
            node_id="node-01",
            base_sha="invalid_sha",
            workspace_id="ws-cand-01",
            run_id="run-cand-01",
        )
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value


def test_candidate_implementation_request_path_escape_rejected():
    # Dot-dot escape
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationRequest(
            candidate_id="c1",
            task_id="t1",
            node_id="n1",
            base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
            repository="/tmp/repo",
            implementation_brief="fix",
            allowed_paths=("../escape.py",),
        )
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value

    # Foreign absolute path
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationRequest(
            candidate_id="c1",
            task_id="t1",
            node_id="n1",
            base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
            repository="/tmp/repo",
            implementation_brief="fix",
            allowed_paths=("/etc/passwd",),
        )
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value


def test_candidate_implementation_batch_duplicate_candidate_id():
    req1 = CandidateImplementationRequest(
        candidate_id="cand-1",
        task_id="task-1",
        node_id="node-1",
        base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
        repository="/tmp/repo",
        implementation_brief="fix 1",
        allowed_paths=("src/a.py",),
    )
    req2 = CandidateImplementationRequest(
        candidate_id="cand-1",
        task_id="task-1",
        node_id="node-2",
        base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
        repository="/tmp/repo",
        implementation_brief="fix 2",
        allowed_paths=("src/b.py",),
    )
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationBatch(
            batch_id="b-1",
            task_id="task-1",
            node_id="node-1",
            base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
            candidates=(req1, req2),
        )
    assert exc.value.code == CandidateBlockingReason.CANDIDATE_ID_COLLISION.value


def test_candidate_implementation_batch_hard_ceiling_exceeded():
    cands = [
        CandidateImplementationRequest(
            candidate_id=f"c-{i}",
            task_id="task-1",
            node_id="node-1",
            base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
            repository="/tmp/repo",
            implementation_brief=f"fix {i}",
            allowed_paths=("src/a.py",),
        )
        for i in range(4)
    ]
    with pytest.raises(CandidateError) as exc:
        CandidateImplementationBatch(
            batch_id="b-1",
            task_id="task-1",
            node_id="node-1",
            base_sha="4badb9cdb434d7fd3b1102829fa89ca8b11415a2",
            candidates=tuple(cands),
        )
    assert exc.value.code == CandidateBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value
