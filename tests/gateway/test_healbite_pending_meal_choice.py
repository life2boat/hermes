"""Regression tests for HEALBITE R1 pending-meal choice and save keyboard (P0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.healbite_nutrition_diary import (
    ALBUM_OVERLAP_UNRESOLVED,
    HealBiteNutritionDiary,
    build_food_vision_inventory_keyboard,
    build_pending_inventory_choice_keyboard,
)
from gateway.run import GatewayRunner

CAPTION = (
    "Обед — фарш куриный (20 грамм белка, 5 грамм жира), 160 грамм, "
    "грибы 50 грамм, пассата 50 грамм, лук репчатый 30 грамм, "
    "макароны твёрдых сортов пшеницы варёные 160 грамм"
)
NEW_SNACK = "На перекус съел один банан среднего размера. Точный вес не знаю"


def _diary(tmp_path) -> HealBiteNutritionDiary:
    return HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)


def _source(user_id: int = 700):
    return SimpleNamespace(platform=Platform.TELEGRAM, user_id=str(user_id), chat_id=f"chat-{user_id}")


def _event(text: str):
    return SimpleNamespace(text=text, message_type=None, media_types=[])


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


def _runner(diary: HealBiteNutritionDiary) -> tuple[GatewayRunner, _FakeAdapter]:
    runner = object.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    adapter = _FakeAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}
    return runner, adapter


def _flatten(markup_rows) -> list[dict]:
    return [button for row in markup_rows for button in row]


# --------------------------------------------------------------------------
# New meal must not be merged into the open dish
# --------------------------------------------------------------------------


def test_new_meal_text_does_not_merge_into_pending(tmp_path):
    diary = _diary(tmp_path)
    staged = diary.stage_pending_inventory_from_text(user_id=1, text=CAPTION)
    assert staged is not None and len(staged.items) == 5

    result = diary.offer_pending_inventory_choice(1, NEW_SNACK)

    assert result.status == "choice_required"
    pending = diary.get_pending_inventory(1)
    assert pending is not None
    assert len(pending.items) == 5  # banana was not appended
    assert all("банан" not in item.visible_name.casefold() for item in pending.items)
    assert pending.deferred_new_meal_text == NEW_SNACK
    assert "Новый приём" in result.reply_text


def test_continue_choice_keeps_pending_and_clears_deferred(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=2, text=CAPTION)
    diary.offer_pending_inventory_choice(2, NEW_SNACK)

    result = diary.resolve_pending_inventory_choice(2, "continue")

    assert result.status == "updated"
    pending = diary.get_pending_inventory(2)
    assert pending is not None
    assert len(pending.items) == 5
    assert pending.deferred_new_meal_text is None


def test_new_meal_choice_starts_the_deferred_meal(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=3, text=CAPTION)
    diary.offer_pending_inventory_choice(3, NEW_SNACK)

    result = diary.resolve_pending_inventory_choice(3, "new_meal")

    assert result.status == "new_meal_staged"
    pending = diary.get_pending_inventory(3)
    assert pending is not None
    assert len(pending.items) == 1
    assert "банан" in pending.items[0].visible_name.casefold()


def test_cancel_choice_clears_pending_but_keeps_saved_history(tmp_path):
    diary = _diary(tmp_path)
    # Save one meal first so history exists independently of the pending dish.
    from gateway.healbite_nutrition_diary import NutritionRecord

    diary.save_record(
        user_id=4,
        source="text",
        record=NutritionRecord(
            is_food=True,
            meal_name="Гречка",
            items=[],
            calories_kcal=310.0,
            protein_g=11.0,
            fat_g=7.0,
            carbs_g=49.0,
            confidence=1.0,
            raw_summary="Гречка",
        ),
        image_ref=None,
        occurred_at=None,
    )
    diary.stage_pending_inventory_from_text(user_id=4, text=CAPTION)

    result = diary.resolve_pending_inventory_choice(4, "cancel")

    assert result.status == "cancelled"
    assert diary.get_pending_inventory(4) is None
    summary = diary.get_daily_summary(user_id=4)
    assert summary["entry_count"] == 1
    assert summary["entries"][0]["meal_name"] == "Гречка"


def test_explicit_edit_clears_deferred_new_meal(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=5, text=CAPTION)
    diary.offer_pending_inventory_choice(5, NEW_SNACK)

    result = diary.apply_pending_inventory_action(5, _weight_action(1, 180.0))

    assert result.status == "updated"
    pending = diary.get_pending_inventory(5)
    assert pending is not None
    assert pending.items[0].selected_grams == pytest.approx(180.0)
    assert pending.deferred_new_meal_text is None


def _weight_action(index: int, grams: float):
    from gateway.healbite_nutrition_diary import PendingInventoryAction

    return PendingInventoryAction(kind="weight", index=index, grams=grams)


def test_choice_is_user_isolated(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=6, text=CAPTION)

    result = diary.offer_pending_inventory_choice(7, NEW_SNACK)

    assert result.status == "missing"
    pending = diary.get_pending_inventory(6)
    assert pending is not None
    assert pending.deferred_new_meal_text is None


# --------------------------------------------------------------------------
# Keyboard availability after edits / choices
# --------------------------------------------------------------------------


def test_choice_keyboard_exposes_three_explicit_actions(tmp_path):
    diary = _diary(tmp_path)
    staged = diary.stage_pending_inventory_from_text(user_id=8, text=CAPTION)
    assert staged is not None

    buttons = _flatten(build_pending_inventory_choice_keyboard(staged))
    callbacks = [button["callback_data"] for button in buttons]

    assert any("choice_continue" in cb for cb in callbacks)
    assert any("choice_newmeal" in cb for cb in callbacks)
    assert any("choice_cancel" in cb for cb in callbacks)


@pytest.mark.asyncio
async def test_edit_reply_carries_save_keyboard(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=9, text=CAPTION)
    runner, adapter = _runner(diary)

    result = await runner._maybe_handle_healbite_nutrition_diary_turn(
        message="Вес 1: 180 г",
        history=[],
        source=_source(9),
        session_id="s-9",
        event=_event("Вес 1: 180 г"),
    )

    assert result is not None
    await asyncio.sleep(0.05)
    assert adapter.sent, "edit reply must be sent with a keyboard"
    markup = _flatten(adapter.sent[-1]["metadata"].get("reply_markup") or [])
    texts = [button["text"] for button in markup]
    assert any("Сохранить" in text for text in texts)
    assert any("Исправить" in text for text in texts)


@pytest.mark.asyncio
async def test_new_meal_text_gets_choice_keyboard(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=10, text=CAPTION)
    runner, adapter = _runner(diary)

    result = await runner._maybe_handle_healbite_nutrition_diary_turn(
        message=NEW_SNACK,
        history=[],
        source=_source(10),
        session_id="s-10",
        event=_event(NEW_SNACK),
    )

    assert result is not None
    await asyncio.sleep(0.05)
    assert adapter.sent, "choice reply must be sent with a keyboard"
    callbacks = [button["callback_data"] for button in _flatten(adapter.sent[-1]["metadata"].get("reply_markup") or [])]
    assert any("choice_newmeal" in cb for cb in callbacks)
    pending = diary.get_pending_inventory(10)
    assert pending is not None and len(pending.items) == 5


def test_album_keyboard_regression_unchanged(tmp_path):
    diary = _diary(tmp_path)
    staged = diary.stage_pending_inventory_from_text(user_id=11, text=CAPTION)
    assert staged is not None

    staged.album_overlap_decision = ALBUM_OVERLAP_UNRESOLVED
    buttons = _flatten(build_food_vision_inventory_keyboard(staged))

    assert any("resolve_same" in b["callback_data"] for b in buttons)
    assert any("resolve_distinct" in b["callback_data"] for b in buttons)


# --------------------------------------------------------------------------
# Save path keeps calculated values and never duplicates
# --------------------------------------------------------------------------


def test_save_after_edit_persists_corrected_weight_and_totals(tmp_path):
    diary = _diary(tmp_path)
    staged = diary.stage_pending_inventory_from_text(user_id=12, text="Куриная грудка 100 г")
    assert staged is not None

    diary.apply_pending_inventory_action(12, _weight_action(1, 180.0))
    saved = diary.save_pending_inventory_directly(12)

    assert saved.status in {"saved", "saved_without_kbzhu"}
    summary = diary.get_daily_summary(user_id=12)
    assert summary["entry_count"] == 1
    entry = summary["entries"][0]
    assert entry["items"][0]["estimated_weight_g"] == pytest.approx(180.0)
    assert entry["calories_kcal"] == pytest.approx(297.0)


def test_repeated_save_does_not_duplicate(tmp_path):
    diary = _diary(tmp_path)
    diary.stage_pending_inventory_from_text(user_id=13, text="Куриная грудка 100 г")

    first = diary.save_pending_inventory_directly(13)
    second = diary.save_pending_inventory_directly(13)

    assert first.status in {"saved", "saved_without_kbzhu"}
    assert second.status == "missing"
    assert diary.get_daily_summary(user_id=13)["entry_count"] == 1
