from __future__ import annotations

import json
from pathlib import Path

from tools.healbite_food_vision_benchmark import (
    SCORER_V1_LEGACY,
    SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
    load_scorer_v2_contract,
    manifest_image_map,
    normalize_label_v2,
    replay_historical_model,
    score_food_components,
)


FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "healbite_food_vision_benchmark_historical.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _manifest_map() -> dict[str, dict]:
    return manifest_image_map({"images": _fixture()["manifest_images"]})


def _simple_expected() -> tuple[list[list[str]], list[list[str]]]:
    asset = _manifest_map()["simple_plate"]
    return asset["expected_major_components"], asset["expected_sauces"]


def test_legacy_qwen36_simple_plate_replay_preserved_exactly():
    fixture = _fixture()
    model = fixture["models"]["qwen3.6-plus"]
    manifest_map = _manifest_map()
    simple_response = next(item for item in model["responses"] if item["asset_id"] == "simple_plate")

    replay = score_food_components(
        scorer_version=SCORER_V1_LEGACY,
        expected_major_components=manifest_map["simple_plate"]["expected_major_components"],
        expected_sauces=manifest_map["simple_plate"]["expected_sauces"],
        recognized_items=simple_response["recognized_items"],
    )

    assert replay["major_precision"] == 0.0
    assert replay["major_recall"] == 0.0
    assert replay["sauce_recall"] == 1.0
    assert replay["major_true_positive_prediction_count"] == 0
    assert replay["major_false_positive_prediction_count"] == 5
    assert replay["major_false_negative_expected_count"] == 5


def test_v2_simple_alias_and_compound_rules_produce_expected_counterfactual():
    fixture = _fixture()
    model = fixture["models"]["qwen3.6-plus"]
    manifest_map = _manifest_map()
    contract = load_scorer_v2_contract()
    simple_response = next(item for item in model["responses"] if item["asset_id"] == "simple_plate")

    replay = score_food_components(
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        expected_major_components=manifest_map["simple_plate"]["expected_major_components"],
        expected_sauces=manifest_map["simple_plate"]["expected_sauces"],
        recognized_items=simple_response["recognized_items"],
        contract=contract,
    )

    assert replay["major_precision"] == 0.8
    assert replay["major_recall"] == 1.0
    assert replay["sauce_recall"] == 1.0
    assert replay["major_true_positive_prediction_count"] == 4
    assert replay["major_false_positive_prediction_count"] == 1
    assert replay["covered_expected_components"] == ["курица", "овощи", "помидор", "рис", "салат"]
    assert replay["uncovered_expected_components"] == []
    assert replay["compound_mappings_used"] == [
        {
            "recognized_index": 1,
            "phrase": "рис с овощами",
            "covered_expected_components": ["рис", "овощи"],
        }
    ]


def test_v2_garnish_remains_unmatched_false_positive():
    expected_major, expected_sauces = _simple_expected()

    replay = score_food_components(
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        expected_major_components=expected_major,
        expected_sauces=expected_sauces,
        recognized_items=[{"visible_name": "Веточка зелени", "normalized_name": "fresh_herb_sprig", "is_sauce": False}],
        contract=load_scorer_v2_contract(),
    )

    assert replay["major_precision"] == 0.0
    assert replay["major_recall"] == 0.0
    assert replay["unmatched_recognized_components"][0]["visible_name"] == "Веточка зелени"


def test_v2_normalization_is_idempotent_and_unicode_safe():
    value = "  Листья\u00A0салата  "
    once = normalize_label_v2(value)
    twice = normalize_label_v2(once)

    assert once == "листья салата"
    assert twice == once
    assert normalize_label_v2("рис_с_овощами") == "рис с овощами"
    assert normalize_label_v2("Зелень (тимьян)") == "зелень тимьян"


def test_v2_rejects_generic_phrase_splitting_and_substring_matching():
    expected_major, expected_sauces = _simple_expected()

    replay = score_food_components(
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        expected_major_components=expected_major,
        expected_sauces=expected_sauces,
        recognized_items=[
            {"visible_name": "Чай с лимоном", "normalized_name": "чай с лимоном", "is_sauce": False},
            {"visible_name": "Почти курица", "normalized_name": "почти курица", "is_sauce": False},
        ],
        contract=load_scorer_v2_contract(),
    )

    assert replay["major_precision"] == 0.0
    assert replay["major_recall"] == 0.0
    assert len(replay["unmatched_recognized_components"]) == 2


def test_v2_duplicate_predictions_do_not_inflate_recall():
    replay = score_food_components(
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        expected_major_components=[["рис"], ["овощи"]],
        expected_sauces=[],
        recognized_items=[
            {"visible_name": "Рис с овощами", "normalized_name": "рис с овощами", "is_sauce": False},
            {"visible_name": "Рис с овощами", "normalized_name": "рис с овощами", "is_sauce": False},
        ],
        contract=load_scorer_v2_contract(),
    )

    assert replay["major_precision"] == 0.5
    assert replay["major_recall"] == 1.0
    assert replay["major_true_positive_prediction_count"] == 1
    assert replay["major_false_positive_prediction_count"] == 1


def test_v2_compound_precedence_is_deterministic():
    contract = {
        "contract_version": "test",
        "single_component_aliases": [
            {"canonical_component": "рис", "alias": "рис с овощами"},
        ],
        "compound_mappings": [
            {"phrase": "рис с овощами", "canonical_components": ["рис", "овощи"]},
        ],
    }

    replay = score_food_components(
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        expected_major_components=[["рис"], ["овощи"]],
        expected_sauces=[],
        recognized_items=[{"visible_name": "Рис с овощами", "normalized_name": "рис с овощами", "is_sauce": False}],
        contract=contract,
    )

    assert replay["major_precision"] == 1.0
    assert replay["major_recall"] == 1.0
    assert replay["compound_mappings_used"][0]["covered_expected_components"] == ["рис", "овощи"]
    assert replay["matched_recognized_predictions"][0]["match_type"] == "explicit_compound"


def test_historical_multi_model_replay_reports_complete_available_outputs():
    fixture = _fixture()
    manifest_map = _manifest_map()
    contract = load_scorer_v2_contract()

    qwen36_v1 = replay_historical_model(
        model_id="qwen3.6-plus",
        historical_data=fixture["models"]["qwen3.6-plus"],
        manifest_map=manifest_map,
        scorer_version=SCORER_V1_LEGACY,
    )
    qwen36_v2 = replay_historical_model(
        model_id="qwen3.6-plus",
        historical_data=fixture["models"]["qwen3.6-plus"],
        manifest_map=manifest_map,
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        contract=contract,
    )
    qwen37_v2 = replay_historical_model(
        model_id="qwen3.7-plus",
        historical_data=fixture["models"]["qwen3.7-plus"],
        manifest_map=manifest_map,
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        contract=contract,
    )
    qwen8b_v2 = replay_historical_model(
        model_id="qwen3-vl-8b-instruct",
        historical_data=fixture["models"]["qwen3-vl-8b-instruct"],
        manifest_map=manifest_map,
        scorer_version=SCORER_V2_EXPLICIT_ALIAS_COMPOUND,
        contract=contract,
    )

    assert qwen36_v1["exact_v1_reproduction"] is True
    assert qwen36_v2["historical_replay_complete"] is True
    assert qwen36_v2["quality_gate_result"] == "SCORER_V2_REPLAY_GATE_FAIL"
    assert qwen36_v2["aggregate_metrics"] == {
        "major_component_precision": 0.75,
        "major_component_recall": 0.875,
        "sauce_recall": 0.5,
        "major_precision_numerator": 6,
        "major_precision_denominator": 8,
        "major_recall_numerator": 7,
        "major_recall_denominator": 8,
    }
    assert qwen37_v2["aggregate_metrics"]["major_component_precision"] == 0.5
    assert qwen37_v2["aggregate_metrics"]["major_component_recall"] == 0.625
    assert qwen37_v2["quality_gate_result"] == "SCORER_V2_REPLAY_GATE_FAIL"
    assert qwen8b_v2["aggregate_metrics"]["major_component_precision"] == 0.714286
    assert qwen8b_v2["aggregate_metrics"]["major_component_recall"] == 0.75
    assert qwen8b_v2["quality_gate_result"] == "SCORER_V2_REPLAY_GATE_FAIL"
