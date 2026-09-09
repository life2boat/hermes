from ai_engineering.supervisor.policy.contracts import (
    AutonomyState,
    AutonomyLevel,
    WorkProfile,
    PolicyError
)

def _level_int(level: AutonomyLevel) -> int:
    _levels = {
        AutonomyLevel.LEVEL_0_OBSERVE: 0,
        AutonomyLevel.LEVEL_1_LOCAL_WRITE: 1,
        AutonomyLevel.LEVEL_2_DEV_AUTONOMY: 2,
        AutonomyLevel.LEVEL_3_CI_AUTONOMY: 3,
        AutonomyLevel.LEVEL_4_STAGING_AUTONOMY: 4,
        AutonomyLevel.LEVEL_5_PROD_PREP: 5,
        AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION: 6,
    }
    return _levels[level]

def _level_from_int(val: int) -> AutonomyLevel:
    _levels = {
        0: AutonomyLevel.LEVEL_0_OBSERVE,
        1: AutonomyLevel.LEVEL_1_LOCAL_WRITE,
        2: AutonomyLevel.LEVEL_2_DEV_AUTONOMY,
        3: AutonomyLevel.LEVEL_3_CI_AUTONOMY,
        4: AutonomyLevel.LEVEL_4_STAGING_AUTONOMY,
        5: AutonomyLevel.LEVEL_5_PROD_PREP,
        6: AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION,
    }
    if val not in _levels:
        raise PolicyError("UNKNOWN_AUTONOMY_LEVEL")
    return _levels[val]


def evaluate_promotion(
    state: AutonomyState,
    profile: WorkProfile,
    operator_policy_allows: bool
) -> AutonomyLevel:
    current = state.current_level
    if current == profile.maximum_autonomy_level:
        return current

    req_runs = profile.promotion_thresholds.required_successful_runs
    if state.successful_runs < req_runs:
        return current

    if state.critical_failures > profile.promotion_thresholds.allowed_critical_failures:
        return current

    if profile.promotion_thresholds.require_rollback_verified and not state.rollback_verified:
        return current

    for val_name in profile.required_validators:
        if state.required_validators_status.get(val_name) != "PASS":
            return current

    if not operator_policy_allows:
        return current

    current_val = _level_int(current)
    max_val = _level_int(profile.maximum_autonomy_level)
    
    if current_val < max_val:
        return _level_from_int(current_val + 1)
    
    return current


def evaluate_demotion(
    state: AutonomyState,
    profile: WorkProfile,
    critical_failure_occurred: bool,
    policy_invalidated: bool
) -> AutonomyLevel:
    current = state.current_level
    
    if _level_int(current) > _level_int(profile.maximum_autonomy_level):
        current = profile.maximum_autonomy_level
        
    if policy_invalidated or critical_failure_occurred:
        return AutonomyLevel.LEVEL_0_OBSERVE

    return current
