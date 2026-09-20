"""Tests for HealBite Public Onboarding Guard (Cases A-F, Allowlist Bypass, Callbacks, E2E)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.platforms.telegram import (
    FAMILY_COMMAND,
    WEEKLY_MENU_COMMAND,
    SHOPPING_COMMAND,
    TelegramAdapter,
)
from gateway.healbite_inventory_telegram import INVENTORY_COMMAND
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_households import HouseholdFeatureConfig
from gateway.healbite_family_telegram import HealBiteFamilyTelegramController
from gateway.healbite_weekly_menu_telegram import HealBiteWeeklyMenuTelegramController
from gateway.healbite_weekly_menu_runtime import build_weekly_menu_runtime_service
from gateway.healbite_shopping_telegram import HealBiteShoppingTelegramController
from gateway.healbite_shopping_runtime import build_shopping_runtime_service


def _make_adapter(*, tmp_path: Path, allowlisted_user: int = 101) -> TelegramAdapter:
    adapter = object.__new__(TelegramAdapter)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._ensure_forum_commands = AsyncMock()
    adapter.handle_message = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter._should_process_message = lambda msg, is_command=False: True
    adapter._maybe_handle_healbite_menu_button = TelegramAdapter._maybe_handle_healbite_menu_button.__get__(
        adapter, TelegramAdapter
    )
    adapter._dispatch_healbite_keyboard_action = TelegramAdapter._dispatch_healbite_keyboard_action.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_family_command = TelegramAdapter._maybe_handle_healbite_family_command.__get__(
        adapter, TelegramAdapter
    )
    adapter._handle_healbite_family_callback = TelegramAdapter._handle_healbite_family_callback.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_weekly_menu_command = TelegramAdapter._maybe_handle_healbite_weekly_menu_command.__get__(
        adapter, TelegramAdapter
    )
    adapter._handle_healbite_weekly_menu_callback = TelegramAdapter._handle_healbite_weekly_menu_callback.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_shopping_command = TelegramAdapter._maybe_handle_healbite_shopping_command.__get__(
        adapter, TelegramAdapter
    )
    adapter._handle_healbite_shopping_callback = TelegramAdapter._handle_healbite_shopping_callback.__get__(
        adapter, TelegramAdapter
    )
    adapter._send_healbite_family_result = AsyncMock()
    adapter._send_healbite_shopping_result = AsyncMock()
    adapter._send_healbite_weekly_menu_result = AsyncMock()
    adapter._healbite_weekly_menu_keyboard = Mock(return_value=None)
    adapter._healbite_now_utc = TelegramAdapter._healbite_now_utc
    adapter._maybe_handle_healbite_inventory_command = TelegramAdapter._maybe_handle_healbite_inventory_command.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_inventory_pending_text = TelegramAdapter._maybe_handle_healbite_inventory_pending_text.__get__(
        adapter, TelegramAdapter
    )
    adapter._maybe_handle_healbite_inventory_photo = TelegramAdapter._maybe_handle_healbite_inventory_photo.__get__(
        adapter, TelegramAdapter
    )
    adapter._handle_healbite_inventory_callback = TelegramAdapter._handle_healbite_inventory_callback.__get__(
        adapter, TelegramAdapter
    )
    adapter._healbite_inventory_source_message = TelegramAdapter._healbite_inventory_source_message
    adapter._send_healbite_inventory_result = AsyncMock()
    adapter._healbite_inventory_keyboard = Mock(return_value=None)
    adapter._fridge_menu_telegram = Mock()
    adapter._fridge_menu_telegram.cancel_pending = Mock()
    adapter._inventory_telegram = Mock()
    adapter._inventory_telegram.pending_input_kind = Mock(return_value=None)
    adapter._inventory_telegram.home = Mock()
    adapter._inventory_telegram.handle_text = Mock()
    adapter._inventory_telegram.handle_callback = Mock()
    adapter._healbite_inventory_photo_batches = {}
    adapter._largest_photo_size = Mock(return_value=None)
    adapter._telegram_media_size_allowed = Mock(return_value=(True, None))

    # Wire family controller
    db_path = tmp_path / "healbite_guard.db"
    adapter._family_telegram = HealBiteFamilyTelegramController(
        db_path=db_path,
        config=HouseholdFeatureConfig(
            enabled=True,
            allowlist=frozenset({allowlisted_user}),
            allowlist_valid=True,
            public_access=True,
        ),
    )
    # Wire weekly menu controller
    adapter._weekly_menu_telegram = HealBiteWeeklyMenuTelegramController(
        db_path=db_path,
        runtime_factory=lambda: build_weekly_menu_runtime_service(
            db_path=db_path,
            env={
                "HEALBITE_WEEKLY_MENU_ENABLED": "true",
                "HEALBITE_WEEKLY_MENU_ALLOWLIST": str(allowlisted_user),
                "HEALBITE_WEEKLY_MENU_PUBLIC": "true",
            },
        ),
    )
    # Wire shopping controller
    adapter._shopping_telegram = HealBiteShoppingTelegramController(
        runtime_factory=lambda: build_shopping_runtime_service(
            db_path=db_path,
            env={
                "HEALBITE_SHOPPING_LIST_ENABLED": "true",
                "HEALBITE_SHOPPING_LIST_ALLOWLIST": str(allowlisted_user),
                "HEALBITE_SHOPPING_LIST_PUBLIC": "true",
            },
        ),
    )

    adapter.config = SimpleNamespace(extra={})
    return adapter


def _make_msg(text: str, *, user_id: int = 1, chat_type: str = "private") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=user_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id, username=f"user_{user_id}", first_name="Test"),
        message_thread_id=None,
        reply_to_message=None,
        message_id=999,
    )


def _make_callback_query(data: str, *, user_id: int = 1, chat_type: str = "private") -> SimpleNamespace:
    msg = _make_msg("", user_id=user_id, chat_type=chat_type)
    query = SimpleNamespace(
        id="cq_123",
        data=data,
        from_user=SimpleNamespace(id=user_id, username=f"user_{user_id}"),
        message=msg,
        answer=AsyncMock(),
    )
    return query


@pytest.mark.asyncio
async def test_case_a_start_command_starts_onboarding_for_new_user(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    new_user_id = 901
    msg = _make_msg("/start", user_id=new_user_id)
    update = SimpleNamespace(update_id=1, message=msg, effective_message=None)
    await adapter._maybe_handle_healbite_start_command(msg)

    # Onboarding must be started
    assert store.get_onboarding_state(new_user_id) is not None
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "настроим профиль" in kwargs["text"].casefold()


@pytest.mark.asyncio
async def test_case_b_active_onboarding_blocks_features(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    user_id = 902
    store.begin_onboarding(user_id=user_id, username="active_user")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    for cmd, feature in [
        (FAMILY_COMMAND, "family"),
        (WEEKLY_MENU_COMMAND, "weekly_menu"),
        (SHOPPING_COMMAND, "shopping"),
        ("👨‍👩‍👧 Семья", "family"),
        ("📋 Меню на неделю", "weekly_menu"),
        ("🛒 Список покупок", "shopping"),
    ]:
        caplog.clear()
        adapter._send_message_with_thread_fallback.reset_mock()
        adapter._send_healbite_family_result.reset_mock()
        adapter._send_healbite_shopping_result.reset_mock()

        msg = _make_msg(cmd, user_id=user_id)
        with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
            if cmd.startswith("/"):
                if cmd == FAMILY_COMMAND:
                    handled = await adapter._maybe_handle_healbite_family_command(msg)
                elif cmd == WEEKLY_MENU_COMMAND:
                    handled = await adapter._maybe_handle_healbite_weekly_menu_command(msg)
                else:
                    handled = await adapter._maybe_handle_healbite_shopping_command(msg)
            else:
                handled = await adapter._dispatch_healbite_keyboard_action(
                    msg,
                    action=adapter._healbite_command_from_text(cmd),
                )

        assert handled is True
        # Controller was NOT called
        adapter._send_healbite_family_result.assert_not_called()
        adapter._send_healbite_shopping_result.assert_not_called()
        # Onboarding continuation text sent
        kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
        assert "Продолжим настройку профиля" in kwargs["text"]
        # Audit log verified
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "route=public_lane_blocked" in joined
        assert "lane=healbite_public" in joined
        assert "result=active_onboarding" in joined


@pytest.mark.asyncio
async def test_case_c_incomplete_profile_blocks_features(tmp_path, monkeypatch, caplog):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    user_id = 903
    # Incomplete profile (no daily_kcal_target)
    store.upsert_user_profile(user_id=user_id, username="incomplete_user", daily_kcal_target=None)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    msg = _make_msg(FAMILY_COMMAND, user_id=user_id)
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        handled = await adapter._maybe_handle_healbite_family_command(msg)

    assert handled is True
    adapter._send_healbite_family_result.assert_not_called()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Чтобы начать пользоваться HealBite, нажми /start" in kwargs["text"]
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "result=incomplete_profile" in joined


@pytest.mark.asyncio
async def test_case_d_completed_profile_allows_public_features(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    user_id = 904
    # Completed profile
    store.upsert_user_profile(user_id=user_id, username="complete_user", daily_kcal_target=2100)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    # Family command
    msg_fam = _make_msg(FAMILY_COMMAND, user_id=user_id)
    handled_fam = await adapter._maybe_handle_healbite_family_command(msg_fam)
    assert handled_fam is True
    adapter._send_healbite_family_result.assert_called_once()

    # Shopping command
    msg_shop = _make_msg(SHOPPING_COMMAND, user_id=user_id)
    handled_shop = await adapter._maybe_handle_healbite_shopping_command(msg_shop)
    assert handled_shop is True
    adapter._send_healbite_shopping_result.assert_called_once()


@pytest.mark.asyncio
async def test_case_e_legacy_flag_off_preserves_behavior(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    user_id = 905
    # No profile, but HEALBITE_PUBLIC_ONBOARDING is false
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "false")

    msg = _make_msg(FAMILY_COMMAND, user_id=user_id)
    handled = await adapter._maybe_handle_healbite_family_command(msg)
    assert handled is True
    # Family controller was called because guard was disabled
    adapter._send_healbite_family_result.assert_called_once()


@pytest.mark.asyncio
async def test_case_f_group_behavior_unchanged(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    user_id = 906
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    # In group chat, guard returns False (does not block)
    msg_group = _make_msg(FAMILY_COMMAND, user_id=user_id, chat_type="supergroup")
    handled = await adapter._maybe_handle_healbite_family_command(msg_group)
    assert handled is True
    adapter._send_healbite_family_result.assert_called_once()


@pytest.mark.asyncio
async def test_allowlisted_actor_bypasses_onboarding_guard(tmp_path, monkeypatch):
    operator_id = 101
    adapter = _make_adapter(tmp_path=tmp_path, allowlisted_user=operator_id)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    # Operator is in active onboarding
    store.begin_onboarding(user_id=operator_id, username="operator")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    msg = _make_msg(FAMILY_COMMAND, user_id=operator_id)
    handled = await adapter._maybe_handle_healbite_family_command(msg)
    assert handled is True
    # Allowed because operator is allowlisted!
    adapter._send_healbite_family_result.assert_called_once()


@pytest.mark.asyncio
async def test_callbacks_blocked_for_unlisted_uncompleted_user(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    user_id = 907
    store.begin_onboarding(user_id=user_id, username="cb_user")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    query = _make_callback_query("hb:fam:create", user_id=user_id)
    await adapter._handle_healbite_family_callback(query, "hb:fam:create")
    query.answer.assert_called_once()
    assert "Продолжим настройку профиля" in query.answer.call_args.kwargs["text"]
    assert query.answer.call_args.kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_phase7_synthetic_temp_db_e2e_flow(tmp_path, monkeypatch):
    """Complete lifecycle verification on synthetic temp DB from brand new user to public access."""
    adapter = _make_adapter(tmp_path=tmp_path)
    db_path = tmp_path / "synthetic_e2e.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    new_user_id = 777777

    # 1. New user attempts /weekly_menu before doing anything -> BLOCKED (missing profile)
    msg_wm = _make_msg(WEEKLY_MENU_COMMAND, user_id=new_user_id)
    handled_wm = await adapter._maybe_handle_healbite_weekly_menu_command(msg_wm)
    assert handled_wm is True
    assert "Чтобы начать пользоваться HealBite, нажми /start" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    # 2. User starts onboarding via /start
    msg_start = _make_msg("/start", user_id=new_user_id)
    await adapter._maybe_handle_healbite_start_command(msg_start)
    assert store.get_onboarding_state(new_user_id) is not None

    # 3. User attempts /family while onboarding is active -> BLOCKED (active onboarding)
    msg_fam = _make_msg(FAMILY_COMMAND, user_id=new_user_id)
    handled_fam = await adapter._maybe_handle_healbite_family_command(msg_fam)
    assert handled_fam is True
    assert "Продолжим настройку профиля" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    # 4. User completes onboarding
    store.upsert_user_profile(user_id=new_user_id, username="e2e_user", daily_kcal_target=2200)
    store.clear_onboarding_state(new_user_id)
    profile = store.get_user_profile(new_user_id)
    assert profile is not None and profile.daily_kcal_target == 2200

    # 5. User attempts /family -> ALLOWED!
    adapter._send_healbite_family_result.reset_mock()
    handled_fam_ok = await adapter._maybe_handle_healbite_family_command(msg_fam)
    assert handled_fam_ok is True
    adapter._send_healbite_family_result.assert_called_once()

    # 6. User attempts /shopping -> ALLOWED!
    adapter._send_healbite_shopping_result.reset_mock()
    msg_shop = _make_msg(SHOPPING_COMMAND, user_id=new_user_id)
    handled_shop_ok = await adapter._maybe_handle_healbite_shopping_command(msg_shop)
    assert handled_shop_ok is True
    adapter._send_healbite_shopping_result.assert_called_once()


@pytest.mark.asyncio
async def test_inventory_command_blocked_for_uncompleted_user(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    user_id = 910
    msg = _make_msg(INVENTORY_COMMAND, user_id=user_id)
    handled = await adapter._maybe_handle_healbite_inventory_command(msg)
    assert handled is True
    adapter._send_healbite_inventory_result.assert_not_called()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Чтобы начать пользоваться HealBite, нажми /start" in kwargs["text"]


@pytest.mark.asyncio
async def test_inventory_pending_text_blocked_for_uncompleted_user(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    user_id = 911
    adapter._inventory_telegram.pending_input_kind.return_value = "text"
    msg = _make_msg("Яйца 10 шт", user_id=user_id)
    handled = await adapter._maybe_handle_healbite_inventory_pending_text(msg)
    assert handled is True
    adapter._send_healbite_inventory_result.assert_not_called()
    adapter._inventory_telegram.handle_text.assert_not_called()
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Чтобы начать пользоваться HealBite, нажми /start" in kwargs["text"]


@pytest.mark.asyncio
async def test_inventory_photo_blocked_before_download_for_uncompleted_user(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    user_id = 912
    adapter._inventory_telegram.pending_input_kind.return_value = "photo"

    photo_mock = Mock()
    photo_mock.get_file = AsyncMock()
    adapter._largest_photo_size.return_value = photo_mock

    msg = _make_msg("", user_id=user_id)
    msg.photo = [photo_mock]

    handled = await adapter._maybe_handle_healbite_inventory_photo(msg)
    assert handled is True
    # Invariant: ZERO provider calls, ZERO photo downloads before onboarding completes!
    photo_mock.get_file.assert_not_called()
    assert len(adapter._healbite_inventory_photo_batches) == 0
    kwargs = adapter._send_message_with_thread_fallback.await_args.kwargs
    assert "Чтобы начать пользоваться HealBite, нажми /start" in kwargs["text"]


@pytest.mark.asyncio
async def test_inventory_callbacks_blocked_for_uncompleted_user(tmp_path, monkeypatch):
    adapter = _make_adapter(tmp_path=tmp_path)
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite_guard.db")
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    user_id = 913

    for cb_data in ["inv:home", "inv:t", "inv:p", "inv:g:20260706"]:
        query = _make_callback_query(cb_data, user_id=user_id)
        await adapter._handle_healbite_inventory_callback(query, cb_data)
        query.answer.assert_called_once()
        assert "Чтобы начать пользоваться HealBite, нажми /start" in query.answer.call_args.kwargs["text"]
        assert query.answer.call_args.kwargs["show_alert"] is True
        adapter._inventory_telegram.handle_callback.assert_not_called()


def test_is_feature_allowlisted_all_feature_types(monkeypatch):
    from gateway.platforms.telegram import TelegramAdapter
    adapter = Mock(spec=TelegramAdapter)
    adapter._family_telegram = None
    adapter._is_feature_allowlisted = TelegramAdapter._is_feature_allowlisted.__get__(adapter, TelegramAdapter)

    monkeypatch.setenv("HEALBITE_HOUSEHOLDS_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_HOUSEHOLDS_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_WEEKLY_MENU_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_WEEKLY_MENU_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_SHOPPING_LIST_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_SHOPPING_LIST_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_INVENTORY_TEXT_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_INVENTORY_TEXT_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_INVENTORY_TEXT_UI_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_INVENTORY_TEXT_UI_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_INVENTORY_PHOTO_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_INVENTORY_PHOTO_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_INVENTORY_PHOTO_UI_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_INVENTORY_PHOTO_UI_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_INVENTORY_WEEKLY_GENERATION_UI_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_INVENTORY_WEEKLY_GENERATION_UI_ALLOWLIST", "1001,1002")
    monkeypatch.setenv("HEALBITE_WEEKLY_MENU_INVENTORY_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_WEEKLY_MENU_INVENTORY_ALLOWLIST", "1001,1002")

    for feat in [
        "HEALBITE_HOUSEHOLDS",
        "HEALBITE_WEEKLY_MENU",
        "HEALBITE_SHOPPING_LIST",
        "HEALBITE_INVENTORY_HOME",
        "HEALBITE_INVENTORY_TEXT",
        "HEALBITE_INVENTORY_TEXT_UI",
        "HEALBITE_INVENTORY_PHOTO",
        "HEALBITE_INVENTORY_PHOTO_UI",
        "HEALBITE_INVENTORY_WEEKLY_GENERATION_UI",
        "HEALBITE_WEEKLY_MENU_INVENTORY",
    ]:
        assert adapter._is_feature_allowlisted(feat, 1001) is True
        assert adapter._is_feature_allowlisted(feat, "1002") is True
        assert adapter._is_feature_allowlisted(feat, 9999) is False
        assert adapter._is_feature_allowlisted(feat, None) is False
        assert adapter._is_feature_allowlisted(feat, "invalid") is False
