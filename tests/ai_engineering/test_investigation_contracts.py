"""Unit tests for Repository Investigation contracts and serialization."""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from ai_engineering.investigation.investigation_contracts import (
    INVESTIGATION_CONTRACT_VERSION,
    INVESTIGATION_RESULT_SCHEMA_VERSION,
    InvestigationBlockingReason,
    InvestigationError,
    RepositoryInvestigationAggregate,
    RepositoryInvestigationBatch,
    RepositoryInvestigationRequest,
    RepositoryInvestigationResult,
    RepositoryMatch,
)
from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy


def test_repository_match_valid_and_serialization():
    m = RepositoryMatch(
        path="ai_engineering/contracts.py",
        line_start=10,
        line_end=15,
        snippet="class TaskClass(StrEnum):",
        match_kind="TEXT",
    )
    d = m.to_dict()
    assert d["path"] == "ai_engineering/contracts.py"
    assert d["line_start"] == 10
    reconstructed = RepositoryMatch.from_dict(d)
    assert reconstructed == m


def test_repository_match_path_escape_rejected():
    with pytest.raises(InvestigationError) as exc:
        RepositoryMatch(
            path="/etc/passwd",
            line_start=1,
            line_end=1,
            snippet="root:x:0:0",
        )
    assert exc.value.code == InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value

    with pytest.raises(InvestigationError):
        RepositoryMatch(
            path="../secrets.env",
            line_start=1,
            line_end=1,
            snippet="SECRET=1",
        )


def test_repository_investigation_result_serialization():
    res = RepositoryInvestigationResult(
        investigation_id="inv-001",
        run_id="run-001",
        base_sha="5cb25e2a5a0bedada2e1e918fa52470e6aefd797",
        matches=(
            RepositoryMatch(
                path="ai_engineering/contracts.py",
                line_start=1,
                line_end=1,
                snippet="test",
            ),
        ),
        summary="Test summary",
        completed_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    raw = res.to_json()
    reconstructed = RepositoryInvestigationResult.from_json(raw)
    assert reconstructed.investigation_id == res.investigation_id
    assert reconstructed.matches == res.matches
    assert reconstructed.success is True


def test_repository_investigation_batch_strategy_validation():
    with pytest.raises(InvestigationError) as exc:
        RepositoryInvestigationBatch(
            batch_id="batch-001",
            task_id="task-001",
            base_sha="5cb25e2a5a0bedada2e1e918fa52470e6aefd797",
            strategy=ParallelizationStrategy.CANDIDATE,  # Non-preparatory forbidden
        )
    assert exc.value.code == InvestigationBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value
