"""Deterministic, provider-free model recommendation and selection policy."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from enum import StrEnum
from typing import TypeVar

from ai_engineering.contracts import (
    MODEL_POLICY_VERSION,
    AuthorityBoundary,
    LLMOpsPolicyError,
    ModelPolicyReceipt,
    ModelRecommendation,
    ReasoningLevel,
    Status,
    SubstitutionClass,
    TaskClass,
)
from ai_engineering.redaction import verify_sanitized_evidence


MODEL_TERRA = "GPT-5.6 Terra"
MODEL_SOL = "GPT-5.6 Sol"
MODEL_SOL_ULTRA = "GPT-5.6 Sol Ultra"
MODEL_APPROVED_55 = "GPT-5.5"

MODEL_POLICY_TASK_CLASS_UNKNOWN = "MODEL_POLICY_TASK_CLASS_UNKNOWN"
MODEL_POLICY_SELECTION_MATCH = "MODEL_POLICY_SELECTION_MATCH"
MODEL_POLICY_ALTERNATIVE_ALLOWED = "MODEL_POLICY_ALTERNATIVE_ALLOWED"
MODEL_POLICY_SUBSTITUTION_UNDECLARED = "MODEL_POLICY_SUBSTITUTION_UNDECLARED"
MODEL_POLICY_REASONING_INSUFFICIENT = "MODEL_POLICY_REASONING_INSUFFICIENT"
MODEL_POLICY_PROVIDER_CHANGE_UNAPPROVED = "MODEL_POLICY_PROVIDER_CHANGE_UNAPPROVED"
MODEL_POLICY_PROVIDER_CHANGE_EVIDENCE_MISSING = (
    "MODEL_POLICY_PROVIDER_CHANGE_EVIDENCE_MISSING"
)
MODEL_POLICY_PROVIDER_CHANGE_APPROVED = "MODEL_POLICY_PROVIDER_CHANGE_APPROVED"
MODEL_POLICY_ESCALATION_UNJUSTIFIED = "MODEL_POLICY_ESCALATION_UNJUSTIFIED"
MODEL_POLICY_ESCALATION_ALLOWED = "MODEL_POLICY_ESCALATION_ALLOWED"
MODEL_POLICY_FALLBACK_ALLOWED = "MODEL_POLICY_FALLBACK_ALLOWED"
MODEL_POLICY_AUTHORITY_PRESERVED = "MODEL_POLICY_AUTHORITY_PRESERVED"
MODEL_POLICY_AUTHORITY_CHANGED = "MODEL_POLICY_AUTHORITY_CHANGED"
MODEL_POLICY_AUTHORITY_EVIDENCE_INCOMPLETE = (
    "MODEL_POLICY_AUTHORITY_EVIDENCE_INCOMPLETE"
)
MODEL_POLICY_VALUE_INVALID = "MODEL_POLICY_VALUE_INVALID"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,127}$")
_REASON_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_T = TypeVar("_T", bound=StrEnum)

_REASONING_RANK = {
    ReasoningLevel.LOW: 0,
    ReasoningLevel.MEDIUM: 1,
    ReasoningLevel.HIGH: 2,
    ReasoningLevel.VERY_HIGH: 3,
}
_MODEL_RANK = {
    MODEL_APPROVED_55: 0,
    MODEL_TERRA: 1,
    MODEL_SOL: 2,
    MODEL_SOL_ULTRA: 3,
}
_ESCALATION_REASONS = frozenset(
    {
        "COMPLEXITY_DISCOVERED",
        "SECURITY_COMPLEXITY",
        "ARCHITECTURE_COUPLING",
        "VALIDATION_UNCERTAINTY",
    }
)
_FALLBACK_REASONS = frozenset({"MODEL_UNAVAILABLE"})
_PROVIDER_CHANGE_REASONS = frozenset({"PROVIDER_POLICY_APPROVED"})

_MODEL_MATRIX: dict[
    TaskClass, tuple[str, tuple[str, ...], ReasoningLevel, str]
] = {
    TaskClass.REPOSITORY_SEARCH_LOGS: (
        MODEL_TERRA,
        (),
        ReasoningLevel.MEDIUM,
        "MODEL_POLICY_REPOSITORY_SEARCH_LOGS",
    ),
    TaskClass.SMALL_PRECISE_FIX: (
        MODEL_TERRA,
        (MODEL_APPROVED_55,),
        ReasoningLevel.MEDIUM,
        "MODEL_POLICY_SMALL_PRECISE_FIX",
    ),
    TaskClass.BOUNDED_IMPLEMENTATION: (
        MODEL_SOL,
        (),
        ReasoningLevel.MEDIUM,
        "MODEL_POLICY_BOUNDED_IMPLEMENTATION",
    ),
    TaskClass.ARCHITECTURE: (
        MODEL_SOL,
        (),
        ReasoningLevel.HIGH,
        "MODEL_POLICY_ARCHITECTURE",
    ),
    TaskClass.MIGRATION_ROLLBACK_DESIGN: (
        MODEL_SOL,
        (),
        ReasoningLevel.HIGH,
        "MODEL_POLICY_MIGRATION_ROLLBACK_DESIGN",
    ),
    TaskClass.SECURITY_AUDIT: (
        MODEL_SOL,
        (),
        ReasoningLevel.HIGH,
        "MODEL_POLICY_SECURITY_AUDIT",
    ),
    TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION: (
        MODEL_SOL_ULTRA,
        (),
        ReasoningLevel.HIGH,
        "MODEL_POLICY_HIGH_RISK_PRODUCTION",
    ),
}


def _parse_enum(enum_type: type[_T], value: _T | str, code: str) -> _T:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise LLMOpsPolicyError(code)
    try:
        return enum_type(value.strip().upper())
    except ValueError as exc:
        raise LLMOpsPolicyError(code) from exc


def _policy_identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise LLMOpsPolicyError(MODEL_POLICY_VALUE_INVALID)
    return value


def _reason_code(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _REASON_CODE_RE.fullmatch(value):
        raise LLMOpsPolicyError(MODEL_POLICY_VALUE_INVALID)
    return value


def _evidence_refs(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise LLMOpsPolicyError(MODEL_POLICY_VALUE_INVALID)
    result = tuple(_policy_identifier(value) for value in values)
    if len(result) != len(set(result)):
        raise LLMOpsPolicyError(MODEL_POLICY_VALUE_INVALID)
    return result


def recommend_model(task_class: TaskClass | str) -> ModelRecommendation:
    """Return the repository matrix result without availability lookup."""

    task = _parse_enum(
        TaskClass, task_class, MODEL_POLICY_TASK_CLASS_UNKNOWN
    )
    model, alternatives, reasoning, reason_code = _MODEL_MATRIX[task]
    return ModelRecommendation(
        policy_version=MODEL_POLICY_VERSION,
        task_class=task,
        recommended_model=model,
        allowed_alternatives=alternatives,
        recommended_reasoning=reasoning,
        reason_code=reason_code,
    )


def _authority_status(
    before: AuthorityBoundary | None,
    after: AuthorityBoundary | None,
) -> tuple[Status, str | None]:
    if before is None and after is None:
        return Status.NOT_PERFORMED, None
    if before is None or after is None:
        return Status.BLOCKED, MODEL_POLICY_AUTHORITY_EVIDENCE_INCOMPLETE
    if before != after:
        return Status.FAIL, MODEL_POLICY_AUTHORITY_CHANGED
    return Status.PASS, MODEL_POLICY_AUTHORITY_PRESERVED


def _blocked_receipt(reason_code: str) -> ModelPolicyReceipt:
    return ModelPolicyReceipt(
        policy_version=MODEL_POLICY_VERSION,
        task_class=None,
        recommended_model=None,
        actual_model=None,
        recommended_reasoning=None,
        actual_reasoning=None,
        substitution_class=None,
        substitution_reason_code=None,
        authority_status=Status.NOT_PERFORMED,
        status=Status.BLOCKED,
        reason_codes=(reason_code,),
        evidence_refs=(),
    )


def evaluate_model_selection(
    *,
    task_class: TaskClass | str,
    actual_model: str,
    actual_reasoning: ReasoningLevel | str,
    substitution_class: SubstitutionClass | str = SubstitutionClass.NONE,
    substitution_reason_code: str | None = None,
    substitution_approved: bool = False,
    provider_security_change: bool = False,
    provider_security_approved: bool | None = None,
    authority_before: AuthorityBoundary | None = None,
    authority_after: AuthorityBoundary | None = None,
    evidence_refs: tuple[str, ...] | list[str] = (),
) -> ModelPolicyReceipt:
    """Evaluate an observed selection without changing task authority."""

    try:
        recommendation = recommend_model(task_class)
        actual = _policy_identifier(actual_model)
        reasoning = _parse_enum(
            ReasoningLevel, actual_reasoning, MODEL_POLICY_VALUE_INVALID
        )
        substitution = _parse_enum(
            SubstitutionClass, substitution_class, MODEL_POLICY_VALUE_INVALID
        )
        reason = _reason_code(
            substitution_reason_code,
            required=substitution is not SubstitutionClass.NONE,
        )
        refs = _evidence_refs(evidence_refs)
    except LLMOpsPolicyError as exc:
        return _blocked_receipt(exc.code)

    reasons: list[str] = []
    required_reasoning = recommendation.recommended_reasoning
    if _REASONING_RANK[reasoning] < _REASONING_RANK[required_reasoning]:
        selection_status = Status.FAIL
        reasons.append(MODEL_POLICY_REASONING_INSUFFICIENT)
    else:
        selection_status = Status.PASS

    model_status = Status.FAIL
    model_reason = MODEL_POLICY_SUBSTITUTION_UNDECLARED
    if substitution is SubstitutionClass.PROVIDER_CHANGE:
        if provider_security_approved is None:
            model_status = Status.BLOCKED
            model_reason = MODEL_POLICY_PROVIDER_CHANGE_EVIDENCE_MISSING
        elif (
            provider_security_change
            and provider_security_approved
            and substitution_approved
            and reason in _PROVIDER_CHANGE_REASONS
        ):
            model_status = Status.PASS
            model_reason = MODEL_POLICY_PROVIDER_CHANGE_APPROVED
        else:
            model_reason = MODEL_POLICY_PROVIDER_CHANGE_UNAPPROVED
    elif actual == recommendation.recommended_model:
        if substitution is SubstitutionClass.NONE and not provider_security_change:
            model_status = Status.PASS
            model_reason = MODEL_POLICY_SELECTION_MATCH
    elif actual in recommendation.allowed_alternatives:
        if substitution is SubstitutionClass.ALLOWED_ALTERNATIVE:
            model_status = Status.PASS
            model_reason = MODEL_POLICY_ALTERNATIVE_ALLOWED
        elif (
            substitution is SubstitutionClass.FALLBACK
            and substitution_approved
            and reason in _FALLBACK_REASONS
            and not provider_security_change
        ):
            model_status = Status.PASS
            model_reason = MODEL_POLICY_FALLBACK_ALLOWED
    elif substitution is SubstitutionClass.ESCALATION:
        recommended_rank = _MODEL_RANK.get(recommendation.recommended_model)
        actual_rank = _MODEL_RANK.get(actual)
        if (
            recommended_rank is not None
            and actual_rank is not None
            and actual_rank > recommended_rank
            and reason in _ESCALATION_REASONS
            and not provider_security_change
        ):
            model_status = Status.PASS
            model_reason = MODEL_POLICY_ESCALATION_ALLOWED
        else:
            model_reason = MODEL_POLICY_ESCALATION_UNJUSTIFIED
    elif substitution is SubstitutionClass.FALLBACK:
        if provider_security_change and provider_security_approved is None:
            model_status = Status.BLOCKED
            model_reason = MODEL_POLICY_PROVIDER_CHANGE_EVIDENCE_MISSING
        elif provider_security_change and not provider_security_approved:
            model_reason = MODEL_POLICY_PROVIDER_CHANGE_UNAPPROVED
        elif substitution_approved and reason in _FALLBACK_REASONS:
            model_status = Status.PASS
            model_reason = MODEL_POLICY_FALLBACK_ALLOWED

    reasons.append(model_reason)
    authority_status, authority_reason = _authority_status(
        authority_before, authority_after
    )
    if authority_reason is not None:
        reasons.append(authority_reason)

    statuses = (selection_status, model_status, authority_status)
    if Status.BLOCKED in statuses:
        status = Status.BLOCKED
    elif Status.FAIL in statuses:
        status = Status.FAIL
    else:
        status = Status.PASS
    receipt = ModelPolicyReceipt(
        policy_version=MODEL_POLICY_VERSION,
        task_class=recommendation.task_class,
        recommended_model=recommendation.recommended_model,
        actual_model=actual,
        recommended_reasoning=required_reasoning,
        actual_reasoning=reasoning,
        substitution_class=substitution,
        substitution_reason_code=reason,
        authority_status=authority_status,
        status=status,
        reason_codes=tuple(dict.fromkeys(reasons)),
        evidence_refs=refs,
    )
    verify_sanitized_evidence(normalize_model_policy_receipt(receipt))
    return receipt


def normalize_recommendation(value: ModelRecommendation) -> dict[str, object]:
    payload = asdict(value)
    payload["task_class"] = value.task_class.value
    payload["recommended_reasoning"] = value.recommended_reasoning.value
    verify_sanitized_evidence(payload)
    return payload


def normalize_model_policy_receipt(value: ModelPolicyReceipt) -> dict[str, object]:
    payload = asdict(value)
    payload["task_class"] = value.task_class.value if value.task_class else None
    payload["recommended_reasoning"] = (
        value.recommended_reasoning.value if value.recommended_reasoning else None
    )
    payload["actual_reasoning"] = (
        value.actual_reasoning.value if value.actual_reasoning else None
    )
    payload["substitution_class"] = (
        value.substitution_class.value if value.substitution_class else None
    )
    payload["authority_status"] = value.authority_status.value
    payload["status"] = value.status.value
    verify_sanitized_evidence(payload)
    return payload


def serialize_model_policy_receipt(value: ModelPolicyReceipt) -> str:
    return json.dumps(
        normalize_model_policy_receipt(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
