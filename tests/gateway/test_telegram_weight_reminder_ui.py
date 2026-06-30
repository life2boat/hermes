from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weight_reminders import (
    ReminderDeliveryState,
    HealBiteWeightReminderStore,
    WEIGHT_REMINDER_DELIVERIES_TABLE,
    WEIGHT_REMINDER_SETTINGS_TABLE,
)
from gateway.healbite_weight_tracker import HealBiteWeightTracker
from gateway.platforms.telegram import TelegramAdapter
from gateway import healbite_weight_reminder_ui as reminder_ui


USER_ID = 101
OTHER_USER_ID = 202


class FakeButton:
    def __init__(self, text, callback_data):
        self.text = text
        self.callback_data = callback_data


class FakeMarkup:
    def __init__(self, rows):
        self.inline_keyboard = rows


def _message(*, user_id: int = USER_ID):
    return SimpleNamespace(
        chat_id=555,
        chat=SimpleNamespace(id=555, type="private"),
        message_thread_id=None,
        text="",
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
    )


def _query(*, user_id: int = USER_ID, query_id: str = "reminder-callback-1"):
    return SimpleNamespace(
        id=query_id,
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester"),
        message=_message(user_id=user_id),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _adapter(monkeypatch):
    monkeypatch.setattr("gateway.platforms.telegram.TELEGRAM_AVAILABLE", True)
    monkeypatch.setattr("gateway.platforms.telegram.InlineKeyboardButton", FakeButton)
    monkeypatch.setattr("gateway.platforms.telegram.InlineKeyboardMarkup", FakeMarkup)
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token", extra={}))
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    return adapter


def _button_texts(markup) -> list[str]:
    return [button.text for row in getattr(markup, "inline_keyboard", []) for button in row]


def _button_callbacks(markup) -> list[str]:
    return [button.callback_data for row in getattr(markup, "inline_keyboard", []) for button in row]


def _setup_stores(monkeypatch, db_path):
    profile = HealBiteUserProfileStore(db_path=db_path)
    for user_id in (USER_ID, OTHER_USER_ID):
        profile.upsert_user_profile(
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
        profile.recalculate_profile_targets(user_id=user_id, target_source="manual", manual_kcal_target=2000)
    tracker = HealBiteWeightTracker(db_path=db_path)
    reminder = HealBiteWeightReminderStore(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: profile)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: profile)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_weight_tracker", lambda: tracker)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_weight_reminder_store", lambda: reminder)
    return profile, tracker, reminder


def _enable(monkeypatch, *, allowlist: str | None = str(USER_ID)):
    monkeypatch.setenv("WEIGHT_REMINDERS_ENABLED", "true")
    if allowlist is None:
        monkeypatch.delenv("WEIGHT_REMINDERS_ALLOWLIST", raising=False)
    else:
        monkeypatch.setenv("WEIGHT_REMINDERS_ALLOWLIST", allowlist)


async def _select_default_schedule(adapter: TelegramAdapter, query):
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:start")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:tz:berlin")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:day:1")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:hour:09")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:minute:15")


def _counts(db_path):
    with sqlite3.connect(db_path) as conn:
        settings = conn.execute(f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_SETTINGS_TABLE}").fetchone()[0]
        deliveries = conn.execute(f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_DELIVERIES_TABLE}").fetchone()[0]
    return settings, deliveries


def test_feature_off_and_empty_allowlist_hide_reminder_button(tmp_path, monkeypatch):
    _setup_stores(monkeypatch, tmp_path / "healbite.db")
    adapter = _adapter(monkeypatch)

    monkeypatch.delenv("WEIGHT_REMINDERS_ENABLED", raising=False)
    monkeypatch.delenv("WEIGHT_REMINDERS_ALLOWLIST", raising=False)
    assert "Напоминание" not in _button_texts(adapter._healbite_weight_keyboard(user_id=USER_ID))

    _enable(monkeypatch, allowlist=None)
    assert "Напоминание" not in _button_texts(adapter._healbite_weight_keyboard(user_id=USER_ID))


@pytest.mark.asyncio
async def test_forged_callback_when_not_allowlisted_does_not_write(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch, allowlist=str(OTHER_USER_ID))
    adapter = _adapter(monkeypatch)
    query = _query()

    await adapter._handle_healbite_weight_callback(query, "weight:reminder:start")

    assert _counts(db_path) == (0, 0)
    query.answer.assert_awaited()
    assert "Напоминание" not in _button_texts(query.edit_message_text.await_args.kwargs["reply_markup"])
    adapter._enqueue_text_event.assert_not_called()


def test_allowlisted_user_sees_reminder_button(tmp_path, monkeypatch):
    _setup_stores(monkeypatch, tmp_path / "healbite.db")
    _enable(monkeypatch)
    adapter = _adapter(monkeypatch)

    buttons = _button_texts(adapter._healbite_weight_keyboard(user_id=USER_ID))

    assert "Напоминание" in buttons


@pytest.mark.asyncio
async def test_first_opt_in_uses_draft_and_writes_only_on_confirm(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _profile, _tracker, store = _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    adapter = _adapter(monkeypatch)
    query = _query()

    await _select_default_schedule(adapter, query)

    assert _counts(db_path) == (0, 0)
    assert USER_ID in adapter._weight_reminder_drafts
    review_buttons = _button_texts(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert "Включить" in review_buttons
    assert "Изменить день" in review_buttons
    assert "Изменить время" in review_buttons
    assert "Изменить часовой пояс" in review_buttons

    await adapter._handle_healbite_weight_callback(query, "weight:reminder:confirm")

    setting = store.get_settings(USER_ID)
    assert setting is not None
    assert setting.enabled is True
    assert setting.delivery_state is ReminderDeliveryState.ACTIVE
    assert setting.timezone_name == "Europe/Berlin"
    assert setting.weekday == 1
    assert setting.local_time == "09:15"
    assert setting.next_due_at_utc is not None
    assert _counts(db_path) == (1, 0)
    assert USER_ID not in adapter._weight_reminder_drafts
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_expired_draft_is_rejected_without_write(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    adapter = _adapter(monkeypatch)
    query = _query()

    await _select_default_schedule(adapter, query)
    adapter._weight_reminder_drafts[USER_ID].updated_at_utc = datetime.now(timezone.utc) - timedelta(minutes=31)
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:confirm")

    assert USER_ID not in adapter._weight_reminder_drafts
    assert _counts(db_path) == (0, 0)


@pytest.mark.asyncio
async def test_review_edit_actions_preserve_draft_without_write(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    adapter = _adapter(monkeypatch)
    query = _query()

    await _select_default_schedule(adapter, query)
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:weekday")
    assert adapter._weight_reminder_drafts[USER_ID].step == "weekday"
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:time")
    assert adapter._weight_reminder_drafts[USER_ID].step == "hour"
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:timezone")
    assert adapter._weight_reminder_drafts[USER_ID].step == "timezone"
    assert _counts(db_path) == (0, 0)


@pytest.mark.asyncio
async def test_cancel_and_back_do_not_write(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    adapter = _adapter(monkeypatch)
    query = _query()

    await adapter._handle_healbite_weight_callback(query, "weight:reminder:start")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:tz:utc")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:back")
    assert adapter._weight_reminder_drafts[USER_ID].step == "timezone"

    await adapter._handle_healbite_weight_callback(query, "weight:reminder:cancel")

    assert USER_ID not in adapter._weight_reminder_drafts
    assert _counts(db_path) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    "weight:reminder:tz:badzone",
    "weight:reminder:day:9",
    "weight:reminder:hour:24",
    "weight:reminder:minute:17",
    "weight:reminder:unknown:payload",
])
async def test_forged_or_invalid_callbacks_do_not_write(tmp_path, monkeypatch, payload):
    db_path = tmp_path / "healbite.db"
    _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    adapter = _adapter(monkeypatch)
    query = _query()

    await adapter._handle_healbite_weight_callback(query, payload)

    assert _counts(db_path) == (0, 0)
    adapter._enqueue_text_event.assert_not_called()


@pytest.mark.asyncio
async def test_stale_edit_is_rejected_by_schedule_version(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _profile, _tracker, store = _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    original = store.create_or_update_settings(
        user_id=USER_ID,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
        enabled=True,
    )
    adapter = _adapter(monkeypatch)
    query = _query()

    await _select_default_schedule(adapter, query)
    store.create_or_update_settings(
        user_id=USER_ID,
        timezone_name="UTC",
        weekday=2,
        local_time="10:00",
        enabled=True,
    )
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:confirm")

    setting = store.get_settings(USER_ID)
    assert setting is not None
    assert setting.schedule_version == original.schedule_version + 1
    assert setting.weekday == 2
    assert setting.local_time == "10:00"
    assert setting.timezone_name == "UTC"


@pytest.mark.asyncio
async def test_disable_and_resume_require_confirmation_and_are_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _profile, _tracker, store = _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    store.create_or_update_settings(
        user_id=USER_ID,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
        enabled=True,
    )
    adapter = _adapter(monkeypatch)
    query = _query()

    await adapter._handle_healbite_weight_callback(query, "weight:reminder:disable")
    assert store.get_settings(USER_ID).enabled is True
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:disable_confirm")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:disable_confirm")
    assert store.get_settings(USER_ID).enabled is False
    assert store.get_settings(USER_ID).next_due_at_utc is None

    store.suspend_delivery(USER_ID, safe_reason="manual_suspend")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:resume")
    assert store.get_settings(USER_ID).delivery_state is ReminderDeliveryState.SUSPENDED
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:resume_confirm")
    setting = store.get_settings(USER_ID)
    assert setting.enabled is True
    assert setting.delivery_state is ReminderDeliveryState.ACTIVE
    assert setting.next_due_at_utc is not None


@pytest.mark.asyncio
async def test_user_isolation_and_duplicate_confirm(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _profile, _tracker, store = _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch, allowlist=f"{USER_ID},{OTHER_USER_ID}")
    adapter = _adapter(monkeypatch)

    await _select_default_schedule(adapter, _query(user_id=USER_ID))
    await adapter._handle_healbite_weight_callback(_query(user_id=OTHER_USER_ID), "weight:reminder:confirm")
    assert store.get_settings(OTHER_USER_ID) is None
    assert store.get_settings(USER_ID) is None

    await adapter._handle_healbite_weight_callback(_query(user_id=USER_ID), "weight:reminder:confirm")
    first = store.get_settings(USER_ID)
    await adapter._handle_healbite_weight_callback(_query(user_id=USER_ID), "weight:reminder:confirm")
    second = store.get_settings(USER_ID)

    assert first is not None and second is not None
    assert first.schedule_version == second.schedule_version
    assert _counts(db_path) == (1, 0)


@pytest.mark.asyncio
async def test_privacy_markers_omit_payload_identity_timezone_and_time(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "healbite.db"
    _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    adapter = _adapter(monkeypatch)
    query = _query(query_id="PII_REMINDER_QUERY_SECRET")

    with caplog.at_level(logging.INFO, logger="gateway.platforms.telegram"):
        await _select_default_schedule(adapter, query)
        await adapter._handle_healbite_weight_callback(query, "weight:reminder:confirm")

    log_text = caplog.text
    assert "healbite_route_selected" in log_text
    assert "route=weight_reminder" in log_text
    assert "timezone_region_bucket=europe" in log_text
    assert "time_bucket=morning" in log_text
    assert "PII_REMINDER_QUERY_SECRET" not in log_text
    assert str(USER_ID) not in log_text
    assert "Europe/Berlin" not in log_text
    assert "09:15" not in log_text
    assert "weight:reminder" not in log_text
    assert "tester" not in log_text


def test_callback_payloads_are_short_and_do_not_contain_private_values():
    payloads = list(reminder_ui.callback_payloads())

    assert payloads
    assert all(len(payload.encode("utf-8")) <= 64 for payload in payloads)
    assert all("Europe/" not in payload and "Asia/" not in payload and "America/" not in payload for payload in payloads)
    assert all("101" not in payload for payload in payloads)


@pytest.mark.asyncio
async def test_identical_save_preserves_next_due_and_schedule_version(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _profile, _tracker, store = _setup_stores(monkeypatch, db_path)
    _enable(monkeypatch)
    original = store.create_or_update_settings(
        user_id=USER_ID,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
        enabled=True,
    )
    adapter = _adapter(monkeypatch)
    query = _query()

    await adapter._handle_healbite_weight_callback(query, "weight:reminder:edit")
    await adapter._handle_healbite_weight_callback(query, "weight:reminder:confirm")

    setting = store.get_settings(USER_ID)
    assert setting is not None
    assert setting.schedule_version == original.schedule_version
    assert setting.next_due_at_utc == original.next_due_at_utc


def test_all_supported_timezone_aliases_are_unique_valid_and_private():
    aliases = list(reminder_ui.SUPPORTED_WEIGHT_REMINDER_TIMEZONES)
    values = [entry[1] for entry in reminder_ui.SUPPORTED_WEIGHT_REMINDER_TIMEZONES.values()]

    assert len(aliases) == len(set(aliases))
    assert len(values) == len(set(values))
    for alias, (_label, timezone_name) in reminder_ui.SUPPORTED_WEIGHT_REMINDER_TIMEZONES.items():
        assert alias == alias.lower()
        assert reminder_ui.timezone_name_for_alias(alias) == timezone_name
        assert ZoneInfo(timezone_name)
        assert "101" not in alias
