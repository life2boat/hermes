from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weight_tracker import HealBiteWeightTracker
from gateway.platforms.telegram import TelegramAdapter


def _message(*, text: str = "⚖️ Трекер веса", user_id: int = 101):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=555, type="private"),
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
        message_id=42,
        message_thread_id=None,
    )


def _query(*, user_id: int = 101, query_id: str = "weight-callback-1"):
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


def _profile_store(db_path, *, user_id: int = 101) -> HealBiteUserProfileStore:
    store = HealBiteUserProfileStore(db_path=db_path)
    store.upsert_user_profile(
        user_id=user_id,
        username="tester",
        sex="male",
        age=35,
        height_cm=180,
        weight_kg=80,
        goal="maintain",
        activity_level="moderate",
        manual_kcal_target=2000,
    )
    store.recalculate_profile_targets(user_id=user_id, target_source="manual", manual_kcal_target=2000)
    return store


def _patch_weight(monkeypatch, db_path, *, user_id: int = 101):
    tracker = HealBiteWeightTracker(db_path=db_path)
    store = _profile_store(db_path, user_id=user_id)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_weight_tracker", lambda: tracker)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    return tracker, store


@pytest.mark.asyncio
async def test_weight_keyboard_routes_to_local_tracker_without_generic_dispatch(tmp_path, monkeypatch):
    tracker, _store = _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()
    update = SimpleNamespace(update_id=1, message=_message(), effective_message=None)
    adapter._should_process_message = lambda msg, is_command=False: True

    handled = await adapter._maybe_handle_healbite_menu_button(update, SimpleNamespace())

    assert handled is True
    adapter._enqueue_text_event.assert_not_called()
    sent = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Вес" in sent["text"]
    assert sent.get("reply_markup") is not None
    assert tracker.get_summary(101).latest is None


@pytest.mark.asyncio
async def test_weight_command_with_value_saves_and_recalculates_profile(tmp_path, monkeypatch):
    tracker, store = _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()

    handled = await adapter._maybe_handle_healbite_weight_command(_message(text="/weight 82,4"))

    profile = store.get_user_profile(101)

    assert handled is True
    assert tracker.get_summary(101).latest.weight_grams == 82400
    assert profile.weight_kg == 82.4
    assert profile.daily_protein_g == 132
    adapter._enqueue_text_event.assert_not_called()
    assert "КБЖУ пересчитаны" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_weight_custom_pending_text_saves_locally_and_clears_state(tmp_path, monkeypatch):
    tracker, _store = _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()

    await adapter._handle_healbite_weight_callback(_query(), "weight:custom")
    handled = await adapter._maybe_handle_healbite_weight_pending_reply(_message(text="82,4 кг"))

    assert handled is True
    assert tracker.get_summary(101).latest.weight_grams == 82400
    assert tracker.get_pending_state(101) is None
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_weight_custom_invalid_input_stays_local_and_writes_nothing(tmp_path, monkeypatch):
    tracker, _store = _patch_weight(monkeypatch, tmp_path / "healbite.db")
    tracker.stage_custom_weight(101)
    adapter = _adapter()

    handled = await adapter._maybe_handle_healbite_weight_pending_reply(_message(text="много"))

    assert handled is True
    assert tracker.get_summary(101).latest is None
    assert tracker.get_pending_state(101) == "weight_custom_amount"
    adapter._enqueue_text_event.assert_not_called()
    assert "Не понял вес" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_weight_reminder_callback_toggles_without_logging_payload(tmp_path, monkeypatch, caplog):
    tracker, _store = _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()
    query = _query(query_id="PII_S70C_CALLBACK")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_healbite_weight_callback(query, "weight:reminder")

    assert tracker.get_weekly_reminder(101).enabled is True
    assert "route=weight" in caplog.text
    assert "PII_S70C_CALLBACK" not in caplog.text
    query.answer.assert_awaited_once()
