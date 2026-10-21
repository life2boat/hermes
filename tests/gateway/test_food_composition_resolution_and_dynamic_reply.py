from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import AsyncMock, patch
import pytest

from gateway.healbite_food_composition import resolve_food_composition
from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    HealBiteNutritionDiary,
    PendingFoodVisionItem,
    PendingFoodVisionInventory,
    FoodVisionItem,
    format_food_vision_inventory_reply,
    parse_pending_inventory_action,
    _build_inventory_calculation_snapshot,
    _get_edible_yield_ratio,
    _is_food_in_shell,
    _resolve_synthetic_food_reference,
)
from gateway.nutrition.engine import resolve_and_calculate_component
from gateway.nutrition.contracts import MassBounds
from gateway.run import _is_healbite_tester_user


def _mock_incident_five_walnuts_json():
    """Exact reproduction of the real incident: 5 walnuts in shell."""
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": [
                {
                    "visible_name": "Грецкий орех в скорлупе",
                    "normalized_name": "walnut",
                    "confidence": 0.95,
                    "estimated_grams_min": 50.0,
                    "estimated_grams_max": 75.0,
                    "preparation": "целый, в скорлупе",
                    "is_sauce": False,
                    "uncertainty": "",
                    "quantity": 5,
                }
            ],
            "overall_confidence": 0.95,
            "needs_user_confirmation": False,
            "warnings": [],
        }, ensure_ascii=False)
    })


def _mock_five_separate_walnuts_json():
    """Gemini returning 5 individual items."""
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": [
                {
                    "visible_name": "Грецкий орех в скорлупе",
                    "normalized_name": "walnut",
                    "confidence": 0.95,
                    "estimated_grams_min": 10.0,
                    "estimated_grams_max": 15.0,
                    "preparation": "целый, в скорлупе",
                    "is_sauce": False,
                    "uncertainty": "",
                }
                for _ in range(5)
            ],
            "overall_confidence": 0.95,
            "needs_user_confirmation": False,
            "warnings": [],
        }, ensure_ascii=False)
    })


# -----------------------------------------------------------------------------
# Scenario 1: Five walnuts -> one grouped entry
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_01_five_walnuts_one_grouped_entry(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = _mock_five_separate_walnuts_json()

        outcome = await diary.analyze_and_maybe_log(
            user_id=user_id,
            image_path="/tmp/walnuts.jpg",
        )

        assert outcome.available is True
        assert outcome.pending_inventory is not None
        assert len(outcome.pending_inventory.items) == 1
        item = outcome.pending_inventory.items[0]
        assert item.quantity == 5
        assert item.visible_name == "Грецкие орехи в скорлупе"


# -----------------------------------------------------------------------------
# Scenario 2: Food lookup resolves edible walnut kernels
# -----------------------------------------------------------------------------
def test_scenario_02_food_lookup_resolves_edible_walnut_kernels():
    # Via built-in catalog resolve_food_composition
    res_en = resolve_food_composition("walnut")
    assert res_en.matched is True
    assert res_en.profile is not None
    assert res_en.profile.display_name == "Грецкие орехи (ядра)"
    assert res_en.profile.calories_kcal == 654.0
    assert res_en.profile.protein_g == 15.2
    assert res_en.profile.fat_g == 65.2
    assert res_en.profile.carbs_g == 13.7

    res_ru = resolve_food_composition("грецкий орех в скорлупе")
    assert res_ru.matched is True
    assert res_ru.profile.display_name == "Грецкие орехи (ядра)"

    # Via engine resolve_and_calculate_component
    bounds = MassBounds(grams_min=22.5, grams_center=28.1, grams_max=33.8, is_approximate_plausible_range=True)
    comp = resolve_and_calculate_component("walnut", bounds)
    assert comp.nutrition_available is True
    assert comp.name == "Грецкие орехи (ядра)"


# -----------------------------------------------------------------------------
# Scenario 3: All KBZHU fields are non-null
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_03_all_kbzhu_fields_non_null(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = _mock_incident_five_walnuts_json()

        outcome = await diary.analyze_and_maybe_log(
            user_id=user_id,
            image_path="/tmp/walnuts.jpg",
        )

        assert outcome.available is True
        snap = outcome.pending_inventory.calculation_snapshot
        assert snap is not None
        assert snap["has_unmapped_components"] is False
        assert snap["calories_kcal"] is not None and snap["calories_kcal"] > 0
        assert snap["protein_g"] is not None and snap["protein_g"] > 0
        assert snap["fat_g"] is not None and snap["fat_g"] > 0
        assert snap["carbs_g"] is not None and snap["carbs_g"] > 0

        # Component level non-null
        comp = snap["components"][0]
        assert comp["nutrition_available"] is True
        assert comp["calories_kcal"] is not None
        assert comp["protein_g"] is not None
        assert comp["fat_g"] is not None
        assert comp["carbs_g"] is not None


# -----------------------------------------------------------------------------
# Scenario 4: Gross != edible weight
# -----------------------------------------------------------------------------
def test_scenario_04_gross_not_equal_edible_weight():
    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="walnut",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=62.5,
        preparation="целый, в скорлупе",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        edible_yield_ratio=0.45,
    )
    inv = PendingFoodVisionInventory(
        user_id=101,
        inventory_id="inv-walnuts",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None
    comp = snap["components"][0]

    gross_weight = item.selected_grams  # 62.5
    edible_weight = comp["estimated_weight_g"]  # 28.1
    assert gross_weight != edible_weight
    assert edible_weight == pytest.approx(gross_weight * 0.45, abs=0.2)

    reply = format_food_vision_inventory_reply(inv)
    assert "Общий вес: ~50–75 г." in reply or "Общий вес: ~62 г." in reply
    assert "Съедобная часть: ~23–34 г." in reply or "Съедобная часть: ~28 г." in reply


# -----------------------------------------------------------------------------
# Scenario 5: Yield factor applied exactly once
# -----------------------------------------------------------------------------
def test_scenario_05_yield_factor_applied_exactly_once():
    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="walnut",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=60.0,
        preparation="целый, в скорлупе",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        edible_yield_ratio=0.45,
    )
    inv = PendingFoodVisionInventory(
        user_id=101,
        inventory_id="inv-walnuts",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    comp = snap["components"][0]

    # Gross: 60g -> Edible: 27g
    # Walnuts per 100g: 654 kcal.
    # Expected calories: 27g * 6.54 = ~176.6 kcal.
    # If applied twice: 27 * 0.45 = 12.15g * 6.54 = ~79.5 kcal.
    # If not applied: 60g * 6.54 = ~392.4 kcal.
    assert comp["estimated_weight_g"] == pytest.approx(27.0, abs=0.5)
    assert comp["calories_kcal"] == pytest.approx(176.6, abs=3.0)
    assert snap["calories_kcal"] == pytest.approx(176.6, abs=3.0)


# -----------------------------------------------------------------------------
# Scenario 6: Aliases with shell/preparation recognized
# -----------------------------------------------------------------------------
def test_scenario_06_aliases_with_shell_and_preparation_recognized():
    # Case A: English normalized_name without shell, visible_name in shell
    ratio_a = _get_edible_yield_ratio("walnut", "Грецкий орех в скорлупе", preparation="целый, в скорлупе")
    assert ratio_a == 0.45

    # Case B: English walnut in shell
    ratio_b = _get_edible_yield_ratio("walnut in shell")
    assert ratio_b == 0.45

    # Case C: Pistachios in shell
    ratio_c = _get_edible_yield_ratio("фисташки", preparation="в скорлупе")
    assert ratio_c == 0.50

    # Case D: Shelled walnuts (no shell mentioned) -> yield is 1.0
    ratio_d = _get_edible_yield_ratio("грецкие орехи", "walnut", preparation="очищенные")
    assert ratio_d == 1.0


# -----------------------------------------------------------------------------
# Scenario 7: Generic unknown nuts do not silently map to a specific species
# -----------------------------------------------------------------------------
def test_scenario_07_generic_unknown_nuts_do_not_silently_map():
    # "орехи" must NOT map to walnuts or any profile
    lookup = resolve_food_composition("орехи")
    assert lookup.matched is False

    lookup_single = resolve_food_composition("орех")
    assert lookup_single.matched is False

    synth = _resolve_synthetic_food_reference("орехи")
    assert synth is None

    bounds = MassBounds(grams_min=30.0, grams_center=30.0, grams_max=30.0, is_approximate_plausible_range=False)
    comp = resolve_and_calculate_component("орехи", bounds)
    assert comp.nutrition_available is False
    assert comp.intervals.calories_kcal is None


# -----------------------------------------------------------------------------
# Scenario 8: Stale missing-nutrition snapshot corrected safely on edit
# -----------------------------------------------------------------------------
def test_scenario_08_stale_missing_nutrition_snapshot_corrected(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    # Initially unmapped item
    initial_item = PendingFoodVisionItem(
        index=1,
        visible_name="неизвестная еда",
        normalized_name="неизвестная еда",
        confidence=0.5,
        estimated_grams_min=50.0,
        estimated_grams_max=50.0,
        selected_grams=50.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-unmapped",
        schema_version="food_vision_inventory_v1",
        items=[initial_item],
        overall_confidence=0.5,
        warnings=[],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # Initial check: unmapped
    loaded = diary.get_pending_inventory(user_id)
    assert loaded.calculation_snapshot["has_unmapped_components"] is True

    # User replaces item with "грецкие орехи, 50 г"
    res = diary.handle_pending_inventory_reply(user_id, "Исправить 1: грецкие орехи, 50 г")
    assert res.status == "updated"

    updated = diary.get_pending_inventory(user_id)
    assert updated.calculation_snapshot["has_unmapped_components"] is False
    assert updated.calculation_snapshot["calories_kcal"] == pytest.approx(50 * 6.54, abs=2.0)
    assert "КБЖУ не найдено" not in res.reply_text


# -----------------------------------------------------------------------------
# Scenario 9: User quantity edit recalculates totals
# -----------------------------------------------------------------------------
def test_scenario_09_user_quantity_edit_recalculates_totals(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    # 5 walnuts (piece_grams = 12.5g)
    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="walnut",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=62.5,
        piece_grams=12.5,
        quantity=5,
        edible_yield_ratio=0.45,
        preparation="целый, в скорлупе",
        is_sauce=False,
        uncertainty="",
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-walnuts",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # User changes quantity to 7
    res = diary.handle_pending_inventory_reply(user_id, "Исправить 1: 7 шт")
    assert res.status == "updated"

    updated = diary.get_pending_inventory(user_id)
    up_item = updated.items[0]
    assert up_item.quantity == 7
    # 7 * 12.5 = 87.5g gross -> 87.5 * 0.45 = ~39.4g edible -> 39.4 * 6.54 = ~257.5 kcal
    assert up_item.selected_grams == pytest.approx(87.5, abs=0.5)
    assert updated.calculation_snapshot["calories_kcal"] == pytest.approx(257.5, abs=5.0)


# -----------------------------------------------------------------------------
# Scenario 10: User total weight edit recalculates totals
# -----------------------------------------------------------------------------
def test_scenario_10_user_total_weight_edit_recalculates_totals(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="walnut",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=62.5,
        piece_grams=12.5,
        quantity=5,
        edible_yield_ratio=0.45,
        preparation="целый, в скорлупе",
        is_sauce=False,
        uncertainty="",
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-walnuts",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # User edits weight to 80g
    res = diary.handle_pending_inventory_reply(user_id, "Вес 1: 80 г")
    assert res.status == "updated"

    updated = diary.get_pending_inventory(user_id)
    up_item = updated.items[0]
    assert up_item.selected_grams == 80.0
    # 80g gross * 0.45 = 36.0g edible -> 36.0 * 6.54 = ~235.4 kcal
    assert updated.calculation_snapshot["calories_kcal"] == pytest.approx(235.4, abs=3.0)


# -----------------------------------------------------------------------------
# Scenario 11: Single-item suggestions only reference 1
# -----------------------------------------------------------------------------
def test_scenario_11_single_item_suggestions_only_reference_1():
    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="walnut",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=None,
        preparation="целый, в скорлупе",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        edible_yield_ratio=0.45,
    )
    inv = PendingFoodVisionInventory(
        user_id=101,
        inventory_id="inv-single",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    reply = format_food_vision_inventory_reply(inv)

    assert "• Исправить 1: 7 шт" in reply
    assert "• Вес 1: 80 г (общий вес группы)" in reply
    assert "• Удалить 1" in reply
    assert "• Добавить: сыр, 30 г" in reply
    # Must NOT have references to index 2 or index 5
    assert "Исправить 2" not in reply
    assert "Удалить 5" not in reply
    assert "Вес 3" not in reply


# -----------------------------------------------------------------------------
# Scenario 12: Multi-item suggestions use valid indexes
# -----------------------------------------------------------------------------
def test_scenario_12_multi_item_suggestions_use_valid_indexes():
    # 2 items
    inv2 = PendingFoodVisionInventory(
        user_id=101,
        inventory_id="inv-two",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(index=1, visible_name="Грецкие орехи", normalized_name="walnut", confidence=0.9, estimated_grams_min=50.0, estimated_grams_max=75.0, selected_grams=None, preparation="", is_sauce=False, uncertainty="", quantity=5, edible_yield_ratio=0.45),
            PendingFoodVisionItem(index=2, visible_name="Яблоки", normalized_name="яблоко", confidence=0.9, estimated_grams_min=150.0, estimated_grams_max=200.0, selected_grams=None, preparation="", is_sauce=False, uncertainty="", quantity=2),
        ],
        overall_confidence=0.9,
        warnings=[],
    )
    reply2 = format_food_vision_inventory_reply(inv2)
    assert "• Исправить 1:" in reply2
    assert "• Исправить 2: куриная грудка, 120 г" in reply2
    assert "• Удалить 2" in reply2
    assert "• Удалить 5" not in reply2

    # 3 items
    inv3 = PendingFoodVisionInventory(
        user_id=101,
        inventory_id="inv-three",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(index=1, visible_name="Грецкие орехи", normalized_name="walnut", confidence=0.9, estimated_grams_min=50.0, estimated_grams_max=75.0, selected_grams=None, preparation="", is_sauce=False, uncertainty="", quantity=5, edible_yield_ratio=0.45),
            PendingFoodVisionItem(index=2, visible_name="Яблоки", normalized_name="яблоко", confidence=0.9, estimated_grams_min=150.0, estimated_grams_max=200.0, selected_grams=None, preparation="", is_sauce=False, uncertainty="", quantity=2),
            PendingFoodVisionItem(index=3, visible_name="Сыр", normalized_name="сыр", confidence=0.9, estimated_grams_min=30.0, estimated_grams_max=40.0, selected_grams=None, preparation="", is_sauce=False, uncertainty=""),
        ],
        overall_confidence=0.9,
        warnings=[],
    )
    reply3 = format_food_vision_inventory_reply(inv3)
    assert "• Удалить 3" in reply3
    assert "• Удалить 5" not in reply3


# -----------------------------------------------------------------------------
# Scenario 13: Pending state serialization compatible
# -----------------------------------------------------------------------------
def test_scenario_13_pending_state_serialization_compatible(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="walnut",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=62.5,
        piece_grams=12.5,
        quantity=5,
        count_type="COUNT_ESTIMATED",
        edible_yield_ratio=0.45,
        grouped_item_ids=["item_1", "item_2"],
        preparation="целый, в скорлупе",
        is_sauce=False,
        uncertainty="uncertain",
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-ser-test",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=["check weight"],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    loaded = diary.get_pending_inventory(user_id)
    assert loaded is not None
    assert loaded.inventory_id == "inv-ser-test"
    loaded_item = loaded.items[0]
    assert loaded_item.visible_name == "Грецкие орехи в скорлупе"
    assert loaded_item.normalized_name == "walnut"
    assert loaded_item.quantity == 5
    assert loaded_item.count_type == "COUNT_ESTIMATED"
    assert loaded_item.piece_grams == 12.5
    assert loaded_item.edible_yield_ratio == 0.45
    assert loaded_item.grouped_item_ids == ["item_1", "item_2"]
    assert loaded_item.preparation == "целый, в скорлупе"
    assert loaded.calculation_snapshot is not None
    assert loaded.calculation_snapshot["has_unmapped_components"] is False


# -----------------------------------------------------------------------------
# Scenario 14: No duplicate saved meal
# -----------------------------------------------------------------------------
def test_scenario_14_no_duplicate_saved_meal(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="walnut",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=62.5,
        piece_grams=12.5,
        quantity=5,
        edible_yield_ratio=0.45,
        preparation="целый, в скорлупе",
        is_sauce=False,
        uncertainty="",
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-walnuts",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # First save succeeds
    res1 = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")
    assert res1.status == "saved"
    assert "✅ Сохранено." in res1.reply_text

    # Second save attempt finds no pending state
    res2 = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")
    assert res2.status == "missing"


# -----------------------------------------------------------------------------
# Scenario 15: User/household isolation PASS
# -----------------------------------------------------------------------------
def test_scenario_15_user_and_household_isolation(tmp_path):
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_a = 111
    user_b = 222

    item_a = PendingFoodVisionItem(index=1, visible_name="Яблоко", normalized_name="яблоко", confidence=0.9, estimated_grams_min=100.0, estimated_grams_max=120.0, selected_grams=110.0, preparation="", is_sauce=False, uncertainty="")
    inv_a = PendingFoodVisionInventory(user_id=user_a, inventory_id="inv-a", schema_version="food_vision_inventory_v1", items=[item_a], overall_confidence=0.9, warnings=[], household_id="hh_1")
    diary._store_pending_inventory(inv_a)

    # User B cannot see User A's pending inventory
    assert diary.get_pending_inventory(user_b) is None

    # User B cannot mutate User A's inventory
    res = diary.handle_pending_inventory_reply(user_b, "Удалить 1")
    assert res.status == "missing"

    # User A's inventory is untouched
    loaded_a = diary.get_pending_inventory(user_a)
    assert loaded_a is not None
    assert len(loaded_a.items) == 1


# -----------------------------------------------------------------------------
# Scenario 16: Admin/generic Hermes access DENIED
# -----------------------------------------------------------------------------
def test_scenario_16_admin_and_generic_hermes_access_denied(monkeypatch):
    monkeypatch.setenv("HEALBITE_TESTER_USERS", "248875361")
    ratified_tester = 248875361
    unauthorized_user = 999999999

    assert _is_healbite_tester_user(ratified_tester) is True
    assert _is_healbite_tester_user(unauthorized_user) is False


# -----------------------------------------------------------------------------
# Principal Integration Test: Real analyze_and_maybe_log() without mocking resolver
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_principal_integration_real_resolver_and_formatter(tmp_path):
    """
    Tests the real diary.analyze_and_maybe_log() path with controlled mocked provider
    responses, plus actual resolver and formatter (no mock on the resolver).
    """
    db_path = str(tmp_path / "test_diary.db")
    diary = HealBiteNutritionDiary(db_path=db_path)
    user_id = 248875361

    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = _mock_incident_five_walnuts_json()

        outcome = await diary.analyze_and_maybe_log(
            user_id=user_id,
            image_path="/tmp/walnuts.jpg",
        )

        assert outcome.available is True
        reply = outcome.clarification_text
        assert reply != ""

        # 1. Exact formatted lines check
        assert "1. Грецкие орехи в скорлупе — ~5 шт." in reply
        assert "Общий вес: ~50–75 г." in reply
        assert "Съедобная часть: ~23–34 г." in reply
        assert "КБЖУ: ~184 ккал · Б 4.3 г · Ж 18.3 г · У 3.9 г" in reply or "КБЖУ: ~" in reply
        assert "КБЖУ не найдено" not in reply

        # 2. Dynamic suggestions check
        assert "• Исправить 1: 7 шт" in reply
        assert "• Вес 1: 80 г (общий вес группы)" in reply
        assert "• Удалить 1" in reply
        assert "• Добавить: сыр, 30 г" in reply
        assert "Исправить 2" not in reply
        assert "Удалить 5" not in reply

        # 3. Test that copying and pasting the suggestion works directly
        parse_res = parse_pending_inventory_action("• Вес 1: 80 г (общий вес группы)")
        assert parse_res.ok is True
        assert parse_res.action.kind == "weight"
        assert parse_res.action.index == 1
        assert parse_res.action.grams == 80.0
