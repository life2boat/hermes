from ai_engineering.supervisor.policy.contracts import (
    AutonomyBudgetState,
    BudgetLimits
)

def evaluate_budget_consumption(
    state: AutonomyBudgetState,
    limits: BudgetLimits,
    decisions_increment: int = 0,
    child_tasks_increment: int = 0,
    retries_increment: int = 0,
    fix_cycles_increment: int = 0,
    failures_increment: int = 0,
    failures_reset: bool = False,
    provider_calls_increment: int = 0,
    policy_denials_increment: int = 0
) -> AutonomyBudgetState:
    
    new_decisions = state.decisions_used + decisions_increment
    new_child_tasks = state.child_tasks_used + child_tasks_increment
    new_retries = state.retries_used + retries_increment
    new_fix_cycles = state.fix_cycles_used + fix_cycles_increment
    
    if failures_reset:
        new_failures = 0
    else:
        new_failures = state.consecutive_failures + failures_increment
        
    new_provider_calls = state.provider_calls_used + provider_calls_increment
    new_denials = state.policy_denials + policy_denials_increment
    
    exhausted = []
    if limits.max_supervisor_decisions is not None and new_decisions >= limits.max_supervisor_decisions:
        exhausted.append("DECISIONS")
    if limits.max_child_tasks is not None and new_child_tasks >= limits.max_child_tasks:
        exhausted.append("CHILD_TASKS")
    if limits.max_retries_per_task is not None and new_retries >= limits.max_retries_per_task:
        exhausted.append("RETRIES")
    if limits.max_fix_cycles_per_task is not None and new_fix_cycles >= limits.max_fix_cycles_per_task:
        exhausted.append("FIX_CYCLES")
    if limits.max_consecutive_failures is not None and new_failures >= limits.max_consecutive_failures:
        exhausted.append("FAILURES")
    if limits.max_provider_calls is not None and new_provider_calls >= limits.max_provider_calls:
        exhausted.append("PROVIDER_CALLS")
    if limits.max_policy_denials is not None and new_denials >= limits.max_policy_denials:
        exhausted.append("POLICY_DENIALS")
        
    return AutonomyBudgetState(
        schema_version=state.schema_version,
        budget_id=state.budget_id,
        budget_digest=state.budget_digest, # In reality, digest should be recomputed on persistence
        decisions_used=new_decisions,
        child_tasks_used=new_child_tasks,
        retries_used=new_retries,
        fix_cycles_used=new_fix_cycles,
        consecutive_failures=new_failures,
        provider_calls_used=new_provider_calls,
        policy_denials=new_denials,
        exhausted_dimensions=tuple(sorted(exhausted))
    )
