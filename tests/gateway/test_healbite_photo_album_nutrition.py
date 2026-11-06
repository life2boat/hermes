"""Regression tests for HEALBITE R1 photo/album nutrition accuracy."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from gateway.healbite_nutrition_diary import (
    ALBUM_PARTIAL_PROMPT,
    ALBUM_PROCESSING_PARTIAL,
    FOOD_VISION_SCHEMA_VERSION,
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    _reconcile_multi_photo_inventories,
    build_food_vision_inventory_keyboard,
    calculate_inventory_nutrition,
    format_food_vision_inventory_reply,
)


def _item(name: str, gmin=None, gmax=None, *, sauce: bool = False, conf: float = 0.8) -> FoodVisionItem:
    return FoodVisionItem(
        visible_name=name,
        normalized_name=name.casefold(),
        confidence=conf,
        estimated_grams_min=gmin,
        estimated_grams_max=gmax,
        preparation="",
        is_sauce=sauce,
        uncertainty="",
    )


def _inventory(*items: FoodVisionItem) -> FoodVisionInventory:
    return FoodVisionInventory(
        schema_version=FOOD_VISION_SCHEMA_VERSION,
        items=list(items),
        overall_confidence=0.8,
        needs_user_confirmation=False,
        warnings=[],
    )


def _diary(tmp_path) -> HealBiteNutritionDiary:
    return HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)


def _stage(diary, user_id, *items, photo_count=1, proc="COMPLETE", decision=None, overlap="NONE", image_ref="telegram:1:1"):
    return diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=_inventory(*items),
        image_ref=image_ref,
        occurred_at=datetime.now(timezone.utc),
        photo_count=photo_count,
        album_decision=decision,
        album_raw_photo_items=None,
        album_processing_status=proc,
        album_overlap_decision=overlap,
    )


def _vision_payload(names, grams=175.0):
    return {
        "schema_version": FOOD_VISION_SCHEMA_VERSION,
        "items": [
            {
                "visible_name": name,
                "normalized_name": name.casefold(),
                "confidence": 0.8,
                "estimated_grams_min": grams,
                "estimated_grams_max": grams,
                "preparation": "",
                "is_sauce": False,
                "uncertainty": "",
            }
            for name in names
        ],
        "overall_confidence": 0.8,
        "needs_user_confirmation": False,
        "warnings": [],
    }


def _flatten(rows):
    return [button for row in rows for button in row]


# --------------------------------------------------------------------------
# Defect 1 — component weights
# --------------------------------------------------------------------------


def test_album_without_scale_gives_each_component_its_own_estimate(tmp_path):
    diary = _diary(tmp_path)
    names = ["Курица терияки", "Гёдза", "Рис", "Соус терияки", "Салат", "Помидор", "Зелень", "Чили", "Специи"]

    pending = _stage(diary, 1, *[ _item(name) for name in names ], photo_count=2)

    centers = [(float(item.estimated_grams_min) + float(item.estimated_grams_max)) / 2.0 for item in pending.items]
    assert len(centers) == len(names)
    assert len(set(centers)) > 1, "every component must not receive the same estimate"
    assert centers != [180.0] * len(names)
    for item in pending.items:
        assert item.estimated_grams_min is not None and item.estimated_grams_max is not None
        assert item.estimated_grams_min < item.estimated_grams_max, "uncertainty must stay visible"
        assert item.selected_grams is None, "an inferred range must not be presented as a confirmed weight"
        assert "типовой порции" in item.uncertainty
    assert sum(centers) <= 1200.0


def test_herbs_spices_and_chili_do_not_get_full_portion(tmp_path):
    diary = _diary(tmp_path)
    pending = _stage(diary, 2, _item("Зелень"), _item("Чили"), _item("Специи"), _item("Гарнир"))

    by_name = {item.visible_name.casefold(): item for item in pending.items}
    for name in ("зелень", "чили", "специи"):
        item = by_name[name]
        center = (float(item.estimated_grams_min) + float(item.estimated_grams_max)) / 2.0
        assert center <= 40.0
    garnish = by_name["гарнир"]
    assert (float(garnish.estimated_grams_min) + float(garnish.estimated_grams_max)) / 2.0 <= 180.0


def test_model_weights_are_not_overwritten(tmp_path):
    diary = _diary(tmp_path)
    pending = _stage(diary, 3, _item("Рис", 175, 175), _item("Курица терияки", 175, 175))

    assert [item.selected_grams for item in pending.items] == [175.0, 175.0]
    assert [item.uncertainty for item in pending.items] == ["", ""]


# --------------------------------------------------------------------------
# Defect 2 — partial album
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_album_reports_limitation_and_blocks_plain_save(tmp_path, monkeypatch):
    async def _fake_vision(image_url: str, user_prompt: str):
        if image_url.endswith("2.jpg"):
            return json.dumps({"success": False, "diagnostic": {"retryable": False}})
        return json.dumps({"success": True, "analysis": json.dumps(_vision_payload(["Рис", "Курица терияки"]))})

    monkeypatch.setattr("tools.vision_tools.vision_analyze_tool", _fake_vision)
    diary = _diary(tmp_path)

    outcome = await diary.analyze_and_maybe_log(
        user_id=4,
        image_paths=["1.jpg", "2.jpg"],
        source="vision",
        image_ref="telegram:4:album",
    )

    pending = diary.get_pending_inventory(4)
    assert pending is not None
    assert pending.album_processing_status == ALBUM_PROCESSING_PARTIAL
    assert any("1 из 2" in warning for warning in pending.warnings)
    assert "1 из 2" in (outcome.clarification_text or "")

    buttons = _flatten(build_food_vision_inventory_keyboard(pending))
    callbacks = [button["callback_data"] for button in buttons]
    assert any("save_partial" in cb for cb in callbacks)
    assert not any(cb.endswith(":save:") for cb in callbacks)

    blocked = diary.save_pending_inventory_directly(4)
    assert blocked.status == "album_partial_blocked"
    assert blocked.reply_text == ALBUM_PARTIAL_PROMPT


def test_partial_album_uses_only_confirmed_photo_result(tmp_path):
    diary = _diary(tmp_path)
    partial = _stage(diary, 5, _item("Рис", 175, 175), photo_count=2, proc=ALBUM_PROCESSING_PARTIAL, decision="PARTIAL")

    assert len(partial.items) == 1
    assert partial.album_processing_status == ALBUM_PROCESSING_PARTIAL


def test_same_dish_album_is_one_portion_not_doubled():
    photo_a = [_item("Курица", 150, 150), _item("Рис", 175, 175)]
    photo_b = [_item("Курица", 140, 140), _item("Рис", 170, 170)]

    result = _reconcile_multi_photo_inventories(
        [photo_a, photo_b], user_text="одна и та же порция"
    )
    merged, warnings, requires_confirmation = result

    assert len(merged) == 2
    assert not any(w == ALBUM_PARTIAL_PROMPT for w in warnings)
    assert result.album_decision == "SAME_PORTION"


# --------------------------------------------------------------------------
# Defect 3 — incomplete nutrition
# --------------------------------------------------------------------------


def test_unknown_component_is_not_zeroed_and_total_is_partial(tmp_path):
    diary = _diary(tmp_path)
    pending = _stage(
        diary,
        6,
        _item("Рис", 175, 175),
        _item("Гёдза", 140, 140),
        _item("Курица терияки", 175, 175),
        _item("Помидор", 12, 12),
    )

    calculation = calculate_inventory_nutrition(pending)
    by_name = {component.name: component for component in calculation.components}

    assert by_name["Гёдза"].nutrition_available is False
    assert by_name["Гёдза"].calories_kcal is None, "unknown nutrition must stay unknown, not zero"
    assert calculation.status == "UNKNOWN_COMPONENT_BLOCKED"
    reply = format_food_vision_inventory_reply(pending)
    assert "Итого (частично)" in reply
    assert "КБЖУ не найдено" in reply


def test_totals_equal_component_sum_without_double_counting(tmp_path):
    diary = _diary(tmp_path)
    pending = _stage(
        diary,
        7,
        _item("Рис", 175, 175),
        _item("Курица терияки", 175, 175),
        _item("Помидор", 12, 12),
    )

    calculation = calculate_inventory_nutrition(pending)
    assert calculation.record is not None
    expected = round(sum(component.calories_kcal for component in calculation.components), 1)
    assert calculation.record.calories_kcal == pytest.approx(expected, abs=0.05)


def test_small_tomato_weight_has_small_energy(tmp_path):
    diary = _diary(tmp_path)
    pending = _stage(diary, 8, _item("Помидор", 12, 12))

    calculation = calculate_inventory_nutrition(pending)
    component = calculation.components[0]
    assert component.calories_kcal is not None
    assert component.calories_kcal <= 5.0, "12 g of tomato cannot be ~28 kcal"


# --------------------------------------------------------------------------
# Save / diary consistency and regressions
# --------------------------------------------------------------------------


def test_single_confirmation_saves_one_entry_matching_estimate(tmp_path):
    diary = _diary(tmp_path)
    _stage(diary, 9, _item("Рис", 175, 175), _item("Курица терияки", 175, 175))

    saved = diary.save_pending_inventory_directly(9)
    second = diary.save_pending_inventory_directly(9)

    assert saved.status in {"saved", "saved_without_kbzhu"}
    assert second.status == "missing"
    summary = diary.get_daily_summary(user_id=9)
    assert summary["entry_count"] == 1
    entry = summary["entries"][0]
    assert entry["calories_kcal"] == pytest.approx(saved.record.calories_kcal)
    assert [item["estimated_weight_g"] for item in entry["items"]] == [175.0, 175.0]


def test_text_meal_weights_stay_exact(tmp_path):
    diary = _diary(tmp_path)
    pending = diary.stage_pending_inventory_from_text(user_id=10, text="Гречка 120 г, курица 150 г")

    assert pending is not None
    assert [item.selected_grams for item in pending.items] == [120.0, 150.0]
    assert all("типовой порции" not in item.uncertainty for item in pending.items)


def test_pending_choice_regression_from_pr488(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=11, text="Гречка 120 г, курица 150 г")

    offer = diary.offer_pending_inventory_choice(11, "На перекус съел банан")

    assert offer.status == "choice_required"
    pending = diary.get_pending_inventory(11)
    assert pending is not None
    assert len(pending.items) == 2
