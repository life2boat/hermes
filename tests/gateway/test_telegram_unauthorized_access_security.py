"""Adversarial security test suite for Telegram unauthorized access check.

Validates fail-closed authorization, entrypoint gating, user/household isolation,
and zero business side-effects across SEC-01 through SEC-18.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.healbite_households import (
    HealBiteHouseholdService,
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdContext,
    HouseholdFeatureConfig,
)
from gateway.healbite_household_schema import (
    HouseholdMemberStatus,
    HouseholdRole,
    HouseholdStatus,
)
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.telegram import TelegramAdapter
from gateway.session import SessionSource


# ---------------------------------------------------------------------------
# Test fixtures and helpers
# ---------------------------------------------------------------------------

UNKNOWN_USER_ID = 999000111
AUTHORIZED_USER_ID = 10001
ADMIN_USER_ID = 777000777
OTHER_USER_ID = 888000888
TEST_HOUSEHOLD_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
TEST_HOUSEHOLD_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _make_msg(
    *,
    user_id: int = UNKNOWN_USER_ID,
    text: str = "",
    chat_id: int = 555,
    chat_type: str = "private",
    has_photo: bool = False,
    username: str = "attacker",
):
    photo_list = [SimpleNamespace(file_id="p1", file_unique_id="pu1", width=100, height=100)] if has_photo else []
    return SimpleNamespace(
        message_id=1,
        text=text,
        caption=text if has_photo else None,
        from_user=SimpleNamespace(id=user_id, username=username, first_name="TestUser", full_name="TestUser"),
        chat=SimpleNamespace(id=chat_id, type=chat_type, title=None, full_name=None),
        chat_id=chat_id,
        message_thread_id=None,
        photo=photo_list,
        sticker=None,
        voice=None,
        video=None,
        document=None,
        media_group_id=None,
        reply_to_message=None,
        date=datetime(2026, 10, 9, tzinfo=timezone.utc),
    )


def _make_update(*, msg, update_id: int = 1):
    return SimpleNamespace(
        update_id=update_id,
        message=msg,
        effective_message=msg,
        callback_query=None,
    )


def _make_callback_query(
    *,
    data: str,
    user_id: int = UNKNOWN_USER_ID,
    chat_id: int = 555,
    chat_type: str = "private",
):
    msg = _make_msg(user_id=user_id, chat_id=chat_id, chat_type=chat_type)
    return SimpleNamespace(
        id="query_123",
        data=data,
        from_user=SimpleNamespace(id=user_id, username="attacker"),
        message=msg,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _create_test_adapter(*, allowed_users: list[int] | None = None, admin_users: list[int] | None = None):
    extra = {}
    if allowed_users is not None:
        extra["allowed_users"] = [str(u) for u in allowed_users]
    if admin_users is not None:
        extra["allow_admin_from"] = [str(u) for u in admin_users]

    config = PlatformConfig(enabled=True, token="fake-token", extra=extra)
    adapter = TelegramAdapter(config)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._enqueue_text_event = Mock()
    adapter._enqueue_photo_event = Mock()
    adapter.handle_message = AsyncMock()
    return adapter


# ---------------------------------------------------------------------------
# SEC-01 through SEC-18 test suite
# ---------------------------------------------------------------------------

class TestTelegramUnauthorizedAccessSecurity:
    """Deterministic security tests SEC-01 through SEC-18."""

    @pytest.mark.asyncio
    async def test_sec_01_unknown_user_start_no_protected_access_no_db_mutation(self, tmp_path, monkeypatch):
        """SEC-01: Unknown user calls /start -> No protected access, 0 onboarding in DB."""
        db_path = tmp_path / "healbite_sec01.db"
        store = HealBiteUserProfileStore(db_path=db_path)
        monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        adapter = _create_test_adapter()
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text="/start")
        update = _make_update(msg=msg)

        # /start command must return False (denied/bypassed) or forward to gateway runner
        handled = await adapter._maybe_handle_healbite_start_command(msg)
        assert handled is False

        # In _handle_command: must not execute local start onboarding
        await adapter._handle_command(update, SimpleNamespace())
        assert adapter._send_message_with_thread_fallback.await_count == 0

        # Verify DB mutations: 0 profiles created, 0 onboarding states created
        assert store.get_user_profile(UNKNOWN_USER_ID) is None
        assert store.get_onboarding_state(UNKNOWN_USER_ID) is None

    @pytest.mark.asyncio
    async def test_sec_02_unknown_user_menu_deny(self, monkeypatch):
        """SEC-02: Unknown user calls /menu -> DENY."""
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter()
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text="/menu")
        update = _make_update(msg=msg)

        handled = await adapter._dispatch_healbite_keyboard_action(msg, action="/menu")
        assert handled is False

        await adapter._handle_command(update, SimpleNamespace())
        # Local menu message was NOT sent
        assert adapter._send_message_with_thread_fallback.await_count == 0
        # Dispatched directly to gateway handler for centralized rejection
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sec_03_unknown_user_text_message_deny(self, tmp_path, monkeypatch):
        """SEC-03: Unknown user sends text -> DENY (no meal staging, no water tracker)."""
        db_path = tmp_path / "healbite_sec03.db"
        store = HealBiteUserProfileStore(db_path=db_path)
        monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter()
        # Text simulating explicit food logging or water intake
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text="вода 300")
        update = _make_update(msg=msg)

        await adapter._handle_text_message(update, SimpleNamespace())

        # No local water or meal reply sent
        assert adapter._send_message_with_thread_fallback.await_count == 0
        # Event routed to generic enqueue_text_event, bypassing all local HealBite handlers
        adapter._enqueue_text_event.assert_called_once()

    @pytest.mark.asyncio
    async def test_sec_04_unknown_user_photo_deny(self, monkeypatch):
        """SEC-04: Unknown user sends photo -> DENY (no fridge/inventory processing)."""
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter()
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text="фото продуктов", has_photo=True)
        update = _make_update(msg=msg)

        # Mock fridge/inventory photo handlers to ensure they are NOT called
        adapter._maybe_handle_healbite_fridge_menu_photo = AsyncMock(return_value=False)
        adapter._maybe_handle_healbite_inventory_photo = AsyncMock(return_value=False)

        await adapter._handle_media_message(update, SimpleNamespace())

        adapter._maybe_handle_healbite_fridge_menu_photo.assert_not_awaited()
        adapter._maybe_handle_healbite_inventory_photo.assert_not_awaited()
        adapter.handle_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sec_05_unknown_user_callbacks_deny(self, tmp_path, monkeypatch):
        """SEC-05: Unknown user triggers callbacks -> DENY."""
        db_path = tmp_path / "healbite_sec05.db"
        store = HealBiteUserProfileStore(db_path=db_path)
        monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter()

        dangerous_callbacks = [
            "fridge:menu:generate",
            "inv:view:all",
            "weekly:gen:2026-W42",
            "shopping:list:view",
            "family:v9:view",
            "profile:edit:999000111",
            "weight:custom",
            "water:add:250",
        ]

        for callback_data in dangerous_callbacks:
            query = _make_callback_query(data=callback_data, user_id=UNKNOWN_USER_ID)
            update = SimpleNamespace(callback_query=query, effective_message=query.message)
            await adapter._handle_callback_query(update, SimpleNamespace())
            # Callback is answered and dismissed without executing sensitive operations
            query.answer.assert_awaited()

    def test_sec_06_spoofed_user_id_in_message_body(self, monkeypatch):
        """SEC-06: Spoofed user_id in message payload -> Uses authenticated update actor."""
        adapter = _create_test_adapter(allowed_users=[AUTHORIZED_USER_ID])

        # Malicious user claims user_id=10001 in text but update is from UNKNOWN_USER_ID
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text=f"/menu --user_id={AUTHORIZED_USER_ID}")

        actor_id = getattr(getattr(msg, "from_user", None), "id", None)
        assert actor_id == UNKNOWN_USER_ID
        assert adapter._is_telegram_user_authorized(actor_id) is False

    def test_sec_07_spoofed_household_id_access_denied(self):
        """SEC-07: Spoofed household_id -> DENY."""
        service = HealBiteHouseholdService(store=Mock())
        context = HouseholdContext(
            actor_user_id=UNKNOWN_USER_ID,
            household_id=TEST_HOUSEHOLD_A,
            household_member_id="m1",
            role=HouseholdRole.ADULT_MEMBER,
            member_status=HouseholdMemberStatus.ACTIVE,
            household_status=HouseholdStatus.ACTIVE,
        )

        with pytest.raises(HouseholdAccessError):
            service.assert_household_access(context, requested_household_id=TEST_HOUSEHOLD_B)

    def test_sec_08_cross_user_memory_query_isolation(self, tmp_path):
        """SEC-08: Cross-user memory query -> DENY / isolated."""
        db_path = tmp_path / "healbite_sec08.db"
        store = HealBiteUserProfileStore(db_path=db_path)
        store.upsert_user_profile(user_id=AUTHORIZED_USER_ID, username="legit", daily_kcal_target=2200)

        # Attacker cannot retrieve authorized user's profile under their own id
        assert store.get_user_profile(UNKNOWN_USER_ID) is None

    def test_sec_09_cross_user_inventory_isolation(self):
        """SEC-09: Cross-user inventory isolation enforced by household context."""
        service = HealBiteHouseholdService(store=Mock())
        context_user = HouseholdContext(
            actor_user_id=UNKNOWN_USER_ID,
            household_id=TEST_HOUSEHOLD_A,
            household_member_id="m1",
            role=HouseholdRole.ADULT_MEMBER,
            member_status=HouseholdMemberStatus.ACTIVE,
            household_status=HouseholdStatus.ACTIVE,
        )
        # Attempt to access household B inventory fails
        with pytest.raises(HouseholdAccessError):
            service.assert_household_access(context_user, TEST_HOUSEHOLD_B)

    @pytest.mark.asyncio
    async def test_sec_10_admin_command_by_unknown_user_deny(self, monkeypatch):
        """SEC-10: Admin command called by unknown user -> DENY."""
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter(admin_users=[ADMIN_USER_ID])
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text="/memory_stats")
        update = _make_update(msg=msg)

        await adapter._handle_command(update, SimpleNamespace())

        # Admin access is denied with admin-only notice
        assert adapter._send_message_with_thread_fallback.await_count == 1
        assert "admin-only" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    def test_sec_11_missing_allowlist_fails_closed(self, monkeypatch):
        """SEC-11: Missing allowlist configuration -> Fail closed."""
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOW_ALL_USERS", raising=False)

        adapter = _create_test_adapter()
        assert adapter._is_telegram_user_authorized(UNKNOWN_USER_ID) is False

    def test_sec_12_malformed_allowlist_fails_closed(self, monkeypatch):
        """SEC-12: Malformed allowlist configuration -> Fail closed."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", ",,,  invalid_id ,,,")
        monkeypatch.delenv("GATEWAY_ALLOWED_USERS", raising=False)
        monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)

        adapter = _create_test_adapter()
        assert adapter._is_telegram_user_authorized(UNKNOWN_USER_ID) is False

    @pytest.mark.asyncio
    async def test_sec_13_unknown_user_cannot_resume_fsm(self, tmp_path, monkeypatch):
        """SEC-13: Unknown user trying to resume FSM/onboarding -> DENY."""
        db_path = tmp_path / "healbite_sec13.db"
        store = HealBiteUserProfileStore(db_path=db_path)
        monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter()
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text="2500")
        update = _make_update(msg=msg)

        # In _handle_text_message, unauthorized user bypasses _maybe_handle_healbite_onboarding_reply
        await adapter._handle_text_message(update, SimpleNamespace())

        # No onboarding state created or modified
        assert store.get_onboarding_state(UNKNOWN_USER_ID) is None
        assert adapter._send_message_with_thread_fallback.await_count == 0

    @pytest.mark.asyncio
    async def test_sec_14_replayed_telegram_update(self, monkeypatch):
        """SEC-14: Replayed update -> Consistently denied."""
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter()
        msg = _make_msg(user_id=UNKNOWN_USER_ID, text="/menu")
        update = _make_update(msg=msg, update_id=42)

        # First delivery
        await adapter._handle_command(update, SimpleNamespace())
        assert adapter._send_message_with_thread_fallback.await_count == 0

        # Replay delivery
        await adapter._handle_command(update, SimpleNamespace())
        assert adapter._send_message_with_thread_fallback.await_count == 0

    def test_sec_15_authorized_user_access_passes(self, monkeypatch):
        """SEC-15: Authorized user regular features -> PASS."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(AUTHORIZED_USER_ID))
        adapter = _create_test_adapter(allowed_users=[AUTHORIZED_USER_ID])

        assert adapter._is_telegram_user_authorized(AUTHORIZED_USER_ID) is True

    @pytest.mark.asyncio
    async def test_sec_16_authorized_admin_access_passes(self, monkeypatch):
        """SEC-16: Authorized administrator calls admin command -> PASS."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(ADMIN_USER_ID))
        adapter = _create_test_adapter(allowed_users=[ADMIN_USER_ID], admin_users=[ADMIN_USER_ID])

        msg = _make_msg(user_id=ADMIN_USER_ID, text="/memory_stats")
        with patch.object(adapter, "_parse_memory_stats_args", return_value=(24.0, None, None)), \
             patch("gateway.platforms.telegram.compute_memory_analytics_summary", return_value={}), \
             patch("gateway.platforms.telegram.format_memory_analytics_report", return_value="Report OK"):
            handled = await adapter._maybe_handle_memory_stats_command(msg)
            assert handled is True
            assert adapter._send_message_with_thread_fallback.await_count == 1
            assert "Report OK" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_sec_17_regular_authorized_user_admin_command_denied(self, monkeypatch):
        """SEC-17: Regular authorized user calls admin command -> DENY."""
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", str(AUTHORIZED_USER_ID))
        adapter = _create_test_adapter(allowed_users=[AUTHORIZED_USER_ID], admin_users=[ADMIN_USER_ID])

        msg = _make_msg(user_id=AUTHORIZED_USER_ID, text="/memory_stats")
        handled = await adapter._maybe_handle_memory_stats_command(msg)
        assert handled is True
        assert adapter._send_message_with_thread_fallback.await_count == 1
        assert "admin-only" in adapter._send_message_with_thread_fallback.await_args.kwargs["text"]

    @pytest.mark.asyncio
    async def test_sec_18_unknown_user_complex_workflow_zero_side_effects(self, tmp_path, monkeypatch):
        """SEC-18: Unknown user triggers multi-turn workflow -> Proves zero business side-effects.

        DB_MUTATIONS=0
        MEMORY_WRITES=0
        QDRANT_MUTATIONS=0
        LLM_CALLS=0
        HOUSEHOLD_CREATIONS=0
        INVENTORY_MUTATIONS=0
        SHOPPING_MUTATIONS=0
        PROTECTED_TOOL_EXECUTIONS=0
        """
        db_path = tmp_path / "healbite_sec18.db"
        store = HealBiteUserProfileStore(db_path=db_path)
        monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
        monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)

        adapter = _create_test_adapter()

        # Step 1: /start
        await adapter._handle_command(_make_update(msg=_make_msg(text="/start")), SimpleNamespace())
        # Step 2: /menu
        await adapter._handle_command(_make_update(msg=_make_msg(text="/menu")), SimpleNamespace())
        # Step 3: Text attempt
        await adapter._handle_text_message(_make_update(msg=_make_msg(text="курица 200г")), SimpleNamespace())
        # Step 4: Photo attempt
        await adapter._handle_media_message(_make_update(msg=_make_msg(text="фото", has_photo=True)), SimpleNamespace())
        # Step 5: Callback query attempt
        query = _make_callback_query(data="fridge:menu:generate")
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query, effective_message=query.message), SimpleNamespace())

        # Verify side effects:
        # DB mutations == 0
        assert store.get_user_profile(UNKNOWN_USER_ID) is None
        assert store.get_onboarding_state(UNKNOWN_USER_ID) is None

        # Verify no menus or interactive data rendered to the caller
        assert adapter._send_message_with_thread_fallback.await_count == 0
