from __future__ import annotations

import json

import pytest

from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    MIN_ITEM_CONFIDENCE,
    MIN_OVERALL_CONFIDENCE,
    FoodVisionInventory,
    FoodVisionItem,
    _VISION_PROMPT,
    derive_inventory_confirmation_requirement,
    normalize_nutrition_payload,
    validate_food_vision_inventory,
)


QUALITY_THRESHOLDS = {
    "major_component_precision": 0.90,
    "major_component_recall": 0.90,
    "sauce_recall": 0.90,
    "unsupported_combined_title_count": 0,
    "aggregate_macro_violation_count": 0,
    "invalid_output_staged_count": 0,
    "low_confidence_gate_correctness": 1.0,
}


def _inventory_payload(items, *, overall_confidence=0.9, needs_user_confirmation=False, warnings=None):
    return json.dumps(
        {
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "items": items,
            "overall_confidence": overall_confidence,
            "needs_user_confirmation": needs_user_confirmation,
            "warnings": warnings or [],
        },
        ensure_ascii=False,
    )


def _item(
    visible_name: str,
    normalized_name: str,
    *,
    confidence: float = 0.9,
    grams_min: float | None = 40,
    grams_max: float | None = 60,
    preparation: str = "",
    is_sauce: bool = False,
    uncertainty: str = "",
):
    return {
        "visible_name": visible_name,
        "normalized_name": normalized_name,
        "confidence": confidence,
        "estimated_grams_min": grams_min,
        "estimated_grams_max": grams_max,
        "preparation": preparation,
        "is_sauce": is_sauce,
        "uncertainty": uncertainty,
    }


def _confirmation_decision(items, *, overall_confidence=0.9, needs_user_confirmation=False, warnings=None):
    return derive_inventory_confirmation_requirement(
        FoodVisionInventory(
            schema_version=FOOD_VISION_SCHEMA_VERSION,
            items=[
                FoodVisionItem(
                    visible_name=item["visible_name"],
                    normalized_name=item["normalized_name"],
                    confidence=item["confidence"],
                    estimated_grams_min=item["estimated_grams_min"],
                    estimated_grams_max=item["estimated_grams_max"],
                    preparation=item["preparation"],
                    is_sauce=item["is_sauce"],
                    uncertainty=item["uncertainty"],
                )
                for item in items
            ],
            overall_confidence=overall_confidence,
            needs_user_confirmation=needs_user_confirmation,
            warnings=list(warnings or []),
        )
    )


def _canonical_component(name: str) -> str:
    normalized = name.casefold()
    if "ваф" in normalized:
        return "waffle"
    if "мяс" in normalized:
        return "meat"
    if "огур" in normalized:
        return "cucumber"
    if "майонез" in normalized:
        return "mayonnaise"
    if "горч" in normalized or ("желт" in normalized and "соус" in normalized):
        return "yellow_sauce"
    if "суп" in normalized:
        return "soup"
    if "хлеб" in normalized:
        return "bread"
    if "сметан" in normalized:
        return "sour_cream"
    if normalized == "рис":
        return "rice"
    if "кур" in normalized:
        return "chicken"
    if "салат" in normalized:
        return "salad"
    if "паст" in normalized:
        return "pasta"
    if normalized == "сыр":
        return "cheese"
    if "соус" in normalized:
        return "sauce"
    return normalized


def _evaluate_quality(payload_text: str, *, expected_components: set[str], expected_clarification: bool) -> dict[str, float]:
    validation = validate_food_vision_inventory(payload_text)
    if validation.inventory is None:
        return {
            "major_component_precision": 0.0,
            "major_component_recall": 0.0,
            "sauce_recall": 0.0,
            "unsupported_combined_title_count": 1.0 if validation.reason == "combined_dish_title" else 0.0,
            "aggregate_macro_violation_count": 1.0 if validation.reason == "aggregate_nutrition_present" else 0.0,
            "invalid_output_staged_count": 0.0,
            "low_confidence_gate_correctness": 1.0 if expected_clarification else 0.0,
        }
    actual_components = {_canonical_component(item.normalized_name) for item in validation.inventory.items}
    tp = len(actual_components & expected_components)
    precision = tp / len(actual_components) if actual_components else 0.0
    recall = tp / len(expected_components) if expected_components else 1.0
    expected_sauces = {item for item in expected_components if item in {"mayonnaise", "yellow_sauce", "sour_cream", "sauce"}}
    actual_sauces = {item for item in actual_components if item in {"mayonnaise", "yellow_sauce", "sour_cream", "sauce"}}
    sauce_recall = len(actual_sauces & expected_sauces) / len(expected_sauces) if expected_sauces else 1.0
    needs_clarification = validation.status == "NEEDS_CLARIFICATION"
    return {
        "major_component_precision": precision,
        "major_component_recall": recall,
        "sauce_recall": sauce_recall,
        "unsupported_combined_title_count": 0.0,
        "aggregate_macro_violation_count": 0.0,
        "invalid_output_staged_count": 0.0,
        "low_confidence_gate_correctness": 1.0 if needs_clarification == expected_clarification else 0.0,
    }


def _meets_thresholds(metrics: dict[str, float]) -> bool:
    return (
        metrics["major_component_precision"] >= QUALITY_THRESHOLDS["major_component_precision"]
        and metrics["major_component_recall"] >= QUALITY_THRESHOLDS["major_component_recall"]
        and metrics["sauce_recall"] >= QUALITY_THRESHOLDS["sauce_recall"]
        and metrics["unsupported_combined_title_count"] == QUALITY_THRESHOLDS["unsupported_combined_title_count"]
        and metrics["aggregate_macro_violation_count"] == QUALITY_THRESHOLDS["aggregate_macro_violation_count"]
        and metrics["invalid_output_staged_count"] == QUALITY_THRESHOLDS["invalid_output_staged_count"]
        and metrics["low_confidence_gate_correctness"] == QUALITY_THRESHOLDS["low_confidence_gate_correctness"]
    )


def test_mixed_plate_correct_inventory_meets_offline_quality_thresholds():
    payload = _inventory_payload(
        [
            _item("Вафли", "вафли", grams_min=45, grams_max=70),
            _item("Нарезанное мясо", "мясо", grams_min=90, grams_max=130),
            _item("Огурцы", "огурцы", grams_min=60, grams_max=100),
            _item("Майонез", "майонез", grams_min=10, grams_max=20, is_sauce=True),
            _item("Горчица или желтый соус", "горчица", grams_min=None, grams_max=None, is_sauce=True, uncertainty="количество неясно"),
        ],
        overall_confidence=0.86,
        needs_user_confirmation=True,
    )

    validation = validate_food_vision_inventory(payload)
    metrics = _evaluate_quality(
        payload,
        expected_components={"waffle", "meat", "cucumber", "mayonnaise", "yellow_sauce"},
        expected_clarification=True,
    )

    assert validation.status == "NEEDS_CLARIFICATION"
    assert validation.inventory is not None
    assert len(validation.inventory.items) == 5
    assert _meets_thresholds(metrics) is True


def test_collapsed_mixed_plate_output_is_rejected_fail_closed():
    payload = json.dumps(
        {
            "meal_name": "Завтрак с ватрушками",
            "calories_kcal": 780,
            "protein_g": 22,
            "fat_g": 31,
            "carbs_g": 77,
            "items": [],
        },
        ensure_ascii=False,
    )

    validation = validate_food_vision_inventory(payload)

    assert validation.status == "INVALID_PROVIDER_OUTPUT"
    assert validation.inventory is None


@pytest.mark.parametrize(
    ("items", "expected_names"),
    [
        (
            [
                _item("Суп", "суп", grams_min=250, grams_max=350),
                _item("Хлеб", "хлеб", grams_min=25, grams_max=45),
                _item("Сметана", "сметана", grams_min=10, grams_max=20, is_sauce=True),
            ],
            ["soup", "bread", "sour_cream"],
        ),
        (
            [
                _item("Рис", "рис", grams_min=120, grams_max=180),
                _item("Курица", "курица", grams_min=90, grams_max=140),
                _item("Салат", "салат", grams_min=70, grams_max=120),
            ],
            ["rice", "chicken", "salad"],
        ),
        (
            [
                _item("Паста", "паста", grams_min=120, grams_max=190),
                _item("Сыр", "сыр", grams_min=15, grams_max=35),
                _item("Томатный соус", "томатный соус", grams_min=30, grams_max=60, is_sauce=True),
            ],
            ["pasta", "cheese", "sauce"],
        ),
    ],
)
def test_reference_plate_inventories_require_clarification(items, expected_names):
    validation = validate_food_vision_inventory(_inventory_payload(items))

    assert validation.status == "NEEDS_CLARIFICATION"
    assert validation.inventory is not None
    assert [_canonical_component(item.normalized_name) for item in validation.inventory.items] == expected_names


def test_single_clear_component_with_narrow_weight_range_remains_valid():
    validation = validate_food_vision_inventory(
        _inventory_payload(
            [_item("????", "????", grams_min=250, grams_max=260)],
            overall_confidence=0.92,
            needs_user_confirmation=False,
        )
    )

    assert validation.status == "VALID"
    assert validation.inventory is not None


def test_ambiguous_pastry_forces_clarification_gate():
    payload = _inventory_payload(
        [
            _item(
                "Выпечка",
                "выпечка",
                confidence=MIN_ITEM_CONFIDENCE - 0.05,
                grams_min=50,
                grams_max=110,
                uncertainty="похоже на сладкую выпечку",
            )
        ],
        overall_confidence=MIN_OVERALL_CONFIDENCE - 0.05,
        needs_user_confirmation=True,
    )

    validation = validate_food_vision_inventory(payload)

    assert validation.status == "NEEDS_CLARIFICATION"
    assert validation.inventory is not None


def test_missing_sauce_fixture_fails_offline_quality_thresholds():
    payload = _inventory_payload(
        [
            _item("Вафли", "вафли", grams_min=45, grams_max=70),
            _item("Нарезанное мясо", "мясо", grams_min=90, grams_max=130),
            _item("Огурцы", "огурцы", grams_min=60, grams_max=100),
        ],
        overall_confidence=0.86,
        needs_user_confirmation=True,
    )

    metrics = _evaluate_quality(
        payload,
        expected_components={"waffle", "meat", "cucumber", "mayonnaise", "yellow_sauce"},
        expected_clarification=True,
    )

    assert metrics["sauce_recall"] < QUALITY_THRESHOLDS["sauce_recall"]
    assert _meets_thresholds(metrics) is False


def test_invalid_gram_range_is_rejected():
    payload = _inventory_payload(
        [_item("Соус", "соус", grams_min=80, grams_max=20, is_sauce=True)]
    )

    validation = validate_food_vision_inventory(payload)

    assert validation.status == "INVALID_PROVIDER_OUTPUT"
    assert validation.reason == "invalid_gram_range"


def test_aggregate_nutrition_injection_is_rejected():
    payload = json.dumps(
        {
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "items": [_item("Паста", "паста")],
            "overall_confidence": 0.91,
            "needs_user_confirmation": False,
            "warnings": [],
            "totals": {"calories_kcal": 640},
        },
        ensure_ascii=False,
    )

    validation = validate_food_vision_inventory(payload)

    assert validation.status == "INVALID_PROVIDER_OUTPUT"
    assert validation.reason == "aggregate_nutrition_present"


def test_invalid_confidence_is_rejected():
    payload = _inventory_payload(
        [_item("Курица", "курица", confidence=1.2)],
        overall_confidence=0.9,
    )

    validation = validate_food_vision_inventory(payload)

    assert validation.status == "INVALID_PROVIDER_OUTPUT"
    assert validation.reason == "invalid_item_confidence"


def test_malformed_json_fails_safely():
    validation = validate_food_vision_inventory('{"schema_version":')

    assert validation.status == "INVALID_PROVIDER_OUTPUT"
    assert validation.reason == "invalid_json"


def test_legacy_non_vision_payload_remains_accepted_for_text_flows():
    record = normalize_nutrition_payload(
        json.dumps(
            {
                "is_food": True,
                "meal_name": "Омлет",
                "raw_summary": "Омлет на тарелке.",
                "confidence": 0.88,
                "items": [{"name": "Омлет", "calories_kcal": 220, "protein_g": 14, "fat_g": 15, "carbs_g": 4}],
            },
            ensure_ascii=False,
        )
    )

    assert record is not None
    assert record.meal_name == "Омлет"
    assert record.calories_kcal == pytest.approx(220.0)

def test_prompt_is_provider_neutral_and_not_benchmark_specific():
    prompt = _VISION_PROMPT.casefold()

    assert len(_VISION_PROMPT) < 900
    assert prompt.count("food_vision_inventory_v1") == 1
    assert "qwen" not in prompt
    assert "gemini" not in prompt
    assert "waffle" not in prompt
    assert "ватруш" not in prompt
    assert "сырник" not in prompt
    assert "пирож" not in prompt
    assert "telegram" not in prompt
    assert "diary" not in prompt
    assert "uncertainty" in prompt
    assert "separately visible food components" in _VISION_PROMPT


def test_provider_false_cannot_suppress_local_confirmation_for_mixed_plate():
    decision = _confirmation_decision(
        [
            _item("Вафли", "вафли", grams_min=45, grams_max=70),
            _item("Мясо", "мясо", grams_min=90, grams_max=130),
            _item("Огурцы", "огурцы", grams_min=60, grams_max=100),
        ],
        needs_user_confirmation=False,
    )

    assert decision.required is True
    assert decision.provider_requested is False
    assert decision.local_required is True
    assert "multiple_major_components" in decision.reasons


def test_provider_true_remains_true_even_without_local_risk():
    decision = _confirmation_decision(
        [_item("Борщ", "борщ", grams_min=250, grams_max=260)],
        needs_user_confirmation=True,
    )

    assert decision.required is True
    assert decision.provider_requested is True
    assert "provider_requested" in decision.reasons


def test_sauce_presence_forces_local_confirmation():
    decision = _confirmation_decision(
        [
            _item("Соус", "соус", grams_min=10, grams_max=15, is_sauce=True),
        ],
        needs_user_confirmation=False,
    )

    assert decision.required is True
    assert "sauce_present" in decision.reasons


def test_missing_weight_range_forces_local_confirmation():
    decision = _confirmation_decision(
        [_item("Сыр", "сыр", grams_min=None, grams_max=None)],
        needs_user_confirmation=False,
    )

    assert decision.required is True
    assert "missing_weight_range" in decision.reasons


def test_broad_weight_range_forces_local_confirmation():
    decision = _confirmation_decision(
        [_item("Паста", "паста", grams_min=80, grams_max=160)],
        needs_user_confirmation=False,
    )

    assert decision.required is True
    assert "broad_weight_range" in decision.reasons


def test_ambiguous_specific_normalization_forces_local_confirmation():
    payload = _inventory_payload(
        [
            _item(
                "Слоёная выпечка",
                "ватрушка с творогом",
                confidence=0.72,
                grams_min=70,
                grams_max=110,
                uncertainty="точный вид неясен",
            )
        ],
        overall_confidence=0.82,
        needs_user_confirmation=False,
    )

    validation = validate_food_vision_inventory(payload)
    decision = _confirmation_decision(
        [
            _item(
                "Слоёная выпечка",
                "ватрушка с творогом",
                confidence=0.72,
                grams_min=70,
                grams_max=110,
                uncertainty="точный вид неясен",
            )
        ],
        overall_confidence=0.82,
        needs_user_confirmation=False,
    )

    assert validation.status == "NEEDS_CLARIFICATION"
    assert validation.inventory is not None
    assert validation.inventory.items[0].visible_name == "Слоёная выпечка"
    assert decision.required is True
    assert "ambiguous_normalization" in decision.reasons


def test_warnings_and_low_confidence_force_local_confirmation():
    decision = _confirmation_decision(
        [_item("Рыба", "рыба", confidence=MIN_ITEM_CONFIDENCE - 0.05, grams_min=120, grams_max=140)],
        overall_confidence=MIN_OVERALL_CONFIDENCE - 0.05,
        warnings=["часть блюда вне кадра"],
        needs_user_confirmation=False,
    )

    assert decision.required is True
    assert "low_item_confidence" in decision.reasons
    assert "low_overall_confidence" in decision.reasons
    assert "warnings_present" in decision.reasons


def test_generic_ambiguous_label_remains_schema_valid():
    payload = _inventory_payload(
        [
            _item(
                "Выпечка",
                "сладкая выпечка",
                confidence=0.78,
                grams_min=None,
                grams_max=None,
                uncertainty="начинка не видна",
            )
        ],
        overall_confidence=0.84,
        needs_user_confirmation=False,
    )

    validation = validate_food_vision_inventory(payload)

    assert validation.status == "NEEDS_CLARIFICATION"
    assert validation.inventory is not None
    assert validation.reason == "clarification_required"
