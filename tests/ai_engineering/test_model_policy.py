from __future__ import annotations

import json

import pytest

from ai_engineering.contracts import (
    MODEL_POLICY_VERSION,
    AuthorityBoundary,
    EffectClass,
    LLMOpsPolicyError,
    ReasoningLevel,
    Status,
    StopBoundary,
    SubstitutionClass,
    TaskClass,
)
from ai_engineering.model_policy import (
    MODEL_APPROVED_55,
    MODEL_SOL,
    MODEL_SOL_ULTRA,
    MODEL_TERRA,
    evaluate_model_selection,
    normalize_model_policy_receipt,
    recommend_model,
    serialize_model_policy_receipt,
)
from ai_engineering.redaction import verify_sanitized_evidence


@pytest.mark.parametrize(
    ("task_class", "model", "reasoning", "alternatives"),
    [
        (TaskClass.REPOSITORY_SEARCH_LOGS, MODEL_TERRA, ReasoningLevel.MEDIUM, ()),
        (
            TaskClass.SMALL_PRECISE_FIX,
            MODEL_TERRA,
            ReasoningLevel.MEDIUM,
            (MODEL_APPROVED_55,),
        ),
        (TaskClass.BOUNDED_IMPLEMENTATION, MODEL_SOL, ReasoningLevel.MEDIUM, ()),
        (TaskClass.ARCHITECTURE, MODEL_SOL, ReasoningLevel.HIGH, ()),
        (TaskClass.MIGRATION_ROLLBACK_DESIGN, MODEL_SOL, ReasoningLevel.HIGH, ()),
        (TaskClass.SECURITY_AUDIT, MODEL_SOL, ReasoningLevel.HIGH, ()),
        (
            TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION,
            MODEL_SOL_ULTRA,
            ReasoningLevel.HIGH,
            (),
        ),
    ],
)
def test_normative_model_matrix(
    task_class: TaskClass,
    model: str,
    reasoning: ReasoningLevel,
    alternatives: tuple[str, ...],
) -> None:
    recommendation = recommend_model(task_class)
    assert recommendation.policy_version == MODEL_POLICY_VERSION
    assert recommendation.recommended_model == model
    assert recommendation.recommended_reasoning is reasoning
    assert recommendation.allowed_alternatives == alternatives


def test_task_class_parsing_is_closed_and_unknown_blocks_selection() -> None:
    with pytest.raises(LLMOpsPolicyError) as caught:
        recommend_model("UNCLASSIFIED_WORK")
    assert caught.value.code == "MODEL_POLICY_TASK_CLASS_UNKNOWN"

    receipt = evaluate_model_selection(
        task_class="UNCLASSIFIED_WORK",
        actual_model=MODEL_SOL,
        actual_reasoning="HIGH",
    )
    assert receipt.status is Status.BLOCKED
    assert receipt.reason_codes == ("MODEL_POLICY_TASK_CLASS_UNKNOWN",)


def test_exact_selection_and_higher_reasoning_pass() -> None:
    exact = evaluate_model_selection(
        task_class=TaskClass.ARCHITECTURE,
        actual_model=MODEL_SOL,
        actual_reasoning=ReasoningLevel.HIGH,
    )
    higher = evaluate_model_selection(
        task_class=TaskClass.ARCHITECTURE,
        actual_model=MODEL_SOL,
        actual_reasoning=ReasoningLevel.VERY_HIGH,
    )
    assert exact.status is Status.PASS
    assert higher.status is Status.PASS


def test_lower_reasoning_fails() -> None:
    receipt = evaluate_model_selection(
        task_class=TaskClass.SECURITY_AUDIT,
        actual_model=MODEL_SOL,
        actual_reasoning=ReasoningLevel.MEDIUM,
    )
    assert receipt.status is Status.FAIL
    assert "MODEL_POLICY_REASONING_INSUFFICIENT" in receipt.reason_codes


def test_listed_alternative_requires_explicit_classification() -> None:
    allowed = evaluate_model_selection(
        task_class=TaskClass.SMALL_PRECISE_FIX,
        actual_model=MODEL_APPROVED_55,
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.ALLOWED_ALTERNATIVE,
        substitution_reason_code="APPROVED_ALTERNATIVE",
    )
    undeclared = evaluate_model_selection(
        task_class=TaskClass.SMALL_PRECISE_FIX,
        actual_model=MODEL_APPROVED_55,
        actual_reasoning=ReasoningLevel.HIGH,
    )
    assert allowed.status is Status.PASS
    assert "MODEL_POLICY_ALTERNATIVE_ALLOWED" in allowed.reason_codes
    assert undeclared.status is Status.FAIL


def test_justified_escalation_passes_but_unjustified_escalation_fails() -> None:
    justified = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL_ULTRA,
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.ESCALATION,
        substitution_reason_code="SECURITY_COMPLEXITY",
    )
    unjustified = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL_ULTRA,
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.ESCALATION,
        substitution_reason_code="PREFERENCE",
    )
    assert justified.status is Status.PASS
    assert unjustified.status is Status.FAIL
    assert "MODEL_POLICY_ESCALATION_UNJUSTIFIED" in unjustified.reason_codes


def test_fallback_must_be_declared_and_approved() -> None:
    declared = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_TERRA,
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.FALLBACK,
        substitution_reason_code="MODEL_UNAVAILABLE",
        substitution_approved=True,
    )
    unapproved = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_TERRA,
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.FALLBACK,
        substitution_reason_code="MODEL_UNAVAILABLE",
    )
    assert declared.status is Status.PASS
    assert unapproved.status is Status.FAIL


def test_provider_change_missing_evidence_blocks_and_denial_fails() -> None:
    missing = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model="Approved External Model",
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.PROVIDER_CHANGE,
        substitution_reason_code="PROVIDER_POLICY_APPROVED",
        substitution_approved=True,
        provider_security_change=True,
        provider_security_approved=None,
    )
    denied = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model="Approved External Model",
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.PROVIDER_CHANGE,
        substitution_reason_code="PROVIDER_POLICY_APPROVED",
        substitution_approved=True,
        provider_security_change=True,
        provider_security_approved=False,
    )
    assert missing.status is Status.BLOCKED
    assert denied.status is Status.FAIL


def test_explicitly_approved_provider_change_can_pass() -> None:
    receipt = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model="Approved External Model",
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.PROVIDER_CHANGE,
        substitution_reason_code="PROVIDER_POLICY_APPROVED",
        substitution_approved=True,
        provider_security_change=True,
        provider_security_approved=True,
        evidence_refs=("policy/provider-change-v1",),
    )
    assert receipt.status is Status.PASS
    assert "MODEL_POLICY_PROVIDER_CHANGE_APPROVED" in receipt.reason_codes


def test_same_model_provider_change_is_never_silently_treated_as_exact() -> None:
    approved = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL,
        actual_reasoning=ReasoningLevel.HIGH,
        substitution_class=SubstitutionClass.PROVIDER_CHANGE,
        substitution_reason_code="PROVIDER_POLICY_APPROVED",
        substitution_approved=True,
        provider_security_change=True,
        provider_security_approved=True,
        evidence_refs=("policy/provider-change-v1",),
    )
    undeclared = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL,
        actual_reasoning=ReasoningLevel.HIGH,
        provider_security_change=True,
        provider_security_approved=True,
    )

    assert approved.status is Status.PASS
    assert approved.reason_codes == (
        "MODEL_POLICY_PROVIDER_CHANGE_APPROVED",
    )
    assert undeclared.status is Status.FAIL
    assert undeclared.reason_codes == (
        "MODEL_POLICY_SUBSTITUTION_UNDECLARED",
    )


def _authority(*, production: bool = False) -> AuthorityBoundary:
    return AuthorityBoundary(
        allowed_effect_classes=(EffectClass.REPOSITORY_WRITE,),
        forbidden_effect_classes=(EffectClass.DEPLOY, EffectClass.SECRET_MUTATION),
        stop_boundary=StopBoundary.MERGE,
        production_authorized=production,
        secret_access_authorized=False,
        data_access_authorized=False,
    )


def test_stronger_model_does_not_expand_authority() -> None:
    preserved = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL_ULTRA,
        actual_reasoning=ReasoningLevel.VERY_HIGH,
        substitution_class=SubstitutionClass.ESCALATION,
        substitution_reason_code="COMPLEXITY_DISCOVERED",
        authority_before=_authority(),
        authority_after=_authority(),
    )
    expanded = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL_ULTRA,
        actual_reasoning=ReasoningLevel.VERY_HIGH,
        substitution_class=SubstitutionClass.ESCALATION,
        substitution_reason_code="COMPLEXITY_DISCOVERED",
        authority_before=_authority(),
        authority_after=_authority(production=True),
    )
    assert preserved.status is Status.PASS
    assert preserved.authority_status is Status.PASS
    assert expanded.status is Status.FAIL
    assert expanded.authority_status is Status.FAIL
    assert "MODEL_POLICY_AUTHORITY_CHANGED" in expanded.reason_codes


def test_incomplete_authority_evidence_blocks() -> None:
    receipt = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL,
        actual_reasoning=ReasoningLevel.HIGH,
        authority_before=_authority(),
        authority_after=None,
    )
    assert receipt.status is Status.BLOCKED
    assert receipt.authority_status is Status.BLOCKED


def test_receipt_is_sanitized_and_byte_stable() -> None:
    first = evaluate_model_selection(
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        actual_model=MODEL_SOL,
        actual_reasoning=ReasoningLevel.HIGH,
        evidence_refs=("tests/model-selection",),
    )
    second = evaluate_model_selection(
        task_class="bounded_implementation",
        actual_model=MODEL_SOL,
        actual_reasoning="high",
        evidence_refs=["tests/model-selection"],
    )
    serialized = serialize_model_policy_receipt(first)
    assert serialized == serialize_model_policy_receipt(second)
    payload = json.loads(serialized)
    verify_sanitized_evidence(payload)
    forbidden = {
        "raw_prompt",
        "chain_of_thought",
        "raw_user_message",
        "raw_provider_response",
        "credential",
        "environment",
    }
    assert not forbidden.intersection(normalize_model_policy_receipt(first))
