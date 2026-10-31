from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    NutritionRecord,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    FoodVisionItem,
    compute_nutrition_diary_summary,
    format_food_vision_inventory_reply,
    format_nutrition_diary_report,
    format_nutrition_stats_report,
    normalize_nutrition_payload,
)
from gateway.nutrition.contracts import MassBounds
from gateway.nutrition.engine import resolve_and_calculate_component
from gateway.nutrition.household_measures import (
    HOUSEHOLD_DISCLAIMER,
    normalize_food_inflection,
)
from gateway.nutrition.text_parser import parse_food_text_description
from gateway.platforms.telegram import (
    HEALBITE_REPLY_KEYBOARD_ACTIONS,
    _HEALBITE_NORMALIZED_COMMAND_MAP,
    TelegramAdapter,
)
from gateway.run import GatewayRunner


def _make_record(
    meal_name: str,
    calories_kcal: float | None = None,
    protein_g: float | None = None,
    fat_g: float | None = None,
    carbs_g: float | None = None,
    items: list[dict[str, Any]] | None = None,
    is_approx: bool = True,
) -> NutritionRecord:
    totals: dict[str, Any] = {}
    if calories_kcal is not None:
        totals["calories_kcal"] = calories_kcal
    if protein_g is not None:
        totals["protein_g"] = protein_g
    if fat_g is not None:
        totals["fat_g"] = fat_g
    if carbs_g is not None:
        totals["carbs_g"] = carbs_g

    raw_json = json.dumps(
        {
            "is_food": True,
            "meal_name": meal_name,
            "raw_summary": meal_name,
            "confidence": 0.95,
            "totals": totals,
            "items": items or [],
            "is_approximate_range": is_approx,
        },
        ensure_ascii=False,
    )
    return normalize_nutrition_payload(raw_json)


# ══════════════════════════════════════════════════════════════════════════════
# 1. DEFECT-01: Russian food name normalization
# ══════════════════════════════════════════════════════════════════════════════

class TestDefect01RussianFoodNormalization:
    def test_wheat_bread_inflection_and_profile(self):
        """100 г пшеничного хлеба -> wheat bread profile, preserved weight 100g."""
        text = "100 г пшеничного хлеба"
        result = parse_food_text_description(text)
        assert result is not None
        assert len(result.ingredients) == 1
        ing = result.ingredients[0]
        assert ing.grams == 100.0
        norm_name = normalize_food_inflection(ing.name)
        assert "пшеничный хлеб" in norm_name.lower() or "хлеб" in norm_name.lower()

        bounds = MassBounds(grams_min=ing.grams, grams_center=ing.grams, grams_max=ing.grams, is_approximate_plausible_range=False, is_user_confirmed=True)
        comp = resolve_and_calculate_component(ing.name, bounds)
        assert comp is not None
        assert comp.nutrition_available is True
        assert "хлеб" in comp.name.lower()
        assert comp.mass_bounds.grams_center == 100.0

    def test_boiled_eggs_with_parens_weight(self):
        """2 варёных яйца (100 г) -> 2 eggs, 100g, boiled prep, no empty () in name."""
        text = "2 варёных яйца (100 г)"
        result = parse_food_text_description(text)
        assert result is not None
        assert len(result.ingredients) == 1
        ing = result.ingredients[0]
        assert ing.quantity == 2
        assert ing.grams == 100.0
        assert "(" not in ing.name and ")" not in ing.name
        assert "варён" in ing.name.lower() or ing.preparation == "отварной"

        bounds = MassBounds(grams_min=ing.grams, grams_center=ing.grams, grams_max=ing.grams, is_approximate_plausible_range=False, is_user_confirmed=True)
        comp = resolve_and_calculate_component(ing.name, bounds, cooking_state="cooked")
        assert comp is not None
        assert comp.nutrition_available is True
        assert "яйц" in comp.name.lower()
        assert comp.mass_bounds.grams_center == 100.0


# ══════════════════════════════════════════════════════════════════════════════
# 2. DEFECT-02: Quantity and weight parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestDefect02QuantityAndWeightParsing:
    def test_one_banana_weighing_120g(self):
        """один банан весом 120 г -> canonical product: банан, qty: 1, weight: 120g, banana profile."""
        text = "один банан весом 120 г"
        result = parse_food_text_description(text)
        assert result is not None
        assert len(result.ingredients) == 1
        ing = result.ingredients[0]
        assert ing.quantity == 1
        assert ing.grams == 120.0
        assert "один" not in ing.name.lower()
        assert "весом" not in ing.name.lower()
        assert "120" not in ing.name

        bounds = MassBounds(grams_min=ing.grams, grams_center=ing.grams, grams_max=ing.grams, is_approximate_plausible_range=False, is_user_confirmed=True)
        comp = resolve_and_calculate_component(ing.name, bounds)
        assert comp is not None
        assert comp.nutrition_available is True
        assert "банан" in comp.name.lower()
        assert comp.mass_bounds.grams_center == 120.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. DEFECT-03: Multi-item snack routing & conjunction splitting
# ══════════════════════════════════════════════════════════════════════════════

class TestDefect03MultiItemSnackRouting:
    def test_snack_conjunction_splitting(self):
        """Перекус: одно яблоко весом 180 г и натуральный йогурт без добавок 150 г."""
        text = "Перекус: одно яблоко весом 180 г и натуральный йогурт без добавок 150 г."
        result = parse_food_text_description(text)
        assert result is not None
        assert len(result.ingredients) == 2

        apple = result.ingredients[0]
        assert "яблок" in apple.name.lower()
        assert apple.grams == 180.0
        assert "одно" not in apple.name.lower()
        assert "весом" not in apple.name.lower()

        yogurt = result.ingredients[1]
        assert "йогурт" in yogurt.name.lower()
        assert yogurt.grams == 150.0

    @pytest.mark.asyncio
    async def test_full_telegram_snack_routing_not_generic_fallback(self, tmp_path):
        """Ensure message is not hijacked by inventory and does NOT reply with 'Раздел пока недоступен'."""
        from gateway.platforms.telegram import _is_structured_inventory_input

        text = "Перекус: одно яблоко весом 180 г и натуральный йогурт без добавок 150 г."
        # Must NOT be recognized as inventory input
        assert _is_structured_inventory_input(text) is False

        db_path = tmp_path / "telegram_snack.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)

        cfg = PlatformConfig(enabled=True, token="fake-token", extra={"allowed_users": ["*"]})
        adapter = TelegramAdapter(cfg)
        adapter._healbite_diary = diary
        sent_messages: list[str] = []

        async def _fake_send(**kwargs):
            sent_messages.append(kwargs.get("text", ""))
            return MagicMock()

        adapter._send_message_with_thread_fallback = AsyncMock(side_effect=_fake_send)

        msg = MagicMock()
        msg.text = text
        msg.from_user = MagicMock(id=999)
        msg.chat = MagicMock(id=999)
        msg.message_thread_id = None
        msg.photo = None
        msg.caption = None

        # Verify unprompted inventory text does not intercept and send placeholder
        intercepted = await adapter._maybe_handle_healbite_unprompted_inventory_text(msg)
        assert intercepted is False
        assert len(sent_messages) == 0

        # Verify staging inventory from this food text succeeds with 2 items and partial nutrition
        inv = diary.stage_pending_inventory_from_text(user_id=999, text=text)
        assert inv is not None
        assert len(inv.items) == 2
        reply = format_food_vision_inventory_reply(inv)
        assert "Раздел пока недоступен" not in reply
        assert "яблоко" in reply.lower() or "яблоки" in reply.lower()
        assert "йогурт" in reply.lower()
        assert "ккал" in reply


# ══════════════════════════════════════════════════════════════════════════════
# 4. DEFECT-04: Partial nutrition aggregation (Operator Fixture)
# ══════════════════════════════════════════════════════════════════════════════

class TestDefect04PartialNutritionAggregation:
    def test_operator_fixture_three_meals(self, tmp_path):
        """
        Required regression fixture from section 3 of USER_REQUEST:
        Breakfast:
          - Eggs: 155 kcal, P 12.6, F 10.6, C 1.1
          - Bread: unknown
          - Milk: 104 kcal, P 5.6, F 5.0, C 9.4
        Lunch:
          - Fully calculated: 610 kcal, P 55.5, F 18.6, C 52.2
        Dinner:
          - Cottage cheese: 310 kcal, P 33, F 18, C 4
          - Banana: unknown
          - Walnuts: 98 kcal, P 2.3, F 9.8, C 2.1
        Expected aggregate:
          - Calories: 1277 kcal
          - Protein: 109.0 g
          - Fat: 62.0 g
          - Carbohydrates: 68.8 g
          - Incomplete meals: 2
        """
        db_path = tmp_path / "fixture.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_id = 777
        base_time = datetime(2026, 10, 31, 8, 0, tzinfo=timezone.utc)

        # 1. Breakfast (incomplete)
        bf_items = [
            {"name": "Яйца варёные", "calories_kcal": 155.0, "protein_g": 12.6, "fat_g": 10.6, "carbs_g": 1.1, "weight_g": 100},
            {"name": "Пшеничный хлеб", "calories_kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None, "weight_g": 50},
            {"name": "Молоко", "calories_kcal": 104.0, "protein_g": 5.6, "fat_g": 5.0, "carbs_g": 9.4, "weight_g": 200},
        ]
        rec_bf = _make_record(meal_name="Завтрак", calories_kcal=None, protein_g=None, fat_g=None, carbs_g=None, items=bf_items)
        diary.save_record(user_id=user_id, source="text", record=rec_bf, image_ref=None, occurred_at=base_time)

        # 2. Lunch (complete)
        lunch_items = [
            {"name": "Куриное филе с гречкой", "calories_kcal": 610.0, "protein_g": 55.5, "fat_g": 18.6, "carbs_g": 52.2, "weight_g": 350},
        ]
        rec_lunch = _make_record(meal_name="Обед", calories_kcal=610.0, protein_g=55.5, fat_g=18.6, carbs_g=52.2, items=lunch_items)
        diary.save_record(user_id=user_id, source="text", record=rec_lunch, image_ref=None, occurred_at=base_time + timedelta(hours=5))

        # 3. Dinner (incomplete)
        din_items = [
            {"name": "Творог", "calories_kcal": 310.0, "protein_g": 33.0, "fat_g": 18.0, "carbs_g": 4.0, "weight_g": 200},
            {"name": "Банан", "calories_kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None, "weight_g": 120},
            {"name": "Грецкие орехи", "calories_kcal": 98.0, "protein_g": 2.3, "fat_g": 9.8, "carbs_g": 2.1, "weight_g": 15},
        ]
        rec_din = _make_record(meal_name="Ужин", calories_kcal=None, protein_g=None, fat_g=None, carbs_g=None, items=din_items)
        diary.save_record(user_id=user_id, source="text", record=rec_din, image_ref=None, occurred_at=base_time + timedelta(hours=11))

        # Verify summary
        summary = diary.get_summary(user_id=user_id, now=base_time + timedelta(hours=12), days=1)

        assert summary["calories_kcal"] == 1277.0
        assert summary["protein_g"] == 109.0
        assert summary["fat_g"] == 62.0
        assert summary["carbs_g"] == 68.8
        assert summary["incomplete_entry_count"] == 2
        assert summary["complete_entry_count"] == 1
        assert summary["has_incomplete_entries"] is True

        # Check /diary formatting
        diary_report = format_nutrition_diary_report(summary)
        assert "1277 ккал" in diary_report
        assert "109 г" in diary_report or "109.0 г" in diary_report
        assert "62 г" in diary_report or "62.0 г" in diary_report
        assert "68.8 г" in diary_report
        # Verify meal subtotals:
        # Breakfast subtotal: 155 + 104 = 259 kcal
        assert "259 ккал" in diary_report
        # Dinner subtotal: 310 + 98 = 408 kcal
        assert "408 ккал" in diary_report
        # Lunch subtotal: 610 kcal
        assert "610 ккал" in diary_report
        # Missing components displayed with "КБЖУ не указано"
        assert "КБЖУ не указано" in diary_report


# ══════════════════════════════════════════════════════════════════════════════
# 5. Composite dish deduplication
# ══════════════════════════════════════════════════════════════════════════════

class TestCompositeDishDeduplication:
    def test_composite_dish_subingredients_not_double_counted(self, tmp_path):
        """Suppressed components in items_json must not double count towards subtotals."""
        db_path = tmp_path / "dedup.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_id = 888
        now = datetime(2026, 10, 31, 13, 0, tzinfo=timezone.utc)

        items = [
            {
                "item_id": "dish_1",
                "name": "Плов с говядиной",
                "calories_kcal": 650.0,
                "protein_g": 28.0,
                "fat_g": 26.0,
                "carbs_g": 72.0,
                "weight_g": 350,
                "suppressed_by_item_id": None,
            },
            {
                "item_id": "sub_1",
                "name": "Рис отварной",
                "calories_kcal": 260.0,
                "protein_g": 5.0,
                "fat_g": 1.0,
                "carbs_g": 56.0,
                "weight_g": 200,
                "suppressed_by_item_id": "dish_1",
            },
            {
                "item_id": "sub_2",
                "name": "Говядина тушеная",
                "calories_kcal": 390.0,
                "protein_g": 23.0,
                "fat_g": 25.0,
                "carbs_g": 16.0,
                "weight_g": 150,
                "suppressed_by_item_id": "dish_1",
            },
        ]
        # Incomplete row totals: None
        rec = _make_record(meal_name="Обед", calories_kcal=None, protein_g=None, fat_g=None, carbs_g=None, items=items)
        diary.save_record(user_id=user_id, source="vision", record=rec, image_ref=None, occurred_at=now)

        summary = diary.get_summary(user_id=user_id, now=now, days=1)
        # Should sum ONLY dish_1 (650), NOT 650 + 260 + 390 = 1300
        assert summary["calories_kcal"] == 650.0
        assert summary["protein_g"] == 28.0
        assert summary["fat_g"] == 26.0
        assert summary["carbs_g"] == 72.0


# ══════════════════════════════════════════════════════════════════════════════
# 6. Unknown nutrients remain NULL
# ══════════════════════════════════════════════════════════════════════════════

class TestUnknownNutrientsRemainNull:
    def test_completely_unknown_meal_keeps_null(self, tmp_path):
        """Unknown nutrition values must never become zero."""
        db_path = tmp_path / "null_nutr.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_id = 555
        now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

        items = [
            {"name": "Неизвестный продукт", "calories_kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None, "weight_g": 100}
        ]
        rec = _make_record(meal_name="Обед", calories_kcal=None, protein_g=None, fat_g=None, carbs_g=None, items=items)
        diary.save_record(user_id=user_id, source="text", record=rec, image_ref=None, occurred_at=now)

        summary = diary.get_summary(user_id=user_id, now=now, days=1)
        assert summary["calories_kcal"] is None
        assert summary["protein_g"] is None
        assert summary["fat_g"] is None
        assert summary["carbs_g"] is None
        assert summary["incomplete_entry_count"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 7. DEFECT-05: Accuracy badge/disclaimer consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestAccuracyBadgeDisclaimerConsistency:
    def test_confirmed_weight_reference_catalog_badge(self, tmp_path):
        """Confirmed weight + reference catalog -> 'Вес подтверждён · КБЖУ по справочнику (ориентировочно)'."""
        diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
        inv = diary.stage_pending_inventory_from_text(
            user_id=101,
            text="Гречка отварная 120 г, стейк из индейки 150 г",
        )
        assert inv is not None
        reply = format_food_vision_inventory_reply(inv)
        assert "Вес подтверждён · КБЖУ по справочнику (ориентировочно)" in reply
        assert "Точный расчёт" not in reply
        # Notice must match confirmed weight semantics, not estimated weight
        assert "КБЖУ по справочнику продуктов (ориентировочно)" in reply
        assert "КБЖУ рассчитаны приблизительно. Проверьте количество и вес" not in reply

    def test_estimated_weight_badge(self, tmp_path):
        """Estimated weight -> 'Ориентировочный расчёт (~КБЖУ)'."""
        diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
        inv = diary.stage_pending_inventory_from_text(
            user_id=102,
            text="тарелка борща",
        )
        assert inv is not None
        reply = format_food_vision_inventory_reply(inv)
        assert "Ориентировочный расчёт (~КБЖУ)" in reply
        assert "Вес подтверждён" not in reply

    def test_user_nutritional_override_badge(self):
        """User nutritional override -> 'Точный расчёт'."""
        item = PendingFoodVisionItem(
            index=1,
            visible_name="Протеиновый батончик",
            normalized_name="протеиновый батончик",
            confidence=1.0,
            estimated_grams_min=60.0,
            estimated_grams_max=60.0,
            selected_grams=60.0,
            preparation="",
            is_sauce=False,
            user_modified=True,
        )
        from gateway.healbite_nutrition_diary import UserNutritionOverride
        item.user_nutrition = UserNutritionOverride(
            grams=60.0,
            calories_kcal=210.0,
            protein_g=20.0,
            fat_g=7.0,
            carbs_g=15.0,
            nutrition_basis="TOTAL_PORTION",
        )
        pending = PendingFoodVisionInventory(
            user_id=103,
            inventory_id="inv_override",
            schema_version="1.0",
            items=[item],
            overall_confidence=1.0,
            warnings=[],
        )
        reply = format_food_vision_inventory_reply(pending)
        assert "🎯 Точный расчёт" in reply
        assert "КБЖУ рассчитаны приблизительно" not in reply


# ══════════════════════════════════════════════════════════════════════════════
# 8. DEFECT-06: /diary versus /stats today and /stats 7d
# ══════════════════════════════════════════════════════════════════════════════

class TestDefect06DiaryVsStatsSeparation:
    def test_diary_shows_detailed_meals_while_stats_today_is_summary_only(self, tmp_path):
        """/diary shows detailed meals and items; /stats today is a dedicated target/macro screen."""
        db_path = tmp_path / "sep.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_id = 404
        now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

        items = [
            {"name": "Овсянка с ягодами", "calories_kcal": 320.0, "protein_g": 12.0, "fat_g": 6.0, "carbs_g": 52.0, "weight_g": 250}
        ]
        rec = _make_record(meal_name="Завтрак", calories_kcal=320.0, protein_g=12.0, fat_g=6.0, carbs_g=52.0, items=items)
        diary.save_record(user_id=user_id, source="text", record=rec, image_ref=None, occurred_at=now)

        summary = diary.get_summary(user_id=user_id, now=now, days=1)

        diary_rep = format_nutrition_diary_report(summary)
        stats_today_rep = format_nutrition_stats_report(summary)

        # /diary contains detailed breakdown
        assert "Твой дневник за сегодня:" in diary_rep
        assert "Приемы пищи:" in diary_rep
        assert "Овсянка с ягодами" in diary_rep
        assert "250 г" in diary_rep
        assert "Итого за приём:" in diary_rep

        # /stats today does NOT contain individual meals
        assert "Статистика питания за сегодня:" in stats_today_rep
        assert "Приемы пищи:" not in stats_today_rep
        assert "Овсянка с ягодами" not in stats_today_rep
        assert "Итого за день:" in stats_today_rep
        assert "Калории" in stats_today_rep

    def test_stats_7d_totals_and_calendar_averages(self, tmp_path):
        """/stats 7d displays weekly statistics, 7-day totals, and calendar averages including empty days."""
        db_path = tmp_path / "stats7d.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_id = 505
        now = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)

        # Record on day 1 (today)
        rec1 = _make_record(meal_name="Обед", calories_kcal=700.0, protein_g=50.0, fat_g=20.0, carbs_g=70.0)
        diary.save_record(user_id=user_id, source="text", record=rec1, image_ref=None, occurred_at=now - timedelta(hours=2))

        # Record on day 3 (2 days ago)
        rec2 = _make_record(meal_name="Обед", calories_kcal=700.0, protein_g=50.0, fat_g=20.0, carbs_g=70.0)
        diary.save_record(user_id=user_id, source="text", record=rec2, image_ref=None, occurred_at=now - timedelta(days=2))

        summary_7d = diary.get_summary(user_id=user_id, now=now, days=7)
        stats_7d_rep = format_nutrition_stats_report(summary_7d)

        assert "Статистика питания за 7 дней:" in stats_7d_rep
        # Total for 7 days: 700 + 700 = 1400 kcal
        assert "1400 ккал" in stats_7d_rep
        # Calendar-day average: 1400 / 7 = 200 kcal
        assert "200 ккал" in stats_7d_rep
        # Empty days explanation
        assert "Пустые дни включены в расчёт среднего значения за календарный период." in stats_7d_rep
        # No meal breakdown
        assert "Приемы пищи:" not in stats_7d_rep


# ══════════════════════════════════════════════════════════════════════════════
# 9. Statistics button and command routing
# ══════════════════════════════════════════════════════════════════════════════

class TestStatisticsRoutingAndKeyboard:
    def test_keyboard_action_mapping(self):
        """Verify reply keyboard action mapping for statistics."""
        assert HEALBITE_REPLY_KEYBOARD_ACTIONS["📊 Статистика"] == "/stats 7d"
        assert _HEALBITE_NORMALIZED_COMMAND_MAP["статистика"] == "/stats 7d"

    @pytest.mark.asyncio
    async def test_telegram_stats_routing(self, tmp_path):
        """TelegramAdapter routes /stats today and /stats 7d to format_nutrition_stats_report."""
        db_path = tmp_path / "tg_stats.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_id = 606
        now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

        rec = _make_record(meal_name="Завтрак", calories_kcal=400.0, protein_g=30.0, fat_g=10.0, carbs_g=40.0)
        diary.save_record(user_id=user_id, source="text", record=rec, image_ref=None, occurred_at=now)

        cfg = PlatformConfig(enabled=True, token="fake-token", extra={"allowed_users": ["*"]})
        adapter = TelegramAdapter(cfg)
        sent: list[str] = []

        async def _fake_send(**kwargs):
            sent.append(kwargs.get("text", ""))
            return MagicMock()

        adapter._send_message_with_thread_fallback = AsyncMock(side_effect=_fake_send)

        msg = MagicMock()
        msg.from_user = MagicMock(id=user_id)
        msg.chat = MagicMock(id=user_id)
        msg.message_thread_id = None

        with patch("gateway.platforms.telegram.compute_nutrition_diary_summary") as mock_sum:
            mock_sum.return_value = diary.get_summary(user_id=user_id, now=now, days=1)

            # /stats today
            msg.text = "/stats today"
            handled = await adapter._maybe_handle_nutrition_diary_command(msg)
            assert handled is True
            assert "Статистика питания за сегодня:" in sent[-1]
            assert "Приемы пищи:" not in sent[-1]

            # /diary
            msg.text = "/diary"
            handled = await adapter._maybe_handle_nutrition_diary_command(msg)
            assert handled is True
            assert "Твой дневник за сегодня:" in sent[-1]
            assert "Приемы пищи:" in sent[-1]

    def test_gateway_runner_stats_routing(self, tmp_path):
        """GatewayRunner routes /stats to format_nutrition_stats_report and /diary to format_nutrition_diary_report."""
        db_path = tmp_path / "gw_stats.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_id = 707
        now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)

        rec = _make_record(meal_name="Завтрак", calories_kcal=400.0, protein_g=30.0, fat_g=10.0, carbs_g=40.0)
        diary.save_record(user_id=user_id, source="text", record=rec, image_ref=None, occurred_at=now)

        runner = GatewayRunner()

        event_stats = MagicMock()
        event_stats.text = "/stats today"
        event_stats.source = MagicMock(user_id=user_id)

        with patch("gateway.healbite_nutrition_diary.get_existing_nutrition_diary", return_value=diary), \
             patch("gateway.healbite_nutrition_diary.compute_nutrition_diary_summary", side_effect=lambda user_id, days: diary.get_summary(user_id=user_id, now=now, days=days)):
            import asyncio
            out_stats = asyncio.run(runner._handle_healbite_nutrition_diary_command(event_stats))
            assert "Статистика питания за сегодня:" in out_stats
            assert "Приемы пищи:" not in out_stats

            event_diary = MagicMock()
            event_diary.text = "/diary"
            event_diary.source = MagicMock(user_id=user_id)
            out_diary = asyncio.run(runner._handle_healbite_nutrition_diary_command(event_diary))
            assert "Твой дневник за сегодня:" in out_diary
            assert "Приемы пищи:" in out_diary


# ══════════════════════════════════════════════════════════════════════════════
# 10. User isolation & two-device consistency
# ══════════════════════════════════════════════════════════════════════════════

class TestUserIsolationAndTwoDeviceConsistency:
    def test_same_telegram_user_on_phone_and_desktop_resolves_identically(self, tmp_path):
        """
        Verify:
        - Same Telegram user ID on two devices accesses same records, targets, and stats.
        - Two different users are completely isolated.
        - Repeated report requests do not duplicate or alter data.
        """
        db_path = tmp_path / "isolation.db"
        diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
        user_a = 10001
        user_b = 10002
        now = datetime(2026, 10, 31, 14, 0, tzinfo=timezone.utc)

        # User A logs meals
        rec_a1 = _make_record(meal_name="Завтрак", calories_kcal=500.0, protein_g=35.0, fat_g=15.0, carbs_g=55.0)
        diary.save_record(user_id=user_a, source="text", record=rec_a1, image_ref=None, occurred_at=now - timedelta(hours=4))

        # Device 1 (phone) queries User A
        summary_phone = diary.get_summary(user_id=user_a, now=now, days=1)
        # Device 2 (desktop) queries User A
        summary_desktop = diary.get_summary(user_id=user_a, now=now, days=1)

        assert summary_phone["calories_kcal"] == summary_desktop["calories_kcal"] == 500.0
        assert summary_phone["protein_g"] == summary_desktop["protein_g"] == 35.0
        assert summary_phone["entry_count"] == summary_desktop["entry_count"] == 1

        # User B queries: must be empty
        summary_b = diary.get_summary(user_id=user_b, now=now, days=1)
        assert summary_b["entry_count"] == 0
        assert summary_b["calories_kcal"] is None or summary_b["calories_kcal"] == 0.0

        # Repeated queries do not mutate state
        summary_phone_again = diary.get_summary(user_id=user_a, now=now, days=1)
        assert summary_phone_again["entry_count"] == 1
        assert summary_phone_again["calories_kcal"] == 500.0
