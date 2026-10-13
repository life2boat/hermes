"""Deterministic regressions for HealBite /start onboarding completeness (HER-8).

Verifies that:
1. New users without profiles enter onboarding at step 'sex'.
2. Legacy users with pre-existing default daily_kcal_target (e.g. 1750.0)
   but missing profile fields enter onboarding at step 'sex' instead of
   incorrectly skipping to the main menu.
3. Partially completed users resume onboarding at the correct first missing field.
4. Fully completed users (all 6 fields + daily_kcal_target) receive the main menu.
5. `/start edit` launches onboarding in edit mode at step 'sex'.
6. Non-allowlisted users with public onboarding enabled can access only the public
   onboarding / profile scope, while privileged admin commands are rejected.
7. Public onboarding disabled rejects unauthorized users fail-closed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    ONBOARDING_STEP_AGE,
    ONBOARDING_STEP_HEIGHT,
    ONBOARDING_STEP_SEX,
)
from gateway.platforms.telegram import TelegramAdapter


def _make_adapter() -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._send_healbite_menu_message = AsyncMock()
    adapter._healbite_public_onboarding_enabled = MagicMock(return_value=True)
    adapter._is_telegram_user_authorized = MagicMock(return_value=True)
    adapter._healbite_sender_username = TelegramAdapter._healbite_sender_username
    adapter._healbite_reply_keyboard = TelegramAdapter._healbite_reply_keyboard.__get__(adapter, TelegramAdapter)
    adapter._log_healbite_route_selected = TelegramAdapter._log_healbite_route_selected.__get__(adapter, TelegramAdapter)
    adapter._log_healbite_marker = TelegramAdapter._log_healbite_marker.__get__(adapter, TelegramAdapter)
    adapter._healbite_correlation_id = TelegramAdapter._healbite_correlation_id.__get__(adapter, TelegramAdapter)
    adapter._safe_log_scalar = TelegramAdapter._safe_log_scalar
    adapter._HEALBITE_LOG_FIELD_ALLOWLIST = TelegramAdapter._HEALBITE_LOG_FIELD_ALLOWLIST
    adapter._maybe_handle_healbite_start_command = TelegramAdapter._maybe_handle_healbite_start_command.__get__(adapter, TelegramAdapter)
    adapter._is_feature_allowlisted = TelegramAdapter._is_feature_allowlisted.__get__(adapter, TelegramAdapter)
    adapter._family_telegram = None
    return adapter


def _make_msg(text: str, user_id: int = 1001, chat_id: int = 1001, chat_type: str = "private") -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.chat.id = chat_id
    msg.chat.type = chat_type
    msg.message_thread_id = None
    msg.from_user.id = user_id
    msg.from_user.username = f"user_{user_id}"
    return msg


@pytest.mark.asyncio
async def test_new_user_starts_onboarding_at_sex_step(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_onboarding.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    user_id = 1001
    msg = _make_msg("/start", user_id=user_id)

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        handled = await adapter._maybe_handle_healbite_start_command(msg)

    assert handled is True
    adapter._send_message_with_thread_fallback.assert_called_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert kwargs["chat_id"] == str(user_id)
    # Step 1 asks for sex
    assert "Укажите пол" in kwargs["text"]

    state = store.get_onboarding_state(user_id)
    assert state is not None
    assert state.step == ONBOARDING_STEP_SEX

    adapter._send_healbite_menu_message.assert_not_called()
    assert "outcome=onboarding_started" in caplog.text


@pytest.mark.asyncio
async def test_legacy_user_with_target_but_missing_profile_fields_starts_onboarding(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_onboarding.db")
    user_id = 1002

    # Pre-populate legacy record: user row exists with daily_kcal_target 1750.0,
    # but profiles table has NO entry (all profile fields missing)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, daily_kcal_target) VALUES (?, ?, ?)",
            (user_id, "legacy_user", 1750.0),
        )

    # Verify initial DB state: profile has target 1750.0 but missing fields
    profile = store.get_user_profile(user_id)
    assert profile is not None
    assert profile.daily_kcal_target == 1750.0
    assert profile.sex is None
    assert profile.age is None

    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    msg = _make_msg("/start", user_id=user_id)
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        handled = await adapter._maybe_handle_healbite_start_command(msg)

    assert handled is True
    # In the prior bug, outcome was menu_returned. In the fixed code, onboarding is started!
    adapter._send_message_with_thread_fallback.assert_called_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Укажите пол" in kwargs["text"]

    state = store.get_onboarding_state(user_id)
    assert state is not None
    assert state.step == ONBOARDING_STEP_SEX

    adapter._send_healbite_menu_message.assert_not_called()
    assert "outcome=onboarding_started" in caplog.text


@pytest.mark.asyncio
async def test_partially_completed_profile_resumes_at_correct_missing_field(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_onboarding.db")
    user_id = 1003

    # User completed sex and age, but height and beyond are missing
    store.upsert_user_profile(user_id=user_id, username="partial_user", sex="m", age=28)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    msg = _make_msg("/start", user_id=user_id)
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        handled = await adapter._maybe_handle_healbite_start_command(msg)

    assert handled is True
    adapter._send_message_with_thread_fallback.assert_called_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "рост" in kwargs["text"].lower()

    state = store.get_onboarding_state(user_id)
    assert state is not None
    assert state.step == ONBOARDING_STEP_HEIGHT
    adapter._send_healbite_menu_message.assert_not_called()


@pytest.mark.asyncio
async def test_fully_completed_profile_receives_main_menu(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_onboarding.db")
    user_id = 1004

    # Full profile with all 6 fields and target
    store.upsert_user_profile(
        user_id=user_id,
        username="complete_user",
        sex="m",
        age=30,
        height_cm=180,
        weight_kg=75,
        goal="maintain",
        activity_level="moderate",
        daily_kcal_target=2200.0,
    )
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    msg = _make_msg("/start", user_id=user_id)
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        handled = await adapter._maybe_handle_healbite_start_command(msg)

    assert handled is True
    adapter._send_healbite_menu_message.assert_called_once()
    adapter._send_message_with_thread_fallback.assert_not_called()

    state = store.get_onboarding_state(user_id)
    assert state is None
    assert "outcome=menu_returned" in caplog.text
    assert "onboarding=already_completed" in caplog.text


@pytest.mark.asyncio
async def test_start_edit_restarts_onboarding_at_sex_step_for_completed_user(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_onboarding.db")
    user_id = 1005

    store.upsert_user_profile(
        user_id=user_id,
        username="complete_user",
        sex="f",
        age=25,
        height_cm=165,
        weight_kg=55,
        goal="lose",
        activity_level="low",
        daily_kcal_target=1500.0,
    )
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    msg = _make_msg("/start edit", user_id=user_id)
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        handled = await adapter._maybe_handle_healbite_start_command(msg)

    assert handled is True
    adapter._send_message_with_thread_fallback.assert_called_once()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Укажите пол" in kwargs["text"]

    state = store.get_onboarding_state(user_id)
    assert state is not None
    assert state.step == ONBOARDING_STEP_SEX
    adapter._send_healbite_menu_message.assert_not_called()
    assert "outcome=onboarding_started" in caplog.text


@pytest.mark.asyncio
async def test_non_allowlisted_user_with_public_onboarding_disabled_denied(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_onboarding.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    # Public onboarding disabled
    adapter._healbite_public_onboarding_enabled = MagicMock(return_value=False)
    # User not authorized in allowlist
    adapter._is_telegram_user_authorized = MagicMock(return_value=False)

    user_id = 1006
    msg = _make_msg("/start", user_id=user_id)
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        handled = await adapter._maybe_handle_healbite_start_command(msg)

    assert handled is False
    adapter._send_message_with_thread_fallback.assert_not_called()
    adapter._send_healbite_menu_message.assert_not_called()
    assert "onboarding=denied" in caplog.text
    assert "outcome=unauthorized" in caplog.text


@pytest.mark.asyncio
async def test_public_user_cannot_access_privileged_hermes_features(tmp_path, monkeypatch):
    adapter = _make_adapter()
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_onboarding.db")
    user_id = 1007
    store.upsert_user_profile(
        user_id=user_id,
        username="public_user",
        sex="m",
        age=32,
        height_cm=178,
        weight_kg=78,
        goal="maintain",
        activity_level="moderate",
        daily_kcal_target=2100.0,
    )
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)

    # User is not allowlisted
    adapter._is_telegram_user_authorized = MagicMock(return_value=False)

    # Check that privileged household feature check returns False
    assert adapter._is_feature_allowlisted("HEALBITE_HOUSEHOLDS", user_id) is False
    assert adapter._is_feature_allowlisted("HEALBITE_WEEKLY_MENU", user_id) is False
    assert adapter._is_feature_allowlisted("HEALBITE_SHOPPING_LIST", user_id) is False
