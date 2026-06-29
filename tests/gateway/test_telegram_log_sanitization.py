from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.telegram import TelegramAdapter
from gateway.session import SessionSource


PII_CANARIES = {
    "PII_USERNAME_SHOULD_NOT_APPEAR",
    "PII_FIRST_NAME_SHOULD_NOT_APPEAR",
    "PII_LAST_NAME_SHOULD_NOT_APPEAR",
    "PII_MESSAGE_TEXT_SHOULD_NOT_APPEAR",
    "PII_CAPTION_SHOULD_NOT_APPEAR",
    "PII_EMAIL_SHOULD_NOT_APPEAR@example.test",
    "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
    "PII_FILE_UNIQUE_ID_SHOULD_NOT_APPEAR",
    "4242424242",
    "3131313131",
    "PII_S70C_WEIGHT_VALUE_821",
    "PII_S70C_PROFILE_FIELD",
    "PII_S70C_REMINDER_TIME",
    "PII_S70C_RECIPIENT",
    "PII_S70C_CALLBACK",
    "PII_S70C_EXCEPTION_BODY",
    "PII_S70C_SESSION_KEY",
}


def _adapter() -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter.handle_message = AsyncMock()
    return adapter


def _message(*, text: str = "PII_MESSAGE_TEXT_SHOULD_NOT_APPEAR", caption: str | None = None):
    user = SimpleNamespace(
        id=3131313131,
        username="PII_USERNAME_SHOULD_NOT_APPEAR",
        first_name="PII_FIRST_NAME_SHOULD_NOT_APPEAR",
        last_name="PII_LAST_NAME_SHOULD_NOT_APPEAR",
        full_name="PII_FIRST_NAME_SHOULD_NOT_APPEAR PII_LAST_NAME_SHOULD_NOT_APPEAR",
    )
    chat = SimpleNamespace(id=4242424242, type="private", is_forum=False)
    return SimpleNamespace(
        message_id=989898,
        text=text,
        caption=caption,
        chat=chat,
        from_user=user,
        message_thread_id=None,
        photo=None,
        sticker=None,
        document=None,
        video=None,
        audio=None,
        voice=None,
    )


def _assert_no_canaries(log_text: str) -> None:
    for canary in PII_CANARIES:
        assert canary not in log_text


def test_healbite_marker_keeps_observability_without_raw_pii(caplog):
    adapter = _adapter()
    msg = _message()

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_marker(
            "telegram_update_received",
            msg=msg,
            update_id=12345,
            route="profile",
            outcome="success",
            content_type="text",
            has_text=True,
            text_length=len(msg.text),
            user_id=3131313131,
            chat_id=4242424242,
            username="PII_USERNAME_SHOULD_NOT_APPEAR",
            message_text=msg.text,
        )

    assert "telegram_update_received" in caplog.text
    assert "corr=" in caplog.text
    assert "route=profile" in caplog.text
    assert "outcome=success" in caplog.text
    assert f"text_length={len(msg.text)}" in caplog.text
    _assert_no_canaries(caplog.text)


def test_healbite_marker_redacts_dynamic_allowlisted_values(caplog):
    adapter = _adapter()

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_marker(
            "healbite_route_selected",
            route="keyboard_action",
            action="PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
            outcome="allowed",
        )

    assert "route=keyboard_action" in caplog.text
    assert "action=redacted" in caplog.text
    assert "outcome=allowed" in caplog.text
    _assert_no_canaries(caplog.text)


@pytest.mark.asyncio
async def test_photo_batch_log_does_not_include_session_batch_key(caplog, monkeypatch):
    adapter = _adapter()
    event = MessageEvent(
        text="PII_CAPTION_SHOULD_NOT_APPEAR",
        message_type=MessageType.PHOTO,
        media_urls=["/tmp/PII_FILE_UNIQUE_ID_SHOULD_NOT_APPEAR.jpg"],
        media_types=["image/jpeg"],
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="4242424242",
            chat_type="dm",
            user_id="3131313131",
            user_name="PII_USERNAME_SHOULD_NOT_APPEAR",
        ),
    )
    batch_key = "telegram:4242424242:3131313131:PII_USERNAME_SHOULD_NOT_APPEAR:photo-burst"
    adapter._pending_photo_batches[batch_key] = event

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr("gateway.platforms.telegram.asyncio.sleep", _no_sleep)
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._flush_photo_batch(batch_key)

    assert "Flushing photo batch media_count=1" in caplog.text
    _assert_no_canaries(caplog.text)


def test_observed_group_log_does_not_include_chat_or_sender(caplog):
    adapter = _adapter()
    msg = _message()
    event = MessageEvent(
        text="PII_MESSAGE_TEXT_SHOULD_NOT_APPEAR",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="4242424242",
            chat_type="group",
            user_id="3131313131",
            user_name="PII_USERNAME_SHOULD_NOT_APPEAR",
        ),
    )

    class Store:
        def get_or_create_session(self, _source):
            return SimpleNamespace(session_id="synthetic-session")

        def append_to_transcript(self, _session_id, _entry):
            return None

    adapter._session_store = Store()
    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._observe_unmentioned_group_message(msg, MessageType.TEXT, event=event)

    assert "Telegram group message observed" in caplog.text
    _assert_no_canaries(caplog.text)


@pytest.mark.asyncio
async def test_sticker_logs_do_not_include_file_unique_id(caplog, monkeypatch):
    adapter = _adapter()
    msg = _message()
    msg.sticker = SimpleNamespace(
        is_animated=False,
        is_video=False,
        emoji="",
        set_name="",
        file_unique_id="PII_FILE_UNIQUE_ID_SHOULD_NOT_APPEAR",
    )
    event = MessageEvent(text="", message_type=MessageType.TEXT)

    import gateway.sticker_cache as sticker_cache

    monkeypatch.setattr(
        sticker_cache,
        "get_cached_description",
        lambda _file_unique_id: {"description": "synthetic sticker", "emoji": "", "set_name": ""},
    )

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        await adapter._handle_sticker(msg, event)

    assert "Sticker cache hit" in caplog.text
    _assert_no_canaries(caplog.text)



def _corr_values(log_text: str) -> list[str]:
    values: list[str] = []
    for token in log_text.split():
        if token.startswith("corr="):
            values.append(token.split("=", 1)[1])
    return values


def test_correlation_id_is_stable_within_one_turn_and_random_across_turns(caplog):
    adapter = _adapter()
    msg = _message()
    other_msg = _message()

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_marker("telegram_update_received", msg=msg, update_id=12345, route="profile")
        adapter._log_healbite_marker("healbite_reply_sent", msg=msg, update_id=12345, route="profile")
        adapter._log_healbite_marker("telegram_update_received", msg=other_msg, update_id=12346, route="diary")

    corr_values = _corr_values(caplog.text)
    assert len(corr_values) == 3
    assert corr_values[0] == corr_values[1]
    assert corr_values[2] != corr_values[0]
    assert corr_values[0] != "3131313131"
    assert corr_values[0] != "4242424242"
    _assert_no_canaries(caplog.text)


def test_malformed_update_without_message_gets_safe_correlation_id(caplog):
    adapter = _adapter()

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_marker("telegram_update_received", route="unknown")

    corr_values = _corr_values(caplog.text)
    assert len(corr_values) == 1
    assert len(corr_values[0]) == 12
    assert corr_values[0] != "3131313131"
    assert corr_values[0] != "4242424242"


def test_all_dynamic_allowlisted_fields_redact_pii_canaries(caplog):
    adapter = _adapter()
    dynamic_fields = {
        "source": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "route": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "action": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "lane": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "outcome": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "result": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "content_type": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "command": "/water PII_MESSAGE_TEXT_SHOULD_NOT_APPEAR",
        "error_type": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "kind": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "context": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "mime": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
        "size_bucket": "PII_CALLBACK_SECRET_SHOULD_NOT_APPEAR",
    }

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_marker("telegram_update_received", **dynamic_fields)

    assert "telegram_update_received" in caplog.text
    assert "redacted" in caplog.text
    _assert_no_canaries(caplog.text)


def test_unknown_fields_are_dropped_not_logged(caplog):
    adapter = _adapter()

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_marker(
            "telegram_update_received",
            route="profile",
            raw_text="PII_MESSAGE_TEXT_SHOULD_NOT_APPEAR",
            chat_id="4242424242",
        )

    assert "route=profile" in caplog.text
    assert "raw_text" not in caplog.text
    assert "chat_id" not in caplog.text
    _assert_no_canaries(caplog.text)


def test_weight_tracker_marker_drops_sensitive_health_fields(caplog):
    adapter = _adapter()

    with caplog.at_level("INFO", logger="gateway.platforms.telegram"):
        adapter._log_healbite_marker(
            "healbite_route_selected",
            route="weight",
            action="reminder",
            outcome="allowed",
            weight_kg="PII_S70C_WEIGHT_VALUE_821",
            profile_field="PII_S70C_PROFILE_FIELD",
            reminder_time="PII_S70C_REMINDER_TIME",
            recipient="PII_S70C_RECIPIENT",
            callback_data="PII_S70C_CALLBACK",
            exception_body="PII_S70C_EXCEPTION_BODY",
            session_key="PII_S70C_SESSION_KEY",
        )

    assert "route=weight" in caplog.text
    assert "action=reminder" in caplog.text
    _assert_no_canaries(caplog.text)
