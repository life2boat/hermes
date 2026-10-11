from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.platforms.telegram import TelegramAdapter


PII_CANARIES = {
    "SECRET_BOT_TOKEN_998877",
    "RAW_USER_ID_7788991122",
    "RAW_CHAT_ID_5566778899",
    "CANARY_USERNAME_ALICE",
    "CANARY_FIRSTNAME_BOB",
    "CANARY_MESSAGE_TEXT_PRIVATE_BODY",
    "CANARY_CALLBACK_DATA_PAYLOAD",
}


def _assert_no_canaries(log_text: str) -> None:
    for canary in PII_CANARIES:
        assert canary not in log_text, f"Canary leaked in logs: {canary}"


def _make_adapter() -> TelegramAdapter:
    config = PlatformConfig(
        enabled=True,
        token="123456:SECRET_BOT_TOKEN_998877",
        extra={"allowed_users": ["111222"]},
    )
    adapter = TelegramAdapter(config)
    adapter.handle_message = AsyncMock()
    return adapter


def _make_telegram_update(
    update_id: int = 9001,
    *,
    text: str = "/start",
    chat_type: str = "private",
    user_id: int = 7788991122,
    chat_id: int = 5566778899,
    is_callback: bool = False,
    is_membership: bool = False,
) -> SimpleNamespace:
    from_user = SimpleNamespace(
        id=user_id,
        first_name="CANARY_FIRSTNAME_BOB",
        username="CANARY_USERNAME_ALICE",
        full_name="CANARY_FIRSTNAME_BOB",
    )
    chat = SimpleNamespace(
        id=chat_id,
        type=chat_type,
        title=None,
        full_name=None,
    )

    if is_membership:
        old_member = SimpleNamespace(status="kicked")
        new_member = SimpleNamespace(status="member")
        chat_member_updated = SimpleNamespace(
            chat=chat,
            from_user=from_user,
            date=datetime.now(timezone.utc),
            old_chat_member=old_member,
            new_chat_member=new_member,
        )
        return SimpleNamespace(
            update_id=update_id,
            my_chat_member=chat_member_updated,
            message=None,
            effective_message=None,
            callback_query=None,
        )

    if is_callback:
        query = SimpleNamespace(
            id="query_123",
            from_user=from_user,
            message=SimpleNamespace(
                chat=chat,
                message_id=42,
                text="Prompt question",
            ),
            data="update_prompt:y",
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )
        return SimpleNamespace(
            update_id=update_id,
            callback_query=query,
            message=None,
            effective_message=None,
            my_chat_member=None,
        )

    msg = SimpleNamespace(
        message_id=42,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=from_user,
        text=text,
        caption=None,
        message_thread_id=None,
        photo=[],
        sticker=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
        reply_to_message=None,
    )
    return SimpleNamespace(
        update_id=update_id,
        message=msg,
        effective_message=msg,
        callback_query=None,
        my_chat_member=None,
    )


@pytest.mark.asyncio
async def test_early_ptb_ingress_observer_records_update_types_and_safe_corr(caplog):
    adapter = _make_adapter()

    # 1. Message update
    msg_update = _make_telegram_update(9101, text="/start")
    # 2. Callback update
    cb_update = _make_telegram_update(9102, is_callback=True)
    # 3. Membership update
    member_update = _make_telegram_update(9103, is_membership=True)

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._observe_ptb_update_ingress(msg_update, None)
        await adapter._observe_ptb_update_ingress(cb_update, None)
        await adapter._observe_ptb_update_ingress(member_update, None)

    log_text = caplog.text
    _assert_no_canaries(log_text)

    # Ingress markers logged
    assert "[telegram_ptb_update_ingress]" in log_text
    assert "update_type=message" in log_text
    assert "update_type=callback_query" in log_text
    assert "update_type=my_chat_member" in log_text
    assert "corr=" in log_text
    assert "timestamp_utc=" in log_text


@pytest.mark.asyncio
async def test_handler_dispatch_outcome_matched_vs_unhandled(caplog):
    adapter = _make_adapter()

    update_matched = _make_telegram_update(9201, text="/start")
    update_unhandled = _make_telegram_update(9202, text="unknown")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        # Ingress
        await adapter._observe_ptb_update_ingress(update_matched, None)
        await adapter._observe_ptb_update_ingress(update_unhandled, None)

        # Simulate handler matched for 9201
        adapter._record_handler_selection(9201, "command")

        # Dispatch outcome (group 1)
        await adapter._observe_ptb_update_dispatch_outcome(update_matched, None)
        await adapter._observe_ptb_update_dispatch_outcome(update_unhandled, None)

    log_text = caplog.text
    _assert_no_canaries(log_text)

    # Matched verification
    assert "handler=command" in log_text
    assert "handler_outcome=matched" in log_text

    # Unhandled verification
    assert "handler_outcome=unhandled" in log_text
    assert "handler=none" in log_text

    # Ensure in-flight cleanup prunes map
    assert 9201 not in adapter._in_flight_ptb_updates
    assert 9202 not in adapter._in_flight_ptb_updates


def test_ptb_handler_groups_registration_order():
    adapter = _make_adapter()
    app_mock = MagicMock()
    adapter._app = app_mock

    adapter._register_handlers()

    calls = app_mock.add_handler.call_args_list
    assert len(calls) >= 3, "Handlers must be registered across groups"

    groups = [call.kwargs.get("group", 0) for call in calls]
    assert -1 in groups, "Group -1 (early ingress observer) must be registered"
    assert 0 in groups, "Group 0 (business handlers) must be registered"
    assert 1 in groups, "Group 1 (dispatch outcome observer) must be registered"

    # Verify group -1 handler registration
    group_minus_1_calls = [c for c in calls if c.kwargs.get("group") == -1]
    assert len(group_minus_1_calls) == 1

    # Verify group 1 handler registration
    group_1_calls = [c for c in calls if c.kwargs.get("group") == 1]
    assert len(group_1_calls) == 1


@pytest.mark.asyncio
async def test_harmless_membership_update_acknowledged(caplog):
    adapter = _make_adapter()
    member_update = _make_telegram_update(9301, is_membership=True)

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._observe_ptb_update_ingress(member_update, None)
        await adapter._handle_my_chat_member(member_update, None)
        await adapter._observe_ptb_update_dispatch_outcome(member_update, None)

    log_text = caplog.text
    _assert_no_canaries(log_text)

    assert "[telegram_membership_update]" in log_text
    assert "update_type=my_chat_member" in log_text
    assert "outcome=acknowledged" in log_text
    assert "handler=my_chat_member" in log_text
    assert "handler_outcome=matched" in log_text


@pytest.mark.asyncio
async def test_non_allowlisted_start_public_onboarding_disabled(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    profile_store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr(
        "gateway.platforms.telegram.get_default_healbite_user_profile",
        lambda: profile_store,
    )
    monkeypatch.setattr(
        "gateway.healbite_user_profile.get_default_healbite_user_profile",
        lambda: profile_store,
    )

    adapter = _make_adapter()
    adapter._send_message_with_thread_fallback = AsyncMock()

    update = _make_telegram_update(9401, text="/start")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._observe_ptb_update_ingress(update, None)
        await adapter._handle_command(update, None)
        await adapter._observe_ptb_update_dispatch_outcome(update, None)
        # Direct start command attempt logs onboarding=denied
        handled = await adapter._maybe_handle_healbite_start_command(update.message)

    log_text = caplog.text
    _assert_no_canaries(log_text)

    # Ingress -> Matched
    assert "update_type=message" in log_text
    assert "handler=command" in log_text
    assert "handler_outcome=matched" in log_text

    # Authorization decision denied
    assert "[telegram_auth_decision]" in log_text
    assert "auth_decision=denied" in log_text
    assert "public_onboarding=false" in log_text

    # No message sent to unauthorized user when public onboarding is off
    adapter._send_message_with_thread_fallback.assert_not_called()

    assert handled is False
    assert "[healbite_onboarding_entry]" in log_text
    assert "onboarding=denied" in log_text


@pytest.mark.asyncio
async def test_non_allowlisted_start_public_onboarding_enabled(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS", raising=False)
    profile_store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr(
        "gateway.platforms.telegram.get_default_healbite_user_profile",
        lambda: profile_store,
    )
    monkeypatch.setattr(
        "gateway.healbite_user_profile.get_default_healbite_user_profile",
        lambda: profile_store,
    )

    adapter = _make_adapter()
    adapter._send_message_with_thread_fallback = AsyncMock(return_value=SimpleNamespace(message_id=777))

    update = _make_telegram_update(9501, text="/start")

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._observe_ptb_update_ingress(update, None)
        await adapter._handle_command(update, None)
        await adapter._observe_ptb_update_dispatch_outcome(update, None)

    log_text = caplog.text
    _assert_no_canaries(log_text)

    # Ingress -> Matched
    assert "update_type=message" in log_text
    assert "handler=command" in log_text
    assert "handler_outcome=matched" in log_text

    # Authorization decision public bypass
    assert "[telegram_auth_decision]" in log_text
    assert "auth_decision=public_bypass" in log_text
    assert "public_onboarding=true" in log_text

    # Onboarding entry started
    assert "[healbite_onboarding_entry]" in log_text
    assert "onboarding=started" in log_text

    adapter._send_message_with_thread_fallback.assert_called_once()


@pytest.mark.asyncio
async def test_response_outcome_success_and_failure_logged(caplog):
    adapter = _make_adapter()
    adapter._bot = MagicMock()
    adapter._bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=123))

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._send_message_with_thread_fallback(
            chat_id=5566778899,
            text="CANARY_MESSAGE_TEXT_PRIVATE_BODY",
        )

    log_text = caplog.text
    _assert_no_canaries(log_text)
    assert "[telegram_response_outcome]" in log_text
    assert "response_outcome=sent" in log_text

    # Now test failure outcome
    adapter._bot.send_message = AsyncMock(side_effect=RuntimeError("Network socket closed"))

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        with pytest.raises(RuntimeError):
            await adapter._send_message_with_thread_fallback(
                chat_id=5566778899,
                text="CANARY_MESSAGE_TEXT_PRIVATE_BODY",
            )

    log_text = caplog.text
    _assert_no_canaries(log_text)
    assert "response_outcome=failed" in log_text
    assert "error_type=RuntimeError" in log_text


def test_privacy_sanitization_invariants_across_all_markers(caplog):
    adapter = _make_adapter()
    msg = SimpleNamespace(
        message_id=42,
        chat=SimpleNamespace(id=5566778899, type="private"),
        from_user=SimpleNamespace(
            id=7788991122,
            username="CANARY_USERNAME_ALICE",
            first_name="CANARY_FIRSTNAME_BOB",
        ),
    )

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        # Log marker with all diagnostic fields and potential canary injections
        adapter._log_healbite_marker(
            "telegram_diagnostic_invariants_check",
            msg=msg,
            update_id=9999,
            timestamp_utc="2026-10-11T12:00:00+00:00",
            update_type="message",
            handler="command",
            handler_outcome="matched",
            auth_decision="public_bypass",
            onboarding="started",
            response_outcome="sent",
            # PII fields that must be dropped by allowlist or sanitized
            raw_text="CANARY_MESSAGE_TEXT_PRIVATE_BODY",
            token="SECRET_BOT_TOKEN_998877",
            user_id=7788991122,
            chat_id=5566778899,
        )

    log_text = caplog.text
    _assert_no_canaries(log_text)

    # Verify all safe fields are present
    assert "timestamp_utc=2026-10-11T12:00:00+00:00" in log_text
    assert "update_type=message" in log_text
    assert "handler=command" in log_text
    assert "handler_outcome=matched" in log_text
    assert "auth_decision=public_bypass" in log_text
    assert "onboarding=started" in log_text
    assert "response_outcome=sent" in log_text
    assert "corr=" in log_text
