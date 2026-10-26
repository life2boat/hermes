"""
Mandatory acceptance tests for HEALBITE_R1_07_SINGLE_PHOTO_RECOGNITION_CLOSEOUT:
Ten mandatory checks for single photo food recognition:
1. Single well-distinguished product (e.g. apple or banana).
2. Single simple prepared dish (e.g. portion of rice or omelette).
3. Single portion without specified weight (default mass estimation via HIPE/priors).
4. Several distinguishable components on one plate in a single photo.
5. Product present in base food table (correctness of KBZHU calculation).
6. Low-confidence recognition (clarification / candidate offer with modification option).
7. Photo without food (polite rejection without crash or false recognition).
8. Vision error or timeout (graceful degradation without hang or message loss).
9. Correct user-facing response formulation (name, approx mass, kcal, PFC, approximation disclaimer).
10. Multi-user isolation (photo from user A does not affect user B).
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import pytest

from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    build_food_vision_inventory_keyboard,
    format_food_vision_inventory_reply,
)
from gateway.nutrition.contracts import MassBounds
from gateway.nutrition.engine import resolve_and_calculate_component


def _make_mock_vision_response(items: list[dict], overall_confidence: float = 0.90, warnings: list[str] | None = None, is_food: bool = True):
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": is_food,
            "items": items if is_food else [],
            "overall_confidence": overall_confidence if is_food else 0.0,
            "needs_user_confirmation": False,
            "warnings": warnings or [],
        }),
    })


# -----------------------------------------------------------------------------
# Check 1: Одиночный хорошо различимый продукт (например, яблоко)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_1_single_distinct_product(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp = _make_mock_vision_response([
        {
            "visible_name": "Яблоко",
            "normalized_name": "яблоко",
            "confidence": 0.95,
            "estimated_grams_min": 130.0,
            "estimated_grams_max": 170.0,
            "preparation": "свежий",
            "is_sauce": False,
            "uncertainty": "",
        }
    ])

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=201,
            image_paths=["/tmp/apple.jpg"],
            user_text="",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    assert len(inv.items) == 1
    assert "яблоко" in inv.items[0].visible_name.lower()
    # Not saved directly to diary without confirmation
    assert diary.get_daily_summary(user_id=201)["entry_count"] == 0
    # Pending inventory exists
    assert diary.get_pending_inventory(201) is not None
    # Reply contains name, mass, kcal, macros
    reply = outcome.clarification_text
    assert "Яблоко" in reply
    assert "ккал" in reply
    assert "Б " in reply and "Ж " in reply and "У " in reply


# -----------------------------------------------------------------------------
# Check 2: Одиночное простое готовое блюдо (например, омлет)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_2_single_simple_dish(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp = _make_mock_vision_response([
        {
            "visible_name": "Омлет",
            "normalized_name": "омлет",
            "confidence": 0.92,
            "estimated_grams_min": 130.0,
            "estimated_grams_max": 170.0,
            "preparation": "жареный",
            "is_sauce": False,
            "uncertainty": "",
        }
    ])

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=202,
            image_paths=["/tmp/omelet.jpg"],
            user_text="",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    assert len(inv.items) == 1
    assert "омлет" in inv.items[0].visible_name.lower()
    assert inv.calculation_snapshot is not None
    assert inv.calculation_snapshot["calories_kcal"] > 150.0
    assert diary.get_daily_summary(user_id=202)["entry_count"] == 0


# -----------------------------------------------------------------------------
# Check 3: Одиночная порция без указания веса (дефолтная оценка массы via HIPE)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_3_single_portion_without_specified_weight(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp = _make_mock_vision_response([
        {
            "visible_name": "Омлет",
            "normalized_name": "омлет",
            "confidence": 0.90,
            "estimated_grams_min": None,
            "estimated_grams_max": None,
            "preparation": "жареный",
            "is_sauce": False,
            "uncertainty": "",
        }
    ])

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=203,
            image_paths=["/tmp/omelet_no_weight.jpg"],
            user_text="",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    # Calculation snapshot must have estimated weight from HIPE priors
    assert inv.calculation_snapshot is not None
    assert inv.calculation_snapshot["calories_kcal"] > 0
    # Response contains approximate weight and disclaimer
    reply = outcome.clarification_text
    assert "Омлет" in reply
    assert "ккал" in reply
    assert "приблизительно" in reply.lower() or "ориентировочный" in reply.lower()


# -----------------------------------------------------------------------------
# Check 4: Несколько различимых компонентов на одной тарелке
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_4_multi_component_plate_single_photo(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp = _make_mock_vision_response([
        {
            "visible_name": "Куриная грудка",
            "normalized_name": "куриная грудка",
            "confidence": 0.95,
            "estimated_grams_min": 100.0,
            "estimated_grams_max": 140.0,
            "preparation": "запеченный",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "Гречневая каша",
            "normalized_name": "гречка вареная",
            "confidence": 0.95,
            "estimated_grams_min": 130.0,
            "estimated_grams_max": 170.0,
            "preparation": "вареный",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "Свежий огурец",
            "normalized_name": "огурец",
            "confidence": 0.90,
            "estimated_grams_min": 60.0,
            "estimated_grams_max": 100.0,
            "preparation": "свежий",
            "is_sauce": False,
            "uncertainty": "",
        },
    ])

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=204,
            image_paths=["/tmp/mixed_plate.jpg"],
            user_text="",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert len(inv.items) == 3
    reply = outcome.clarification_text
    assert "Куриная грудка" in reply
    assert "Гречневая каша" in reply
    assert "Свежий огурец" in reply
    assert "Итого:" in reply
    assert diary.get_daily_summary(user_id=204)["entry_count"] == 0


# -----------------------------------------------------------------------------
# Check 5: Продукт, присутствующий в базовой пищевой таблице (проверка КБЖУ)
# -----------------------------------------------------------------------------
def test_scenario_5_food_database_kbzhu_correctness():
    # Apple 150g
    bounds = MassBounds(150.0, 150.0, 150.0, False, True)
    comp = resolve_and_calculate_component("яблоко", bounds)
    assert comp.nutrition_available is True
    # 52 kcal / 100g -> ~78 kcal for 150g
    cal = comp.intervals.calories_kcal.center_value
    assert 70.0 <= cal <= 85.0
    prot = comp.intervals.protein_g.center_value
    assert 0.2 <= prot <= 1.0
    carbs = comp.intervals.carbs_g.center_value
    assert 18.0 <= carbs <= 24.0


# -----------------------------------------------------------------------------
# Check 6: Неуверенное распознавание (переспрос / предложение варианта)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_6_low_confidence_clarification(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp = _make_mock_vision_response(
        items=[
            {
                "visible_name": "Слоёная выпечка",
                "normalized_name": "выпечка",
                "confidence": 0.65,
                "estimated_grams_min": 70.0,
                "estimated_grams_max": 110.0,
                "preparation": "запеченный",
                "is_sauce": False,
                "uncertainty": "Точный состав начинки не виден",
            }
        ],
        overall_confidence=0.68,
    )

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=206,
            image_paths=["/tmp/uncertain_pastry.jpg"],
            user_text="",
        )

    assert outcome.available is True
    inv = outcome.pending_inventory
    assert inv is not None
    # Clarification / uncertainty notes presented
    reply = outcome.clarification_text
    assert "Точный состав начинки не виден" in reply or "не удалось уверенно" in reply.lower()
    # Offers proposed option with action options
    assert "Можно сохранить кнопкой ниже или отправить исправление:" in reply
    keyboard = build_food_vision_inventory_keyboard(inv)
    flat_buttons = [btn["text"] for row in keyboard for btn in row]
    assert "✅ Сохранить" in flat_buttons
    assert "✏️ Исправить" in flat_buttons
    assert "❌ Отмена" in flat_buttons


# -----------------------------------------------------------------------------
# Check 7: Фото без еды (вежливое отклонение без падения и без ложного распознавания)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_7_non_food_polite_rejection(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp = _make_mock_vision_response(items=[], is_food=False)

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=207,
            image_paths=["/tmp/office_chair.jpg"],
            user_text="",
        )

    assert outcome.validation_status == "NON_FOOD"
    assert outcome.available is False
    assert diary.get_pending_inventory(207) is None
    assert diary.get_daily_summary(user_id=207)["entry_count"] == 0
    # Polite text in clarification_text
    assert outcome.clarification_text is not None
    assert "нет еды" in outcome.clarification_text.lower()


# -----------------------------------------------------------------------------
# Check 8: Ошибка или таймаут Vision (грациозная деградация)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_8_vision_timeout_and_error_graceful_degradation(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)

    # 8a: Timeout
    with patch("tools.vision_tools.vision_analyze_tool", side_effect=asyncio.TimeoutError()):
        outcome_timeout = await diary.analyze_and_maybe_log(
            user_id=208,
            image_paths=["/tmp/timeout.jpg"],
            user_text="",
        )
    assert outcome_timeout.available is False
    assert outcome_timeout.validation_status == "PROVIDER_TIMEOUT"
    assert diary.get_pending_inventory(208) is None

    # 8b: Provider error
    error_resp = json.dumps({"success": False, "error": "Provider 500 internal error", "analysis": ""})
    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=error_resp)):
        outcome_err = await diary.analyze_and_maybe_log(
            user_id=208,
            image_paths=["/tmp/error.jpg"],
            user_text="",
        )
    assert outcome_err.available is False
    assert outcome_err.validation_status == "PROVIDER_ERROR"
    assert diary.get_pending_inventory(208) is None


# -----------------------------------------------------------------------------
# Check 9: Корректность формулировки ответа пользователю
# (название, ориентировочный вес, калории, БЖУ, дисклеймер о приблизительности)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_9_response_formatting_completeness(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp = _make_mock_vision_response([
        {
            "visible_name": "Банан",
            "normalized_name": "банан",
            "confidence": 0.95,
            "estimated_grams_min": 110.0,
            "estimated_grams_max": 150.0,
            "preparation": "свежий",
            "is_sauce": False,
            "uncertainty": "",
        }
    ])

    with patch("tools.vision_tools.vision_analyze_tool", new=AsyncMock(return_value=resp)):
        outcome = await diary.analyze_and_maybe_log(
            user_id=209,
            image_paths=["/tmp/banana.jpg"],
            user_text="",
        )

    reply = outcome.clarification_text
    # 1. Dish name
    assert "Банан" in reply
    # 2. Approximate mass
    assert ("г" in reply) and ("~" in reply or "примерно" in reply)
    # 3. Calories
    assert "ккал" in reply
    # 4. Protein, Fat, Carbs
    assert "Б " in reply
    assert "Ж " in reply
    assert "У " in reply
    # 5. Approximation disclaimer
    assert "приблизительно" in reply.lower() or "ориентировочный" in reply.lower()


# -----------------------------------------------------------------------------
# Check 10: Изоляция нескольких пользователей
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_10_multi_user_isolation(tmp_path: Path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db", background_write=False)
    resp_a = _make_mock_vision_response([
        {"visible_name": "Яблоко", "normalized_name": "яблоко", "confidence": 0.95, "estimated_grams_min": 140.0, "estimated_grams_max": 160.0, "preparation": "", "is_sauce": False, "uncertainty": ""}
    ])
    resp_b = _make_mock_vision_response([
        {"visible_name": "Банан", "normalized_name": "банан", "confidence": 0.95, "estimated_grams_min": 110.0, "estimated_grams_max": 130.0, "preparation": "", "is_sauce": False, "uncertainty": ""}
    ])

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=[resp_a, resp_b]):
        outcome_a = await diary.analyze_and_maybe_log(
            user_id=1001,
            image_paths=["/tmp/apple_user_a.jpg"],
            user_text="",
        )
        outcome_b = await diary.analyze_and_maybe_log(
            user_id=1002,
            image_paths=["/tmp/banana_user_b.jpg"],
            user_text="",
        )

    inv_a = diary.get_pending_inventory(1001)
    inv_b = diary.get_pending_inventory(1002)

    assert inv_a is not None and inv_a.items[0].visible_name == "Яблоко"
    assert inv_b is not None and inv_b.items[0].visible_name == "Банан"

    # User A confirming does not affect User B
    save_res = diary.save_pending_inventory_directly(user_id=1001, expected_inventory_id=inv_a.inventory_id)
    assert save_res is not None

    # User A has 1 entry, User B has 0 entries
    assert diary.get_daily_summary(user_id=1001)["entry_count"] == 1
    assert diary.get_daily_summary(user_id=1002)["entry_count"] == 0

    # User B's pending inventory remains intact
    assert diary.get_pending_inventory(1002) is not None
    assert diary.get_pending_inventory(1002).items[0].visible_name == "Банан"
