"""test_supervisor_policy.py - Comprehensive test suite for Task 3 Supervisor Policy Engine.

Covers:
1. Level matrix tests (Level 0, 1, 2, 3, 4, 5, 6 mappings to bounds)
2. Authority intersection tests
3. Policy binding tests
4. Promotion and Demotion tests
5. Budget tests
6. Work Profile tests
7. Offline E2E ALLOW test
8. Offline E2E DENY test
9. Policy tamper test
10. Profile tamper test
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from dataclasses import replace
from typing import Any

import pytest

from ai_engineering.contracts import EffectClass, GateResult, Status, StopBoundary
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)
from ai_engineering.task_intent import (
    NodeKind,
    RelationKind,
    TaskIntent,
    TaskLineage,
    LineageNode,
    intent_digest,
    deserialize_intent,
    TASK_INTENT_SCHEMA_VERSION,
    LINEAGE_SCHEMA_VERSION,
)
from ai_engineering.supervisor.context_pack import build_context_pack, context_pack_digest
from ai_engineering.supervisor.decision import (
    AstraDecision,
    DecisionReceipt,
    SupervisorAction,
    deserialize_astra_decision,
)
from ai_engineering.supervisor.events import (
    SupervisorEvent,
    SupervisorEventType,
    create_event,
    deserialize_event,
)
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.policy.contracts import (
    AutonomyBudgetState,
    AutonomyLevel,
    AutonomyState,
    BudgetLimits,
    ExecutionTarget,
    PolicyDecision,
    PolicyError,
    PolicyReceipt,
    PolicyRequest,
    PolicyVerdict,
    PromotionThresholds,
    WorkProfile,
    compute_deterministic_digest,
    AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
    AUTONOMY_STATE_SCHEMA_VERSION,
    POLICY_REQUEST_SCHEMA_VERSION,
    WORK_PROFILE_SCHEMA_VERSION,
)
from ai_engineering.supervisor.policy.autonomy import evaluate_demotion, evaluate_promotion
from ai_engineering.supervisor.policy.budget import evaluate_budget_consumption
from ai_engineering.supervisor.policy.engine import evaluate_policy
from ai_engineering.supervisor.policy.work_profile import validate_work_profile
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorPhase,
    SupervisorState,
    state_digest,
)
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.validator import (
    VerifiedResult,
    VERIFIED_RESULT_SCHEMA_VERSION,
    deserialize_verified_result,
)


# ── Constants & Fixtures ───────────────────────────────────────────────────────

BASE_SHA = "a9dc4b18ae8d29e8b2201285afc526e6d98be067"
HEAD_SHA = "b1ec5c29bf9e40f8c3302396bfd637e7e9bc5178"
TASK_ID = "task-policy-01"
RUN_ID = "run-policy-01"
ATTEMPT_ID = "attempt-01"
TS = "2026-09-09T14:14:23+00:00"


def make_test_intent(
    task_id: str = TASK_ID,
    allowed_mutations: Any = None,
    forbidden_mutations: Any = None,
    stop_boundary: Any = "COMMIT",
) -> TaskIntent:
    sb_str = stop_boundary.value if hasattr(stop_boundary, "value") else str(stop_boundary)
    al = list(allowed_mutations) if allowed_mutations is not None else ["REPOSITORY_WRITE", "GIT_COMMIT"]
    fb = list(forbidden_mutations) if forbidden_mutations is not None else ["PR_MERGE", "DEPLOY"]
    d = {
        "schema_version": 1,
        "task_id": task_id,
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Implement policy engine wiring",
        "source_repository": "life2boat/hermes",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": BASE_SHA,
        "constraints": ["No external deps"],
        "allowed_mutations": [m.value if hasattr(m, "value") else str(m) for m in al],
        "forbidden_mutations": [m.value if hasattr(m, "value") else str(m) for m in fb],
        "stop_boundary": sb_str,
        "acceptance_criteria": [{"criterion_id": "AC-1", "statement": "Policy evaluates"}],
        "unknowns": [],
        "applicable_invariants": ["DETERMINISTIC_PIPELINE"],
        "required_gates": ["tests", "lint"],
        "parent_intent_digest": None,
    }
    return deserialize_intent(json.dumps(d))


def make_test_lineage(task_id: str = TASK_ID) -> TaskLineage:
    return TaskLineage(
        schema_version=1,
        nodes=(LineageNode(node_id=task_id, kind=NodeKind.TASK),),
        edges=(),
    )


def make_test_effective_policy(
    intent: TaskIntent,
    status: EffectivePolicyStatus = EffectivePolicyStatus.COMPLETE,
    effective_policy_id: str = "e" * 64,
) -> EffectivePolicyReport:
    tpa = TaskPolicyAttribution(
        task_id=intent.task_id,
        intent_revision=intent.intent_revision,
        intent_digest=intent_digest(intent),
        source_base_sha=intent.source_base_sha,
        constraints=intent.constraints,
        allowed_mutations=intent.allowed_mutations,
        forbidden_mutations=intent.forbidden_mutations,
        stop_boundary=intent.stop_boundary.value,
        source_id="s" * 64,
    )
    return EffectivePolicyReport(
        schema_version=1,
        effective_policy_id=effective_policy_id,
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        source_base_sha=intent.source_base_sha,
        subject_sha=intent.source_base_sha,
        status=status,
        policy_sources=(),
        task_policy=tpa,
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="p" * 64,
    )


def make_profile_dict(
    profile_id: str = "wp-test-profile",
    profile_version: int = 1,
    max_autonomy_level: str = "LEVEL_2_DEV_AUTONOMY",
    allowed_effect_classes: list[str] | None = None,
    forbidden_effect_classes: list[str] | None = None,
    allowed_targets: list[str] | None = None,
    budget_limits: dict | None = None,
    promotion_thresholds: dict | None = None,
    production_allowed: bool = False,
    vector_allowed: bool = False,
    secret_allowed: bool = False,
    external_send_allowed: bool = False,
) -> dict:
    d = {
        "schema_version": WORK_PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "profile_version": profile_version,
        "preferred_worker_model": "model-worker",
        "preferred_verifier_model": "model-verifier",
        "preferred_supervisor_model": "model-supervisor",
        "escalation_model": "model-escalation",
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION", "ANALYSIS"],
        "maximum_autonomy_level": max_autonomy_level,
        "allowed_effect_classes": allowed_effect_classes if allowed_effect_classes is not None else ["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT"],
        "forbidden_effect_classes": forbidden_effect_classes if forbidden_effect_classes is not None else ["PR_MERGE", "DEPLOY"],
        "allowed_targets": allowed_targets if allowed_targets is not None else ["LOCAL", "DEV"],
        "required_validators": ["tests", "lint"],
        "promotion_thresholds": promotion_thresholds or {
            "required_successful_runs": 3,
            "allowed_critical_failures": 0,
            "require_rollback_verified": True,
        },
        "budget_limits": budget_limits or {
            "max_supervisor_decisions": 10,
            "max_child_tasks": 5,
            "max_retries_per_task": 3,
            "max_fix_cycles_per_task": 3,
            "max_consecutive_failures": 2,
            "max_provider_calls": 20,
            "max_policy_denials": 3,
        },
        "production_execution_allowed": production_allowed,
        "vector_mutation_allowed": vector_allowed,
        "secret_mutation_allowed": secret_allowed,
        "external_send_allowed": external_send_allowed,
    }
    d["profile_digest"] = compute_deterministic_digest(d)
    return d


def make_test_profile(**kwargs: Any) -> WorkProfile:
    return validate_work_profile(make_profile_dict(**kwargs))


def make_test_budget_state(
    budget_id: str = "b-test",
    decisions_used: int = 0,
    child_tasks_used: int = 0,
    retries_used: int = 0,
    fix_cycles_used: int = 0,
    consecutive_failures: int = 0,
    provider_calls_used: int = 0,
    policy_denials: int = 0,
    exhausted_dimensions: tuple[str, ...] = (),
) -> AutonomyBudgetState:
    b_dict = {
        "budget_id": budget_id,
        "child_tasks_used": child_tasks_used,
        "consecutive_failures": consecutive_failures,
        "decisions_used": decisions_used,
        "exhausted_dimensions": list(exhausted_dimensions),
        "fix_cycles_used": fix_cycles_used,
        "policy_denials": policy_denials,
        "provider_calls_used": provider_calls_used,
        "retries_used": retries_used,
        "schema_version": AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
    }
    b_dg = compute_deterministic_digest(b_dict)
    return AutonomyBudgetState(
        schema_version=AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
        budget_id=budget_id,
        budget_digest=b_dg,
        decisions_used=decisions_used,
        child_tasks_used=child_tasks_used,
        retries_used=retries_used,
        fix_cycles_used=fix_cycles_used,
        consecutive_failures=consecutive_failures,
        provider_calls_used=provider_calls_used,
        policy_denials=policy_denials,
        exhausted_dimensions=exhausted_dimensions,
    )


def make_test_autonomy_state(
    run_id: str = RUN_ID,
    profile: WorkProfile | None = None,
    current_level: AutonomyLevel = AutonomyLevel.LEVEL_2_DEV_AUTONOMY,
    budget_state: AutonomyBudgetState | None = None,
    successful_runs: int = 0,
    critical_failures: int = 0,
    rollback_verified: bool = False,
    required_validators_status: dict[str, str] | None = None,
) -> AutonomyState:
    prof = profile or make_test_profile()
    bs = budget_state or make_test_budget_state()
    vals = required_validators_status or {"tests": "PASS", "lint": "PASS"}
    a_dict = {
        "budget_state_digest": bs.budget_digest,
        "created_at_utc": TS,
        "critical_failures": critical_failures,
        "current_level": current_level.value,
        "last_transition_receipt_id": None,
        "maximum_allowed_level": prof.maximum_autonomy_level.value,
        "profile_digest": prof.profile_digest,
        "profile_id": prof.profile_id,
        "promotion_sequence": 0,
        "required_validators_status": vals,
        "rollback_verified": rollback_verified,
        "run_id": run_id,
        "schema_version": AUTONOMY_STATE_SCHEMA_VERSION,
        "successful_runs": successful_runs,
        "updated_at_utc": TS,
    }
    a_dg = compute_deterministic_digest(a_dict)
    return AutonomyState(
        schema_version=AUTONOMY_STATE_SCHEMA_VERSION,
        run_id=run_id,
        profile_id=prof.profile_id,
        profile_digest=prof.profile_digest,
        current_level=current_level,
        maximum_allowed_level=prof.maximum_autonomy_level,
        successful_runs=successful_runs,
        critical_failures=critical_failures,
        rollback_verified=rollback_verified,
        required_validators_status=vals,
        budget_state_digest=bs.budget_digest,
        promotion_sequence=0,
        last_transition_receipt_id=None,
        created_at_utc=TS,
        updated_at_utc=TS,
        state_digest=a_dg,
    )


def make_test_policy_request(
    intent: TaskIntent,
    effective_policy: EffectivePolicyReport,
    profile: WorkProfile,
    autonomy_state: AutonomyState,
    budget_state: AutonomyBudgetState,
    requested_action: str = "CONTINUE",
    requested_effects: tuple[EffectClass, ...] = (EffectClass.REPOSITORY_WRITE, EffectClass.GIT_COMMIT),
    requested_boundary: StopBoundary = StopBoundary.COMMIT,
    target: ExecutionTarget = ExecutionTarget.DEV,
    decision_id: str = "d" * 64,
    decision_receipt_id: str = "dr" * 64,
) -> PolicyRequest:
    return PolicyRequest(
        schema_version=POLICY_REQUEST_SCHEMA_VERSION,
        request_id="r" * 64,
        run_id=autonomy_state.run_id,
        task_id=intent.task_id,
        attempt_id=ATTEMPT_ID,
        intent_digest=intent_digest(intent),
        decision_id=decision_id,
        decision_receipt_id=decision_receipt_id,
        work_profile_id=profile.profile_id,
        work_profile_digest=profile.profile_digest,
        current_autonomy_level=autonomy_state.current_level,
        requested_action=requested_action,
        requested_effect_classes=requested_effects,
        requested_stop_boundary=requested_boundary,
        execution_target=target,
        effective_policy_id=effective_policy.effective_policy_id,
        effective_policy_digest=effective_policy.effective_policy_id,
        budget_state_digest=budget_state.budget_digest,
    )


def make_test_vr_pass(task_id: str = TASK_ID, idg: str = "") -> VerifiedResult:
    fixtures_dir = Path(__file__).parent / "fixtures" / "supervisor" / "task02"
    vr_text = (fixtures_dir / "supervisor_verified_result_pass.json").read_text()
    vr_base = deserialize_verified_result(vr_text)
    from dataclasses import replace
    return replace(
        vr_base,
        task_id=task_id,
        attempt_id=ATTEMPT_ID,
        intent_digest=idg or ("i" * 64),
    )


def make_test_decision(
    state: SupervisorState,
    vr: VerifiedResult,
    vr_digest: str,
    pack_dg: str,
    action: str = "CONTINUE",
) -> AstraDecision:
    d = {
        "schema_version": "hermes.astra-decision.v1",
        "run_id": state.run_id,
        "task_id": state.current_task_id,
        "attempt_id": state.current_attempt_id,
        "intent_digest": state.current_intent_digest,
        "verified_result_id": vr.result_id,
        "verified_result_digest": vr_digest,
        "context_pack_digest": pack_dg,
        "action": action,
        "rationale_summary": f"Automated decision: {action}",
        "next_objective": "Continue to next task" if action == "CONTINUE" else None,
        "acceptance_delta": None,
        "requested_required_gates": ["tests", "lint"],
        "created_at_utc": TS,
    }
    without_id = {k: v for k, v in d.items() if k != "decision_id"}
    raw = json.dumps(without_id, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    d["decision_id"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return deserialize_astra_decision(json.dumps(d))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. LEVEL MATRIX TESTS (Level 0, 1, 2, 3, 4, 5, 6 mappings to bounds)
# ═══════════════════════════════════════════════════════════════════════════════

class TestAutonomyLevelMatrix:
    def test_all_seven_levels_defined(self):
        levels = list(AutonomyLevel)
        assert len(levels) == 7
        assert AutonomyLevel.LEVEL_0_OBSERVE in levels
        assert AutonomyLevel.LEVEL_1_LOCAL_WRITE in levels
        assert AutonomyLevel.LEVEL_2_DEV_AUTONOMY in levels
        assert AutonomyLevel.LEVEL_3_CI_AUTONOMY in levels
        assert AutonomyLevel.LEVEL_4_STAGING_AUTONOMY in levels
        assert AutonomyLevel.LEVEL_5_PROD_PREP in levels
        assert AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION in levels

    def test_level_0_observe_bounds(self):
        intent = make_test_intent(allowed_mutations=("REPOSITORY_WRITE",), stop_boundary=StopBoundary.COMMIT)
        ep = make_test_effective_policy(intent)
        prof = make_test_profile(max_autonomy_level="LEVEL_6_AUTHORIZED_PRODUCTION")
        bs = make_test_budget_state()
        state = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_0_OBSERVE, profile=prof, budget_state=bs)

        # Level 0 requesting LOCAL_DIFF (> READ_ONLY) -> AUTONOMY_LEVEL_TOO_LOW
        req = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.READ_ONLY,),
            requested_boundary=StopBoundary.LOCAL_DIFF,
        )
        receipt = evaluate_policy(req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "AUTONOMY_LEVEL_TOO_LOW" in receipt.reason_codes

        # Level 0 requesting non-READ_ONLY mutation -> EFFECT_CLASS_DENIED
        req2 = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.REPOSITORY_WRITE,),
            requested_boundary=StopBoundary.READ_ONLY,
        )
        receipt2 = evaluate_policy(req2, intent, ep, prof, state, bs)
        assert receipt2.verdict == PolicyVerdict.DENY
        assert "EFFECT_CLASS_DENIED" in receipt2.reason_codes

        # Level 0 requesting READ_ONLY boundary + effect -> ALLOW
        req3 = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.READ_ONLY,),
            requested_boundary=StopBoundary.READ_ONLY,
        )
        receipt3 = evaluate_policy(req3, intent, ep, prof, state, bs)
        assert receipt3.verdict == PolicyVerdict.ALLOW

    def test_level_1_local_write_bounds(self):
        intent = make_test_intent(allowed_mutations=("REPOSITORY_WRITE", "GIT_COMMIT"), stop_boundary=StopBoundary.COMMIT)
        ep = make_test_effective_policy(intent)
        prof = make_test_profile(max_autonomy_level="LEVEL_6_AUTHORIZED_PRODUCTION")
        bs = make_test_budget_state()
        state = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_1_LOCAL_WRITE, profile=prof, budget_state=bs)

        # Level 1 requesting COMMIT boundary (> LOCAL_DIFF) -> AUTONOMY_LEVEL_TOO_LOW
        req = make_test_policy_request(intent, ep, prof, state, bs, requested_boundary=StopBoundary.COMMIT)
        receipt = evaluate_policy(req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "AUTONOMY_LEVEL_TOO_LOW" in receipt.reason_codes

        # Level 1 requesting GIT_COMMIT effect (not allowed at level 1) -> EFFECT_CLASS_DENIED
        req2 = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.GIT_COMMIT,),
            requested_boundary=StopBoundary.LOCAL_DIFF,
        )
        receipt2 = evaluate_policy(req2, intent, ep, prof, state, bs)
        assert receipt2.verdict == PolicyVerdict.DENY
        assert "EFFECT_CLASS_DENIED" in receipt2.reason_codes

        # Level 1 requesting LOCAL_DIFF + REPOSITORY_WRITE -> ALLOW
        req3 = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.REPOSITORY_WRITE,),
            requested_boundary=StopBoundary.LOCAL_DIFF,
        )
        receipt3 = evaluate_policy(req3, intent, ep, prof, state, bs)
        assert receipt3.verdict == PolicyVerdict.ALLOW

    def test_level_2_dev_autonomy_bounds(self):
        intent = make_test_intent(
            allowed_mutations=("REPOSITORY_WRITE", "GIT_COMMIT", "PR_MUTATION"),
            stop_boundary=StopBoundary.READY_PR,
        )
        ep = make_test_effective_policy(intent)
        prof = make_test_profile(
            max_autonomy_level="LEVEL_6_AUTHORIZED_PRODUCTION",
            allowed_effect_classes=["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "PR_MUTATION"],
        )
        bs = make_test_budget_state()
        state = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY, profile=prof, budget_state=bs)

        # Level 2 requesting READY_PR boundary (> COMMIT) -> AUTONOMY_LEVEL_TOO_LOW
        req = make_test_policy_request(intent, ep, prof, state, bs, requested_boundary=StopBoundary.READY_PR)
        receipt = evaluate_policy(req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "AUTONOMY_LEVEL_TOO_LOW" in receipt.reason_codes

        # Level 2 requesting PR_MUTATION effect -> EFFECT_CLASS_DENIED
        req2 = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.PR_MUTATION,),
            requested_boundary=StopBoundary.COMMIT,
        )
        receipt2 = evaluate_policy(req2, intent, ep, prof, state, bs)
        assert receipt2.verdict == PolicyVerdict.DENY
        assert "EFFECT_CLASS_DENIED" in receipt2.reason_codes

        # Level 2 requesting COMMIT + GIT_COMMIT -> ALLOW
        req3 = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.GIT_COMMIT,),
            requested_boundary=StopBoundary.COMMIT,
        )
        receipt3 = evaluate_policy(req3, intent, ep, prof, state, bs)
        assert receipt3.verdict == PolicyVerdict.ALLOW

    def test_level_3_4_5_6_ceilings(self):
        intent = make_test_intent(
            allowed_mutations=("REPOSITORY_WRITE", "GIT_COMMIT", "BUILD", "DEPLOY"),
            forbidden_mutations=(),
            stop_boundary=StopBoundary.LIVE_SMOKE,
        )
        ep = make_test_effective_policy(intent)
        prof = make_test_profile(
            max_autonomy_level="LEVEL_6_AUTHORIZED_PRODUCTION",
            allowed_effect_classes=["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "BUILD", "DEPLOY"],
            forbidden_effect_classes=[],
            allowed_targets=["LOCAL", "DEV", "CI", "STAGING", "PRODUCTION"],
            production_allowed=True,
        )
        bs = make_test_budget_state()

        # Level 3 CI ceiling is READY_PR (rank 4) -> requesting DEPLOY (rank 6) fails
        s3 = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_3_CI_AUTONOMY, profile=prof, budget_state=bs)
        req3 = make_test_policy_request(intent, ep, prof, s3, bs, requested_boundary=StopBoundary.DEPLOY)
        assert "AUTONOMY_LEVEL_TOO_LOW" in evaluate_policy(req3, intent, ep, prof, s3, bs).reason_codes

        # Level 4 Staging ceiling is DEPLOY (rank 6) -> requesting LIVE_SMOKE (rank 7) fails
        s4 = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY, profile=prof, budget_state=bs)
        req4 = make_test_policy_request(intent, ep, prof, s4, bs, requested_boundary=StopBoundary.LIVE_SMOKE)
        assert "AUTONOMY_LEVEL_TOO_LOW" in evaluate_policy(req4, intent, ep, prof, s4, bs).reason_codes

        # Level 5 Prod Prep ceiling is BUILD (rank 3) -> requesting READY_PR (rank 4) fails
        s5 = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_5_PROD_PREP, profile=prof, budget_state=bs)
        req5 = make_test_policy_request(intent, ep, prof, s5, bs, requested_boundary=StopBoundary.READY_PR)
        assert "AUTONOMY_LEVEL_TOO_LOW" in evaluate_policy(req5, intent, ep, prof, s5, bs).reason_codes

        # Level 6 Authorized Production ceiling is LIVE_SMOKE (rank 7) -> requesting LIVE_SMOKE succeeds
        s6 = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION, profile=prof, budget_state=bs)
        req6 = make_test_policy_request(
            intent, ep, prof, s6, bs,
            requested_boundary=StopBoundary.LIVE_SMOKE,
            requested_effects=(EffectClass.DEPLOY,),
            target=ExecutionTarget.PRODUCTION,
        )
        receipt6 = evaluate_policy(req6, intent, ep, prof, s6, bs)
        assert receipt6.verdict == PolicyVerdict.ALLOW, f"Reasons: {receipt6.reason_codes}"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. AUTHORITY INTERSECTION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthorityIntersection:
    def test_intent_forbidden_mutation_denied(self):
        intent = make_test_intent(
            allowed_mutations=("REPOSITORY_WRITE", "GIT_COMMIT", "PR_MUTATION"),
            forbidden_mutations=("PR_MUTATION",),
        )
        ep = make_test_effective_policy(intent)
        prof = make_test_profile(
            allowed_effect_classes=["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT", "PR_MUTATION"],
        )
        bs = make_test_budget_state()
        state = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_3_CI_AUTONOMY, profile=prof, budget_state=bs)

        req = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.PR_MUTATION,),
            requested_boundary=StopBoundary.READY_PR,
        )
        receipt = evaluate_policy(req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "TASK_INTENT_AUTHORITY_DENIED" in receipt.reason_codes

    def test_profile_forbidden_effect_denied(self):
        intent = make_test_intent(allowed_mutations=("REPOSITORY_WRITE", "GIT_COMMIT"))
        ep = make_test_effective_policy(intent)
        prof = make_test_profile(
            allowed_effect_classes=["READ_ONLY", "REPOSITORY_WRITE"],
            forbidden_effect_classes=["GIT_COMMIT"],
        )
        bs = make_test_budget_state()
        state = make_test_autonomy_state(current_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY, profile=prof, budget_state=bs)

        req = make_test_policy_request(
            intent, ep, prof, state, bs,
            requested_effects=(EffectClass.GIT_COMMIT,),
            requested_boundary=StopBoundary.COMMIT,
        )
        receipt = evaluate_policy(req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "WORK_PROFILE_DENIED" in receipt.reason_codes

    def test_special_mutation_permissions(self):
        intent = make_test_intent(
            allowed_mutations=("REPOSITORY_WRITE", "VECTOR_MUTATION", "SECRET_MUTATION", "EXTERNAL_SEND"),
            stop_boundary=StopBoundary.DEPLOY,
        )
        ep = make_test_effective_policy(intent)
        bs = make_test_budget_state()

        # Vector mutation disallowed by profile
        prof_no_vec = make_test_profile(
            allowed_effect_classes=["READ_ONLY", "VECTOR_MUTATION"],
            vector_allowed=False,
        )
        state_vec = make_test_autonomy_state(profile=prof_no_vec, budget_state=bs, current_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY)
        req_vec = make_test_policy_request(
            intent, ep, prof_no_vec, state_vec, bs,
            requested_effects=(EffectClass.VECTOR_MUTATION,),
            requested_boundary=StopBoundary.COMMIT,
        )
        r_vec = evaluate_policy(req_vec, intent, ep, prof_no_vec, state_vec, bs)
        assert "VECTOR_MUTATION_PERMISSION_REQUIRED" in r_vec.reason_codes

        # Secret mutation disallowed
        prof_no_sec = make_test_profile(
            allowed_effect_classes=["READ_ONLY", "SECRET_MUTATION"],
            secret_allowed=False,
        )
        state_sec = make_test_autonomy_state(profile=prof_no_sec, budget_state=bs, current_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY)
        req_sec = make_test_policy_request(
            intent, ep, prof_no_sec, state_sec, bs,
            requested_effects=(EffectClass.SECRET_MUTATION,),
            requested_boundary=StopBoundary.COMMIT,
        )
        r_sec = evaluate_policy(req_sec, intent, ep, prof_no_sec, state_sec, bs)
        assert "SECRET_MUTATION_PERMISSION_REQUIRED" in r_sec.reason_codes

        # External send disallowed
        prof_no_ext = make_test_profile(
            allowed_effect_classes=["READ_ONLY", "EXTERNAL_SEND"],
            external_send_allowed=False,
        )
        state_ext = make_test_autonomy_state(profile=prof_no_ext, budget_state=bs, current_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY)
        req_ext = make_test_policy_request(
            intent, ep, prof_no_ext, state_ext, bs,
            requested_effects=(EffectClass.EXTERNAL_SEND,),
            requested_boundary=StopBoundary.COMMIT,
        )
        r_ext = evaluate_policy(req_ext, intent, ep, prof_no_ext, state_ext, bs)
        assert "EXTERNAL_SEND_PERMISSION_REQUIRED" in r_ext.reason_codes

    def test_target_environment_constraints(self):
        intent = make_test_intent(allowed_mutations=("REPOSITORY_WRITE", "DEPLOY"), stop_boundary=StopBoundary.LIVE_SMOKE)
        ep = make_test_effective_policy(intent)
        bs = make_test_budget_state()

        # Target not in allowed targets
        prof_dev_only = make_test_profile(allowed_targets=["LOCAL", "DEV"])
        s_dev = make_test_autonomy_state(profile=prof_dev_only, budget_state=bs, current_level=AutonomyLevel.LEVEL_4_STAGING_AUTONOMY)
        req_staging = make_test_policy_request(
            intent, ep, prof_dev_only, s_dev, bs,
            target=ExecutionTarget.STAGING,
            requested_boundary=StopBoundary.COMMIT,
        )
        r_staging = evaluate_policy(req_staging, intent, ep, prof_dev_only, s_dev, bs)
        assert "TARGET_ENVIRONMENT_DENIED" in r_staging.reason_codes

        # Production requested without profile permission
        prof_no_prod = make_test_profile(
            allowed_targets=["LOCAL", "DEV", "PRODUCTION"],
            production_allowed=False,
            max_autonomy_level="LEVEL_6_AUTHORIZED_PRODUCTION",
        )
        s_prod = make_test_autonomy_state(profile=prof_no_prod, budget_state=bs, current_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION)
        req_prod = make_test_policy_request(
            intent, ep, prof_no_prod, s_prod, bs,
            target=ExecutionTarget.PRODUCTION,
            requested_boundary=StopBoundary.LIVE_SMOKE,
        )
        r_prod = evaluate_policy(req_prod, intent, ep, prof_no_prod, s_prod, bs)
        assert "PRODUCTION_PERMISSION_REQUIRED" in r_prod.reason_codes

        # Production requested with level < LEVEL_6
        prof_prod = make_test_profile(
            allowed_targets=["LOCAL", "DEV", "PRODUCTION"],
            production_allowed=True,
            max_autonomy_level="LEVEL_6_AUTHORIZED_PRODUCTION",
        )
        s_low = make_test_autonomy_state(profile=prof_prod, budget_state=bs, current_level=AutonomyLevel.LEVEL_5_PROD_PREP)
        req_low = make_test_policy_request(
            intent, ep, prof_prod, s_low, bs,
            target=ExecutionTarget.PRODUCTION,
            requested_boundary=StopBoundary.BUILD,
        )
        r_low = evaluate_policy(req_low, intent, ep, prof_prod, s_low, bs)
        assert "AUTONOMY_LEVEL_TOO_LOW" in r_low.reason_codes


# ═══════════════════════════════════════════════════════════════════════════════
# 3. POLICY BINDING TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestPolicyBinding:
    def test_all_binding_mismatches_rejected(self):
        intent = make_test_intent()
        ep = make_test_effective_policy(intent)
        prof = make_test_profile()
        bs = make_test_budget_state()
        state = make_test_autonomy_state(profile=prof, budget_state=bs)

        # 1. intent_digest mismatch
        r1 = replace(make_test_policy_request(intent, ep, prof, state, bs), intent_digest="f" * 64)
        assert "POLICY_BINDING_MISMATCH" in evaluate_policy(r1, intent, ep, prof, state, bs).reason_codes

        # 2. effective_policy_digest mismatch
        r2 = replace(make_test_policy_request(intent, ep, prof, state, bs), effective_policy_digest="f" * 64)
        assert "POLICY_BINDING_MISMATCH" in evaluate_policy(r2, intent, ep, prof, state, bs).reason_codes

        # 3. work_profile_digest mismatch
        r3 = replace(make_test_policy_request(intent, ep, prof, state, bs), work_profile_digest="f" * 64)
        assert "POLICY_BINDING_MISMATCH" in evaluate_policy(r3, intent, ep, prof, state, bs).reason_codes

        # 4. budget_state_digest mismatch
        r4 = replace(make_test_policy_request(intent, ep, prof, state, bs), budget_state_digest="f" * 64)
        assert "POLICY_BINDING_MISMATCH" in evaluate_policy(r4, intent, ep, prof, state, bs).reason_codes

        # 5. current_autonomy_level mismatch
        r5 = replace(make_test_policy_request(intent, ep, prof, state, bs), current_autonomy_level=AutonomyLevel.LEVEL_0_OBSERVE)
        assert "POLICY_BINDING_MISMATCH" in evaluate_policy(r5, intent, ep, prof, state, bs).reason_codes

        # 6. task_id mismatch
        r6 = replace(make_test_policy_request(intent, ep, prof, state, bs), task_id="different-task")
        assert "POLICY_BINDING_MISMATCH" in evaluate_policy(r6, intent, ep, prof, state, bs).reason_codes

    def test_incomplete_effective_policy_denied(self):
        intent = make_test_intent()
        ep = make_test_effective_policy(intent, status=EffectivePolicyStatus.INCOMPLETE)
        prof = make_test_profile()
        bs = make_test_budget_state()
        state = make_test_autonomy_state(profile=prof, budget_state=bs)

        req = make_test_policy_request(intent, ep, prof, state, bs)
        receipt = evaluate_policy(req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "EFFECTIVE_POLICY_INCOMPLETE" in receipt.reason_codes


# ═══════════════════════════════════════════════════════════════════════════════
# 4. PROMOTION AND DEMOTION TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestPromotionAndDemotion:
    def test_evaluate_promotion_criteria(self):
        prof = make_test_profile(
            max_autonomy_level="LEVEL_3_CI_AUTONOMY",
            promotion_thresholds={
                "required_successful_runs": 3,
                "allowed_critical_failures": 0,
                "require_rollback_verified": True,
            },
        )
        bs = make_test_budget_state()

        # Happy path: eligible for promotion
        s_pass = make_test_autonomy_state(
            profile=prof,
            budget_state=bs,
            current_level=AutonomyLevel.LEVEL_1_LOCAL_WRITE,
            successful_runs=3,
            critical_failures=0,
            rollback_verified=True,
            required_validators_status={"tests": "PASS", "lint": "PASS"},
        )
        assert evaluate_promotion(s_pass, prof, operator_policy_allows=True) == AutonomyLevel.LEVEL_2_DEV_AUTONOMY

        # Insufficient successful runs
        s_low_runs = replace(s_pass, successful_runs=2)
        assert evaluate_promotion(s_low_runs, prof, operator_policy_allows=True) == AutonomyLevel.LEVEL_1_LOCAL_WRITE

        # Critical failures exceed allowed
        s_fails = replace(s_pass, critical_failures=1)
        assert evaluate_promotion(s_fails, prof, operator_policy_allows=True) == AutonomyLevel.LEVEL_1_LOCAL_WRITE

        # Rollback not verified
        s_no_rb = replace(s_pass, rollback_verified=False)
        assert evaluate_promotion(s_no_rb, prof, operator_policy_allows=True) == AutonomyLevel.LEVEL_1_LOCAL_WRITE

        # Validator not PASS
        s_val_fail = replace(s_pass, required_validators_status={"tests": "PASS", "lint": "FAIL"})
        assert evaluate_promotion(s_val_fail, prof, operator_policy_allows=True) == AutonomyLevel.LEVEL_1_LOCAL_WRITE

        # Operator policy disallows
        assert evaluate_promotion(s_pass, prof, operator_policy_allows=False) == AutonomyLevel.LEVEL_1_LOCAL_WRITE

        # Already at maximum level
        s_max = replace(s_pass, current_level=AutonomyLevel.LEVEL_3_CI_AUTONOMY)
        assert evaluate_promotion(s_max, prof, operator_policy_allows=True) == AutonomyLevel.LEVEL_3_CI_AUTONOMY

    def test_evaluate_demotion_criteria(self):
        prof = make_test_profile(max_autonomy_level="LEVEL_2_DEV_AUTONOMY")
        bs = make_test_budget_state()
        state = make_test_autonomy_state(profile=prof, budget_state=bs, current_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY)

        # Critical failure -> demoted to LEVEL_0_OBSERVE
        assert evaluate_demotion(state, prof, critical_failure_occurred=True, policy_invalidated=False) == AutonomyLevel.LEVEL_0_OBSERVE

        # Policy invalidated -> demoted to LEVEL_0_OBSERVE
        assert evaluate_demotion(state, prof, critical_failure_occurred=False, policy_invalidated=True) == AutonomyLevel.LEVEL_0_OBSERVE

        # Current level exceeds maximum -> clamped to maximum
        state_exceeded = replace(state, current_level=AutonomyLevel.LEVEL_5_PROD_PREP)
        assert evaluate_demotion(state_exceeded, prof, critical_failure_occurred=False, policy_invalidated=False) == AutonomyLevel.LEVEL_2_DEV_AUTONOMY

        # Normal condition -> retains level
        assert evaluate_demotion(state, prof, critical_failure_occurred=False, policy_invalidated=False) == AutonomyLevel.LEVEL_2_DEV_AUTONOMY


# ═══════════════════════════════════════════════════════════════════════════════
# 5. BUDGET TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestBudgetLimits:
    def test_budget_consumption_and_exhaustion(self):
        limits = BudgetLimits(
            max_supervisor_decisions=5,
            max_child_tasks=3,
            max_retries_per_task=2,
            max_fix_cycles_per_task=2,
            max_consecutive_failures=2,
            max_provider_calls=10,
            max_policy_denials=2,
        )
        bs = make_test_budget_state()

        # Step 1: Normal increments without exhaustion
        bs1 = evaluate_budget_consumption(bs, limits, decisions_increment=1, child_tasks_increment=1)
        assert bs1.decisions_used == 1
        assert bs1.child_tasks_used == 1
        assert bs1.exhausted_dimensions == ()

        # Step 2: Exhaust child tasks
        bs2 = evaluate_budget_consumption(bs1, limits, child_tasks_increment=2)
        assert "CHILD_TASKS" in bs2.exhausted_dimensions

        # Step 3: Exhaust consecutive failures
        bs3 = evaluate_budget_consumption(bs, limits, failures_increment=2)
        assert "FAILURES" in bs3.exhausted_dimensions

        # Step 4: Reset consecutive failures
        bs4 = evaluate_budget_consumption(bs3, limits, failures_reset=True)
        assert bs4.consecutive_failures == 0
        assert "FAILURES" not in bs4.exhausted_dimensions

        # Step 5: Exhaust policy denials
        bs5 = evaluate_budget_consumption(bs, limits, policy_denials_increment=2)
        assert "POLICY_DENIALS" in bs5.exhausted_dimensions

        # Step 6: Exhausted budget causes evaluate_policy to DENY
        intent = make_test_intent()
        ep = make_test_effective_policy(intent)
        prof = make_test_profile()
        s_exh = make_test_autonomy_state(profile=prof, budget_state=bs5)
        req = make_test_policy_request(intent, ep, prof, s_exh, bs5)
        receipt = evaluate_policy(req, intent, ep, prof, s_exh, bs5)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "AUTONOMY_BUDGET_EXHAUSTED" in receipt.reason_codes


# ═══════════════════════════════════════════════════════════════════════════════
# 6. WORK PROFILE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestWorkProfile:
    def test_valid_profile_parsing(self):
        d = make_profile_dict()
        profile = validate_work_profile(d)
        assert profile.schema_version == WORK_PROFILE_SCHEMA_VERSION
        assert profile.profile_id == "wp-test-profile"
        assert profile.maximum_autonomy_level == AutonomyLevel.LEVEL_2_DEV_AUTONOMY
        assert profile.budget_limits.max_supervisor_decisions == 10

    def test_unsupported_schema_version_rejected(self):
        d = make_profile_dict()
        d["schema_version"] = "hermes.work-profile.v999"
        d["profile_digest"] = compute_deterministic_digest(d)
        with pytest.raises(PolicyError) as exc:
            validate_work_profile(d)
        assert exc.value.code == "UNSUPPORTED_SCHEMA"

    def test_forbidden_raw_fields_rejected(self):
        d = make_profile_dict()
        d["api_key"] = "super-secret-key"
        d["profile_digest"] = compute_deterministic_digest(d)
        with pytest.raises(PolicyError) as exc:
            validate_work_profile(d)
        assert exc.value.code == "FORBIDDEN_RAW_FIELDS"

    def test_tampered_profile_digest_rejected(self):
        d = make_profile_dict()
        # Alter field without recomputing profile_digest
        d["maximum_autonomy_level"] = "LEVEL_6_AUTHORIZED_PRODUCTION"
        with pytest.raises(PolicyError) as exc:
            validate_work_profile(d)
        assert exc.value.code == "PROFILE_DIGEST_MISMATCH"

    def test_malformed_profile_rejected(self):
        d = make_profile_dict()
        del d["allowed_effect_classes"]
        with pytest.raises(PolicyError) as exc:
            validate_work_profile(d)
        assert exc.value.code == "MALFORMED_PROFILE"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. OFFLINE E2E ALLOW TEST
# ═══════════════════════════════════════════════════════════════════════════════

class TestOfflineE2EAllow:
    def test_full_allow_lifecycle_and_replay(self, tmp_path):
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)

        intent = make_test_intent(
            allowed_mutations=("REPOSITORY_WRITE", "GIT_COMMIT"),
            stop_boundary=StopBoundary.COMMIT,
        )
        lineage = make_test_lineage()
        ep = make_test_effective_policy(intent)
        prof = make_test_profile(
            max_autonomy_level="LEVEL_2_DEV_AUTONOMY",
            allowed_effect_classes=["READ_ONLY", "REPOSITORY_WRITE", "GIT_COMMIT"],
        )

        # 1. Initialize run
        state1 = loop.initialize_run(intent, lineage, "Implement feature", RUN_ID, TS, ATTEMPT_ID)
        assert state1.phase == SupervisorPhase.RUNNING
        assert state1.autonomy_state is None

        # 2. Bind profile
        state2 = loop.bind_profile(
            RUN_ID,
            prof,
            initial_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY,
            effective_policy=ep,
        )
        assert state2.autonomy_state is not None
        assert state2.autonomy_state.current_level == AutonomyLevel.LEVEL_2_DEV_AUTONOMY
        assert state2.budget_state is not None
        assert state2.budget_state.decisions_used == 0

        # 3. Ingest verified result
        vr = make_test_vr_pass(idg=intent_digest(intent))
        state3 = loop.ingest_verified_result(RUN_ID, vr, "vr_dg_" + "1" * 58)
        assert state3.phase == SupervisorPhase.VERIFYING

        # 4. Build context pack
        pack, state4 = loop.build_context_pack(RUN_ID, intent)
        pack_dg = context_pack_digest(pack)
        assert state4.phase == SupervisorPhase.DECIDING

        # 5. Accept decision (CONTINUE -> candidate child task -> evaluate policy -> ALLOW)
        decision = make_test_decision(state4, vr, "vr_dg_" + "1" * 58, pack_dg, action="CONTINUE")
        receipt, state5 = loop.accept_decision(
            RUN_ID,
            decision,
            pack_dg,
            verified_result_status="PASS",
            validated_at_utc=TS,
            parent_intent=intent,
            effective_policy=ep,
        )

        assert receipt.receipt_id is not None
        # On ALLOW: advances to candidate child task, phase is RUNNING
        assert state5.phase == SupervisorPhase.RUNNING
        assert state5.current_task_id != intent.task_id  # Candidate task is authoritative
        assert state5.budget_state.child_tasks_used == 1
        assert state5.budget_state.decisions_used == 1

        # Check event log
        events = store.load_events(RUN_ID)
        event_types = [e.event_type for e in events]
        assert SupervisorEventType.RUN_INITIALIZED in event_types
        assert SupervisorEventType.WORK_PROFILE_BOUND in event_types
        assert SupervisorEventType.RESULT_INGESTED in event_types
        assert SupervisorEventType.CONTEXT_PACK_BUILT in event_types
        assert SupervisorEventType.DECISION_ACCEPTED in event_types
        assert SupervisorEventType.POLICY_EVALUATED in event_types
        assert SupervisorEventType.NEXT_TASK_GENERATED in event_types

        # 6. Replay verification: reconstruct state without memory cache
        store2 = FileSupervisorStateStore(tmp_path)
        loop2 = SupervisorLoop(store2)
        replayed = loop2.replay(RUN_ID)

        assert replayed.current_task_id == state5.current_task_id
        assert replayed.phase == state5.phase
        assert replayed.state_revision == state5.state_revision
        assert replayed.budget_state.child_tasks_used == 1
        assert replayed.budget_state.decisions_used == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 8. OFFLINE E2E DENY TEST
# ═══════════════════════════════════════════════════════════════════════════════

class TestOfflineE2EDeny:
    def test_full_deny_lifecycle_and_stopped_continuation(self, tmp_path):
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)

        intent = make_test_intent(
            allowed_mutations=("REPOSITORY_WRITE", "GIT_COMMIT"),
            stop_boundary=StopBoundary.COMMIT,
        )
        lineage = make_test_lineage()
        ep = make_test_effective_policy(intent)

        # Restrictive profile: only LEVEL_0_OBSERVE (READ_ONLY), but decision will attempt REPOSITORY_WRITE
        prof = make_test_profile(
            max_autonomy_level="LEVEL_0_OBSERVE",
            allowed_effect_classes=["READ_ONLY"],
        )

        # 1. Initialize run
        loop.initialize_run(intent, lineage, "Observe only", RUN_ID, TS, ATTEMPT_ID)

        # 2. Bind profile with LEVEL_0_OBSERVE
        loop.bind_profile(
            RUN_ID,
            prof,
            initial_level=AutonomyLevel.LEVEL_0_OBSERVE,
            effective_policy=ep,
        )

        # 3. Ingest verified result
        vr = make_test_vr_pass(idg=intent_digest(intent))
        loop.ingest_verified_result(RUN_ID, vr, "vr_dg_" + "1" * 58)

        # 4. Build context pack
        pack, state4 = loop.build_context_pack(RUN_ID, intent)
        pack_dg = context_pack_digest(pack)

        # 5. Accept decision (CONTINUE requires mutations that violate LEVEL_0)
        decision = make_test_decision(state4, vr, "vr_dg_" + "1" * 58, pack_dg, action="CONTINUE")
        receipt, state5 = loop.accept_decision(
            RUN_ID,
            decision,
            pack_dg,
            verified_result_status="PASS",
            validated_at_utc=TS,
            parent_intent=intent,
            effective_policy=ep,
        )

        assert receipt.receipt_id is not None
        # On DENY: autonomous continuation STOPPED
        assert state5.phase == SupervisorPhase.BLOCKED
        # Candidate task must NOT be authoritative -> current_task_id remains original
        assert state5.current_task_id == intent.task_id
        assert state5.budget_state.policy_denials == 1
        assert state5.budget_state.decisions_used == 1
        assert any("POLICY_DENIED" in b for b in state5.blockers)

        # Verify events: POLICY_EVALUATED and BLOCKER_RECORDED are present, but NEXT_TASK_GENERATED is NOT
        events = store.load_events(RUN_ID)
        event_types = [e.event_type for e in events]
        assert SupervisorEventType.DECISION_ACCEPTED in event_types
        assert SupervisorEventType.POLICY_EVALUATED in event_types
        assert SupervisorEventType.BLOCKER_RECORDED in event_types
        assert SupervisorEventType.NEXT_TASK_GENERATED not in event_types

        # 6. Replay verification
        store2 = FileSupervisorStateStore(tmp_path)
        loop2 = SupervisorLoop(store2)
        replayed = loop2.replay(RUN_ID)

        assert replayed.current_task_id == intent.task_id
        assert replayed.phase == SupervisorPhase.BLOCKED
        assert replayed.budget_state.policy_denials == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 9. POLICY TAMPER TEST
# ═══════════════════════════════════════════════════════════════════════════════

class TestPolicyTamper:
    def test_tampered_policy_request_rejected(self):
        intent = make_test_intent()
        ep = make_test_effective_policy(intent)
        prof = make_test_profile()
        bs = make_test_budget_state()
        state = make_test_autonomy_state(profile=prof, budget_state=bs)

        # Tampering with request digest fields
        req = make_test_policy_request(intent, ep, prof, state, bs)
        tampered_req = replace(req, task_id="unauthorized-task-id")
        receipt = evaluate_policy(tampered_req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "POLICY_BINDING_MISMATCH" in receipt.reason_codes

    def test_tampered_event_chain_rejected(self, tmp_path):
        store = FileSupervisorStateStore(tmp_path)
        loop = SupervisorLoop(store)
        intent = make_test_intent()
        lineage = make_test_lineage()
        loop.initialize_run(intent, lineage, "Tamper test", RUN_ID, TS, ATTEMPT_ID)

        events = store.load_events(RUN_ID)
        ev = events[0]
        # Tamper with the event file on disk
        event_file = store._events_dir(RUN_ID) / f"{ev.sequence:06d}_{ev.event_id}.json"
        raw_dict = json.loads(event_file.read_text())
        raw_dict["payload_digest"] = "0" * 64
        event_file.write_text(json.dumps(raw_dict))

        with pytest.raises(SupervisorError) as exc:
            store.load_events(RUN_ID)
        assert exc.value.code in ("EVENT_TAMPERED", "EVENT_PAYLOAD_TAMPERED")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. PROFILE TAMPER TEST
# ═══════════════════════════════════════════════════════════════════════════════

class TestProfileTamper:
    def test_profile_field_tamper_fails_digest(self):
        # Create a valid profile dict
        d = make_profile_dict(max_autonomy_level="LEVEL_1_LOCAL_WRITE")
        # Attacker escalates maximum_autonomy_level without updating profile_digest
        d["maximum_autonomy_level"] = "LEVEL_6_AUTHORIZED_PRODUCTION"
        with pytest.raises(PolicyError) as exc:
            validate_work_profile(d)
        assert exc.value.code == "PROFILE_DIGEST_MISMATCH"

    def test_profile_tamper_in_policy_request(self):
        intent = make_test_intent()
        ep = make_test_effective_policy(intent)
        prof = make_test_profile()
        bs = make_test_budget_state()
        state = make_test_autonomy_state(profile=prof, budget_state=bs)

        # Attacker provides different work_profile_digest in PolicyRequest
        req = make_test_policy_request(intent, ep, prof, state, bs)
        tampered_req = replace(req, work_profile_digest="deadbeef" * 8)
        receipt = evaluate_policy(tampered_req, intent, ep, prof, state, bs)
        assert receipt.verdict == PolicyVerdict.DENY
        assert "POLICY_BINDING_MISMATCH" in receipt.reason_codes
