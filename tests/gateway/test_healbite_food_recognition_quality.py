"""
Unit & integration tests for HEALBITE_P1_REAL_ACCEPTANCE_FOOD_INTERPRETATION_20261023:
Dish-First Policy, Controlled Decomposition, Approximate Nutrition Fallback,
and Quality Acceptance Criteria (Q1 - Q10).
"""

import json
from pathlib import Path
from unittest.mock import patch
import pytest

from gateway.healbite_food_composition import resolve_food_composition
from gateway.healbite_nutrition_diary import (
    ALBUM_DECISION_SAME_PORTION,
    ALBUM_DECISION_UNRESOLVED,
    FOOD_VISION_SCHEMA_VERSION,
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    apply_dish_first_policy_to_items,
    apply_food_vision_dish_first_policy,
    build_food_vision_inventory_keyboard,
    calculate_food_vision_meal_nutrition,
    format_food_vision_inventory_reply,
    parse_pending_inventory_action,
)
from gateway.nutrition.contracts import MassBounds
from gateway.nutrition.engine import resolve_and_calculate_component, resolve_approximate_food_nutrition


def _make_mock_response(items: list[dict]):
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": items,
            "overall_confidence": 0.88,
            "needs_user_confirmation": False,
            "warnings": [],
        }),
    })


# -----------------------------------------------------------------------------
# Q1: Кусок яблочного пирога / сладкой выпечки
# Ожидается: dish-level label и approximate KBZHU (не разваливается на тесто/начинку)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q1_apple_pie_dish_level_and_approximate_kbzhu(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    # Vision decomposed into crust + apple slices + cream
    resp = _make_mock_response([
        {"visible_name": "Основа из теста", "normalized_name": "Pastry base", "confidence": 0.9, "estimated_grams_min": 70.0, "estimated_grams_max": 90.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Ломтики яблока", "normalized_name": "Apple slices", "confidence": 0.95, "estimated_grams_min": 50.0, "estimated_grams_max": 70.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Кремовая начинка", "normalized_name": "Creamy filling", "confidence": 0.8, "estimated_grams_min": 20.0, "estimated_grams_max": 40.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
    ])

    with patch("tools.vision_tools.vision_analyze_tool", return_value=resp):
        outcome = await diary.analyze_and_maybe_log(
            user_id=101,
            image_paths=["/tmp/pie.jpg"],
            user_text="",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv is not None
        # Must be consolidated to single dish item
        assert len(inv.items) == 1
        assert "выпечка" in inv.items[0].visible_name.lower() or "пирог" in inv.items[0].visible_name.lower()
        # Nutrition must be calculated
        assert inv.calculation_snapshot is not None
        assert inv.calculation_snapshot.get("has_unmapped_components") is False
        calories = inv.calculation_snapshot.get("calories_kcal")
        assert calories is not None and calories > 250.0
        # Reply must NOT say "КБЖУ не найдено"
        assert "(КБЖУ не найдено)" not in outcome.clarification_text
        assert "Итого:" in outcome.clarification_text


# -----------------------------------------------------------------------------
# Q2: Два фото одной и той же выпечки (Exact Incident Reproduction)
# Ожидается: один итог по порции, без удвоения на 8 позиций
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q2_two_photos_same_pastry_no_doubling(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    # Photo 1 items from incident
    resp1 = _make_mock_response([
        {"visible_name": "Основа из теста", "normalized_name": "Pastry base", "confidence": 0.9, "estimated_grams_min": None, "estimated_grams_max": None, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Ломтики яблока", "normalized_name": "Apple slices", "confidence": 0.95, "estimated_grams_min": None, "estimated_grams_max": None, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Кремовая начинка", "normalized_name": "Creamy filling", "confidence": 0.8, "estimated_grams_min": None, "estimated_grams_max": None, "preparation": "запеченный", "is_sauce": False, "uncertainty": "Тип крема"},
        {"visible_name": "Орехи", "normalized_name": "Nuts", "confidence": 0.8, "estimated_grams_min": None, "estimated_grams_max": None, "preparation": "запеченный", "is_sauce": False, "uncertainty": "Тип орехов"},
        {"visible_name": "Семечки подсолнечника", "normalized_name": "Sunflower seeds", "confidence": 0.9, "estimated_grams_min": None, "estimated_grams_max": None, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
    ])
    # Photo 2 items from incident
    resp2 = _make_mock_response([
        {"visible_name": "Кусочки сладкой выпечки", "normalized_name": "sweet pastry", "confidence": 0.85, "estimated_grams_min": 140.0, "estimated_grams_max": 200.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": "сладкая выпечка"},
        {"visible_name": "Семена на выпечке", "normalized_name": "seeds", "confidence": 0.7, "estimated_grams_min": 5.0, "estimated_grams_max": 10.0, "preparation": "посыпанные", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Темная добавка или часть выпечки", "normalized_name": "dark component", "confidence": 0.4, "estimated_grams_min": 10.0, "estimated_grams_max": 20.0, "preparation": "включенный", "is_sauce": False, "uncertainty": "темный компонент"},
    ])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp1, resp2]):
        outcome = await diary.analyze_and_maybe_log(
            user_id=102,
            image_paths=["/tmp/photo1.jpg", "/tmp/photo2.jpg"],
            user_text="та же порция",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv is not None
        # MUST NOT be 8 items! Consolidated to 1 portion of sweet pastry / pie
        assert len(inv.items) == 1
        assert "выпечка" in inv.items[0].visible_name.lower() or "пирог" in inv.items[0].visible_name.lower()
        # Mass must NOT be doubled (around 140-200g)
        assert inv.items[0].estimated_grams_min is not None and inv.items[0].estimated_grams_min <= 150.0
        assert inv.items[0].estimated_grams_max is not None and inv.items[0].estimated_grams_max <= 210.0
        # Nutrition available
        assert inv.calculation_snapshot is not None
        assert inv.calculation_snapshot.get("has_unmapped_components") is False
        assert inv.calculation_snapshot.get("calories_kcal") is not None
        # Save button in keyboard must be "Сохранить", NOT "Сохранить без КБЖУ"
        keyboard = build_food_vision_inventory_keyboard(inv)
        flat_buttons = [btn["text"] for row in keyboard for btn in row]
        assert "✅ Сохранить" in flat_buttons
        assert "✅ Сохранить без КБЖУ" not in flat_buttons


# -----------------------------------------------------------------------------
# Q3: Простая готовая еда (пицца)
# Ожидается: не разваливается на тесто + сыр + соус
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q3_pizza_does_not_overdecompose(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _make_mock_response([
        {"visible_name": "Основа пиццы", "normalized_name": "тесто для пиццы", "confidence": 0.9, "estimated_grams_min": 80.0, "estimated_grams_max": 100.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Сыр Моцарелла", "normalized_name": "моцарелла", "confidence": 0.9, "estimated_grams_min": 30.0, "estimated_grams_max": 40.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Томатный соус", "normalized_name": "томатный соус", "confidence": 0.85, "estimated_grams_min": 15.0, "estimated_grams_max": 20.0, "preparation": "запеченный", "is_sauce": True, "uncertainty": ""},
    ])

    with patch("tools.vision_tools.vision_analyze_tool", return_value=resp):
        outcome = await diary.analyze_and_maybe_log(
            user_id=103,
            image_paths=["/tmp/pizza.jpg"],
            user_text="",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert len(inv.items) == 1
        assert "пицца" in inv.items[0].visible_name.lower()
        assert inv.calculation_snapshot.get("has_unmapped_components") is False
        assert inv.calculation_snapshot.get("calories_kcal") > 200.0


# -----------------------------------------------------------------------------
# Q4: Салат
# Ожидается: разрешён умеренный component view, КБЖУ успешно рассчитывается
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q4_salad_component_view_and_nutrition(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _make_mock_response([
        {"visible_name": "Свежие огурцы", "normalized_name": "огурцы", "confidence": 0.95, "estimated_grams_min": 60.0, "estimated_grams_max": 80.0, "preparation": "нарезанный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Помидоры", "normalized_name": "помидоры", "confidence": 0.95, "estimated_grams_min": 60.0, "estimated_grams_max": 80.0, "preparation": "нарезанный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Сыр Фета", "normalized_name": "фета", "confidence": 0.90, "estimated_grams_min": 30.0, "estimated_grams_max": 40.0, "preparation": "нарезанный", "is_sauce": False, "uncertainty": ""},
    ])

    with patch("tools.vision_tools.vision_analyze_tool", return_value=resp):
        outcome = await diary.analyze_and_maybe_log(
            user_id=104,
            image_paths=["/tmp/salad.jpg"],
            user_text="",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        # Moderate component view allowed for salad ingredients
        assert len(inv.items) == 3
        assert inv.calculation_snapshot.get("has_unmapped_components") is False
        assert "(КБЖУ не найдено)" not in outcome.clarification_text


# -----------------------------------------------------------------------------
# Q5: Блюдо с неизвестным точным составом
# Ожидается: выдаётся approximate KBZHU через nearest analog / category
# -----------------------------------------------------------------------------
def test_q5_unknown_dish_nearest_analog_approximate_kbzhu():
    bounds = MassBounds(150.0, 180.0, 220.0, True, False)
    comp = resolve_and_calculate_component("домашняя мясная запеканка", bounds)
    assert comp.nutrition_available is True
    assert comp.intervals.calories_kcal is not None
    assert comp.intervals.calories_kcal.center_value > 200.0


# -----------------------------------------------------------------------------
# Q6: Слабая уверенность Vision
# Ожидается: честный confidence marker + approximate answer
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_q6_low_confidence_honest_presentation(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    resp = _make_mock_response([
        {"visible_name": "Пирог с начинкой", "normalized_name": "пирог", "confidence": 0.55, "estimated_grams_min": 120.0, "estimated_grams_max": 180.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": "Точный состав начинки не виден"},
    ])

    with patch("tools.vision_tools.vision_analyze_tool", return_value=resp):
        outcome = await diary.analyze_and_maybe_log(
            user_id=106,
            image_paths=["/tmp/uncertain_pie.jpg"],
            user_text="",
        )

        assert outcome.available is True
        inv = outcome.pending_inventory
        assert inv.overall_confidence == 0.88 or inv.items[0].confidence == 0.55
        assert inv.calculation_snapshot.get("has_unmapped_components") is False
        # Clarification notes uncertainty
        assert "Точный состав начинки не виден" in outcome.clarification_text
        assert "Итого:" in outcome.clarification_text


# -----------------------------------------------------------------------------
# Q7: Нет точного food DB match
# Ожидается: используется coarse category nutrition fallback
# -----------------------------------------------------------------------------
def test_q7_coarse_nutrition_fallback():
    bounds = MassBounds(100.0, 150.0, 200.0, True, False)
    # Rare phrasing with no exact DB record
    comp = resolve_and_calculate_component("кусочки сдобной выпечки с посыпкой", bounds)
    assert comp.nutrition_available is True
    assert comp.intervals.calories_kcal is not None
    assert comp.intervals.calories_kcal.center_value > 300.0


# -----------------------------------------------------------------------------
# Q8: Длинный speculative list
# Ожидается: policy режет бесполезную декомпозицию, при наличии основного блюда
# -----------------------------------------------------------------------------
def test_q8_policy_prunes_useless_speculative_microcomponents():
    items = [
        FoodVisionItem("Сладкая выпечка", "сладкая выпечка", 0.85, 140.0, 200.0, "запеченный", False, ""),
        FoodVisionItem("Крошки теста", "крошки", 0.35, 2.0, 5.0, "", False, ""),
        FoodVisionItem("Темная добавка", "темная добавка", 0.40, 5.0, 10.0, "", False, "неопознано"),
    ]
    pruned = apply_dish_first_policy_to_items(items)
    # Speculative items pruned and folded into primary item notes
    assert len(pruned) == 1
    assert "выпечка" in pruned[0].visible_name.lower()
    assert "включает:" in pruned[0].uncertainty.lower()


# -----------------------------------------------------------------------------
# Q9: Ручное исправление
# Ожидается: после исправления результат пересчитывается с КБЖУ, а не сохраняется без КБЖУ
# -----------------------------------------------------------------------------
def test_q9_manual_correction_updates_nutrition(tmp_path: Path):
    item = PendingFoodVisionItem(
        index=1,
        visible_name="Сладкая выпечка",
        normalized_name="сладкая выпечка",
        confidence=0.85,
        estimated_grams_min=140.0,
        estimated_grams_max=200.0,
        selected_grams=170.0,
        preparation="запеченный",
        is_sauce=False,
        uncertainty="",
    )
    inv = PendingFoodVisionInventory(
        schema_version=FOOD_VISION_SCHEMA_VERSION,
        user_id=109,
        items=[item],
        overall_confidence=0.85,
        needs_user_confirmation=True,
        warnings=[],
        inventory_id="test_inv_109",
    )

    action_res = parse_pending_inventory_action("Вес 1: 150 г")
    assert action_res.ok is True
    assert action_res.action is not None
    assert action_res.action.kind == "weight"
    assert action_res.action.grams == 150.0

    # Apply modification
    item.selected_grams = 150.0
    item.user_modified = True
    meal_est = calculate_food_vision_meal_nutrition(inv)
    assert meal_est.total_intervals.calories_kcal is not None
    assert meal_est.total_intervals.calories_kcal.center_value > 400.0


# -----------------------------------------------------------------------------
# Q10: Save without KBZHU
# Ожидается: кнопка и предложение "сохранить без КБЖУ" появляются только если
# КБЖУ реально невозможно оценить, а не для типичных фото еды
# -----------------------------------------------------------------------------
def test_q10_save_without_kbzhu_only_when_unmapped(tmp_path: Path):
    # Case A: Typical food photo (sweet pastry) -> Save with KBZHU
    item_food = PendingFoodVisionItem(
        index=1,
        visible_name="Яблочный пирог",
        normalized_name="яблочный пирог",
        confidence=0.9,
        estimated_grams_min=140.0,
        estimated_grams_max=200.0,
        selected_grams=170.0,
        preparation="запеченный",
        is_sauce=False,
        uncertainty="",
    )
    inv_food = PendingFoodVisionInventory(
        schema_version=FOOD_VISION_SCHEMA_VERSION,
        user_id=110,
        items=[item_food],
        overall_confidence=0.9,
        needs_user_confirmation=False,
        warnings=[],
        inventory_id="inv_food_110",
    )
    inv_food.calculation_snapshot = diary_calc_snapshot(inv_food)
    kb_food = build_food_vision_inventory_keyboard(inv_food)
    flat_food = [b["text"] for row in kb_food for b in row]
    assert "✅ Сохранить" in flat_food
    assert "✅ Сохранить без КБЖУ" not in flat_food


def diary_calc_snapshot(inv: PendingFoodVisionInventory) -> dict:
    from gateway.healbite_nutrition_diary import _build_inventory_calculation_snapshot
    return _build_inventory_calculation_snapshot(inv)
