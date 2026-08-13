from __future__ import annotations

import json

from gateway.food_vision_quality import score_food_vision_payload
from gateway.healbite_nutrition_diary import FOOD_VISION_SCHEMA_VERSION


def _item(name: str, *, sauce: bool = True) -> dict[str, object]:
    return {
        "visible_name": name,
        "normalized_name": name,
        "confidence": 0.9,
        "estimated_grams_min": 10,
        "estimated_grams_max": 30,
        "preparation": "",
        "is_sauce": sauce,
        "uncertainty": "",
    }


def _payload(*names: str, extra: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {
        "schema_version": FOOD_VISION_SCHEMA_VERSION,
        "items": [_item(name) for name in names],
        "overall_confidence": 0.9,
        "needs_user_confirmation": False,
        "warnings": [],
    }
    payload.update(extra or {})
    return json.dumps(payload)


def _ambiguity_items() -> list[dict[str, object]]:
    return [
        {
            "generic_label": "sauce",
            "plausible_specific_labels": ["mayonnaise", "sour_cream", "yogurt"],
            "exact_subtype_supported": False,
            "clarification_required": True,
        }
    ]


def _score(payload: str) -> dict[str, object]:
    return score_food_vision_payload(
        payload,
        expected_food_items=[],
        expected_sauce_items=["ketchup", "yellow_sauce", "sauce"],
        expected_needs_clarification=True,
        allowed_aliases={
            "ketchup": ["tomato sauce"],
            "yellow_sauce": ["mustard"],
        },
        ambiguity_items=_ambiguity_items(),
    )


def test_ambiguous_generic_with_runtime_clarification_is_product_correct() -> None:
    score = _score(_payload("ketchup", "yellow_sauce", "sauce"))

    assert score["schema_valid"] is True
    assert score["major_component_precision"] == 1.0
    assert score["major_component_recall"] == 1.0
    assert score["sauce_recall"] == 1.0
    assert score["low_confidence_gate_correctness"] == 1
    assert score["unsupported_specificity_count"] == 0
    assert score["product_outcome"] == "AMBIGUOUS_BUT_SAFELY_CLARIFIED"


def test_ambiguous_unsupported_exact_subtype_is_not_product_correct() -> None:
    score = _score(_payload("ketchup", "yellow_sauce", "sour_cream"))

    assert score["schema_valid"] is True
    assert score["unsupported_specificity_count"] == 1
    assert score["product_outcome"] == "UNSUPPORTED_SPECIFICITY"
    assert score["major_component_precision"] < 1.0


def test_visible_expected_component_omission_is_recognition_miss() -> None:
    score = _score(_payload("ketchup", "sauce"))

    assert score["schema_valid"] is True
    assert score["unsupported_specificity_count"] == 0
    assert score["product_outcome"] == "RECOGNITION_MISS"


def test_schema_invalid_and_unsafe_output_remain_distinct() -> None:
    schema_invalid = _score("not-json")
    unsafe = _score(_payload("sauce", extra={"totals": {"calories_kcal": 1}}))

    assert schema_invalid["product_outcome"] == "SCHEMA_INVALID"
    assert schema_invalid["invalid_aggregate_count"] == 1
    assert unsafe["product_outcome"] == "UNSAFE_OUTPUT"
    assert unsafe["unsafe_aggregate_count"] == 1


def test_non_food_distractor_absence_does_not_reduce_precision() -> None:
    payload = json.dumps(
        {
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "items": [
                {**_item("carrot", sauce=False)},
                {**_item("cucumber", sauce=False)},
                {**_item("cheese", sauce=False)},
            ],
            "overall_confidence": 0.9,
            "needs_user_confirmation": False,
            "warnings": [],
        }
    )
    score = score_food_vision_payload(
        payload,
        expected_food_items=["carrot", "cucumber", "cheese"],
        expected_sauce_items=[],
        expected_needs_clarification=True,
    )

    assert score["major_component_precision"] == 1.0
    assert score["major_component_recall"] == 1.0
    assert score["product_outcome"] == "RECOGNITION_CORRECT"
