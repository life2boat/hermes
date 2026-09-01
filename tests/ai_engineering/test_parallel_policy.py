"""Unit tests for ParallelizationPolicy engine and routing logic."""

from __future__ import annotations

import pytest

from ai_engineering.contracts import (
    EffectClass,
    ReasoningLevel,
    TaskClass,
)
from ai_engineering.parallel.parallel_contracts import (
    ConcurrencyBudget,
    ParallelizationBlockingReason,
    ParallelizationPolicyInput,
    ParallelizationStrategy,
)
from ai_engineering.parallel.parallel_policy import (
    ParallelizationPolicy,
    evaluate_parallelization_policy,
)


def test_default_policy_evaluation():
    policy = ParallelizationPolicy()
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.SMALL_PRECISE_FIX,
        task_complexity=ReasoningLevel.LOW,
    )
    decision = policy.evaluate(inp)
    assert decision.allowed is False
    assert decision.strategy == ParallelizationStrategy.NONE
    assert decision.max_candidates == 1


def test_budget_clamping():
    policy = ParallelizationPolicy()
    budget = ConcurrencyBudget(max_agents=2, max_candidates=2, max_worktrees=2)
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        production_risk="LOW",
        expected_information_gain="HIGH",
        budget=budget,
    )
    decision = policy.evaluate(inp)
    assert decision.allowed is True
    assert decision.strategy == ParallelizationStrategy.CANDIDATE
    assert decision.max_candidates == 2
    assert decision.max_agents == 2
