from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from ai_engineering.contracts import (
    COST_POLICY_VERSION,
    RATE_CARD_SCHEMA_VERSION,
    BudgetLimits,
    LLMOpsPolicyError,
    Status,
    UsageAccounting,
    UsageEvidence,
)
from ai_engineering.cost_policy import (
    aggregate_llm_ops,
    evaluate_cost_policy,
    load_rate_card,
    normalize_cost_policy_receipt,
    normalize_llm_ops_receipt,
    rate_card_digest,
    serialize_cost_policy_receipt,
    total_model_calls,
    usage_from_trace,
    validate_budget,
    validate_rate_card,
    validate_usage,
)
from ai_engineering.model_policy import MODEL_SOL, MODEL_TERRA, evaluate_model_selection
from ai_engineering.redaction import verify_sanitized_evidence
from scripts.check_llm_ops_policy import run as run_cli


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ai_engineering" / "llm_ops"


def _budget(**overrides: object) -> BudgetLimits:
    values: dict[str, object] = {
        "max_model_calls": 3,
        "max_input_tokens": 500_000,
        "max_output_tokens": 250_000,
        "max_estimated_cost": "2.03",
        "currency": "USD",
    }
    values.update(overrides)
    return validate_budget(values)


def _usage(**overrides: object) -> UsageAccounting:
    values: dict[str, object] = {
        "primary_calls": 1,
        "retry_calls": 1,
        "judge_calls": 0,
        "fallback_calls": 1,
        "live_eval_calls": 0,
        "input_tokens": 500_000,
        "output_tokens": 250_000,
    }
    values.update(overrides)
    return validate_usage(values)


def _rate_card_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "pricing_source_id": "synthetic-rate-card-v1",
        "currency": "USD",
        "models": {
            MODEL_SOL: {
                "input_per_million_tokens": "2",
                "output_per_million_tokens": "4",
                "per_call": "0.01",
            }
        },
    }


def test_synthetic_rate_card_loads_and_has_explicit_identity() -> None:
    card = load_rate_card(FIXTURE_ROOT, "synthetic-rate-card-v1.json")
    assert card.schema_version == RATE_CARD_SCHEMA_VERSION
    assert card.pricing_source_id == "synthetic-rate-card-v1"
    assert card.currency == "USD"
    assert len(rate_card_digest(card)) == 64


def test_rate_card_digest_is_order_independent_and_meaning_sensitive() -> None:
    first = _rate_card_payload()
    second = {
        "models": first["models"],
        "currency": "USD",
        "pricing_source_id": "synthetic-rate-card-v1",
        "schema_version": 1,
    }
    assert rate_card_digest(first) == rate_card_digest(second)
    changed = _rate_card_payload()
    changed["models"] = {
        MODEL_SOL: {
            "input_per_million_tokens": "2.01",
            "output_per_million_tokens": "4",
            "per_call": "0.01",
        }
    }
    assert rate_card_digest(first) != rate_card_digest(changed)


def test_within_limits_and_boundary_equality_pass() -> None:
    receipt = evaluate_cost_policy(
        budget=_budget(),
        usage=_usage(),
        actual_model=MODEL_SOL,
        rate_card=_rate_card_payload(),
    )
    assert receipt.policy_version == COST_POLICY_VERSION
    assert receipt.total_model_calls == 3
    assert receipt.estimated_cost == "2.03"
    assert receipt.model_call_status is Status.PASS
    assert receipt.input_token_status is Status.PASS
    assert receipt.output_token_status is Status.PASS
    assert receipt.estimated_cost_status is Status.PASS
    assert receipt.status is Status.PASS


@pytest.mark.parametrize(
    ("budget", "usage", "reason_code"),
    [
        (
            _budget(max_model_calls=2),
            _usage(),
            "COST_POLICY_CALL_BUDGET_EXCEEDED",
        ),
        (
            _budget(max_input_tokens=499_999),
            _usage(),
            "COST_POLICY_INPUT_TOKEN_BUDGET_EXCEEDED",
        ),
        (
            _budget(max_output_tokens=249_999),
            _usage(),
            "COST_POLICY_OUTPUT_TOKEN_BUDGET_EXCEEDED",
        ),
        (
            _budget(max_estimated_cost="2.029"),
            _usage(),
            "COST_POLICY_ESTIMATED_COST_BUDGET_EXCEEDED",
        ),
    ],
)
def test_each_proven_budget_overrun_fails(
    budget: BudgetLimits,
    usage: UsageAccounting,
    reason_code: str,
) -> None:
    receipt = evaluate_cost_policy(
        budget=budget,
        usage=usage,
        actual_model=MODEL_SOL,
        rate_card=_rate_card_payload(),
    )
    assert receipt.status is Status.FAIL
    assert reason_code in receipt.reason_codes


def test_every_call_category_counts_toward_total() -> None:
    passing = _usage(judge_calls=0)
    failing = _usage(judge_calls=1)
    assert total_model_calls(passing) == 3
    assert total_model_calls(failing) == 4
    assert evaluate_cost_policy(
        budget=_budget(max_estimated_cost=None, currency=None),
        usage=passing,
        actual_model=MODEL_SOL,
    ).status is Status.PASS
    receipt = evaluate_cost_policy(
        budget=_budget(max_estimated_cost=None, currency=None),
        usage=failing,
        actual_model=MODEL_SOL,
    )
    assert receipt.status is Status.FAIL
    assert "COST_POLICY_CALL_BUDGET_EXCEEDED" in receipt.reason_codes


@pytest.mark.parametrize(
    ("budget_override", "usage_override"),
    [
        ({"max_model_calls": 3}, {"retry_calls": None}),
        ({"max_input_tokens": 100}, {"input_tokens": None}),
        ({"max_output_tokens": 100}, {"output_tokens": None}),
    ],
)
def test_unknown_required_usage_blocks(
    budget_override: dict[str, object],
    usage_override: dict[str, object],
) -> None:
    budget = _budget(max_estimated_cost=None, currency=None, **budget_override)
    receipt = evaluate_cost_policy(
        budget=budget,
        usage=_usage(**usage_override),
        actual_model=MODEL_SOL,
    )
    assert receipt.status is Status.BLOCKED
    assert "COST_POLICY_USAGE_UNKNOWN" in receipt.reason_codes


def test_required_cost_missing_rate_card_or_model_blocks_not_zero() -> None:
    missing_card = evaluate_cost_policy(
        budget=_budget(), usage=_usage(), actual_model=MODEL_SOL
    )
    missing_model = evaluate_cost_policy(
        budget=_budget(),
        usage=_usage(),
        actual_model="Unpriced Model",
        rate_card=_rate_card_payload(),
    )
    for receipt in (missing_card, missing_model):
        assert receipt.status is Status.BLOCKED
        assert receipt.estimated_cost is None
        assert receipt.estimated_cost_status is Status.BLOCKED


def test_currency_mismatch_blocks_without_conversion() -> None:
    receipt = evaluate_cost_policy(
        budget=_budget(currency="EUR"),
        usage=_usage(),
        actual_model=MODEL_SOL,
        rate_card=_rate_card_payload(),
    )
    assert receipt.status is Status.BLOCKED
    assert "COST_POLICY_CURRENCY_MISMATCH" in receipt.reason_codes


def test_optional_monetary_estimate_does_not_block_unrelated_budgets() -> None:
    no_card = evaluate_cost_policy(
        budget=_budget(max_estimated_cost=None, currency=None),
        usage=_usage(),
        actual_model=MODEL_SOL,
    )
    unknown_model = evaluate_cost_policy(
        budget=_budget(max_estimated_cost=None, currency=None),
        usage=_usage(),
        actual_model="Unpriced Model",
        rate_card=_rate_card_payload(),
    )
    assert no_card.status is Status.PASS
    assert no_card.estimated_cost_status is Status.NOT_PERFORMED
    assert unknown_model.status is Status.PASS
    assert unknown_model.estimated_cost_status is Status.UNKNOWN
    assert unknown_model.estimated_cost is None


@pytest.mark.parametrize(
    "invalid",
    [-1, True, "1", 1.5],
)
def test_negative_or_noninteger_usage_is_rejected(invalid: object) -> None:
    with pytest.raises(LLMOpsPolicyError):
        _usage(retry_calls=invalid)


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-0.01", 0.1])
def test_invalid_cost_values_are_rejected(invalid: object) -> None:
    with pytest.raises(LLMOpsPolicyError):
        _budget(max_estimated_cost=invalid)


def test_trace_v1_adapter_preserves_unknown_and_does_not_invent_retries() -> None:
    known = usage_from_trace(
        UsageEvidence(model_calls=2, input_tokens=10, output_tokens=5, estimated_cost=None)
    )
    unknown = usage_from_trace(
        UsageEvidence(
            model_calls=None,
            input_tokens=None,
            output_tokens=None,
            estimated_cost=None,
        )
    )
    assert known.primary_calls == 2
    assert known.retry_calls == known.judge_calls == known.fallback_calls == 0
    assert total_model_calls(unknown) is None


def test_rate_card_loader_rejects_unsafe_paths_and_malformed_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_rate_card_payload()), encoding="utf-8")
    (root / "duplicate.json").write_text(
        '{"schema_version":1,"schema_version":1}', encoding="utf-8"
    )
    (root / "unknown.json").write_text(
        json.dumps({**_rate_card_payload(), "unexpected": True}), encoding="utf-8"
    )
    (root / "oversized.json").write_text("x" * 1_048_577, encoding="utf-8")
    for path in (
        "../outside.json",
        str(outside.resolve()),
        "duplicate.json",
        "unknown.json",
        "oversized.json",
    ):
        with pytest.raises(LLMOpsPolicyError):
            load_rate_card(root, path)


def test_rate_card_loader_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_rate_card_payload()), encoding="utf-8")
    link = root / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available on this platform")
    with pytest.raises(LLMOpsPolicyError):
        load_rate_card(root, "link.json")


def test_receipt_is_sanitized_and_byte_stable() -> None:
    first = evaluate_cost_policy(
        budget=_budget(),
        usage=_usage(),
        actual_model=MODEL_SOL,
        rate_card=_rate_card_payload(),
    )
    second = evaluate_cost_policy(
        budget=asdict(_budget()),
        usage=asdict(_usage()),
        actual_model=MODEL_SOL,
        rate_card={
            "models": _rate_card_payload()["models"],
            "currency": "USD",
            "pricing_source_id": "synthetic-rate-card-v1",
            "schema_version": 1,
        },
    )
    assert serialize_cost_policy_receipt(first) == serialize_cost_policy_receipt(second)
    payload = normalize_cost_policy_receipt(first)
    verify_sanitized_evidence(payload)
    forbidden = {
        "raw_prompt",
        "chain_of_thought",
        "raw_user_message",
        "raw_provider_response",
        "credential",
        "environment",
    }
    assert not forbidden.intersection(payload)


def test_llm_ops_aggregate_preserves_both_dimensional_receipts() -> None:
    model_receipt = evaluate_model_selection(
        task_class="BOUNDED_IMPLEMENTATION",
        actual_model=MODEL_SOL,
        actual_reasoning="HIGH",
    )
    cost_receipt = evaluate_cost_policy(
        budget=_budget(),
        usage=_usage(),
        actual_model=MODEL_SOL,
        rate_card=_rate_card_payload(),
    )
    aggregate = aggregate_llm_ops(model_receipt, cost_receipt)
    assert aggregate.overall_status is Status.PASS
    normalized = normalize_llm_ops_receipt(aggregate)
    assert normalized["model_policy_receipt"]["status"] == "PASS"
    assert normalized["cost_policy_receipt"]["status"] == "PASS"


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _model_cli_payload(*, fallback_approved: bool = False) -> dict[str, object]:
    actual = "GPT-5.6 Terra" if fallback_approved else MODEL_SOL
    substitution = "FALLBACK" if fallback_approved else "NONE"
    reason = "MODEL_UNAVAILABLE" if fallback_approved else None
    return {
        "task_class": "BOUNDED_IMPLEMENTATION",
        "actual_model": actual,
        "actual_reasoning": "HIGH",
        "substitution_class": substitution,
        "substitution_reason_code": reason,
        "substitution_approved": fallback_approved,
        "provider_security_change": False,
        "provider_security_approved": None,
        "authority_before": None,
        "authority_after": None,
        "evidence_refs": [],
    }


def _cost_cli_payload(*, max_cost: str | None, card_path: str | None) -> dict[str, object]:
    return {
        "budget": {
            "max_model_calls": 3,
            "max_input_tokens": 500_000,
            "max_output_tokens": 250_000,
            "max_estimated_cost": max_cost,
            "currency": "USD" if max_cost is not None else None,
        },
        "usage": asdict(_usage()),
        "actual_model": MODEL_SOL,
        "rate_card_path": card_path,
    }


def test_cli_proves_pass_fail_and_blocked_exit_codes(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write_json(root / "rate.json", _rate_card_payload())
    _write_json(root / "model-pass.json", _model_cli_payload())
    fallback = _model_cli_payload(fallback_approved=False)
    fallback["actual_model"] = MODEL_TERRA
    fallback["substitution_class"] = "FALLBACK"
    fallback["substitution_reason_code"] = "MODEL_UNAVAILABLE"
    _write_json(root / "model-fail.json", fallback)
    _write_json(
        root / "cost-pass.json",
        _cost_cli_payload(max_cost="2.03", card_path="rate.json"),
    )
    _write_json(
        root / "cost-fail.json",
        _cost_cli_payload(max_cost="2.02", card_path="rate.json"),
    )
    _write_json(
        root / "cost-blocked.json",
        _cost_cli_payload(max_cost="2.03", card_path=None),
    )

    assert run_cli(["recommend", "--task-class", "bounded_implementation"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    assert run_cli(
        ["evaluate-model", "--root", str(root), "--input", "model-pass.json"]
    ) == 0
    assert run_cli(
        ["evaluate-model", "--root", str(root), "--input", "model-fail.json"]
    ) == 1
    assert run_cli(
        ["evaluate-cost", "--root", str(root), "--input", "cost-pass.json"]
    ) == 0
    assert run_cli(
        ["evaluate-cost", "--root", str(root), "--input", "cost-fail.json"]
    ) == 1
    assert run_cli(
        ["evaluate-cost", "--root", str(root), "--input", "cost-blocked.json"]
    ) == 2


def test_cli_combined_mode_is_not_a_release_gate(tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    root.mkdir()
    _write_json(root / "rate.json", _rate_card_payload())
    _write_json(
        root / "combined.json",
        {
            "model_selection": _model_cli_payload(),
            "cost": _cost_cli_payload(max_cost="2.03", card_path="rate.json"),
        },
    )
    assert run_cli(
        ["evaluate", "--root", str(root), "--input", "combined.json"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall_status"] == "PASS"
    assert "release_gate" not in payload
