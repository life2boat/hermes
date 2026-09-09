from typing import Mapping
import json
import hashlib

from ai_engineering.supervisor.policy.contracts import (
    PolicyRequest,
    WorkProfile,
    AutonomyState,
    AutonomyBudgetState,
    PolicyReceipt,
    PolicyVerdict,
    PolicyError,
    AutonomyLevel,
    ExecutionTarget,
    POLICY_RECEIPT_SCHEMA_VERSION,
    compute_deterministic_digest
)
from ai_engineering.task_intent import TaskIntent, intent_digest
from ai_engineering.effective_policy import EffectivePolicyReport
from ai_engineering.contracts import StopBoundary, EffectClass
from ai_engineering.control_plane.orchestrator import _STOP_BOUNDARY_RANK

from datetime import datetime, timezone

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _rank(sb: StopBoundary) -> int:
    return _STOP_BOUNDARY_RANK.get(sb, 999)

def evaluate_policy(
    request: PolicyRequest,
    task_intent: TaskIntent,
    effective_policy: EffectivePolicyReport,
    work_profile: WorkProfile,
    autonomy_state: AutonomyState,
    budget_state: AutonomyBudgetState,
) -> PolicyReceipt:
    reason_codes = []
    verdict = PolicyVerdict.ALLOW

    # 1. Bindings check
    if request.intent_digest != intent_digest(task_intent):
        reason_codes.append("POLICY_BINDING_MISMATCH")
    if request.effective_policy_digest != effective_policy.effective_policy_id:
        reason_codes.append("POLICY_BINDING_MISMATCH")
    if request.work_profile_digest != work_profile.profile_digest:
        reason_codes.append("POLICY_BINDING_MISMATCH")
    if request.budget_state_digest != budget_state.budget_digest:
        reason_codes.append("POLICY_BINDING_MISMATCH")
    if request.current_autonomy_level != autonomy_state.current_level:
        reason_codes.append("POLICY_BINDING_MISMATCH")
        
    if request.task_id != task_intent.task_id:
        reason_codes.append("POLICY_BINDING_MISMATCH")

    # 2. Effective Policy completeness
    # The status in EffectivePolicyReport is EffectivePolicyStatus Enum but represented as string or Enum
    status_str = effective_policy.status.value if hasattr(effective_policy.status, "value") else str(effective_policy.status)
    if status_str != "COMPLETE":
        reason_codes.append("EFFECTIVE_POLICY_INCOMPLETE")

    # 3. StopBoundary evaluation
    req_rank = _rank(request.requested_stop_boundary)
    intent_rank = _rank(task_intent.stop_boundary)
    
    # Autonomy Level Ceilings
    autonomy_ceilings = {
        AutonomyLevel.LEVEL_0_OBSERVE: _rank(StopBoundary.READ_ONLY),
        AutonomyLevel.LEVEL_1_LOCAL_WRITE: _rank(StopBoundary.LOCAL_DIFF),
        AutonomyLevel.LEVEL_2_DEV_AUTONOMY: _rank(StopBoundary.COMMIT),
        AutonomyLevel.LEVEL_3_CI_AUTONOMY: _rank(StopBoundary.READY_PR),
        AutonomyLevel.LEVEL_4_STAGING_AUTONOMY: _rank(StopBoundary.DEPLOY),
        AutonomyLevel.LEVEL_5_PROD_PREP: _rank(StopBoundary.BUILD),
        AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION: _rank(StopBoundary.LIVE_SMOKE),
    }
    level_ceiling_rank = autonomy_ceilings.get(request.current_autonomy_level, -1)
    
    if req_rank > intent_rank:
        reason_codes.append("TASK_INTENT_AUTHORITY_DENIED")
    
    if req_rank > level_ceiling_rank:
        reason_codes.append("AUTONOMY_LEVEL_TOO_LOW")

    # 4. EffectClass evaluation
    for ec in request.requested_effect_classes:
        if ec not in task_intent.allowed_mutations and ec != EffectClass.READ_ONLY:
            reason_codes.append("TASK_INTENT_AUTHORITY_DENIED")
        if ec in task_intent.forbidden_mutations:
            reason_codes.append("TASK_INTENT_AUTHORITY_DENIED")
            
        if ec not in work_profile.allowed_effect_classes and ec != EffectClass.READ_ONLY:
            reason_codes.append("WORK_PROFILE_DENIED")
        if ec in work_profile.forbidden_effect_classes:
            reason_codes.append("WORK_PROFILE_DENIED")

        if ec == EffectClass.VECTOR_MUTATION and not work_profile.vector_mutation_allowed:
            reason_codes.append("VECTOR_MUTATION_PERMISSION_REQUIRED")
        if ec == EffectClass.SECRET_MUTATION and not work_profile.secret_mutation_allowed:
            reason_codes.append("SECRET_MUTATION_PERMISSION_REQUIRED")
        if ec == EffectClass.EXTERNAL_SEND and not work_profile.external_send_allowed:
            reason_codes.append("EXTERNAL_SEND_PERMISSION_REQUIRED")
            
        if request.current_autonomy_level == AutonomyLevel.LEVEL_0_OBSERVE:
            if ec != EffectClass.READ_ONLY:
                reason_codes.append("EFFECT_CLASS_DENIED")
        if request.current_autonomy_level == AutonomyLevel.LEVEL_1_LOCAL_WRITE:
            if ec not in (EffectClass.READ_ONLY, EffectClass.REPOSITORY_WRITE):
                reason_codes.append("EFFECT_CLASS_DENIED")
        if request.current_autonomy_level == AutonomyLevel.LEVEL_2_DEV_AUTONOMY:
            if ec not in (EffectClass.READ_ONLY, EffectClass.REPOSITORY_WRITE, EffectClass.GIT_COMMIT):
                reason_codes.append("EFFECT_CLASS_DENIED")

    # 5. Target environment evaluation
    if request.execution_target not in work_profile.allowed_targets:
        reason_codes.append("TARGET_ENVIRONMENT_DENIED")
        
    if request.execution_target == ExecutionTarget.PRODUCTION and not work_profile.production_execution_allowed:
        reason_codes.append("PRODUCTION_PERMISSION_REQUIRED")
        
    if request.execution_target == ExecutionTarget.PRODUCTION and request.current_autonomy_level != AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION:
        reason_codes.append("AUTONOMY_LEVEL_TOO_LOW")

    # 6. Budget Evaluation
    limits = work_profile.budget_limits
    is_budget_exhausted = bool(budget_state.exhausted_dimensions)
    if limits:
        if limits.max_supervisor_decisions is not None and budget_state.decisions_used >= limits.max_supervisor_decisions:
            is_budget_exhausted = True
        if limits.max_child_tasks is not None and budget_state.child_tasks_used >= limits.max_child_tasks:
            is_budget_exhausted = True
        if limits.max_retries_per_task is not None and budget_state.retries_used >= limits.max_retries_per_task:
            is_budget_exhausted = True
        if limits.max_fix_cycles_per_task is not None and budget_state.fix_cycles_used >= limits.max_fix_cycles_per_task:
            is_budget_exhausted = True
        if limits.max_consecutive_failures is not None and budget_state.consecutive_failures >= limits.max_consecutive_failures:
            is_budget_exhausted = True
        if limits.max_provider_calls is not None and budget_state.provider_calls_used >= limits.max_provider_calls:
            is_budget_exhausted = True
        if limits.max_policy_denials is not None and budget_state.policy_denials >= limits.max_policy_denials:
            is_budget_exhausted = True

    if is_budget_exhausted:
        reason_codes.append("AUTONOMY_BUDGET_EXHAUSTED")

    if reason_codes:
        verdict = PolicyVerdict.DENY
        reason_codes = sorted(list(set(reason_codes)))
    else:
        verdict = PolicyVerdict.ALLOW
        reason_codes = []

    ts = _utc_now()
    
    # Compute receipt_id deterministically
    payload_for_digest = {
        "request_id": request.request_id,
        "task_intent_digest": request.intent_digest,
        "decision_id": request.decision_id,
        "decision_receipt_id": request.decision_receipt_id,
        "effective_policy_id": request.effective_policy_id,
        "work_profile_id": request.work_profile_id,
        "work_profile_digest": request.work_profile_digest,
        "autonomy_state_digest": autonomy_state.state_digest,
        "budget_state_digest": request.budget_state_digest,
        "verdict": str(verdict),
        "reason_codes": reason_codes,
        "created_at_utc": ts
    }
    receipt_id = compute_deterministic_digest(payload_for_digest)

    return PolicyReceipt(
        schema_version=POLICY_RECEIPT_SCHEMA_VERSION,
        receipt_id=receipt_id,
        request_id=request.request_id,
        task_intent_digest=request.intent_digest,
        decision_id=request.decision_id,
        decision_receipt_id=request.decision_receipt_id,
        effective_policy_id=request.effective_policy_id,
        work_profile_id=request.work_profile_id,
        work_profile_digest=request.work_profile_digest,
        autonomy_state_digest=autonomy_state.state_digest,
        budget_state_digest=request.budget_state_digest,
        verdict=verdict,
        reason_codes=tuple(reason_codes),
        created_at_utc=ts
    )
