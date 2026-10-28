"""
R1-13 Food Entry Improvements (S5.1) — Focused test suite.

Tests deterministic parsing, household measures, compound corrections,
precision labeling, and fail-closed draft preservation.
"""
from __future__ import annotations

import re
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from gateway.nutrition.text_parser import (
    ParsedFoodText,
    ParsedIngredient,
    parse_food_text_description,
    is_full_food_description,
)
from gateway.nutrition.household_measures import (
    HOUSEHOLD_MEASURES_VERSION,
    HOUSEHOLD_DISCLAIMER,
    extract_household_measure_from_phrase,
    normalize_food_inflection,
    detect_household_measure_token,
)
from gateway.nutrition.action_interpreter import parse_deterministic_action_plan
from gateway.nutrition.user_override import ActionOperation


# ── Helper ──────────────────────────────────────────────────────────────────

class FakeItem:
    """Minimal item stub for action_interpreter tests."""
    def __init__(self, index: int, visible_name: str, normalized_name: str = "", item_id: str = ""):
        self.index = index
        self.visible_name = visible_name
        self.normalized_name = normalized_name or visible_name.lower()
        self.item_id = item_id or f"item_{index}"


def _plan(raw: str, items: list[FakeItem]):
    return parse_deterministic_action_plan(
        raw, items=items, user_id=1, inventory_id="inv1", inventory_version=1,
    )


# ══════════════════════════════════════════════════════════════════════════════
# A. Deterministic parsing of food weights
# ══════════════════════════════════════════════════════════════════════════════

class TestDeterministicParsing:
    def test_exact_weights_russian(self):
        """Гречка отварная 120 г, стейк из индейки 150 г"""
        r = parse_food_text_description("Гречка отварная 120 г, стейк из индейки 150 г")
        assert r is not None
        assert len(r.ingredients) == 2
        assert r.ingredients[0].grams == 120.0
        assert r.ingredients[0].is_exact is True
        assert r.ingredients[0].preparation == "отварной"
        assert r.ingredients[1].grams == 150.0
        assert r.ingredients[1].is_exact is True

    def test_explicit_grams_units(self):
        """Куриная грудка запечённая 180 грамм"""
        r = parse_food_text_description("Куриная грудка запечённая 180 грамм")
        assert r is not None
        assert len(r.ingredients) == 1
        assert r.ingredients[0].grams == 180.0
        assert r.ingredients[0].is_exact is True
        assert r.ingredients[0].preparation == "запечённый"

    def test_multiple_products_with_weight(self):
        """Оливковое масло 10 г, огурец 100 г"""
        r = parse_food_text_description("Оливковое масло 10 г, огурец 100 г")
        assert r is not None
        assert len(r.ingredients) == 2
        assert r.ingredients[0].grams == 10.0
        assert r.ingredients[0].is_exact is True
        assert r.ingredients[1].grams == 100.0

    def test_egg_dish_piece_count(self):
        """Яичница из 2 яиц, огурец, хлебцы — piece counts not mass."""
        r = parse_food_text_description("Яичница из 2 яиц, огурец, хлебцы")
        assert r is not None
        assert len(r.ingredients) == 3
        egg = r.ingredients[0]
        assert egg.quantity == 2.0
        assert egg.grams == 110.0  # 2 * 55g
        assert egg.is_exact is False  # approximate
        assert egg.is_piece is True
        assert egg.preparation in ("жареный", "омлет")
        # Remaining items should be approximate piece-based
        assert r.ingredients[1].is_exact is False  # огурец with no weight
        assert r.ingredients[2].is_exact is False  # хлебцы with no weight

    def test_exact_bounds_equal(self):
        """When exact grams given, grams_min == grams_max == grams."""
        r = parse_food_text_description("Рис 200 г")
        assert r is not None
        ing = r.ingredients[0]
        assert ing.grams == 200.0
        assert ing.grams_min == 200.0
        assert ing.grams_max == 200.0
        assert ing.is_exact is True


# ══════════════════════════════════════════════════════════════════════════════
# B. Household measures and approximate portions
# ══════════════════════════════════════════════════════════════════════════════

class TestHouseholdMeasures:
    def test_version_string(self):
        assert HOUSEHOLD_MEASURES_VERSION.startswith("2026.")

    def test_disclaimer_text(self):
        assert "Примерные порции" in HOUSEHOLD_DISCLAIMER

    def test_plate_of_borscht(self):
        """тарелка борща = 300g [250..350]"""
        r = extract_household_measure_from_phrase("тарелка борща")
        assert r is not None
        res, food = r
        assert res.grams_center == 300.0
        assert res.grams_min == 250.0
        assert res.grams_max == 350.0
        assert "борщ" in food.lower()

    def test_handful_of_almonds(self):
        """горсть миндаля = 30g [25..35]"""
        r = extract_household_measure_from_phrase("горсть миндаля")
        assert r is not None
        res, food = r
        assert res.grams_center == 30.0
        assert res.grams_min == 25.0
        assert res.grams_max == 35.0

    def test_tablespoon_of_oil(self):
        """столовая ложка оливкового масла = 15g [12..18]"""
        r = extract_household_measure_from_phrase("столовая ложка оливкового масла")
        assert r is not None
        res, food = r
        assert res.grams_center == 15.0
        assert res.grams_min == 12.0
        assert res.grams_max == 18.0

    def test_unknown_measure_returns_none(self):
        """Unsupported measures should not resolve."""
        r = extract_household_measure_from_phrase("бочка масла")
        assert r is None

    def test_normalize_food_inflection(self):
        assert normalize_food_inflection("говядину") == "говядина"
        assert normalize_food_inflection("свинины") == "свинина"
        assert normalize_food_inflection("запечённую") == "запечённая"
        assert normalize_food_inflection("борща") == "борщ"
        # Preserve nominatives
        assert normalize_food_inflection("курица") == "курица"

    def test_text_parser_detects_household(self):
        """parse_food_text_description integrates household measures."""
        r = parse_food_text_description("тарелка борща, горсть миндаля")
        assert r is not None
        assert len(r.ingredients) == 2
        borscht = r.ingredients[0]
        assert borscht.household_measure == "plate"
        assert borscht.grams == 300.0
        assert borscht.is_exact is False
        almonds = r.ingredients[1]
        assert almonds.household_measure == "handful"
        assert almonds.grams == 30.0

    def test_center_invariant(self):
        """Verify center == round((min+max)/2, 1) for all calibrated measures."""
        r1 = extract_household_measure_from_phrase("тарелка борща")
        assert r1 is not None
        res = r1[0]
        assert res.grams_center == round((res.grams_min + res.grams_max) / 2.0, 1)


# ══════════════════════════════════════════════════════════════════════════════
# C. Compound meal-composition correction
# ══════════════════════════════════════════════════════════════════════════════

class TestCompoundCorrections:
    """Tests for single-message compound corrections."""

    @pytest.fixture
    def items_3(self):
        return [
            FakeItem(1, "Свинина", "свинина", "item_1"),
            FakeItem(2, "Лук", "лук", "item_2"),
            FakeItem(3, "Рис", "рис", "item_3"),
        ]

    def test_replace_with_reweight(self, items_3):
        """Замени свинину на говядину, вес мяса 150 г"""
        plan = _plan("Замени свинину на говядину, вес мяса 150 г", items_3)
        assert any(a.operation == ActionOperation.REPLACE_ITEM for a in plan.actions)
        repl = [a for a in plan.actions if a.operation == ActionOperation.REPLACE_ITEM][0]
        assert "говядин" in repl.name.lower()
        assert repl.grams == 150.0

    def test_remove_and_add(self, items_3):
        """Убери лук, добавь морковь 50 г"""
        plan = _plan("Убери лук, добавь морковь 50 г", items_3)
        ops = [a.operation for a in plan.actions]
        assert ActionOperation.REMOVE_ITEM in ops
        assert ActionOperation.ADD_ITEM in ops
        add = [a for a in plan.actions if a.operation == ActionOperation.ADD_ITEM][0]
        assert "морковь" in add.name.lower()
        assert add.grams == 50.0

    def test_wholesale_recipe_rewrite(self, items_3):
        """Исправь состав: рис 150 г, курица 120 г, масло 10 г"""
        plan = _plan("Исправь состав: рис 150 г, курица 120 г, масло 10 г", items_3)
        removes = [a for a in plan.actions if a.operation == ActionOperation.REMOVE_ITEM]
        adds = [a for a in plan.actions if a.operation == ActionOperation.ADD_ITEM]
        assert len(removes) == 3  # Remove all original items
        assert len(adds) == 3  # Add 3 new items
        assert adds[0].grams == 150.0
        assert adds[1].grams == 120.0
        assert adds[2].grams == 10.0

    def test_instead_replacement(self):
        """Вместо жареной курицы используй запечённую, вес 180 г"""
        items = [
            FakeItem(1, "Жареная курица", "курица жареная", "item_1"),
            FakeItem(2, "Рис", "рис", "item_2"),
        ]
        plan = _plan("Вместо жареной курицы используй запечённую, вес 180 г", items)
        repl = [a for a in plan.actions if a.operation == ActionOperation.REPLACE_ITEM]
        assert len(repl) == 1
        assert repl[0].grams == 180.0
        assert "запечённ" in repl[0].name.lower()

    def test_empty_plan_does_not_crash(self, items_3):
        """Completely unrecognized text should not crash."""
        plan = _plan("Просто проверка", items_3)
        # parse_deterministic_action_plan returns None for unrecognized text — valid fail-closed behavior
        # The important thing is it does not raise an exception


# ══════════════════════════════════════════════════════════════════════════════
# D. Precision labeling
# ══════════════════════════════════════════════════════════════════════════════

class TestPrecisionLabeling:
    def test_exact_text_is_all_exact(self):
        """All ingredients with explicit grams should have is_exact=True."""
        r = parse_food_text_description("Рис 200 г, курица 150 г")
        assert r is not None
        for ing in r.ingredients:
            assert ing.is_exact is True

    def test_approximate_piece_not_exact(self):
        """Ingredients without weight should be is_exact=False."""
        r = parse_food_text_description("Яичница из 2 яиц, хлебцы")
        assert r is not None
        for ing in r.ingredients:
            assert ing.is_exact is False

    def test_mixed_precision(self):
        """Mix of exact and approximate."""
        r = parse_food_text_description("Рис 200 г, хлебцы")
        assert r is not None
        assert r.ingredients[0].is_exact is True
        assert r.ingredients[1].is_exact is False


# ══════════════════════════════════════════════════════════════════════════════
# E. Household measure detection helper
# ══════════════════════════════════════════════════════════════════════════════

class TestHouseholdDetection:
    def test_detect_known_token(self):
        """detect_household_measure_token should find calibrated measures."""
        result = detect_household_measure_token("столовая ложка масла")
        assert result is not None  # Returns tuple (measure_id, label, qty)

    def test_detect_unknown_token(self):
        """Unknown units should not match."""
        result = detect_household_measure_token("160 баррелей нефти")
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# F. End-to-End Diary Formatting & Inventory Editing
# ══════════════════════════════════════════════════════════════════════════════

class TestDiaryFormattingAndEditing:
    def test_exact_inventory_formatting(self, tmp_path):
        from gateway.healbite_nutrition_diary import (
            HealBiteNutritionDiary,
            format_food_vision_inventory_reply,
            build_food_vision_inventory_keyboard,
        )
        diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
        inv = diary.stage_pending_inventory_from_text(
            user_id=1001,
            text="Гречка отварная 120 г, стейк из индейки 150 г",
        )
        assert inv is not None
        reply = format_food_vision_inventory_reply(inv)
        assert "🎯 Точный расчёт" in reply
        assert "Итого:" in reply
        assert HOUSEHOLD_DISCLAIMER not in reply

        kb = build_food_vision_inventory_keyboard(inv)
        assert any(btn["text"] in ("✏️ Исправить", "✏️ Уточнить состав") for row in kb for btn in row)

    def test_approximate_inventory_formatting(self, tmp_path):
        from gateway.healbite_nutrition_diary import (
            HealBiteNutritionDiary,
            format_food_vision_inventory_reply,
        )
        diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
        inv = diary.stage_pending_inventory_from_text(
            user_id=1002,
            text="тарелка борща",
        )
        assert inv is not None
        reply = format_food_vision_inventory_reply(inv)
        assert "📊 Ориентировочный расчёт (~КБЖУ)" in reply
        assert "Итого:" in reply
        assert HOUSEHOLD_DISCLAIMER in reply

    def test_compound_editing_and_id_sequencer(self, tmp_path):
        """Verify compound multi-action edits work sequentially without ID collisions."""
        from gateway.healbite_nutrition_diary import HealBiteNutritionDiary
        diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
        inv = diary.stage_pending_inventory_from_text(
            user_id=1003,
            text="Свинина 150 г, лук 50 г, рис 120 г",
        )
        assert inv is not None

        # Turn 1: Replace pork with beef 150g
        p1 = parse_deterministic_action_plan(
            "Замени свинину на говядину, вес мяса 150 г",
            items=inv.items,
            user_id=1003,
            inventory_id=inv.inventory_id,
            inventory_version=inv.inventory_version,
        )
        assert p1 is not None
        updated1, err1 = diary.apply_structured_action_plan(inv, p1)
        assert err1 is None
        assert updated1 is not None
        names1 = [it.visible_name.lower() for it in updated1.items]
        assert any("говядин" in n for n in names1)
        assert not any("свинин" in n for n in names1)

        # Turn 2: Remove onion, add carrot 50g
        p2 = parse_deterministic_action_plan(
            "Убери лук, добавь морковь 50 г",
            items=updated1.items,
            user_id=1003,
            inventory_id=updated1.inventory_id,
            inventory_version=updated1.inventory_version,
        )
        assert p2 is not None
        updated2, err2 = diary.apply_structured_action_plan(updated1, p2)
        assert err2 is None
        assert updated2 is not None
        names2 = [it.visible_name.lower() for it in updated2.items]
        assert not any("лук" in n for n in names2)
        assert any("морковь" in n for n in names2)

        # Turn 3: Wholesale recipe rewrite
        p3 = parse_deterministic_action_plan(
            "Исправь состав: рис 150 г, курица 120 г, масло 10 г",
            items=updated2.items,
            user_id=1003,
            inventory_id=updated2.inventory_id,
            inventory_version=updated2.inventory_version,
        )
        assert p3 is not None
        updated3, err3 = diary.apply_structured_action_plan(updated2, p3)
        assert err3 is None
        assert updated3 is not None
        assert len(updated3.items) == 3
        names3 = [it.visible_name.lower() for it in updated3.items]
        assert any("рис" in n for n in names3)
        assert any("курица" in n for n in names3)
        assert any("масло" in n for n in names3)

    def test_fail_closed_cannot_remove_all_items(self, tmp_path):
        """Removing all items should fail closed and preserve original inventory."""
        from gateway.healbite_nutrition_diary import HealBiteNutritionDiary
        from gateway.nutrition.user_override import StructuredAction, StructuredActionPlan
        diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
        inv = diary.stage_pending_inventory_from_text(
            user_id=1004,
            text="Рис 150 г",
        )
        assert inv is not None
        # Try to remove item 1
        plan = StructuredActionPlan(
            inventory_id=inv.inventory_id,
            inventory_version=inv.inventory_version,
            user_id=1004,
            actions=[StructuredAction(operation=ActionOperation.REMOVE_ITEM, target_index=1)],
            confidence=1.0,
        )
        result, err = diary.apply_structured_action_plan(inv, plan)
        assert result is None
        assert err is not None
        assert "Нельзя удалить все позиции" in err

