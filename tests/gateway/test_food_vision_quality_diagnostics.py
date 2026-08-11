from __future__ import annotations

import json

from gateway.food_vision_quality import score_food_vision_payload
from gateway.healbite_nutrition_diary import FOOD_VISION_SCHEMA_VERSION


def _item(name: str, *, sauce: bool = False) -> dict[str, object]:
    return {
        "visible_name": name,
        "normalized_name": name,
        "confidence": 0.9,
        "estimated_grams_min": 20,
        "estimated_grams_max": 40,
        "preparation": "",
        "is_sauce": sauce,
        "uncertainty": "",
    }


def _payload(*names: str, sauces: tuple[str, ...] = ()) -> str:
    return json.dumps({
        "schema_version": FOOD_VISION_SCHEMA_VERSION,
        "items": [_item(name, sauce=name in sauces) for name in names],
        "overall_confidence": 0.9,
        "needs_user_confirmation": False,
        "warnings": [],
    })


def test_diagnostics_explain_zero_match_without_changing_scores():
    score = score_food_vision_payload(
        _payload("dragonfruit", "mystery-food"),
        expected_food_items=["apple", "banana"],
        expected_sauce_items=[],
        expected_needs_clarification=False,
    )
    diagnostics = score["diagnostics"]
    assert score["schema_valid"] is True
    assert score["normalized_prediction_count"] == 2
    assert score["true_positive_count"] == 0
    assert score["major_component_precision"] == 0.0
    assert score["major_component_recall"] == 0.0
    assert diagnostics["matched_expected_components"] == []
    assert diagnostics["missed_expected_components"] == ["apple", "banana"]
    assert diagnostics["unexpected_predicted_components"] == ["dragonfruit", "mystery-food"]


def test_alias_diagnostics_share_canonical_scoring_semantics():
    score = score_food_vision_payload(
        _payload("green cucumber", "mustard", sauces=("mustard",)),
        expected_food_items=["cucumber"],
        expected_sauce_items=["yellow_sauce"],
        expected_needs_clarification=False,
        allowed_aliases={"cucumber": ["green cucumber"], "yellow_sauce": ["mustard"]},
    )
    diagnostics = score["diagnostics"]
    assert score["true_positive_count"] == 2
    assert diagnostics["matched_expected_components"] == ["cucumber", "yellow_sauce"]
    assert diagnostics["missed_expected_components"] == []
    assert diagnostics["unexpected_predicted_components"] == []
    assert diagnostics["matched_expected_sauces"] == ["yellow_sauce"]


def test_untrusted_validated_label_is_redacted_without_raw_content():
    payload = _payload("https://provider.invalid/data:image/png;base64,SECRET")
    score = score_food_vision_payload(
        payload,
        expected_food_items=["apple"],
        expected_sauce_items=[],
        expected_needs_clarification=False,
    )
    serialized = json.dumps(score["diagnostics"], ensure_ascii=True)
    assert "provider.invalid" not in serialized
    assert "base64" not in serialized
    assert "[REDACTED_LABEL]" in serialized
