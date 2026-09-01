"""Deterministic parallelization policy evaluator and strategy router."""

from __future__ import annotations

from ai_engineering.contracts import (
    EffectClass,
    ReasoningLevel,
    TaskClass,
)
from ai_engineering.parallel.parallel_contracts import (
    DEFAULT_MAX_CANDIDATES,
    GLOBAL_MAX_CANDIDATES_LIMIT,
    PARALLELIZATION_POLICY_VERSION,
    ConcurrencyBudget,
    ParallelizationBlockingReason,
    ParallelizationDecision,
    ParallelizationPolicyInput,
    ParallelizationStrategy,
)

_PRODUCTION_MUTATION_EFFECTS = frozenset({
    EffectClass.DEPLOY,
    EffectClass.RUNTIME_MUTATION,
    EffectClass.DATA_MUTATION,
    EffectClass.VECTOR_MUTATION,
    EffectClass.SECRET_MUTATION,
})


class ParallelizationPolicy:
    """Deterministic policy engine determining whether and how the Hermes execution plane may parallelize."""

    def __init__(self, default_budget: ConcurrencyBudget | None = None) -> None:
        self.default_budget = default_budget or ConcurrencyBudget()

    def evaluate(self, policy_input: ParallelizationPolicyInput) -> ParallelizationDecision:
        """Evaluate input metadata and return a fail-closed ParallelizationDecision."""
        budget = policy_input.budget or self.default_budget

        # 1. Check for missing/unknown task classification
        task_class = policy_input.task_class
        if task_class is None and policy_input.task_intent is not None:
            task_class = policy_input.task_intent.task_class

        if task_class is None and policy_input.task_intent is None:
            return ParallelizationDecision(
                allowed=False,
                strategy=ParallelizationStrategy.NONE,
                max_candidates=1,
                max_agents=1,
                requires_single_mutation_owner=False,
                requires_serialization_barrier=False,
                reason="Unknown or missing task classification; failing conservative to single executor",
                blockers=(ParallelizationBlockingReason.UNKNOWN_TASK_CLASSIFICATION.value,),
                policy_version=PARALLELIZATION_POLICY_VERSION,
            )

        side_effects = policy_input.side_effects
        if policy_input.task_intent is not None and policy_input.task_intent.allowed_mutations:
            # Check intent allowed mutations for hints of production
            pass

        has_prod_mutation_effect = any(eff in _PRODUCTION_MUTATION_EFFECTS for eff in side_effects)
        is_prod_task = task_class == TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION
        is_migration_design = task_class == TaskClass.MIGRATION_ROLLBACK_DESIGN

        # 2. Production Mutation Parallelism Prohibition
        if has_prod_mutation_effect or is_prod_task:
            is_read_only = all(eff == EffectClass.READ_ONLY for eff in side_effects)
            if is_migration_design and is_read_only:
                # Rehearsal allowed for non-mutation simulation
                cand_count = min(DEFAULT_MAX_CANDIDATES, budget.max_candidates, budget.max_agents, budget.max_worktrees)
                return ParallelizationDecision(
                    allowed=True,
                    strategy=ParallelizationStrategy.REHEARSAL,
                    max_candidates=cand_count,
                    max_agents=min(budget.max_agents, cand_count),
                    requires_single_mutation_owner=True,
                    requires_serialization_barrier=True,
                    reason="Rehearsal allowed for migration and rollback design with read-only side effects",
                    blockers=(),
                    policy_version=PARALLELIZATION_POLICY_VERSION,
                )

            # Direct production mutation parallelization is strictly forbidden!
            return ParallelizationDecision(
                allowed=False,
                strategy=ParallelizationStrategy.NONE,
                max_candidates=1,
                max_agents=1,
                requires_single_mutation_owner=True,
                requires_serialization_barrier=True,
                reason="Direct parallel production mutation is strictly prohibited; requires single mutation owner",
                blockers=(ParallelizationBlockingReason.PARALLEL_MUTATION_CONFLICT.value,),
                policy_version=PARALLELIZATION_POLICY_VERSION,
            )

        # 3. Check authorization boundary inheritance
        if policy_input.authority_boundary is not None:
            auth = policy_input.authority_boundary
            if not auth.production_authorized and has_prod_mutation_effect:
                return ParallelizationDecision(
                    allowed=False,
                    strategy=ParallelizationStrategy.NONE,
                    max_candidates=1,
                    max_agents=1,
                    requires_single_mutation_owner=True,
                    requires_serialization_barrier=True,
                    reason="Production mutation requested without production authorization",
                    blockers=(ParallelizationBlockingReason.PARALLELIZATION_NOT_AUTHORIZED.value,),
                    policy_version=PARALLELIZATION_POLICY_VERSION,
                )

        # Normalize complexity and uncertainty
        complexity = (
            policy_input.task_complexity.value
            if isinstance(policy_input.task_complexity, ReasoningLevel)
            else str(policy_input.task_complexity).upper()
        )
        uncertainty = str(policy_input.task_uncertainty).upper()
        ambiguity = str(policy_input.implementation_ambiguity).upper()
        prod_risk = str(policy_input.production_risk).upper()
        info_gain = str(policy_input.expected_information_gain).upper()

        # 4. Routing Rule A: Low complexity / Small precise fix / Low information gain -> NONE
        if complexity == "LOW" or task_class == TaskClass.SMALL_PRECISE_FIX or info_gain == "LOW":
            return ParallelizationDecision(
                allowed=False,
                strategy=ParallelizationStrategy.NONE,
                max_candidates=1,
                max_agents=1,
                requires_single_mutation_owner=False,
                requires_serialization_barrier=False,
                reason="Low complexity, small precise fix, or low expected information gain; single executor is optimal",
                blockers=(),
                policy_version=PARALLELIZATION_POLICY_VERSION,
            )

        # 5. Routing Rule B: High uncertainty + READ_ONLY -> PREPARATORY
        is_all_read_only = all(eff == EffectClass.READ_ONLY for eff in side_effects)
        if (uncertainty == "HIGH" and is_all_read_only) or (task_class == TaskClass.REPOSITORY_SEARCH_LOGS and is_all_read_only):
            cand_count = min(DEFAULT_MAX_CANDIDATES, budget.max_candidates, budget.max_agents, budget.max_worktrees)
            return ParallelizationDecision(
                allowed=True,
                strategy=ParallelizationStrategy.PREPARATORY,
                max_candidates=cand_count,
                max_agents=min(budget.max_agents, cand_count),
                requires_single_mutation_owner=False,
                requires_serialization_barrier=False,
                reason="High uncertainty with read-only side effects warrants preparatory parallel investigation",
                blockers=(),
                policy_version=PARALLELIZATION_POLICY_VERSION,
            )

        # 6. Routing Rule C: High-risk code change / Security audit -> REVIEW
        if prod_risk in ("HIGH", "CRITICAL") or task_class == TaskClass.SECURITY_AUDIT:
            return ParallelizationDecision(
                allowed=True,
                strategy=ParallelizationStrategy.REVIEW,
                max_candidates=1,
                max_agents=min(2, budget.max_agents),
                requires_single_mutation_owner=False,
                requires_serialization_barrier=False,
                reason="High-risk code change warrants dual implementer and independent reviewer",
                blockers=(),
                policy_version=PARALLELIZATION_POLICY_VERSION,
            )

        # 7. Routing Rule D: Migration & Rollback Design Rehearsal
        if is_migration_design and is_all_read_only:
            cand_count = min(DEFAULT_MAX_CANDIDATES, budget.max_candidates, budget.max_agents, budget.max_worktrees)
            return ParallelizationDecision(
                allowed=True,
                strategy=ParallelizationStrategy.REHEARSAL,
                max_candidates=cand_count,
                max_agents=min(budget.max_agents, cand_count),
                requires_single_mutation_owner=True,
                requires_serialization_barrier=True,
                reason="Rehearsal allowed for migration and rollback design with read-only side effects",
                blockers=(),
                policy_version=PARALLELIZATION_POLICY_VERSION,
            )

        # 8. Routing Rule E: High implementation ambiguity + LOW/MEDIUM risk -> CANDIDATE
        if ambiguity == "HIGH" and prod_risk in ("LOW", "MEDIUM") and task_class == TaskClass.BOUNDED_IMPLEMENTATION:
            cand_count = min(DEFAULT_MAX_CANDIDATES, budget.max_candidates, budget.max_agents, budget.max_worktrees)
            return ParallelizationDecision(
                allowed=True,
                strategy=ParallelizationStrategy.CANDIDATE,
                max_candidates=cand_count,
                max_agents=min(budget.max_agents, cand_count),
                requires_single_mutation_owner=False,
                requires_serialization_barrier=False,
                reason="High implementation ambiguity with low production risk warrants candidate parallelization",
                blockers=(),
                policy_version=PARALLELIZATION_POLICY_VERSION,
            )

        # Default fallback: NONE
        return ParallelizationDecision(
            allowed=False,
            strategy=ParallelizationStrategy.NONE,
            max_candidates=1,
            max_agents=1,
            requires_single_mutation_owner=False,
            requires_serialization_barrier=False,
            reason="Default single executor baseline; parallelization criteria not met",
            blockers=(),
            policy_version=PARALLELIZATION_POLICY_VERSION,
        )


def evaluate_parallelization_policy(policy_input: ParallelizationPolicyInput) -> ParallelizationDecision:
    """Convenience functional interface to evaluate parallelization policy."""
    policy = ParallelizationPolicy()
    return policy.evaluate(policy_input)
