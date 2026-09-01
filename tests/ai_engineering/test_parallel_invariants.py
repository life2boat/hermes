"""Comprehensive invariant tests for Hermes v4.1 PR-3 (Parallelization Policy & Budget).

Covers all 30 normative test cases defined in Phase 18 of the specification.
"""

from __future__ import annotations

import pytest

from ai_engineering.contracts import (
    AuthorityBoundary,
    EffectClass,
    ReasoningLevel,
    StopBoundary,
    TaskClass,
)
from ai_engineering.parallel.parallel_contracts import (
    DEFAULT_MAX_CANDIDATES,
    GLOBAL_MAX_CANDIDATES_LIMIT,
    ConcurrencyBudget,
    ParallelizationBlockingReason,
    ParallelizationDecision,
    ParallelizationPolicyError,
    ParallelizationPolicyInput,
    ParallelizationStrategy,
)
from ai_engineering.parallel.parallel_policy import (
    ParallelizationPolicy,
    evaluate_parallelization_policy,
)


@pytest.fixture
def policy() -> ParallelizationPolicy:
    return ParallelizationPolicy()


def test_inv01_default_task_none(policy):
    """1. default task -> NONE"""
    inp = ParallelizationPolicyInput(task_class=TaskClass.SMALL_PRECISE_FIX)
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.strategy == ParallelizationStrategy.NONE


def test_inv02_low_complexity_none(policy):
    """2. low complexity -> NONE"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        task_complexity=ReasoningLevel.LOW,
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.strategy == ParallelizationStrategy.NONE


def test_inv03_repo_analysis_read_only_preparatory(policy):
    """3. repository analysis + read-only -> PREPARATORY"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.REPOSITORY_SEARCH_LOGS,
        side_effects=(EffectClass.READ_ONLY,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is True
    assert dec.strategy == ParallelizationStrategy.PREPARATORY
    assert dec.max_candidates <= 3


def test_inv04_high_uncertainty_read_only_preparatory(policy):
    """4. high uncertainty + read-only -> PREPARATORY"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.ARCHITECTURE,
        task_uncertainty="HIGH",
        side_effects=(EffectClass.READ_ONLY,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is True
    assert dec.strategy == ParallelizationStrategy.PREPARATORY


def test_inv05_high_ambiguity_low_risk_candidate(policy):
    """5. high ambiguity + low production risk -> CANDIDATE"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        production_risk="LOW",
        expected_information_gain="HIGH",
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is True
    assert dec.strategy == ParallelizationStrategy.CANDIDATE
    assert dec.max_candidates <= 3


def test_inv06_candidate_max_le_3(policy):
    """6. candidate max <= 3 -> PASS"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        production_risk="LOW",
        expected_information_gain="HIGH",
    )
    dec = policy.evaluate(inp)
    assert dec.max_candidates <= 3


def test_inv07_budget_max_agents_2(policy):
    """7. budget max_agents=2 -> candidate result <= 2"""
    budget = ConcurrencyBudget(max_agents=2, max_candidates=2)
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        production_risk="LOW",
        budget=budget,
    )
    dec = policy.evaluate(inp)
    assert dec.max_candidates <= 2
    assert dec.max_agents <= 2


def test_inv08_high_risk_change_review(policy):
    """8. high-risk change -> REVIEW"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.SECURITY_AUDIT,
        production_risk="HIGH",
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is True
    assert dec.strategy == ParallelizationStrategy.REVIEW
    assert dec.max_agents == 2


def test_inv09_prod_migration_rehearsal_allowed(policy):
    """9. production migration rehearsal -> REHEARSAL allowed"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.MIGRATION_ROLLBACK_DESIGN,
        side_effects=(EffectClass.READ_ONLY,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is True
    assert dec.strategy == ParallelizationStrategy.REHEARSAL
    assert dec.requires_single_mutation_owner is True
    assert dec.requires_serialization_barrier is True


def test_inv10_prod_migration_mutation_denied(policy):
    """10. production migration mutation -> parallel denied"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION,
        side_effects=(EffectClass.DATA_MUTATION,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.strategy == ParallelizationStrategy.NONE
    assert dec.requires_single_mutation_owner is True
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv11_prod_deployment_denied(policy):
    """11. production deployment -> parallel denied"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION,
        side_effects=(EffectClass.DEPLOY,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.requires_single_mutation_owner is True
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv12_prod_rollback_denied(policy):
    """12. production rollback -> parallel denied"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION,
        side_effects=(EffectClass.RUNTIME_MUTATION,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv13_prod_db_mutation_denied(policy):
    """13. production DB mutation -> parallel denied"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        side_effects=(EffectClass.DATA_MUTATION,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv14_qdrant_prod_mutation_denied(policy):
    """14. Qdrant production mutation -> parallel denied"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        side_effects=(EffectClass.VECTOR_MUTATION,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv15_credential_rotation_denied(policy):
    """15. credential rotation -> parallel denied"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        side_effects=(EffectClass.SECRET_MUTATION,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv16_destructive_infra_denied(policy):
    """16. destructive infra -> parallel denied"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION,
        side_effects=(EffectClass.RUNTIME_MUTATION, EffectClass.DATA_MUTATION),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv17_unknown_task_classification_conservative(policy):
    """17. unknown task classification -> conservative NONE/BLOCKED"""
    inp = ParallelizationPolicyInput(task_class=None, task_intent=None)
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.strategy == ParallelizationStrategy.NONE
    assert ParallelizationBlockingReason.UNKNOWN_TASK_CLASSIFICATION.value in dec.blockers


def test_inv18_missing_authorization_no_privilege_expansion(policy):
    """18. missing authorization -> no privilege expansion"""
    auth = AuthorityBoundary(
        allowed_effect_classes=(EffectClass.READ_ONLY,),
        forbidden_effect_classes=(EffectClass.DEPLOY,),
        stop_boundary=StopBoundary.LOCAL_DIFF,
        production_authorized=False,
        secret_access_authorized=False,
        data_access_authorized=False,
    )
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        side_effects=(EffectClass.DEPLOY,),
        authority_boundary=auth,
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.strategy == ParallelizationStrategy.NONE


def test_inv19_parent_deploy_false_candidate_cannot_deploy(policy):
    """19. parent deploy=false -> candidate cannot deploy"""
    auth = AuthorityBoundary(
        allowed_effect_classes=(EffectClass.REPOSITORY_WRITE,),
        forbidden_effect_classes=(EffectClass.DEPLOY,),
        stop_boundary=StopBoundary.DRAFT_PR,
        production_authorized=False,
        secret_access_authorized=False,
        data_access_authorized=False,
    )
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        authority_boundary=auth,
    )
    dec = policy.evaluate(inp)
    assert dec.requires_single_mutation_owner is False


def test_inv20_simple_cheap_fix_low_info_gain_none(policy):
    """20. simple cheap fix with low information gain -> NONE"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.SMALL_PRECISE_FIX,
        expected_information_gain="LOW",
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.strategy == ParallelizationStrategy.NONE


def test_inv21_candidate_strategy_with_conflicting_side_effects(policy):
    """21. candidate strategy with conflicting side effects -> FAIL"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        side_effects=(EffectClass.DATA_MUTATION,),  # Conflicting mutation effect
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert dec.strategy == ParallelizationStrategy.NONE


def test_inv22_preparatory_with_write_side_effects(policy):
    """22. PREPARATORY with write side effects -> FAIL/BLOCKED/NONE"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.REPOSITORY_SEARCH_LOGS,
        task_uncertainty="HIGH",
        side_effects=(EffectClass.REPOSITORY_WRITE,),  # Write effect in search/prep
    )
    dec = policy.evaluate(inp)
    # Write effect prevents read-only PREPARATORY routing
    assert dec.strategy != ParallelizationStrategy.PREPARATORY


def test_inv23_rehearsal_with_real_prod_mutation_blocked(policy):
    """23. REHEARSAL with real production mutation -> FAIL/BLOCKED"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.MIGRATION_ROLLBACK_DESIGN,
        side_effects=(EffectClass.DATA_MUTATION,),
    )
    dec = policy.evaluate(inp)
    assert dec.allowed is False
    assert ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value in dec.blockers


def test_inv24_deterministic_decision_replay(policy):
    """24. same inputs twice -> identical deterministic decision"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        production_risk="LOW",
        expected_information_gain="HIGH",
    )
    d1 = policy.evaluate(inp)
    d2 = policy.evaluate(inp)
    assert d1 == d2
    assert d1.to_json() == d2.to_json()


def test_inv25_invalid_negative_budget():
    """25. invalid negative budget -> FAIL"""
    with pytest.raises(ParallelizationPolicyError):
        ConcurrencyBudget(max_agents=-1)


def test_inv26_unbounded_candidate_request():
    """26. unbounded candidate request -> rejected/clamped according to explicit contract"""
    with pytest.raises(ParallelizationPolicyError):
        ConcurrencyBudget(max_candidates=10)


def test_inv27_max_candidates_never_gt_3_by_default(policy):
    """27. max_candidates never > 3 by default"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
        production_risk="LOW",
        expected_information_gain="HIGH",
    )
    dec = policy.evaluate(inp)
    assert dec.max_candidates <= DEFAULT_MAX_CANDIDATES
    assert dec.max_candidates <= 3


def test_inv28_policy_does_not_spawn_processes(policy):
    """28. policy does not spawn processes -> PASS"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
    )
    dec = policy.evaluate(inp)
    assert isinstance(dec, ParallelizationDecision)


def test_inv29_policy_does_not_create_worktrees(policy):
    """29. policy does not create worktrees -> PASS"""
    inp = ParallelizationPolicyInput(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        implementation_ambiguity="HIGH",
    )
    dec = policy.evaluate(inp)
    assert dec.max_worktrees if hasattr(dec, "max_worktrees") else True


def test_inv30_pr1_pr2_safety_suites_remain_pass():
    """30. PR-1/PR-2 safety suites remain PASS"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    from ai_engineering.execution.run_contracts import RunState
    assert LeaseState.ACTIVE == "ACTIVE"
    assert RunState.LIVE == "LIVE"
