import json
import hashlib
from dataclasses import dataclass, asdict
from enum import StrEnum
from typing import Mapping, Sequence

from ai_engineering.contracts import EffectClass, StopBoundary

# Schemas
WORK_PROFILE_SCHEMA_VERSION = "hermes.work-profile.v1"
POLICY_REQUEST_SCHEMA_VERSION = "hermes.policy-request.v1"
POLICY_DECISION_SCHEMA_VERSION = "hermes.policy-decision.v1"
POLICY_RECEIPT_SCHEMA_VERSION = "hermes.policy-receipt.v1"
AUTONOMY_STATE_SCHEMA_VERSION = "hermes.autonomy-state.v1"
AUTONOMY_BUDGET_SCHEMA_VERSION = "hermes.autonomy-budget.v1"
AUTONOMY_BUDGET_STATE_SCHEMA_VERSION = "hermes.autonomy-budget-state.v1"


class PolicyError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)

class AutonomyLevel(StrEnum):
    LEVEL_0_OBSERVE = "LEVEL_0_OBSERVE"
    LEVEL_1_LOCAL_WRITE = "LEVEL_1_LOCAL_WRITE"
    LEVEL_2_DEV_AUTONOMY = "LEVEL_2_DEV_AUTONOMY"
    LEVEL_3_CI_AUTONOMY = "LEVEL_3_CI_AUTONOMY"
    LEVEL_4_STAGING_AUTONOMY = "LEVEL_4_STAGING_AUTONOMY"
    LEVEL_5_PROD_PREP = "LEVEL_5_PROD_PREP"
    LEVEL_6_AUTHORIZED_PRODUCTION = "LEVEL_6_AUTHORIZED_PRODUCTION"

class ExecutionTarget(StrEnum):
    LOCAL = "LOCAL"
    DEV = "DEV"
    CI = "CI"
    STAGING = "STAGING"
    PRODUCTION = "PRODUCTION"
    EXTERNAL = "EXTERNAL"

class PolicyVerdict(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"

@dataclass(frozen=True, slots=True)
class PromotionThresholds:
    required_successful_runs: int
    allowed_critical_failures: int
    require_rollback_verified: bool

@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_supervisor_decisions: int | None
    max_child_tasks: int | None
    max_retries_per_task: int | None
    max_fix_cycles_per_task: int | None
    max_consecutive_failures: int | None
    max_provider_calls: int | None
    max_policy_denials: int | None

@dataclass(frozen=True, slots=True)
class WorkProfile:
    schema_version: str
    profile_id: str
    profile_version: int
    profile_digest: str

    preferred_worker_model: str | None
    preferred_verifier_model: str | None
    preferred_supervisor_model: str | None
    escalation_model: str | None

    allowed_task_classes: tuple[str, ...]
    maximum_autonomy_level: AutonomyLevel

    allowed_effect_classes: tuple[EffectClass, ...]
    forbidden_effect_classes: tuple[EffectClass, ...]

    allowed_targets: tuple[ExecutionTarget, ...]
    required_validators: tuple[str, ...]

    promotion_thresholds: PromotionThresholds
    budget_limits: BudgetLimits

    production_execution_allowed: bool
    vector_mutation_allowed: bool
    secret_mutation_allowed: bool
    external_send_allowed: bool

@dataclass(frozen=True, slots=True)
class PolicyRequest:
    schema_version: str
    request_id: str
    run_id: str
    task_id: str
    attempt_id: str
    intent_digest: str
    decision_id: str
    decision_receipt_id: str
    work_profile_id: str
    work_profile_digest: str
    current_autonomy_level: AutonomyLevel
    requested_action: str
    requested_effect_classes: tuple[EffectClass, ...]
    requested_stop_boundary: StopBoundary
    execution_target: ExecutionTarget
    effective_policy_id: str
    effective_policy_digest: str
    budget_state_digest: str

@dataclass(frozen=True, slots=True)
class PolicyDecision:
    schema_version: str
    verdict: PolicyVerdict
    reason_codes: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class PolicyReceipt:
    schema_version: str
    receipt_id: str
    request_id: str
    task_intent_digest: str
    decision_id: str
    decision_receipt_id: str
    effective_policy_id: str
    work_profile_id: str
    work_profile_digest: str
    autonomy_state_digest: str
    budget_state_digest: str
    verdict: PolicyVerdict
    reason_codes: tuple[str, ...]
    created_at_utc: str

@dataclass(frozen=True, slots=True)
class AutonomyBudgetState:
    schema_version: str
    budget_id: str
    budget_digest: str
    decisions_used: int
    child_tasks_used: int
    retries_used: int
    fix_cycles_used: int
    consecutive_failures: int
    provider_calls_used: int
    policy_denials: int
    exhausted_dimensions: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class AutonomyState:
    schema_version: str
    run_id: str
    profile_id: str
    profile_digest: str
    current_level: AutonomyLevel
    maximum_allowed_level: AutonomyLevel
    successful_runs: int
    critical_failures: int
    rollback_verified: bool
    required_validators_status: Mapping[str, str]
    budget_state_digest: str
    promotion_sequence: int
    last_transition_receipt_id: str | None
    created_at_utc: str
    updated_at_utc: str
    state_digest: str

def compute_deterministic_digest(data: Mapping[str, object]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
