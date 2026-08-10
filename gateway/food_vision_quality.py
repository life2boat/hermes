"""Pure, shared scoring contract for food-Vision quality evidence.

The application validator remains authoritative for provider output.  This
module deliberately contains no provider, filesystem, or persistence access so
the offline contract tests and the opt-in provider harness share one scoring
implementation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
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


def score_food_vision_payload(
    payload_text: str,
    *,
    expected_food_items: Iterable[str],
    expected_sauce_items: Iterable[str],
    expected_needs_clarification: bool,
    allowed_aliases: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, float | int | bool]:
    """Validate and score one application-format provider response.

    Returned counts make aggregate scoring deterministic while the percentage
    fields preserve the existing offline test's public behavior.
    """

    expected_food = _canonical_expected(expected_food_items, allowed_aliases)
    expected_sauces = _canonical_expected(expected_sauce_items, allowed_aliases)
    expected_components = expected_food | expected_sauces
    validation = validate_food_vision_inventory(payload_text)
    if validation.inventory is None:
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
        }

    actual_components = {_canonical_with_alias(item.normalized_name, allowed_aliases) for item in validation.inventory.items}
    actual_sauces = {
        _canonical_with_alias(item.normalized_name, allowed_aliases)
        for item in validation.inventory.items
        if item.is_sauce or _canonical_with_alias(item.normalized_name, allowed_aliases) in _SAUCE_COMPONENTS
    }
    true_positive = len(actual_components & expected_components)
    sauce_true_positive = len(actual_sauces & expected_sauces)
    precision = true_positive / len(actual_components) if actual_components else 0.0
    recall = true_positive / len(expected_components) if expected_components else 1.0
    sauce_recall = sauce_true_positive / len(expected_sauces) if expected_sauces else 1.0
    return {
        "major_component_precision": precision,
        "major_component_recall": recall,
        "sauce_recall": sauce_recall,
        "unsafe_aggregate_count": 0,
        "invalid_aggregate_count": 0,
        "unsupported_combined_title_count": 0,
        "low_confidence_gate_correctness": int(
            (validation.status == "NEEDS_CLARIFICATION") == expected_needs_clarification
        ),
        "true_positive_count": true_positive,
        "predicted_count": len(actual_components),
        "expected_count": len(expected_components),
        "sauce_true_positive_count": sauce_true_positive,
        "predicted_sauce_count": len(actual_sauces),
        "expected_sauce_count": len(expected_sauces),
        "schema_valid": True,
        "normalized_prediction_count": len(actual_components),
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
