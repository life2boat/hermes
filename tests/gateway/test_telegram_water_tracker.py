from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_water_tracker import HealBiteWaterTracker
from gateway.platforms.telegram import TelegramAdapter


def _seed_water_target(db_path, *, user_id: int = 101, target_ml: int = 2200) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE profiles (
                telegram_id INTEGER PRIMARY KEY,
                water_target_ml INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO profiles(telegram_id, water_target_ml) VALUES (?, ?)",
            (user_id, target_ml),
        )
        conn.commit()


def _message(*, text: str = "💧 Трекер воды", user_id: int = 101):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=555, type="private"),
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
        message_id=42,
        message_thread_id=None,
    )


def _query(*, user_id: int = 101, query_id: str = "callback-1"):
    message = SimpleNamespace(
        chat_id=555,
        chat=SimpleNamespace(id=555, type="private"),
        message_thread_id=None,
    )
    return SimpleNamespace(
        id=query_id,
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
        message=message,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={}))
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    return adapter


@pytest.mark.asyncio
async def test_water_keyboard_routes_to_local_tracker_without_generic_dispatch(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _seed_water_target(db_path)
    tracker = HealBiteWaterTracker(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
    adapter = _adapter()

    handled = await adapter._dispatch_healbite_keyboard_action(_message(), action="/water")

    assert handled is True
    adapter._enqueue_text_event.assert_not_called()
    sent = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Вода сегодня" in sent["text"]
    assert "2 200 мл" in sent["text"]
    assert sent.get("reply_markup") is not None


@pytest.mark.asyncio
async def test_water_callback_add_is_idempotent_and_answers_callback(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _seed_water_target(db_path)
    tracker = HealBiteWaterTracker(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
    adapter = _adapter()
    query = _query(query_id="same-callback")

    await adapter._handle_healbite_water_callback(query, "water:add:250")
    await adapter._handle_healbite_water_callback(query, "water:add:250")

    assert tracker.get_water_intake_today(101) == 250
    assert query.answer.await_count == 2
    assert query.edit_message_text.await_count == 2
    assert "Вода сегодня" in query.edit_message_text.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_water_custom_pending_text_saves_locally_and_clears_state(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _seed_water_target(db_path)
    tracker = HealBiteWaterTracker(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
    adapter = _adapter()

    await adapter._handle_healbite_water_callback(_query(), "water:custom")
    handled = await adapter._maybe_handle_healbite_water_pending_reply(_message(text="0,5 л"))

    assert handled is True
    assert tracker.get_water_intake_today(101) == 500
    assert tracker.get_pending_state(101) is None
    adapter._enqueue_text_event.assert_not_called()
    sent = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Добавил 500 мл" in sent["text"]


@pytest.mark.asyncio
async def test_water_custom_invalid_input_stays_local_and_writes_nothing(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _seed_water_target(db_path)
    tracker = HealBiteWaterTracker(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
    adapter = _adapter()
    tracker.stage_custom_amount(101)

    handled = await adapter._maybe_handle_healbite_water_pending_reply(_message(text="стакан"))

    assert handled is True
    assert tracker.get_water_intake_today(101) == 0
    assert tracker.get_pending_state(101) == "water_custom_amount"
    adapter._enqueue_text_event.assert_not_called()
    assert "Не понял объём" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_water_undo_callback_removes_only_latest_current_user_entry(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _seed_water_target(db_path)
    tracker = HealBiteWaterTracker(db_path=db_path)
    tracker.add_water_intake(101, 250, idempotency_key="one")
    tracker.add_water_intake(101, 500, idempotency_key="two")
    tracker.add_water_intake(202, 700, idempotency_key="other")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_water_tracker", lambda: tracker)
    adapter = _adapter()

    await adapter._handle_healbite_water_callback(_query(user_id=101, query_id="undo-1"), "water:undo")

    assert tracker.get_water_intake_today(101) == 250
    assert tracker.get_water_intake_today(202) == 700
