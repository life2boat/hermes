"""Pure, shared scoring contract for food-Vision quality evidence.

The application validator remains authoritative for provider output.  This
module deliberately contains no provider, filesystem, or persistence access so
the offline contract tests and the opt-in provider harness share one scoring
implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any

from gateway.healbite_nutrition_diary import validate_food_vision_inventory


QUALITY_THRESHOLDS: dict[str, float | int] = {
    "major_component_precision": 0.90,
    "major_component_recall": 0.90,
    "sauce_recall": 0.90,
    "unsafe_aggregate_count": 0,
    "invalid_aggregate_count": 0,
}

_SAUCE_COMPONENTS = {"mayonnaise", "yellow_sauce", "sour_cream", "sauce"}
_DIAGNOSTIC_MAX_LABEL_LENGTH = 120
_DIAGNOSTIC_MAX_REDACTIONS = 12
_DIAGNOSTIC_UNSAFE_LABEL = "[REDACTED_LABEL]"
_DIAGNOSTIC_UNSAFE_RE = re.compile(
    r"(?:data\s*:|https?://|base64|bearer\s+|api[_-]?key|authorization|password|secret|token)",
    re.IGNORECASE,
)
_SCHEMA_ERROR_SUMMARIES = {
    "invalid_json": "OTHER_PROVEN_CAUSE",
    "aggregate_nutrition_present": "LOCAL_INVARIANT_REJECTION",
    "unknown_top_level_fields": "UNKNOWN_FIELD",
    "unsupported_schema_version": "INVALID_ENUM",
    "empty_items": "OTHER_PROVEN_CAUSE",
    "too_many_items": "LOCAL_INVARIANT_REJECTION",
    "invalid_overall_confidence": "OTHER_PROVEN_CAUSE",
    "invalid_needs_user_confirmation": "FIELD_TYPE_MISMATCH",
    "invalid_warnings": "FIELD_TYPE_MISMATCH",
    "too_many_warnings": "LOCAL_INVARIANT_REJECTION",
    "invalid_warning_text": "OTHER_PROVEN_CAUSE",
    "item_not_object": "FIELD_TYPE_MISMATCH",
    "aggregate_item_field_present": "LOCAL_INVARIANT_REJECTION",
    "unknown_item_fields": "UNKNOWN_FIELD",
    "empty_item_name": "MISSING_REQUIRED_FIELD",
    "invalid_item_confidence": "OTHER_PROVEN_CAUSE",
    "invalid_is_sauce": "FIELD_TYPE_MISMATCH",
    "negative_grams": "LOCAL_INVARIANT_REJECTION",
    "invalid_gram_range": "LOCAL_INVARIANT_REJECTION",
    "absurd_portion_range": "LOCAL_INVARIANT_REJECTION",
    "combined_dish_title": "LOCAL_INVARIANT_REJECTION",
}


def canonical_food_component(name: str) -> str:
    """Normalize the stable food names used by the offline quality contract."""

    normalized = str(name).strip().casefold()
    aliases = (
        (("ваф", "waffle"), "waffle"),
        (("мяс", "meat"), "meat"),
        (("огур", "cucumber"), "cucumber"),
        (("майонез", "mayonnaise"), "mayonnaise"),
        (("горч", "yellow sauce", "yellow_sauce"), "yellow_sauce"),
        (("суп", "soup"), "soup"),
        (("хлеб", "bread"), "bread"),
        (("сметан", "sour cream", "sour_cream"), "sour_cream"),
        (("рис", "rice"), "rice"),
        (("кур", "chicken"), "chicken"),
        (("салат", "salad"), "salad"),
        (("паст", "pasta"), "pasta"),
        (("сыр", "cheese"), "cheese"),
        (("соус", "sauce"), "sauce"),
        (("яблок", "apple"), "apple"),
        (("банан", "banana"), "banana"),
        (("помидор", "tomato"), "tomato"),
        (("морков", "carrot"), "carrot"),
        (("перец", "pepper"), "pepper"),
    )
    for needles, canonical in aliases:
        if any(needle in normalized for needle in needles):
            return canonical
    return normalized


def _canonical_with_alias(name: str, aliases: Mapping[str, Iterable[str]] | None) -> str:
    alias_lookup: dict[str, str] = {}
    for canonical, alternatives in (aliases or {}).items():
        normalized_canonical = canonical_food_component(canonical)
        alias_lookup[normalized_canonical] = normalized_canonical
        for alternative in alternatives:
            alias_lookup[canonical_food_component(str(alternative))] = normalized_canonical
    component = canonical_food_component(name)
    return alias_lookup.get(component, component)


def _canonical_expected(items: Iterable[str], aliases: Mapping[str, Iterable[str]] | None) -> set[str]:
    return {_canonical_with_alias(item, aliases) for item in items}


def _safe_diagnostic_label(label: object) -> tuple[str, bool]:
    """Return a bounded receipt label without persisting untrusted text."""

    if not isinstance(label, str):
        return _DIAGNOSTIC_UNSAFE_LABEL, True
    normalized = " ".join(
        "".join(character if character.isprintable() else " " for character in label).split()
    )
    if (
        not normalized
        or len(normalized) > _DIAGNOSTIC_MAX_LABEL_LENGTH
        or _DIAGNOSTIC_UNSAFE_RE.search(normalized)
    ):
        return _DIAGNOSTIC_UNSAFE_LABEL, True
    return normalized, False


def _safe_diagnostic_labels(labels: Iterable[object]) -> tuple[list[str], int]:
    safe: list[str] = []
    redactions = 0
    string_labels = []
    for value in labels:
        if isinstance(value, str):
            string_labels.append(value)
        else:
            redactions += 1
    for label in sorted(set(string_labels)):
        sanitized, redacted = _safe_diagnostic_label(label)
        if redacted:
            redactions += 1
        if sanitized not in safe:
            safe.append(sanitized)
        if redactions >= _DIAGNOSTIC_MAX_REDACTIONS:
            break
    return safe, redactions


def _schema_error_diagnostic(reason: str) -> tuple[str, str]:
    """Return only closed, non-provider schema failure evidence."""

    if reason in _SCHEMA_ERROR_SUMMARIES:
        return reason, _SCHEMA_ERROR_SUMMARIES[reason]
    return "unknown_validation_failure", "UNKNOWN"


def _empty_diagnostics(
    *,
    schema_error_code: str = "NONE",
    schema_error_summary: str = "NONE",
) -> dict[str, Any]:
    return {
        "schema_error_code": schema_error_code,
        "schema_error_summary": schema_error_summary,
        "validated_prediction_labels": [],
        "canonical_predicted_components": [],
        "matched_expected_components": [],
        "missed_expected_components": [],
        "unexpected_predicted_components": [],
        "canonical_predicted_sauces": [],
        "matched_expected_sauces": [],
        "missed_expected_sauces": [],
        "diagnostic_redaction_count": 0,
    }


def _diagnostics(
    *,
    actual_components: set[str],
    expected_components: set[str],
    actual_sauces: set[str],
    expected_sauces: set[str],
    validated_labels: Iterable[object],
) -> dict[str, Any]:
    labels, label_redactions = _safe_diagnostic_labels(validated_labels)
    canonical_predictions, canonical_redactions = _safe_diagnostic_labels(actual_components)
    matched, matched_redactions = _safe_diagnostic_labels(actual_components & expected_components)
    missed, missed_redactions = _safe_diagnostic_labels(expected_components - actual_components)
    unexpected, unexpected_redactions = _safe_diagnostic_labels(actual_components - expected_components)
    predicted_sauces, predicted_sauce_redactions = _safe_diagnostic_labels(actual_sauces)
    matched_sauces, matched_sauce_redactions = _safe_diagnostic_labels(actual_sauces & expected_sauces)
    missed_sauces, missed_sauce_redactions = _safe_diagnostic_labels(expected_sauces - actual_sauces)
    return {
        "schema_error_code": "NONE",
        "schema_error_summary": "NONE",
        "validated_prediction_labels": labels,
        "canonical_predicted_components": canonical_predictions,
        "matched_expected_components": matched,
        "missed_expected_components": missed,
        "unexpected_predicted_components": unexpected,
        "canonical_predicted_sauces": predicted_sauces,
        "matched_expected_sauces": matched_sauces,
        "missed_expected_sauces": missed_sauces,
        "diagnostic_redaction_count": min(
            _DIAGNOSTIC_MAX_REDACTIONS,
            label_redactions
            + canonical_redactions
            + matched_redactions
            + missed_redactions
            + unexpected_redactions
            + predicted_sauce_redactions
            + matched_sauce_redactions
            + missed_sauce_redactions,
        ),
    }


def score_food_vision_payload(
    payload_text: str,
    *,
    expected_food_items: Iterable[str],
    expected_sauce_items: Iterable[str],
    expected_needs_clarification: bool,
    allowed_aliases: Mapping[str, Iterable[str]] | None = None,
    ambiguity_items: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate and score one application-format provider response.

    Returned counts make aggregate scoring deterministic while the percentage
    fields preserve the existing offline test's public behavior.
    """

    expected_food = _canonical_expected(expected_food_items, allowed_aliases)
    expected_sauces = _canonical_expected(expected_sauce_items, allowed_aliases)
    expected_components = expected_food | expected_sauces
    ambiguity_policies = tuple(
        (
            _canonical_with_alias(str(item["generic_label"]), allowed_aliases),
            _canonical_expected(item["plausible_specific_labels"], allowed_aliases),
            bool(item["exact_subtype_supported"]),
            bool(item["clarification_required"]),
        )
        for item in (ambiguity_items or ())
    )
    clarification_required = expected_needs_clarification or any(policy[3] for policy in ambiguity_policies)
    validation = validate_food_vision_inventory(payload_text)
    if validation.inventory is None:
        schema_error_code, schema_error_summary = _schema_error_diagnostic(validation.reason)
        unsafe = 1 if validation.reason == "aggregate_nutrition_present" else 0
        invalid = 0 if unsafe else 1
        return {
            "major_component_precision": 0.0,
            "major_component_recall": 0.0,
            "sauce_recall": 0.0,
            "unsafe_aggregate_count": unsafe,
            "invalid_aggregate_count": invalid,
            "unsupported_combined_title_count": int(validation.reason == "combined_dish_title"),
            "low_confidence_gate_correctness": int(expected_needs_clarification),
            "true_positive_count": 0,
            "predicted_count": 0,
            "expected_count": len(expected_components),
            "sauce_true_positive_count": 0,
            "predicted_sauce_count": 0,
            "expected_sauce_count": len(expected_sauces),
            "schema_valid": False,
            "normalized_prediction_count": 0,
            "unsupported_specificity_count": 0,
            "product_outcome": "UNSAFE_OUTPUT" if unsafe else "SCHEMA_INVALID",
            "diagnostics": _empty_diagnostics(
                schema_error_code=schema_error_code,
                schema_error_summary=schema_error_summary,
            ),
        }

    actual_components = {_canonical_with_alias(item.normalized_name, allowed_aliases) for item in validation.inventory.items}
    actual_sauces = {
        _canonical_with_alias(item.normalized_name, allowed_aliases)
        for item in validation.inventory.items
        if item.is_sauce or _canonical_with_alias(item.normalized_name, allowed_aliases) in _SAUCE_COMPONENTS
    }
    true_positive = len(actual_components & expected_components)
    sauce_true_positive = len(actual_sauces & expected_sauces)
    unsupported_specificity = set().union(
        *(
            actual_components & specific_labels
            for _generic, specific_labels, exact_supported, _clarification in ambiguity_policies
            if not exact_supported
        ),
        set(),
    )
    precision = true_positive / len(actual_components) if actual_components else 0.0
    recall = true_positive / len(expected_components) if expected_components else 1.0
    sauce_recall = sauce_true_positive / len(expected_sauces) if expected_sauces else 1.0
    clarification_correct = (validation.status == "NEEDS_CLARIFICATION") == clarification_required
    recognition_correct = precision == 1.0 and recall == 1.0 and sauce_recall == 1.0
    if unsupported_specificity:
        product_outcome = "UNSUPPORTED_SPECIFICITY"
    elif not recognition_correct:
        product_outcome = "RECOGNITION_MISS"
    elif not clarification_correct:
        product_outcome = "CLARIFICATION_REQUIRED"
    elif ambiguity_policies:
        product_outcome = "AMBIGUOUS_BUT_SAFELY_CLARIFIED"
    else:
        product_outcome = "RECOGNITION_CORRECT"
    return {
        "major_component_precision": precision,
        "major_component_recall": recall,
        "sauce_recall": sauce_recall,
        "unsafe_aggregate_count": 0,
        "invalid_aggregate_count": 0,
        "unsupported_combined_title_count": 0,
        "low_confidence_gate_correctness": int(clarification_correct),
        "true_positive_count": true_positive,
        "predicted_count": len(actual_components),
        "expected_count": len(expected_components),
        "sauce_true_positive_count": sauce_true_positive,
        "predicted_sauce_count": len(actual_sauces),
        "expected_sauce_count": len(expected_sauces),
        "schema_valid": True,
        "normalized_prediction_count": len(actual_components),
        "unsupported_specificity_count": len(unsupported_specificity),
        "product_outcome": product_outcome,
        "diagnostics": _diagnostics(
            actual_components=actual_components,
            expected_components=expected_components,
            actual_sauces=actual_sauces,
            expected_sauces=expected_sauces,
            validated_labels=(item.normalized_name for item in validation.inventory.items),
        ),
    }


def aggregate_food_vision_scores(scores: Iterable[Mapping[str, Any]]) -> dict[str, float | int]:
    """Aggregate per-fixture counts without inventing alternate semantics."""

    totals = {
        "true_positive_count": 0,
        "predicted_count": 0,
        "expected_count": 0,
        "sauce_true_positive_count": 0,
        "expected_sauce_count": 0,
        "unsafe_aggregate_count": 0,
        "invalid_aggregate_count": 0,
    }
    for score in scores:
        for key in totals:
            totals[key] += int(score[key])
    return {
        "precision": totals["true_positive_count"] / totals["predicted_count"] if totals["predicted_count"] else 0.0,
        "recall": totals["true_positive_count"] / totals["expected_count"] if totals["expected_count"] else 1.0,
        "sauce_recall": (
            totals["sauce_true_positive_count"] / totals["expected_sauce_count"]
            if totals["expected_sauce_count"]
            else 1.0
        ),
        "unsafe_aggregate_count": totals["unsafe_aggregate_count"],
        "invalid_aggregate_count": totals["invalid_aggregate_count"],
    }


def quality_gate_passes(aggregate: Mapping[str, float | int]) -> bool:
    """Return the canonical gate result without relaxing any threshold."""

    return (
        float(aggregate["precision"]) >= float(QUALITY_THRESHOLDS["major_component_precision"])
        and float(aggregate["recall"]) >= float(QUALITY_THRESHOLDS["major_component_recall"])
        and float(aggregate["sauce_recall"]) >= float(QUALITY_THRESHOLDS["sauce_recall"])
        and int(aggregate["unsafe_aggregate_count"]) == int(QUALITY_THRESHOLDS["unsafe_aggregate_count"])
        and int(aggregate["invalid_aggregate_count"]) == int(QUALITY_THRESHOLDS["invalid_aggregate_count"])
    )
