"""
Comprehensive test suite for HEALBITE_P0_MULTIPHOTO_COMPLETENESS_AND_TEXT_PRIORITY_20261023:
Multi-photo completeness (M1 - M5) and Caption-first food logging (T1 - T10).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from gateway.healbite_food_composition import resolve_food_composition
from gateway.healbite_nutrition_diary import (
    ALBUM_DECISION_SAME_PORTION,
    ALBUM_DECISION_DISTINCT_PORTIONS,
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
    _reconcile_multi_photo_inventories,
)
from gateway.nutrition.text_parser import parse_food_text_description, ParsedFoodText


def _make_mock_response(items: list[dict], confidence: float = 0.9):
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": items,
            "overall_confidence": confidence,
            "needs_user_confirmation": False,
            "warnings": [],
        }),
    })


# =============================================================================
# M1: Альбом из 2 разных блюд (Incident A)
# Фото 1: вафли + куриная грудка + помидоры черри
# Фото 2: творожно-маковый рулет
# Ожидается: все позиции сохраняются, рулет не теряется и не склеивается с вафлями
# =============================================================================
@pytest.mark.asyncio
async def test_m1_album_two_different_dishes(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    
    # Photo 1 has chicken, tomatoes, waffles
    photo1_items = [
        {"visible_name": "Куриная грудка", "normalized_name": "куриная грудка", "confidence": 0.92, "estimated_grams_min": 140.0, "estimated_grams_max": 160.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Помидоры черри", "normalized_name": "помидоры черри", "confidence": 0.90, "estimated_grams_min": 50.0, "estimated_grams_max": 70.0, "preparation": "свежий", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Вафли", "normalized_name": "вафли", "confidence": 0.88, "estimated_grams_min": 70.0, "estimated_grams_max": 90.0, "preparation": "выпечка", "is_sauce": False, "uncertainty": ""},
    ]
    # Photo 2 has cottage cheese poppy seed roll
    photo2_items = [
        {"visible_name": "Творожно-маковый рулет", "normalized_name": "рулет", "confidence": 0.93, "estimated_grams_min": 180.0, "estimated_grams_max": 220.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
    ]

    responses = [_make_mock_response(photo1_items), _make_mock_response(photo2_items)]

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(side_effect=responses)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=101,
            image_paths=["/tmp/p1.jpg", "/tmp/p2.jpg"],
            source="photo",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    
    item_names = [it.visible_name.lower() for it in inv.items]
    # Verify all distinct dishes from both photos are preserved
    assert any("курин" in n for n in item_names)
    assert any("помидор" in n or "черри" in n for n in item_names)
    assert any("вафл" in n for n in item_names)
    assert any("рулет" in n for n in item_names)
    assert len(inv.items) >= 4


# =============================================================================
# M2: Альбом из 2 фото одного блюда (2 ракурса яблочного пирога)
# Ожидается: 1 позиция без удвоения массы
# =============================================================================
@pytest.mark.asyncio
async def test_m2_album_two_angles_same_dish(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    
    pie_items_1 = [
        {"visible_name": "Основа из теста", "normalized_name": "pastry base", "confidence": 0.9, "estimated_grams_min": 80.0, "estimated_grams_max": 100.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Ломтики яблока", "normalized_name": "apple slices", "confidence": 0.92, "estimated_grams_min": 60.0, "estimated_grams_max": 80.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
    ]
    pie_items_2 = [
        {"visible_name": "Яблочный пирог", "normalized_name": "яблочный пирог", "confidence": 0.95, "estimated_grams_min": 150.0, "estimated_grams_max": 180.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
    ]

    responses = [_make_mock_response(pie_items_1), _make_mock_response(pie_items_2)]

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(side_effect=responses)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=102,
            image_paths=["/tmp/pie1.jpg", "/tmp/pie2.jpg"],
            source="photo",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    # Deduplicated to a single dish entry
    assert len(inv.items) == 1
    assert "яблок" in inv.items[0].visible_name.lower() or "пирог" in inv.items[0].visible_name.lower()
    # Weight should NOT be doubled to ~350g, should stay around 150-180g
    grams = inv.items[0].selected_grams or inv.items[0].estimated_grams_max
    assert 120.0 <= grams <= 220.0


# =============================================================================
# M3: Альбом: основное блюдо + десерт
# Фото 1: стейк с овощами
# Фото 2: чизкейк
# Ожидается: обе позиции сохраняются
# =============================================================================
@pytest.mark.asyncio
async def test_m3_album_main_dish_and_dessert(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    
    steak_items = [
        {"visible_name": "Стейк говяжий", "normalized_name": "говядина", "confidence": 0.95, "estimated_grams_min": 200.0, "estimated_grams_max": 220.0, "preparation": "жареный", "is_sauce": False, "uncertainty": ""},
    ]
    dessert_items = [
        {"visible_name": "Чизкейк", "normalized_name": "чизкейк", "confidence": 0.92, "estimated_grams_min": 130.0, "estimated_grams_max": 150.0, "preparation": "десерт", "is_sauce": False, "uncertainty": ""},
    ]

    responses = [_make_mock_response(steak_items), _make_mock_response(dessert_items)]

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(side_effect=responses)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=103,
            image_paths=["/tmp/main.jpg", "/tmp/dessert.jpg"],
            source="photo",
        )

    inv = outcome.pending_inventory
    assert inv is not None
    assert len(inv.items) == 2
    names = [it.visible_name.lower() for it in inv.items]
    assert any("говядин" in n or "стейк" in n for n in names)
    assert any("чизкейк" in n for n in names)


# =============================================================================
# M4: Вторая фотография слабее по качеству (ниже confidence)
# Ожидается: вторая фотография не пропадает silently
# =============================================================================
@pytest.mark.asyncio
async def test_m4_second_photo_lower_confidence_not_dropped(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    
    photo1 = [{"visible_name": "Борщ", "normalized_name": "борщ", "confidence": 0.95, "estimated_grams_min": 250.0, "estimated_grams_max": 300.0, "preparation": "суп", "is_sauce": False, "uncertainty": ""}]
    # Photo 2 has lower confidence (0.65)
    photo2 = [{"visible_name": "Сметана", "normalized_name": "сметана", "confidence": 0.65, "estimated_grams_min": 30.0, "estimated_grams_max": 40.0, "preparation": "", "is_sauce": True, "uncertainty": ""}]

    responses = [_make_mock_response(photo1, 0.95), _make_mock_response(photo2, 0.65)]

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(side_effect=responses)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=104,
            image_paths=["/tmp/soup.jpg", "/tmp/sourcream.jpg"],
            source="photo",
        )

    inv = outcome.pending_inventory
    assert inv is not None
    assert len(inv.items) == 2
    names = [it.visible_name.lower() for it in inv.items]
    assert any("борщ" in n for n in names)
    assert any("сметан" in n for n in names)


# =============================================================================
# M5: Dish-first policy on album не сливает разные блюда в одно
# =============================================================================
def test_m5_dish_first_does_not_merge_waffles_and_roll():
    items = [
        FoodVisionItem(visible_name="Вафли", normalized_name="вафли", confidence=0.9, estimated_grams_min=70.0, estimated_grams_max=90.0, preparation="", is_sauce=False, uncertainty="", quantity=1),
        FoodVisionItem(visible_name="Куриное филе", normalized_name="куриное филе", confidence=0.95, estimated_grams_min=140.0, estimated_grams_max=160.0, preparation="запеченный", is_sauce=False, uncertainty="", quantity=1),
        FoodVisionItem(visible_name="Рулет творожный", normalized_name="рулет", confidence=0.92, estimated_grams_min=180.0, estimated_grams_max=220.0, preparation="запеченный", is_sauce=False, uncertainty="", quantity=1),
    ]
    consolidated = apply_dish_first_policy_to_items(items)
    # Must NOT consolidate chicken + waffles + roll into a single dish!
    assert len(consolidated) == 3


# =============================================================================
# T1: Фото + структурированный caption (Incident B text)
# «Это творожно-маковый рулет, состав: армянский лаваш 38 грамм, творог 2% 100 грамм, мак 30 грам, 1/2 яйца»
# =============================================================================
def test_t1_parse_structured_caption():
    text = "Это творожно-маковый рулет, состав: армянский лаваш 38 грамм, творог 2% 100 грамм, мак 30 грам, 1/2 яйца"
    parsed = parse_food_text_description(text)
    assert parsed is not None
    assert parsed.is_structured is True
    assert "рулет" in parsed.dish_title.lower()
    assert len(parsed.ingredients) == 4
    
    # Check grams
    ing_map = {ing.normalized_name: ing for ing in parsed.ingredients}
    assert ing_map["армянский лаваш"].grams == 38.0
    assert ing_map["творог"].grams == 100.0
    assert ing_map["творог"].fat_percent == 2.0
    assert ing_map["мак"].grams == 30.0
    assert ing_map["яйца"].grams == 25.0
    assert parsed.total_grams == 193.0


# =============================================================================
# T2: Расчёт КБЖУ по распарсенному тексту Incident B
# =============================================================================
def test_t2_kbzhu_calculation_for_incident_b_recipe(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    text = "Это творожно-маковый рулет, состав: армянский лаваш 38 грамм, творог 2% 100 грамм, мак 30 грам, 1/2 яйца"
    inv = diary.stage_pending_inventory_from_text(user_id=1, text=text)
    assert inv is not None
    
    meal = calculate_food_vision_meal_nutrition(inv)
    assert meal is not None
    # 38g lavash (~100 kcal) + 100g 2% tvorog (~100 kcal) + 30g poppy (~158 kcal) + 25g egg (~39 kcal) = ~390 - 430 kcal
    assert 360.0 <= meal.total_intervals.calories_kcal.center_value <= 450.0
    assert meal.total_intervals.protein_g.center_value >= 20.0
    assert meal.total_intervals.fat_g.center_value >= 14.0
    assert meal.total_intervals.carbs_g.center_value >= 20.0


# =============================================================================
# T3: Фото + caption, Vision FAILS (Gemini 503 / timeout)
# Ожидается: Ответ строится по тексту, pending inventory создаётся
# =============================================================================
@pytest.mark.asyncio
async def test_t3_photo_caption_vision_fails_fallback_to_text(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    caption = "Это творожно-маковый рулет, состав: армянский лаваш 38 грамм, творог 2% 100 грамм, мак 30 грам, 1/2 яйца"

    # Vision fails with provider error
    fail_resp = json.dumps({"success": False, "_failure_reason": "PROVIDER_ERROR"})

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(return_value=fail_resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=105,
            image_path="/tmp/roll.jpg",
            user_text=caption,
            source="photo",
        )

    # Must NOT fail closed or show unavailable!
    assert outcome.available is True
    assert outcome.pending is True
    assert outcome.validation_status == "TEXT_FALLBACK_STAGED"
    inv = outcome.pending_inventory
    assert inv is not None
    assert len(inv.items) == 4
    assert round(sum(it.selected_grams for it in inv.items), 1) == 193.0


# =============================================================================
# T4: Caption with fractions: 1/2 яйца, 3/4 стакана, 1.5 порции
# =============================================================================
def test_t4_fractions_parsing():
    p1 = parse_food_text_description("1/2 яйца")
    assert p1 is not None and len(p1.ingredients) == 1
    assert p1.ingredients[0].grams == 25.0
    assert p1.ingredients[0].quantity == 0.5

    p2 = parse_food_text_description("2 яйца")
    assert p2 is not None and len(p2.ingredients) == 1
    assert p2.ingredients[0].grams == 100.0


# =============================================================================
# T5: Caption with % fat: творог 2%, молоко 3.2%
# =============================================================================
def test_t5_fat_percentage_parsing():
    p = parse_food_text_description("Творог 2% 150г, молоко 3.2% 200мл")
    assert p is not None and len(p.ingredients) == 2
    assert p.ingredients[0].fat_percent == 2.0
    assert p.ingredients[0].grams == 150.0
    assert p.ingredients[1].grams == 200.0


# =============================================================================
# T6: Text-only meal entry (без фото)
# =============================================================================
def test_t6_text_only_meal_staging(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    text = "Овсянка 50 грамм, банан 120 грамм, арахисовая паста 20 грамм"
    
    pending_inv = diary.stage_pending_inventory_from_text(user_id=106, text=text)
    assert pending_inv is not None
    assert len(pending_inv.items) == 3
    assert pending_inv.source == "text"
    assert pending_inv.photo_count == 0


# =============================================================================
# T7: Caption and photo disagree -> Просьба уточнить, не silent fail
# =============================================================================
@pytest.mark.asyncio
async def test_t7_caption_and_photo_disagree(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    
    # Vision saw soup
    soup_items = [{"visible_name": "Борщ", "normalized_name": "борщ", "confidence": 0.95, "estimated_grams_min": 300.0, "estimated_grams_max": 350.0, "preparation": "суп", "is_sauce": False, "uncertainty": ""}]
    resp = _make_mock_response(soup_items)
    
    # Caption says chocolate cake
    caption = "Это шоколадный торт с вишней"

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=107,
            image_path="/tmp/food.jpg",
            user_text=caption,
            source="photo",
        )

    assert outcome.available is True
    assert outcome.validation_status == "DISAGREEMENT_CLARIFICATION"
    assert outcome.clarification_text is not None
    assert "Борщ" in outcome.clarification_text
    assert "торт" in outcome.clarification_text.lower()


# =============================================================================
# T8: Save parsed text-based meal atomically
# =============================================================================
def test_t8_save_parsed_text_based_meal(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    text = "Гречка отварная 150г, куриная грудка 120г"
    
    pending_inv = diary.stage_pending_inventory_from_text(user_id=108, text=text)
    assert pending_inv is not None
    
    # Confirm and save
    reply_result = diary.confirm_pending_inventory(user_id=108)
    assert reply_result is not None
    assert reply_result.status == "awaiting_save_confirmation"
    assert reply_result.record is not None
    
    save_result = diary.confirm_pending_meal(user_id=108)
    assert save_result.status == "saved"
    assert save_result.sqlite_id is not None
    assert save_result.record.calories_kcal > 0
    
    # State cleared
    assert diary.get_pending_inventory(108) is None


# =============================================================================
# T9: Combined multimodal flow: Text is primary when structured
# =============================================================================
@pytest.mark.asyncio
async def test_t9_combined_multimodal_text_is_primary(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    
    # Vision returned vague "Сладкая выпечка ~150г"
    vague_vision = [{"visible_name": "Сладкая выпечка", "normalized_name": "выпечка", "confidence": 0.85, "estimated_grams_min": 140.0, "estimated_grams_max": 160.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""}]
    resp = _make_mock_response(vague_vision)
    
    # User gave exact recipe
    caption = "Это творожно-маковый рулет, состав: армянский лаваш 38 грамм, творог 2% 100 грамм, мак 30 грам, 1/2 яйца"

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=109,
            image_path="/tmp/roll.jpg",
            user_text=caption,
            source="photo",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    # User's 4 explicit ingredients are preserved as ground truth
    assert len(inv.items) == 4
    assert round(sum(it.selected_grams for it in inv.items), 1) == 193.0


# =============================================================================
# T10: Regression on previous apple pie dish-first fix
# =============================================================================
@pytest.mark.asyncio
async def test_t10_regression_apple_pie_dish_first(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    
    decomposed_pie = [
        {"visible_name": "Основа из теста", "normalized_name": "pastry base", "confidence": 0.9, "estimated_grams_min": 70.0, "estimated_grams_max": 90.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Ломтики яблока", "normalized_name": "apple slices", "confidence": 0.95, "estimated_grams_min": 50.0, "estimated_grams_max": 70.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
        {"visible_name": "Кремовая начинка", "normalized_name": "creamy filling", "confidence": 0.8, "estimated_grams_min": 20.0, "estimated_grams_max": 40.0, "preparation": "запеченный", "is_sauce": False, "uncertainty": ""},
    ]
    resp = _make_mock_response(decomposed_pie)

    with patch("tools.vision_tools.vision_analyze_tool", AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=110,
            image_path="/tmp/pie.jpg",
            source="photo",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    assert len(inv.items) == 1
    assert "яблок" in inv.items[0].visible_name.lower()
    
    # Verify save button is standard "✅ Сохранить"
    keyboard = build_food_vision_inventory_keyboard(inv)
    assert any("✅ Сохранить" == btn["text"] for row in keyboard for btn in row)
