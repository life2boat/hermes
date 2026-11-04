"""Regression tests for HEALBITE R1 defects HB-R1-DEFECT-01 / HB-R1-DEFECT-02.

DEFECT-01: protein/fat statements and a bare weight in the free-form caption
were turned into ingredients ("Жира 5 г", "160 грамм") and the 160 g was not
bound to the chicken mince.
DEFECT-02: the natural correction form "Исправить: 1: <name> <grams>" did not
match the action parser, so the bot re-sent the unchanged list; several
corrections could also not be sent in one message.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    parse_meal_caption_items,
    parse_pending_inventory_actions,
)

CAPTION = (
    "Обед — фарш куриный (20 грамм белка, 5 грамм жира), 160 грамм, "
    "грибы 50 грамм, пассата 50 грамм, лук репчатый 30 грамм, "
    "макароны твердых сортов пшеницы вареные 160 грамм"
)

_CAPTION_NAMES = [
    "фарш куриный",
    "грибы",
    "пассата",
    "лук репчатый",
    "макароны твердых сортов пшеницы вареные",
]


def _food_item(
    visible_name: str,
    normalized_name: str,
    grams_min: float | None,
    grams_max: float | None,
    **kwargs: object,
) -> FoodVisionItem:
    return FoodVisionItem(
        visible_name=visible_name,
        normalized_name=normalized_name,
        confidence=float(kwargs.get("confidence", 0.9)),  # type: ignore[arg-type]
        estimated_grams_min=grams_min,
        estimated_grams_max=grams_max,
        preparation=str(kwargs.get("preparation", "")),
        is_sauce=bool(kwargs.get("is_sauce", False)),
        uncertainty=str(kwargs.get("uncertainty", "")),
    )


def _inventory(*items: FoodVisionItem, overall_confidence: float = 0.91) -> FoodVisionInventory:
    return FoodVisionInventory(
        schema_version=FOOD_VISION_SCHEMA_VERSION,
        items=list(items),
        overall_confidence=overall_confidence,
        needs_user_confirmation=False,
        warnings=[],
    )


def _diary(tmp_path) -> HealBiteNutritionDiary:
    return HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)


def _stage(diary: HealBiteNutritionDiary, user_id: int, *items: FoodVisionItem, image_ref: str = "telegram:1:1"):
    return diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=_inventory(*items),
        image_ref=image_ref,
        occurred_at=datetime.now(timezone.utc),
    )


# The seven components the vision model produced for the reproduction caption.
_VISION_ARTIFACT_PAYLOAD = {
    "schema_version": FOOD_VISION_SCHEMA_VERSION,
    "items": [
        {
            "visible_name": "Фарш куриный",
            "normalized_name": "фарш куриный",
            "confidence": 0.8,
            "estimated_grams_min": 20,
            "estimated_grams_max": 20,
            "preparation": "",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "Жира",
            "normalized_name": "жира",
            "confidence": 0.7,
            "estimated_grams_min": 5,
            "estimated_grams_max": 5,
            "preparation": "",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "160 грамм",
            "normalized_name": "160 грамм",
            "confidence": 0.7,
            "estimated_grams_min": None,
            "estimated_grams_max": None,
            "preparation": "",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "Грибы",
            "normalized_name": "грибы",
            "confidence": 0.8,
            "estimated_grams_min": 50,
            "estimated_grams_max": 50,
            "preparation": "",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "Пассата",
            "normalized_name": "пассата",
            "confidence": 0.8,
            "estimated_grams_min": 50,
            "estimated_grams_max": 50,
            "preparation": "",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "Лук репчатый",
            "normalized_name": "лук репчатый",
            "confidence": 0.8,
            "estimated_grams_min": 30,
            "estimated_grams_max": 30,
            "preparation": "",
            "is_sauce": False,
            "uncertainty": "",
        },
        {
            "visible_name": "Макароны твердых сортов пшеницы вареные",
            "normalized_name": "макароны твердых сортов пшеницы вареные",
            "confidence": 0.8,
            "estimated_grams_min": 160,
            "estimated_grams_max": 160,
            "preparation": "",
            "is_sauce": False,
            "uncertainty": "",
        },
    ],
    "overall_confidence": 0.9,
    "needs_user_confirmation": False,
    "warnings": [],
}


# --------------------------------------------------------------------------
# DEFECT-01 — caption parsing
# --------------------------------------------------------------------------


def test_caption_parsing_exact_telegram_reproduction():
    items = parse_meal_caption_items(CAPTION)

    assert len(items) == 5
    assert [item.name.casefold() for item in items] == _CAPTION_NAMES
    assert [item.grams for item in items] == [160.0, 50.0, 50.0, 30.0, 160.0]


def test_protein_and_fat_stay_metadata_not_ingredients():
    items = parse_meal_caption_items(CAPTION)

    assert len(items) == 5
    names = [item.name.casefold() for item in items]
    assert not any("жир" in name or "белк" in name for name in names)
    assert not any(name.replace(".", "").isdigit() for name in names)
    assert "160 грамм" not in names
    assert items[0].protein_g == pytest.approx(20.0)
    assert items[0].fat_g == pytest.approx(5.0)
    assert items[0].carbs_g is None
    # The macros describe the stated portion, never a per-100g base.
    assert items[0].grams == pytest.approx(160.0)


def test_caption_parsing_without_macros_keeps_plain_weights():
    items = parse_meal_caption_items("завтрак: овсянка 80 грамм, яблоко 150 грамм")

    assert [item.name.casefold() for item in items] == ["овсянка", "яблоко"]
    assert [item.grams for item in items] == [80.0, 150.0]
    assert items[0].protein_g is None


def test_vision_model_artifacts_are_reconciled_with_caption(tmp_path, monkeypatch):
    async def _fake_vision(image_url: str, user_prompt: str) -> str:
        assert "User note:" in user_prompt
        return json.dumps(
            {"success": True, "analysis": json.dumps(_VISION_ARTIFACT_PAYLOAD, ensure_ascii=False)}
        )

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", _fake_vision)

    diary = _diary(tmp_path)
    outcome = asyncio.run(
        diary.analyze_and_maybe_log(
            user_id=11,
            image_path="photo.jpg",
            user_text=CAPTION,
            source="photo",
        )
    )

    assert outcome.available is True
    pending = diary.get_pending_inventory(11)
    assert pending is not None
    assert len(pending.items) == 5
    assert [item.visible_name.casefold() for item in pending.items] == _CAPTION_NAMES
    assert [item.selected_grams for item in pending.items] == [160.0, 50.0, 50.0, 30.0, 160.0]
    assert pending.items[0].protein_g == pytest.approx(20.0)
    assert pending.items[0].fat_g == pytest.approx(5.0)
    # No ingredient may be a macro word or a bare weight.
    assert not any(item.visible_name.casefold() in {"жира", "белка"} for item in pending.items)
    assert all(item.visible_name.strip().replace(".", "").isdigit() is False for item in pending.items)


def test_photo_only_flow_is_unchanged_without_caption(tmp_path, monkeypatch):
    async def _fake_vision(image_url: str, user_prompt: str) -> str:
        return json.dumps(
            {"success": True, "analysis": json.dumps(_VISION_ARTIFACT_PAYLOAD, ensure_ascii=False)}
        )

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", _fake_vision)

    diary = _diary(tmp_path)
    asyncio.run(diary.analyze_and_maybe_log(user_id=12, image_path="photo.jpg", user_text="", source="photo"))

    pending = diary.get_pending_inventory(12)
    assert pending is not None
    # Without a caption the provider inventory is passed through untouched.
    assert len(pending.items) == 7


# --------------------------------------------------------------------------
# DEFECT-02 — corrections
# --------------------------------------------------------------------------


def test_parse_actions_accepts_colon_syntax_and_multiple_clauses():
    single = parse_pending_inventory_actions("Исправить: 1: Куриная грудка 160 грамм")
    assert len(single) == 1
    assert single[0].ok is True
    assert single[0].action is not None
    assert single[0].action.kind == "replace"
    assert single[0].action.index == 1
    assert single[0].action.name.casefold() == "куриная грудка"
    assert single[0].action.grams == pytest.approx(160.0)

    multiple = parse_pending_inventory_actions(
        "Исправить: 1: Куриная грудка 160 грамм, 2: Сыр 80 грамм"
    )
    assert len(multiple) == 2
    assert all(result.ok and result.action is not None for result in multiple)
    assert [result.action.index for result in multiple if result.action is not None] == [1, 2]


def test_edit_first_ingredient_through_reply_changes_weight(tmp_path):
    diary = _diary(tmp_path)
    _stage(
        diary,
        1,
        _food_item("куриная грудка", "куриная грудка", 100, 100),
        _food_item("сыр", "сыр", 50, 50),
    )

    result = diary.handle_pending_inventory_reply(1, "Исправить: 1: Куриная грудка 160 грамм")

    assert result.status == "updated"
    assert "Изменения сохранены" in result.reply_text
    updated = diary.get_pending_inventory(1)
    assert updated is not None
    assert updated.items[0].visible_name == "Куриная грудка"
    assert updated.items[0].selected_grams == pytest.approx(160.0)
    assert updated.items[0].user_modified is True
    assert updated.items[1].selected_grams == pytest.approx(50.0)


def test_multi_item_correction_in_one_message(tmp_path):
    diary = _diary(tmp_path)
    _stage(
        diary,
        2,
        _food_item("куриная грудка", "куриная грудка", 100, 100),
        _food_item("сыр", "сыр", 50, 50),
    )

    result = diary.handle_pending_inventory_reply(
        2, "Исправить: 1: Куриная грудка 160 грамм, 2: Сыр 80 грамм"
    )

    assert result.status == "updated"
    updated = diary.get_pending_inventory(2)
    assert updated is not None
    assert [item.selected_grams for item in updated.items] == [160.0, 80.0]
    assert [item.visible_name for item in updated.items] == ["Куриная грудка", "Сыр"]


def test_noop_correction_never_claims_success(tmp_path):
    diary = _diary(tmp_path)
    _stage(diary, 3, _food_item("куриная грудка", "куриная грудка", 100, 100))

    first = diary.handle_pending_inventory_reply(3, "Исправить: 1: Куриная грудка 160 грамм")
    second = diary.handle_pending_inventory_reply(3, "Исправить: 1: Куриная грудка 160 грамм")

    assert first.status == "updated"
    assert second.status == "unchanged"
    assert "Изменения сохранены" not in second.reply_text


def test_edit_rejects_out_of_range_index_without_false_success(tmp_path):
    diary = _diary(tmp_path)
    _stage(diary, 4, _food_item("куриная грудка", "куриная грудка", 100, 100))

    result = diary.handle_pending_inventory_reply(4, "Исправить: 5: Сыр 80 грамм")

    assert result.status == "invalid_action"
    assert "Изменения сохранены" not in result.reply_text
    unchanged = diary.get_pending_inventory(4)
    assert unchanged is not None
    assert [item.visible_name for item in unchanged.items] == ["куриная грудка"]


# --------------------------------------------------------------------------
# Recalculation, single save, isolation, albums
# --------------------------------------------------------------------------


def test_kbju_recalculation_after_weight_edit(tmp_path):
    diary = _diary(tmp_path)
    _stage(diary, 5, _food_item("куриная грудка", "куриная грудка", 100, 100))

    diary.handle_pending_inventory_reply(5, "Вес 1: 200 г")
    confirm = diary.handle_pending_inventory_reply(5, "да")

    assert confirm.status == "awaiting_save_confirmation"
    assert confirm.record is not None
    # Куриная грудка: 165 kcal / 31 g protein per 100 g, doubled to 200 g.
    assert confirm.record.calories_kcal == pytest.approx(330.0)
    assert confirm.record.protein_g == pytest.approx(62.0)
    assert confirm.record.items[0]["estimated_weight_g"] == pytest.approx(200.0)

    saved = diary.confirm_pending_meal(5)
    second = diary.confirm_pending_meal(5)
    summary = diary.get_daily_summary(user_id=5)

    assert saved.status == "saved"
    assert second.status == "missing"
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == pytest.approx(330.0)


def test_edit_is_user_isolated(tmp_path):
    diary = _diary(tmp_path)
    _stage(diary, 6, _food_item("куриная грудка", "куриная грудка", 100, 100))

    result = diary.handle_pending_inventory_reply(7, "Исправить: 1: Сыр 80 грамм")

    assert result.status == "missing"
    mine = diary.get_pending_inventory(6)
    assert mine is not None
    assert mine.items[0].visible_name == "куриная грудка"
    assert mine.items[0].selected_grams == pytest.approx(100.0)
    assert diary.get_pending_inventory(7) is None


def test_two_photo_album_second_inventory_supersedes_first(tmp_path):
    diary = _diary(tmp_path)
    first = _stage(diary, 8, _food_item("вафли", "вафли", 55, 65), image_ref="telegram:8:photo-1")
    second = _stage(diary, 8, _food_item("огурцы", "огурцы", 60, 70), image_ref="telegram:8:photo-2")

    assert first.inventory_id != second.inventory_id
    current = diary.get_pending_inventory(8)
    assert current is not None
    assert current.inventory_id == second.inventory_id
    assert [item.visible_name for item in current.items] == ["огурцы"]
