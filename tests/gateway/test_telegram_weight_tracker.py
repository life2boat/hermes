from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import logging
import re

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weight_tracker import HealBiteWeightTracker
from gateway.platforms.telegram import HEALBITE_REPLY_KEYBOARD_ACTIONS, TelegramAdapter


def _weight_button_label() -> str:
    for label, action in HEALBITE_REPLY_KEYBOARD_ACTIONS.items():
        if action == "/weight":
            return label
    raise AssertionError("weight button label missing")


def _message(*, text: str | None = None, user_id: int = 101):
    effective_text = text if text is not None else _weight_button_label()
    return SimpleNamespace(
        text=effective_text,
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
    assert sent.get("reply_markup") is not None
    buttons = [button.text for row in sent["reply_markup"].inline_keyboard for button in row]
    assert "Напоминание" not in " ".join(buttons)
    assert tracker.get_summary(101).latest is None


@pytest.mark.asyncio
async def test_weight_command_with_value_saves_and_recalculates_profile(tmp_path, monkeypatch):
    tracker, store = _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()

    handled = await adapter._maybe_handle_healbite_weight_command(_message(text="/weight 82,4"))

    profile = store.get_user_profile(101)

    assert handled is True
    assert tracker.get_summary(101).latest.weight_grams == 82400
    assert profile is not None and profile.weight_kg == 82.4
    assert profile.daily_protein_g == 132
    adapter._enqueue_text_event.assert_not_called()
    assert adapter._send_message_with_thread_fallback.await_args.kwargs["text"]


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
    assert "/cancel" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_weight_stale_reminder_callback_stays_placeholder_without_logging_payload(tmp_path, monkeypatch, caplog):
    tracker, _store = _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()
    query = _query(query_id="PII_S70C_CALLBACK")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_healbite_weight_callback(query, "weight:reminder")

    assert tracker.get_summary(101).latest is None
    assert tracker.get_pending_state(101) is None
    assert "route=weight" in caplog.text
    assert "PII_S70C_CALLBACK" not in caplog.text
    assert query.edit_message_text.await_args.kwargs["text"]
    query.answer.assert_awaited_once()



@pytest.mark.asyncio
async def test_weight_command_core_marker_uses_same_correlation_as_route_marker(tmp_path, monkeypatch, caplog):
    _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()
    message = _message(text="/weight 82,4")

    with caplog.at_level(logging.INFO):
        await adapter._maybe_handle_healbite_weight_command(message)

    route_log = next(record.getMessage() for record in caplog.records if "[Telegram][healbite_route_selected]" in record.getMessage() and "route=weight" in record.getMessage())
    weight_log = next(record.getMessage() for record in caplog.records if "[HealBite][weight_record]" in record.getMessage())
    route_corr = re.search(r"corr=([0-9a-f]+)", route_log)
    weight_corr = re.search(r"corr=([0-9a-f]+)", weight_log)

    assert route_corr is not None
    assert weight_corr is not None
    assert route_corr.group(1) == weight_corr.group(1)
    assert "corr_present=true" in weight_log


@pytest.mark.asyncio
async def test_weight_command_correlation_differs_between_turns(tmp_path, monkeypatch, caplog):
    _patch_weight(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter()

    with caplog.at_level(logging.INFO):
        await adapter._maybe_handle_healbite_weight_command(_message(text="/weight 82,4"))
        await adapter._maybe_handle_healbite_weight_command(_message(text="/weight 82,5"))

    weight_logs = [record.getMessage() for record in caplog.records if "[HealBite][weight_record]" in record.getMessage()]
    assert len(weight_logs) >= 2
    first_corr = re.search(r"corr=([0-9a-f]+)", weight_logs[0])
    second_corr = re.search(r"corr=([0-9a-f]+)", weight_logs[1])
    assert first_corr is not None and second_corr is not None
    assert first_corr.group(1) != second_corr.group(1)
