"""Regression tests for HEALBITE R1 defects HB-R1-DEFECT-01 / HB-R1-DEFECT-02.

DEFECT-01: a free-form caption's macro statements and bare weight were parsed as
ingredients ("Жира 5 г", "160 грамм") and the weight never bound to the food.
DEFECT-02: the natural correction "Исправить: 1: <name> <grams>" (verb plus a
colon) was rejected, several corrections could not share one message, and a
no-op correction still reported success.
"""

from __future__ import annotations

import pytest

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    parse_pending_inventory_action,
)
from gateway.nutrition.text_parser import parse_food_text_description

CAPTION = (
    "Обед — фарш куриный (20 грамм белка, 5 грамм жира), 160 грамм, "
    "грибы 50 грамм, пассата 50 грамм, лук репчатый 30 грамм, "
    "макароны твердых сортов пшеницы вареные 160 грамм"
)


def _diary(tmp_path) -> HealBiteNutritionDiary:
    return HealBiteNutritionDiary(db_path=tmp_path / "test_diary.db", background_write=False)


# --------------------------------------------------------------------------
# DEFECT-01 — caption parsing
# --------------------------------------------------------------------------


def test_caption_macros_and_bare_weight_bind_to_the_ingredient():
    parsed = parse_food_text_description(CAPTION)

    assert parsed is not None
    assert len(parsed.ingredients) == 5
    assert [ingredient.grams for ingredient in parsed.ingredients] == [160.0, 50.0, 50.0, 30.0, 160.0]
    assert parsed.ingredients[0].name.casefold() == "фарш куриный"
    # Macros are portion metadata, not per-100 g values and not weights.
    assert parsed.ingredients[0].protein_g == pytest.approx(20.0)
    assert parsed.ingredients[0].fat_g == pytest.approx(5.0)
    assert parsed.ingredients[0].carbs_g is None


def test_caption_creates_no_macro_or_bare_weight_ingredients():
    parsed = parse_food_text_description(CAPTION)

    assert parsed is not None
    names = [ingredient.name.casefold().strip(" .,;:()") for ingredient in parsed.ingredients]
    assert "жира" not in names
    assert "жир" not in names
    assert "белка" not in names
    assert "160 грамм" not in names
    for name in names:
        assert not name.replace(".", "").isdigit()


def test_plain_text_description_is_unchanged():
    parsed = parse_food_text_description("Рис 200 г, курица 150 г")

    assert parsed is not None
    assert [(ingredient.name, ingredient.grams) for ingredient in parsed.ingredients] == [
        ("Рис", 200.0),
        ("Курица", 150.0),
    ]


def test_caption_staging_keeps_five_items_and_macro_note(tmp_path):
    diary = _diary(tmp_path)
    staged = diary.stage_pending_inventory_from_text(user_id=4, text=CAPTION)

    assert staged is not None
    assert len(staged.items) == 5
    assert staged.items[0].selected_grams == pytest.approx(160.0)
    assert any("Указано на порцию" in warning for warning in staged.warnings)


# --------------------------------------------------------------------------
# DEFECT-02 — corrections
# --------------------------------------------------------------------------


def test_correction_parser_accepts_verb_with_colon():
    result = parse_pending_inventory_action("Исправить: 1: Фарш куриный 160 грамм")

    assert result.ok is True
    assert result.action is not None
    assert result.action.kind == "replace"
    assert result.action.index == 1
    assert result.action.name.casefold() == "фарш куриный"
    assert result.action.grams == pytest.approx(160.0)


def test_corrections_can_share_one_message():
    # One message, one correction per line: the existing fail-closed contract
    # keeps comma-separated one-line corrections ambiguous, so the batch form is
    # newline separated.
    result = parse_pending_inventory_action(
        "Исправить: 1: Фарш куриный 160 грамм\nИсправить: 2: Грибы 80 грамм"
    )

    assert result.ok is True
    assert result.action is not None
    assert result.action.kind == "batch"
    assert len(result.action.sub_actions) == 2


def test_colon_correction_changes_the_pending_meal(tmp_path):
    diary = _diary(tmp_path)
    staged = diary.stage_pending_inventory_from_text(user_id=1, text="Рис 200 г, курица 150 г")
    assert staged is not None

    result = diary.handle_pending_inventory_reply(1, "Исправить: 1: Рис 150 грамм")

    assert result.status == "updated"
    assert "Изменения сохранены" in result.reply_text
    pending = diary.get_pending_inventory(1)
    assert pending is not None
    assert pending.items[0].visible_name == "Рис"
    assert pending.items[0].selected_grams == pytest.approx(150.0)
    assert pending.items[1].selected_grams == pytest.approx(150.0)


def test_noop_correction_never_reports_success(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=2, text="Рис 200 г")

    result = diary.handle_pending_inventory_reply(2, "Вес 1: 200 г")

    assert result.status == "unchanged"
    assert "Изменения сохранены" not in result.reply_text


def test_out_of_range_correction_is_rejected_without_success(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=3, text="Рис 200 г")

    result = diary.handle_pending_inventory_reply(3, "Исправить: 5: Сыр 80 грамм")

    assert result.status == "invalid_action"
    assert "Изменения сохранены" not in result.reply_text
    pending = diary.get_pending_inventory(3)
    assert pending is not None
    assert [item.visible_name for item in pending.items] == ["Рис"]
