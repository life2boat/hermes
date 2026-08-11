"""Deterministic LLM usage accounting, rate-card identity, and budget policy."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NoReturn

from ai_engineering.contracts import (
    COST_POLICY_VERSION,
    RATE_CARD_SCHEMA_VERSION,
    BudgetLimits,
    CostPolicyReceipt,
    LLMOpsPolicyError,
    LLMOpsReceipt,
    ModelPolicyReceipt,
    ModelRate,
    RateCard,
    Status,
    UsageAccounting,
    UsageEvidence,
)
from ai_engineering.model_policy import normalize_model_policy_receipt
from ai_engineering.redaction import verify_sanitized_evidence
from ai_engineering.scenario import load_fixture_bytes


COST_POLICY_CALL_BUDGET_EXCEEDED = "COST_POLICY_CALL_BUDGET_EXCEEDED"
COST_POLICY_INPUT_TOKEN_BUDGET_EXCEEDED = (
    "COST_POLICY_INPUT_TOKEN_BUDGET_EXCEEDED"
)
COST_POLICY_OUTPUT_TOKEN_BUDGET_EXCEEDED = (
    "COST_POLICY_OUTPUT_TOKEN_BUDGET_EXCEEDED"
)
COST_POLICY_ESTIMATED_COST_BUDGET_EXCEEDED = (
    "COST_POLICY_ESTIMATED_COST_BUDGET_EXCEEDED"
)
COST_POLICY_USAGE_UNKNOWN = "COST_POLICY_USAGE_UNKNOWN"
COST_POLICY_RATE_CARD_REQUIRED = "COST_POLICY_RATE_CARD_REQUIRED"
COST_POLICY_MODEL_RATE_MISSING = "COST_POLICY_MODEL_RATE_MISSING"
COST_POLICY_CURRENCY_MISMATCH = "COST_POLICY_CURRENCY_MISMATCH"
COST_POLICY_RATE_CARD_INVALID = "COST_POLICY_RATE_CARD_INVALID"
COST_POLICY_VALUE_INVALID = "COST_POLICY_VALUE_INVALID"
COST_POLICY_WITHIN_BUDGET = "COST_POLICY_WITHIN_BUDGET"
COST_POLICY_ESTIMATE_NOT_REQUIRED = "COST_POLICY_ESTIMATE_NOT_REQUIRED"
COST_POLICY_OPTIONAL_ESTIMATE_UNKNOWN = "COST_POLICY_OPTIONAL_ESTIMATE_UNKNOWN"
LLM_OPS_INPUT_INVALID = "LLM_OPS_INPUT_INVALID"

_BUDGET_FIELDS = frozenset(
    {
        "max_model_calls",
        "max_input_tokens",
        "max_output_tokens",
        "max_estimated_cost",
        "currency",
    }
)
_USAGE_FIELDS = frozenset(
    {
        "primary_calls",
        "retry_calls",
        "judge_calls",
        "fallback_calls",
        "live_eval_calls",
        "input_tokens",
        "output_tokens",
    }
)
_RATE_CARD_FIELDS = frozenset(
    {"schema_version", "pricing_source_id", "currency", "models"}
)
_MODEL_RATE_FIELDS = frozenset(
    {"input_per_million_tokens", "output_per_million_tokens", "per_call"}
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/ -]{0,127}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_MILLION = Decimal(1_000_000)


class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError


def _mapping(value: object, code: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise LLMOpsPolicyError(code)
    return value


def _exact_fields(
    value: object, expected: frozenset[str], code: str
) -> Mapping[str, object]:
    payload = _mapping(value, code)
    if frozenset(payload) != expected:
        raise LLMOpsPolicyError(code)
    return payload


def _identifier(value: object, code: str = COST_POLICY_VALUE_INVALID) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise LLMOpsPolicyError(code)
    return value


def _currency(value: object, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _CURRENCY_RE.fullmatch(value):
        raise LLMOpsPolicyError(COST_POLICY_VALUE_INVALID)
    return value


def _nonnegative_integer(value: object, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LLMOpsPolicyError(COST_POLICY_VALUE_INVALID)
    return value


def _decimal_string(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    if rendered in {"", "-0"}:
        return "0"
    return rendered


def _nonnegative_decimal(value: object, *, optional: bool) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, (str, Decimal)):
        raise LLMOpsPolicyError(COST_POLICY_VALUE_INVALID)
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise LLMOpsPolicyError(COST_POLICY_VALUE_INVALID) from exc
    if not parsed.is_finite() or parsed < 0:
        raise LLMOpsPolicyError(COST_POLICY_VALUE_INVALID)
    return _decimal_string(parsed)


def validate_budget(value: BudgetLimits | Mapping[str, object]) -> BudgetLimits:
    if isinstance(value, BudgetLimits):
        raw: Mapping[str, object] = asdict(value)
    else:
        raw = _exact_fields(value, _BUDGET_FIELDS, COST_POLICY_VALUE_INVALID)
    max_cost = _nonnegative_decimal(raw["max_estimated_cost"], optional=True)
    currency = _currency(raw["currency"], required=max_cost is not None)
    return BudgetLimits(
        max_model_calls=_nonnegative_integer(raw["max_model_calls"], optional=True),
        max_input_tokens=_nonnegative_integer(raw["max_input_tokens"], optional=True),
        max_output_tokens=_nonnegative_integer(raw["max_output_tokens"], optional=True),
        max_estimated_cost=max_cost,
        currency=currency,
    )


def validate_usage(value: UsageAccounting | Mapping[str, object]) -> UsageAccounting:
    if isinstance(value, UsageAccounting):
        raw: Mapping[str, object] = asdict(value)
    else:
        raw = _exact_fields(value, _USAGE_FIELDS, COST_POLICY_VALUE_INVALID)
    return UsageAccounting(
        primary_calls=_nonnegative_integer(raw["primary_calls"], optional=True),
        retry_calls=_nonnegative_integer(raw["retry_calls"], optional=True),
        judge_calls=_nonnegative_integer(raw["judge_calls"], optional=True),
        fallback_calls=_nonnegative_integer(raw["fallback_calls"], optional=True),
        live_eval_calls=_nonnegative_integer(raw["live_eval_calls"], optional=True),
        input_tokens=_nonnegative_integer(raw["input_tokens"], optional=True),
        output_tokens=_nonnegative_integer(raw["output_tokens"], optional=True),
    )


def usage_from_trace(value: UsageEvidence) -> UsageAccounting:
    """Adapt aggregate trace-v1 evidence without claiming hidden retries."""

    model_calls = _nonnegative_integer(value.model_calls, optional=True)
    return UsageAccounting(
        primary_calls=model_calls,
        retry_calls=0,
        judge_calls=0,
        fallback_calls=0,
        live_eval_calls=0,
        input_tokens=_nonnegative_integer(value.input_tokens, optional=True),
        output_tokens=_nonnegative_integer(value.output_tokens, optional=True),
    )


def total_model_calls(value: UsageAccounting) -> int | None:
    values = (
        value.primary_calls,
        value.retry_calls,
        value.judge_calls,
        value.fallback_calls,
        value.live_eval_calls,
    )
    if any(item is None for item in values):
        return None
    return sum(item for item in values if item is not None)


def _rate_card_mapping(value: RateCard) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "pricing_source_id": value.pricing_source_id,
        "currency": value.currency,
        "models": {
            model: {
                "input_per_million_tokens": rate.input_per_million_tokens,
                "output_per_million_tokens": rate.output_per_million_tokens,
                "per_call": rate.per_call,
            }
            for model, rate in value.models
        },
    }


def validate_rate_card(value: RateCard | Mapping[str, object]) -> RateCard:
    raw_value: object = _rate_card_mapping(value) if isinstance(value, RateCard) else value
    raw = _exact_fields(
        raw_value, _RATE_CARD_FIELDS, COST_POLICY_RATE_CARD_INVALID
    )
    if raw["schema_version"] != RATE_CARD_SCHEMA_VERSION:
        raise LLMOpsPolicyError(COST_POLICY_RATE_CARD_INVALID)
    models_payload = _mapping(raw["models"], COST_POLICY_RATE_CARD_INVALID)
    if not models_payload:
        raise LLMOpsPolicyError(COST_POLICY_RATE_CARD_INVALID)
    models: list[tuple[str, ModelRate]] = []
    for model_name in sorted(models_payload):
        model = _identifier(model_name, COST_POLICY_RATE_CARD_INVALID)
        rate = _exact_fields(
            models_payload[model_name],
            _MODEL_RATE_FIELDS,
            COST_POLICY_RATE_CARD_INVALID,
        )
        try:
            validated_rate = ModelRate(
                input_per_million_tokens=_nonnegative_decimal(
                    rate["input_per_million_tokens"], optional=False
                )
                or "0",
                output_per_million_tokens=_nonnegative_decimal(
                    rate["output_per_million_tokens"], optional=False
                )
                or "0",
                per_call=_nonnegative_decimal(rate["per_call"], optional=False)
                or "0",
            )
        except LLMOpsPolicyError as exc:
            raise LLMOpsPolicyError(COST_POLICY_RATE_CARD_INVALID) from exc
        models.append((model, validated_rate))
    try:
        currency = _currency(raw["currency"], required=True)
        assert currency is not None
        card = RateCard(
            schema_version=RATE_CARD_SCHEMA_VERSION,
            pricing_source_id=_identifier(
                raw["pricing_source_id"], COST_POLICY_RATE_CARD_INVALID
            ),
            currency=currency,
            models=tuple(models),
        )
    except (LLMOpsPolicyError, AssertionError) as exc:
        raise LLMOpsPolicyError(COST_POLICY_RATE_CARD_INVALID) from exc
    verify_sanitized_evidence(normalize_rate_card(card))
    return card


def normalize_rate_card(value: RateCard | Mapping[str, object]) -> dict[str, object]:
    card = value if isinstance(value, RateCard) else validate_rate_card(value)
    payload = _rate_card_mapping(card)
    verify_sanitized_evidence(payload)
    return payload


def serialize_rate_card(value: RateCard | Mapping[str, object]) -> str:
    return json.dumps(
        normalize_rate_card(validate_rate_card(value)),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def rate_card_digest(value: RateCard | Mapping[str, object]) -> str:
    return hashlib.sha256(serialize_rate_card(value).encode("utf-8")).hexdigest()


def load_policy_json(
    root: Path,
    relative_path: str | Path,
    *,
    error_code: str = LLM_OPS_INPUT_INVALID,
) -> Mapping[str, object]:
    try:
        data = load_fixture_bytes(root, relative_path)
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        return _mapping(payload, error_code)
    except LLMOpsPolicyError:
        raise
    except Exception as exc:
        raise LLMOpsPolicyError(error_code) from exc


def load_rate_card(root: Path, relative_path: str | Path) -> RateCard:
    try:
        return validate_rate_card(
            load_policy_json(
                root,
                relative_path,
                error_code=COST_POLICY_RATE_CARD_INVALID,
            )
        )
    except LLMOpsPolicyError as exc:
        raise LLMOpsPolicyError(COST_POLICY_RATE_CARD_INVALID) from exc


def _dimension_status(
    limit: int | None,
    observed: int | None,
    overrun_code: str,
) -> tuple[Status, str | None]:
    if limit is None:
        return Status.NOT_PERFORMED, None
    if observed is None:
        return Status.BLOCKED, COST_POLICY_USAGE_UNKNOWN
    if observed > limit:
        return Status.FAIL, overrun_code
    return Status.PASS, None


def _rate_for_model(card: RateCard, model: str) -> ModelRate | None:
    return dict(card.models).get(model)


def _estimated_cost(
    rate: ModelRate,
    usage: UsageAccounting,
    calls: int,
) -> Decimal | None:
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    return (
        Decimal(usage.input_tokens) * Decimal(rate.input_per_million_tokens) / _MILLION
        + Decimal(usage.output_tokens)
        * Decimal(rate.output_per_million_tokens)
        / _MILLION
        + Decimal(calls) * Decimal(rate.per_call)
    )


def evaluate_cost_policy(
    *,
    budget: BudgetLimits | Mapping[str, object],
    usage: UsageAccounting | Mapping[str, object],
    actual_model: str,
    rate_card: RateCard | Mapping[str, object] | None = None,
) -> CostPolicyReceipt:
    limits = validate_budget(budget)
    observed = validate_usage(usage)
    model = _identifier(actual_model)
    calls = total_model_calls(observed)
    reasons: list[str] = []
    call_status, reason = _dimension_status(
        limits.max_model_calls, calls, COST_POLICY_CALL_BUDGET_EXCEEDED
    )
    if reason:
        reasons.append(reason)
    input_status, reason = _dimension_status(
        limits.max_input_tokens,
        observed.input_tokens,
        COST_POLICY_INPUT_TOKEN_BUDGET_EXCEEDED,
    )
    if reason:
        reasons.append(reason)
    output_status, reason = _dimension_status(
        limits.max_output_tokens,
        observed.output_tokens,
        COST_POLICY_OUTPUT_TOKEN_BUDGET_EXCEEDED,
    )
    if reason:
        reasons.append(reason)

    card: RateCard | None = None
    invalid_card = False
    if rate_card is not None:
        try:
            card = validate_rate_card(rate_card)
        except LLMOpsPolicyError:
            invalid_card = True

    monetary_required = limits.max_estimated_cost is not None
    estimated_status = Status.NOT_PERFORMED
    estimated: str | None = None
    currency: str | None = card.currency if card else limits.currency
    pricing_source: str | None = card.pricing_source_id if card else None
    digest: str | None = rate_card_digest(card) if card else None
    if invalid_card:
        estimated_status = Status.BLOCKED
        reasons.append(COST_POLICY_RATE_CARD_INVALID)
    elif card is None:
        if monetary_required:
            estimated_status = Status.BLOCKED
            reasons.append(COST_POLICY_RATE_CARD_REQUIRED)
        else:
            reasons.append(COST_POLICY_ESTIMATE_NOT_REQUIRED)
    else:
        rate = _rate_for_model(card, model)
        if monetary_required and limits.currency != card.currency:
            estimated_status = Status.BLOCKED
            reasons.append(COST_POLICY_CURRENCY_MISMATCH)
        elif rate is None:
            estimated_status = Status.BLOCKED if monetary_required else Status.UNKNOWN
            reasons.append(
                COST_POLICY_MODEL_RATE_MISSING
                if monetary_required
                else COST_POLICY_OPTIONAL_ESTIMATE_UNKNOWN
            )
        elif calls is None:
            estimated_status = Status.BLOCKED if monetary_required else Status.UNKNOWN
            reasons.append(COST_POLICY_USAGE_UNKNOWN)
        else:
            amount = _estimated_cost(rate, observed, calls)
            if amount is None:
                estimated_status = (
                    Status.BLOCKED if monetary_required else Status.UNKNOWN
                )
                reasons.append(COST_POLICY_USAGE_UNKNOWN)
            else:
                estimated = _decimal_string(amount)
                if monetary_required:
                    assert limits.max_estimated_cost is not None
                    if amount > Decimal(limits.max_estimated_cost):
                        estimated_status = Status.FAIL
                        reasons.append(COST_POLICY_ESTIMATED_COST_BUDGET_EXCEEDED)
                    else:
                        estimated_status = Status.PASS
                else:
                    estimated_status = Status.PASS

    required_statuses = [
        status
        for limit, status in (
            (limits.max_model_calls, call_status),
            (limits.max_input_tokens, input_status),
            (limits.max_output_tokens, output_status),
            (limits.max_estimated_cost, estimated_status),
        )
        if limit is not None
    ]
    if invalid_card or Status.BLOCKED in required_statuses:
        status = Status.BLOCKED
    elif Status.FAIL in required_statuses:
        status = Status.FAIL
    else:
        status = Status.PASS
        reasons.append(COST_POLICY_WITHIN_BUDGET)
    receipt = CostPolicyReceipt(
        policy_version=COST_POLICY_VERSION,
        budget=limits,
        usage=observed,
        total_model_calls=calls,
        model_call_status=call_status,
        input_token_status=input_status,
        output_token_status=output_status,
        estimated_cost_status=estimated_status,
        estimated_cost=estimated,
        currency=currency,
        pricing_source_id=pricing_source,
        rate_card_digest=digest,
        status=status,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )
    verify_sanitized_evidence(normalize_cost_policy_receipt(receipt))
    return receipt


def normalize_cost_policy_receipt(value: CostPolicyReceipt) -> dict[str, object]:
    payload = asdict(value)
    for field in (
        "model_call_status",
        "input_token_status",
        "output_token_status",
        "estimated_cost_status",
        "status",
    ):
        payload[field] = getattr(value, field).value
    verify_sanitized_evidence(payload)
    return payload


def serialize_cost_policy_receipt(value: CostPolicyReceipt) -> str:
    return json.dumps(
        normalize_cost_policy_receipt(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def aggregate_llm_ops(
    model_policy_receipt: ModelPolicyReceipt,
    cost_policy_receipt: CostPolicyReceipt,
) -> LLMOpsReceipt:
    statuses = (model_policy_receipt.status, cost_policy_receipt.status)
    if Status.BLOCKED in statuses:
        overall = Status.BLOCKED
    elif Status.FAIL in statuses:
        overall = Status.FAIL
    else:
        overall = Status.PASS
    return LLMOpsReceipt(
        model_policy_receipt=model_policy_receipt,
        cost_policy_receipt=cost_policy_receipt,
        overall_status=overall,
    )


def normalize_llm_ops_receipt(value: LLMOpsReceipt) -> dict[str, object]:
    payload = {
        "model_policy_receipt": normalize_model_policy_receipt(
            value.model_policy_receipt
        ),
        "cost_policy_receipt": normalize_cost_policy_receipt(
            value.cost_policy_receipt
        ),
        "overall_status": value.overall_status.value,
    }
    verify_sanitized_evidence(payload)
    return payload
