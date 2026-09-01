"""Unit tests for Parallelization Contracts and ConcurrencyBudget."""

from __future__ import annotations

import json
import pytest

from ai_engineering.parallel.parallel_contracts import (
    DEFAULT_MAX_CANDIDATES,
    GLOBAL_MAX_CANDIDATES_LIMIT,
    PARALLELIZATION_DECISION_SCHEMA_VERSION,
    PARALLELIZATION_POLICY_VERSION,
    ConcurrencyBudget,
    ParallelizationBlockingReason,
    ParallelizationDecision,
    ParallelizationPolicyError,
    ParallelizationStrategy,
)


def test_concurrency_budget_valid_defaults():
    b = ConcurrencyBudget()
    assert b.max_agents == 3
    assert b.max_worktrees == 3
    assert b.max_remote_processes == 0
    assert b.max_llm_calls == 30
    assert b.max_candidates == 3


def test_concurrency_budget_invalid_values():
    with pytest.raises(ParallelizationPolicyError) as exc:
        ConcurrencyBudget(max_agents=0)
    assert exc.value.code == ParallelizationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value

    with pytest.raises(ParallelizationPolicyError):
        ConcurrencyBudget(max_worktrees=-1)

    with pytest.raises(ParallelizationPolicyError):
        ConcurrencyBudget(max_candidates=4)  # Exceeds GLOBAL_MAX_CANDIDATES_LIMIT = 3


def test_parallelization_decision_serialization():
    dec = ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=3,
        max_agents=3,
        requires_single_mutation_owner=False,
        requires_serialization_barrier=False,
        reason="High ambiguity testing",
        blockers=(),
    )
    d = dec.to_dict()
    assert d["schema_version"] == PARALLELIZATION_DECISION_SCHEMA_VERSION
    assert d["policy_version"] == PARALLELIZATION_POLICY_VERSION
    assert d["strategy"] == "CANDIDATE"
    assert d["allowed"] is True

    raw = dec.to_json()
    reconstructed = ParallelizationDecision.from_json(raw)
    assert reconstructed == dec
