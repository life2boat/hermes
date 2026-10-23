from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.healbite_nutrition_diary import HealBiteNutritionDiary, normalize_nutrition_payload
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.telegram import TelegramAdapter
from gateway.run import (
    _healbite_public_lane_decision,
    _healbite_public_lane_enabled_for_user,
    _is_healbite_registered_user,
    _is_healbite_registered_user_allowed,
    _is_healbite_tester_user,
    _vision_safe_fallback_text,
)
from gateway.session import SessionSource


TESTER_USER_ID = 248875361
REGISTERED_USER_ID = 280200021
STRANGER_USER_ID = 999888777


def _source(user_id: int = REGISTERED_USER_ID) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=str(user_id),
        chat_type="dm",
        user_id=str(user_id),
        user_name="vladimir",
    )


def _event(*, user_id: int = REGISTERED_USER_ID, text: str = "", with_photos: list[str] | None = None) -> MessageEvent:
    photos = with_photos or []
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO if photos else MessageType.TEXT,
        source=_source(user_id),
        media_urls=list(photos),
        media_types=["image/jpeg"] * len(photos),
        message_id="501",
        platform_update_id=183306210,
    )


def _make_telegram_msg(*, user_id: int, text: str = "", photos: list[str] | None = None, media_group_id: str | None = None):
    photo_list = [SimpleNamespace(file_id=f"f_{i}", file_unique_id=f"u_{i}", width=100, height=100) for i in range(len(photos or []))]
    return SimpleNamespace(
        message_id=505,
        text=text,
        caption=text if photos else None,
        from_user=SimpleNamespace(id=user_id, username="vladimir", first_name="Vladimir", full_name="Vladimir"),
        chat=SimpleNamespace(id=user_id, type="private", title=None, full_name=None),
        chat_id=user_id,
        message_thread_id=None,
        photo=photo_list,
        sticker=None,
        voice=None,
        video=None,
        document=None,
        media_group_id=media_group_id,
        reply_to_message=None,
        date=datetime.now(timezone.utc),
    )


def _make_telegram_update(*, msg, update_id: int = 183306210):
    return SimpleNamespace(
        update_id=update_id,
        message=msg,
        effective_message=msg,
        callback_query=None,
    )


# ---------------------------------------------------------------------------
# Incident Root Cause Reproduction & Registered User Bypass Verification
# ---------------------------------------------------------------------------


def test_incident_reproduction_registered_user_denied_without_tester_or_flag(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=REGISTERED_USER_ID, username="vladimir", daily_kcal_target=2200)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))  # only Anastasia
    monkeypatch.delenv("HEALBITE_ALLOW_REGISTERED_USERS", raising=False)

    assert _is_healbite_tester_user(REGISTERED_USER_ID) is False
    assert _is_healbite_registered_user(REGISTERED_USER_ID) is True
    assert _is_healbite_registered_user_allowed(REGISTERED_USER_ID) is False
    assert _healbite_public_lane_enabled_for_user(REGISTERED_USER_ID) is False

    decision = _healbite_public_lane_decision(
        source=_source(REGISTERED_USER_ID),
        event=_event(user_id=REGISTERED_USER_ID, with_photos=["/tmp/p1.jpg", "/tmp/p2.jpg"]),
    )
    assert decision["enabled"] is False


def test_registered_user_allowed_when_flag_enabled(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=REGISTERED_USER_ID, username="vladimir", daily_kcal_target=2200)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))
    monkeypatch.setenv("HEALBITE_ALLOW_REGISTERED_USERS", "true")

    assert _is_healbite_registered_user_allowed(REGISTERED_USER_ID) is True
    assert _healbite_public_lane_enabled_for_user(REGISTERED_USER_ID) is True

    decision = _healbite_public_lane_decision(
        source=_source(REGISTERED_USER_ID),
        event=_event(user_id=REGISTERED_USER_ID, with_photos=["/tmp/p1.jpg", "/tmp/p2.jpg"]),
    )
    assert decision["enabled"] is True
    assert decision["action"] == "allow"
    assert decision["route"] == "public_photo_flow"


def test_registered_user_in_tester_allowlist(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=REGISTERED_USER_ID, username="vladimir", daily_kcal_target=2200)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", f"{TESTER_USER_ID},{REGISTERED_USER_ID}")

    assert _is_healbite_tester_user(REGISTERED_USER_ID) is True
    assert _healbite_public_lane_enabled_for_user(REGISTERED_USER_ID) is True


@pytest.mark.asyncio
async def test_telegram_adapter_marks_registered_user_bypass(tmp_path, caplog, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=REGISTERED_USER_ID, username="vladimir", daily_kcal_target=2200)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.platforms.telegram.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))
    monkeypatch.setenv("HEALBITE_ALLOW_REGISTERED_USERS", "true")

    config = PlatformConfig(enabled=True, token="MOCK_TOKEN", extra={})
    adapter = TelegramAdapter(config)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._is_telegram_user_authorized = MagicMock(return_value=False)

    msg = _make_telegram_msg(user_id=REGISTERED_USER_ID, text="«🍎 Дневник»")
    update = _make_telegram_update(msg=msg, update_id=183306209)

    with caplog.at_level("INFO"):
        await adapter._handle_command(update, None)

    log_text = caplog.text
    assert "[telegram_auth_decision]" in log_text
    assert "auth_decision=registered_user_bypass" in log_text


def test_stranger_receives_polite_notification_when_configured(monkeypatch):
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))
    monkeypatch.setenv("HEALBITE_NOTIFY_UNAUTHORIZED", "true")

    decision = _healbite_public_lane_decision(
        source=_source(STRANGER_USER_ID),
        event=_event(user_id=STRANGER_USER_ID, with_photos=["/tmp/stranger_food.jpg"]),
    )
    assert decision["enabled"] is True
    assert decision["action"] == "reply"
    assert decision["route"] == "public_lane_restricted"
    assert "закрытого тестирования" in decision["reply"]


# ---------------------------------------------------------------------------
# Scenarios T1-T10
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_t7_album_media_group_aggregation_and_debounce():
    """T1 & T7: Two photos of same portion arrive in rapid succession and are debounced into one logical event."""
    config = PlatformConfig(enabled=True, token="MOCK_TOKEN", extra={})
    adapter = TelegramAdapter(config)
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0.05
    handled_events = []
    adapter.handle_message = AsyncMock(side_effect=lambda ev: handled_events.append(ev))

    ev1 = _event(user_id=REGISTERED_USER_ID, with_photos=["/tmp/slice1.jpg"])
    ev2 = _event(user_id=REGISTERED_USER_ID, with_photos=["/tmp/slice2.jpg"])

    await adapter._queue_media_group_event("mg_123", ev1)
    await asyncio.sleep(0.01)
    await adapter._queue_media_group_event("mg_123", ev2)

    await asyncio.sleep(0.1)

    assert len(handled_events) == 1
    merged = handled_events[0]
    assert len(merged.media_urls) == 2
    assert "/tmp/slice1.jpg" in merged.media_urls
    assert "/tmp/slice2.jpg" in merged.media_urls


@pytest.mark.asyncio
async def test_t5_album_flush_exception_is_observable_and_notifies_chat(caplog):
    """T5: Exception during album flush logs [media_group_flush_failed] and sends safe fallback notice."""
    config = PlatformConfig(enabled=True, token="MOCK_TOKEN", extra={})
    adapter = TelegramAdapter(config)
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0.01
    adapter.send = AsyncMock()
    adapter.handle_message = AsyncMock(side_effect=RuntimeError("Simulated pipeline crash"))

    ev = _event(user_id=REGISTERED_USER_ID, with_photos=["/tmp/fail1.jpg", "/tmp/fail2.jpg"])

    with caplog.at_level("ERROR"):
        await adapter._queue_media_group_event("mg_fail", ev)
        await asyncio.sleep(0.05)

    assert "[media_group_flush_failed]" in caplog.text
    adapter.send.assert_called_once()
    assert adapter.send.call_args[0][0] == str(REGISTERED_USER_ID)
    assert adapter.send.call_args[0][1] == _vision_safe_fallback_text()


@pytest.mark.asyncio
async def test_t6_media_group_cancelled_cleanly(caplog):
    """T6: Cancellation of prior debounce task is logged and cleans up safely without infinite wait."""
    config = PlatformConfig(enabled=True, token="MOCK_TOKEN", extra={})
    adapter = TelegramAdapter(config)
    adapter.MEDIA_GROUP_WAIT_SECONDS = 0.1
    adapter.handle_message = AsyncMock()

    ev1 = _event(user_id=REGISTERED_USER_ID, with_photos=["/tmp/a.jpg"])
    ev2 = _event(user_id=REGISTERED_USER_ID, with_photos=["/tmp/b.jpg"])

    with caplog.at_level("INFO"):
        await adapter._queue_media_group_event("mg_cancel", ev1)
        await asyncio.sleep(0.01)
        # Queue ev2 while ev1 task is sleeping -> cancels ev1 task
        await adapter._queue_media_group_event("mg_cancel", ev2)
        await asyncio.sleep(0.05)

    assert "[media_group_cancelled]" in caplog.text


def test_t8_update_deduplication():
    """T8: Deduplication filter drops repeated update_id."""
    dedup = MessageDeduplicator(max_size=100, ttl_seconds=300)

    assert dedup.is_duplicate("media:183306210") is False
    assert dedup.is_duplicate("media:183306210") is True


def test_t9_hipe_portion_approximation_fallback():
    """T9: Portion estimation without gram weight defaults gracefully with confidence and estimates."""
    record = normalize_nutrition_payload(
        '{"is_food": true, "meal_name": "Паста карбонара", "confidence": 0.85, '
        '"calories_kcal": 550, "protein_g": 20, "fat_g": 25, "carbs_g": 60}'
    )
    assert record.is_food is True
    assert record.calories_kcal == 550


def test_t10_atomic_save_preserves_pending_state_on_diary_failure(tmp_path):
    """T10: Failed diary log operation preserves pending state without data loss."""
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        '{"is_food": true, "meal_name": "Омлет", "calories_kcal": 280, "protein_g": 18, "fat_g": 22, "carbs_g": 2}'
    )
    diary.stage_pending_meal(
        user_id=REGISTERED_USER_ID,
        source="vision",
        record=record,
        image_ref="telegram:280200021:501",
        occurred_at=datetime.now(timezone.utc),
    )

    pending_before = diary.get_pending_state(REGISTERED_USER_ID)
    assert pending_before is not None

    # Simulate atomic save failure
    with patch.object(diary, "_atomic_save_pending_record", side_effect=RuntimeError("Disk write failed")):
        with pytest.raises(RuntimeError):
            diary.confirm_pending_meal(REGISTERED_USER_ID)

    pending_after = diary.get_pending_state(REGISTERED_USER_ID)
    assert pending_after is not None
    assert pending_after.record.meal_name == "Омлет"
