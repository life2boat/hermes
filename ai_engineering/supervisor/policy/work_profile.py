from typing import Any, Mapping

from ai_engineering.supervisor.policy.contracts import (
    WORK_PROFILE_SCHEMA_VERSION,
    WorkProfile,
    AutonomyLevel,
    ExecutionTarget,
    PromotionThresholds,
    BudgetLimits,
    PolicyError,
    compute_deterministic_digest,
)
from ai_engineering.contracts import EffectClass
from ai_engineering.redaction import reject_forbidden_raw_fields

def validate_work_profile(profile_dict: Mapping[str, Any]) -> WorkProfile:
    # Fail closed on schema version
    if profile_dict.get("schema_version") != WORK_PROFILE_SCHEMA_VERSION:
        raise PolicyError("UNSUPPORTED_SCHEMA")

    # Reject forbidden raw fields (e.g. secrets)
    try:
        reject_forbidden_raw_fields(profile_dict)
    except ValueError:
        raise PolicyError("FORBIDDEN_RAW_FIELDS")

    try:
        allowed_task_classes = tuple(profile_dict["allowed_task_classes"])
        maximum_autonomy_level = AutonomyLevel(profile_dict["maximum_autonomy_level"])
        allowed_effect_classes = tuple(EffectClass(e) for e in profile_dict["allowed_effect_classes"])
        forbidden_effect_classes = tuple(EffectClass(e) for e in profile_dict["forbidden_effect_classes"])
        allowed_targets = tuple(ExecutionTarget(t) for t in profile_dict["allowed_targets"])
        required_validators = tuple(profile_dict["required_validators"])
        
        pt_dict = profile_dict["promotion_thresholds"]
        promotion_thresholds = PromotionThresholds(
            required_successful_runs=int(pt_dict["required_successful_runs"]),
            allowed_critical_failures=int(pt_dict["allowed_critical_failures"]),
            require_rollback_verified=bool(pt_dict["require_rollback_verified"]),
        )
        
        bl_dict = profile_dict["budget_limits"]
        budget_limits = BudgetLimits(
            max_supervisor_decisions=bl_dict.get("max_supervisor_decisions"),
            max_child_tasks=bl_dict.get("max_child_tasks"),
            max_retries_per_task=bl_dict.get("max_retries_per_task"),
            max_fix_cycles_per_task=bl_dict.get("max_fix_cycles_per_task"),
            max_consecutive_failures=bl_dict.get("max_consecutive_failures"),
            max_provider_calls=bl_dict.get("max_provider_calls"),
            max_policy_denials=bl_dict.get("max_policy_denials"),
        )
        
        # Determine digest if it exists, otherwise compute it (if we're instantiating raw)
        # Normally, profile comes with digest, but if we're generating it we must compute canonical without digest first.
        # But we'll just require profile_digest to match the payload minus profile_digest.
        payload_for_digest = dict(profile_dict)
        provided_digest = payload_for_digest.pop("profile_digest", None)
        
        computed_digest = compute_deterministic_digest(payload_for_digest)
        if provided_digest is not None and provided_digest != computed_digest:
            raise PolicyError("PROFILE_DIGEST_MISMATCH")

        return WorkProfile(
            schema_version=WORK_PROFILE_SCHEMA_VERSION,
            profile_id=str(profile_dict["profile_id"]),
            profile_version=int(profile_dict["profile_version"]),
            profile_digest=computed_digest,
            preferred_worker_model=profile_dict.get("preferred_worker_model"),
            preferred_verifier_model=profile_dict.get("preferred_verifier_model"),
            preferred_supervisor_model=profile_dict.get("preferred_supervisor_model"),
            escalation_model=profile_dict.get("escalation_model"),
            allowed_task_classes=allowed_task_classes,
            maximum_autonomy_level=maximum_autonomy_level,
            allowed_effect_classes=allowed_effect_classes,
            forbidden_effect_classes=forbidden_effect_classes,
            allowed_targets=allowed_targets,
            required_validators=required_validators,
            promotion_thresholds=promotion_thresholds,
            budget_limits=budget_limits,
            production_execution_allowed=bool(profile_dict["production_execution_allowed"]),
            vector_mutation_allowed=bool(profile_dict["vector_mutation_allowed"]),
            secret_mutation_allowed=bool(profile_dict["secret_mutation_allowed"]),
            external_send_allowed=bool(profile_dict["external_send_allowed"]),
        )
    except PolicyError:
        raise
    except (KeyError, ValueError, TypeError) as e:
        raise PolicyError("MALFORMED_PROFILE")
