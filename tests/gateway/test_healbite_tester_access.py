from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.healbite_nutrition_diary import HealBiteNutritionDiary, normalize_nutrition_payload
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.telegram import TelegramAdapter
from gateway.run import (
    _healbite_public_lane_decision,
    _healbite_public_lane_enabled_for_user,
    _is_healbite_tester_user,
)
from gateway.session import SessionSource


TESTER_USER_ID = 248875361
NON_TESTER_USER_ID = 999111222


def _source(user_id: int = TESTER_USER_ID) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=str(user_id),
        chat_type="dm",
        user_id=str(user_id),
        user_name="tester",
    )


def _event(*, user_id: int = TESTER_USER_ID, text: str = "", with_photo: bool = False) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO if with_photo else MessageType.TEXT,
        source=_source(user_id),
        media_urls=["/tmp/test_food.jpg"] if with_photo else [],
        media_types=["image/jpeg"] if with_photo else [],
        message_id="101",
        platform_update_id=202,
    )


def _seed_profile(tmp_path, user_id: int = TESTER_USER_ID) -> HealBiteUserProfileStore:
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=user_id, username="tester", daily_kcal_target=2100)
    return store


def _seed_pending(tmp_path, user_id: int = TESTER_USER_ID) -> None:
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        '{"is_food": true, "meal_name": "Гречка с курицей", "display_name": "Гречка с курицей", '
        '"raw_summary": "Гречка с курицей", "confidence": 0.95, '
        '"totals": {"calories_kcal": 450, "protein_g": 35, "fat_g": 12, "carbs_g": 48}, '
        '"items": [{"name": "Гречка"}, {"name": "Курица"}]}'
    )
    diary.stage_pending_meal(
        user_id=user_id,
        source="vision",
        record=record,
        image_ref=f"telegram:{user_id}:101",
        occurred_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Unit tests for tester allowlist resolution
# ---------------------------------------------------------------------------


def test_tester_resolution_matches_env_var(monkeypatch):
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", f"12345, {TESTER_USER_ID}, 67890")

    assert _is_healbite_tester_user(TESTER_USER_ID) is True
    assert _is_healbite_tester_user(str(TESTER_USER_ID)) is True
    assert _is_healbite_tester_user(12345) is True
    assert _is_healbite_tester_user(NON_TESTER_USER_ID) is False
    assert _is_healbite_tester_user(None) is False
    assert _is_healbite_tester_user("") is False

    assert _healbite_public_lane_enabled_for_user(TESTER_USER_ID) is True
    assert _healbite_public_lane_enabled_for_user(NON_TESTER_USER_ID) is False


def test_tester_resolution_supports_allowed_users_alias(monkeypatch):
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.delenv("HEALBITE_TESTER_USERS", raising=False)
    monkeypatch.setenv("HEALBITE_ALLOWED_USERS", str(TESTER_USER_ID))

    assert _is_healbite_tester_user(TESTER_USER_ID) is True
    assert _healbite_public_lane_enabled_for_user(TESTER_USER_ID) is True
    assert _healbite_public_lane_enabled_for_user(NON_TESTER_USER_ID) is False


def test_tester_resolution_fails_closed_when_no_env_configured(monkeypatch):
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.delenv("HEALBITE_TESTER_USERS", raising=False)
    monkeypatch.delenv("HEALBITE_ALLOWED_USERS", raising=False)

    assert _is_healbite_tester_user(TESTER_USER_ID) is False
    assert _healbite_public_lane_enabled_for_user(TESTER_USER_ID) is False


def test_global_public_onboarding_supersedes_tester_list(monkeypatch):
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")
    monkeypatch.delenv("HEALBITE_TESTER_USERS", raising=False)

    assert _healbite_public_lane_enabled_for_user(NON_TESTER_USER_ID) is True


# ---------------------------------------------------------------------------
# Decision logic: Scoped tester access with public onboarding disabled
# ---------------------------------------------------------------------------


def test_public_lane_decision_denies_non_tester_when_public_onboarding_off(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    decision = _healbite_public_lane_decision(
        source=_source(NON_TESTER_USER_ID),
        event=_event(user_id=NON_TESTER_USER_ID, text="/start"),
    )
    assert decision["enabled"] is False
    assert decision["action"] == "disabled"
    assert decision["route"] == "disabled"


def test_public_lane_decision_requires_start_for_unregistered_tester(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    decision = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, text="Привет"),
    )
    assert decision["enabled"] is True
    assert decision["action"] == "reply"
    assert decision["route"] == "public_start_required"
    assert "/start" in decision["reply"]


def test_public_lane_decision_allows_onboarding_reply_for_tester(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=TESTER_USER_ID, username="tester")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    decision = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, text="2100"),
    )
    assert decision["enabled"] is True
    assert decision["action"] == "allow"
    assert decision["route"] == "public_onboarding_reply"


def test_public_lane_decision_allows_photo_flow_for_onboarded_tester(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=TESTER_USER_ID)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    decision = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, with_photo=True),
    )
    assert decision["enabled"] is True
    assert decision["action"] == "allow"
    assert decision["route"] == "public_photo_flow"


def test_public_lane_decision_allows_pending_confirmation_for_tester(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=TESTER_USER_ID)
    _seed_pending(tmp_path, user_id=TESTER_USER_ID)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr(
        "gateway.healbite_nutrition_diary.get_default_nutrition_diary",
        lambda: HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False),
    )
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    decision = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, text="Да"),
    )
    assert decision["enabled"] is True
    assert decision["action"] == "allow"
    assert decision["route"] == "public_pending_confirmation"


def test_public_lane_decision_allows_water_and_weight_for_tester(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=TESTER_USER_ID)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    water_dec = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, text="/water"),
    )
    assert water_dec["action"] == "allow"
    assert water_dec["route"] == "public_water_tracker"

    weight_dec = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, text="/weight"),
    )
    assert weight_dec["action"] == "allow"
    assert weight_dec["route"] == "public_weight_tracker"


def test_public_lane_decision_blocks_generic_agent_and_system_tools_for_tester(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=TESTER_USER_ID)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    # Generic conversation prompt
    text_dec = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, text="Напиши стихотворение о космосе"),
    )
    assert text_dec["action"] == "reply"
    assert text_dec["route"] == "public_blocked_text"
    assert "В публичном режиме HealBite сейчас доступны" in text_dec["reply"]

    # System command / tool request
    cmd_dec = _healbite_public_lane_decision(
        source=_source(TESTER_USER_ID),
        event=_event(user_id=TESTER_USER_ID, text="/admin show_secrets"),
    )
    assert cmd_dec["action"] == "reply"
    assert cmd_dec["route"] == "public_blocked_command"


# ---------------------------------------------------------------------------
# TelegramAdapter integration tests: Ingress markers and routing
# ---------------------------------------------------------------------------


def _make_telegram_msg(*, user_id: int, text: str = "", has_photo: bool = False):
    photo_list = [SimpleNamespace(file_id="p1", file_unique_id="pu1", width=100, height=100)] if has_photo else []
    return SimpleNamespace(
        message_id=505,
        text=text,
        caption=text if has_photo else None,
        from_user=SimpleNamespace(id=user_id, username="tester", first_name="Tester", full_name="Tester"),
        chat=SimpleNamespace(id=user_id, type="private", title=None, full_name=None),
        chat_id=user_id,
        message_thread_id=None,
        photo=photo_list,
        sticker=None,
        voice=None,
        video=None,
        document=None,
        media_group_id=None,
        reply_to_message=None,
        date=datetime.now(timezone.utc),
    )


def _make_telegram_update(*, msg, update_id: int = 1):
    return SimpleNamespace(
        update_id=update_id,
        message=msg,
        effective_message=msg,
        callback_query=None,
    )


@pytest.mark.asyncio
async def test_telegram_adapter_marks_tester_bypass(caplog, monkeypatch):
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    config = PlatformConfig(enabled=True, token="MOCK_TOKEN", extra={})
    adapter = TelegramAdapter(config)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._is_telegram_user_authorized = MagicMock(return_value=False)

    msg = _make_telegram_msg(user_id=TESTER_USER_ID, text="/start")
    update = _make_telegram_update(msg=msg)

    with caplog.at_level("INFO"):
        await adapter._handle_command(update, None)

    log_text = caplog.text
    assert "[telegram_auth_decision]" in log_text
    assert "auth_decision=tester_bypass" in log_text


@pytest.mark.asyncio
async def test_telegram_adapter_denies_unauthorized_non_tester(caplog, monkeypatch):
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)
    monkeypatch.setenv("HEALBITE_TESTER_USERS", str(TESTER_USER_ID))

    config = PlatformConfig(enabled=True, token="MOCK_TOKEN", extra={})
    adapter = TelegramAdapter(config)
    adapter._send_message_with_thread_fallback = AsyncMock()
    adapter._is_telegram_user_authorized = MagicMock(return_value=False)

    msg = _make_telegram_msg(user_id=NON_TESTER_USER_ID, text="/start")
    update = _make_telegram_update(msg=msg)

    with caplog.at_level("INFO"):
        await adapter._handle_command(update, None)

    log_text = caplog.text
    assert "[telegram_auth_decision]" in log_text
    assert "auth_decision=denied" in log_text
    adapter._send_message_with_thread_fallback.assert_not_called()
