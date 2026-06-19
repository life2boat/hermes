from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from gateway.config import Platform
from gateway.healbite_nutrition_diary import HealBiteNutritionDiary, normalize_nutrition_payload
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import _healbite_event_correlation_id, _healbite_public_lane_decision
from gateway.session import SessionSource


def _source(user_id: int = 100) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=str(user_id),
        chat_type="dm",
        user_id=str(user_id),
        user_name="tester",
    )


def _event(*, user_id: int = 100, text: str = "", with_photo: bool = False) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.PHOTO if with_photo else MessageType.TEXT,
        source=_source(user_id),
        media_urls=["/tmp/photo.jpg"] if with_photo else [],
        media_types=["image/jpeg"] if with_photo else [],
        message_id="42",
        platform_update_id=99,
    )


def _seed_profile(tmp_path, user_id: int = 100) -> HealBiteUserProfileStore:
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.upsert_user_profile(user_id=user_id, username="tester", daily_kcal_target=2000)
    return store


def _seed_pending(tmp_path, user_id: int = 100) -> None:
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    record = normalize_nutrition_payload(
        '{"is_food": true, "meal_name": "Борщ", "display_name": "Борщ", "raw_summary": "Борщ", "confidence": 0.9, "totals": {"calories_kcal": 320, "protein_g": 15, "fat_g": 11, "carbs_g": 29}, "items": [{"name": "Борщ"}]}'
    )
    diary.stage_pending_meal(
        user_id=user_id,
        source="vision",
        record=record,
        image_ref=f"telegram:{user_id}:42",
        occurred_at=datetime.now(timezone.utc),
    )


def test_public_lane_flag_off_preserves_closed_policy(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)

    decision = _healbite_public_lane_decision(source=_source(111), event=_event(user_id=111, text="/start"))

    assert decision["enabled"] is False


def test_public_lane_new_user_requires_start_when_enabled(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(source=_source(112), event=_event(user_id=112, text="привет"))

    assert decision["enabled"] is True
    assert decision["action"] == "reply"
    assert decision["route"] == "public_start_required"
    assert "/start" in decision["reply"]


def test_public_lane_new_user_photo_is_blocked_before_onboarding(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(source=_source(113), event=_event(user_id=113, with_photo=True))

    assert decision["action"] == "reply"
    assert decision["route"] == "public_start_required"


def test_public_lane_onboarding_reply_is_allowed(tmp_path, monkeypatch):
    store = HealBiteUserProfileStore(db_path=tmp_path / "healbite.db")
    store.begin_onboarding(user_id=114, username="tester")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(source=_source(114), event=_event(user_id=114, text="2000"))

    assert decision["action"] == "allow"
    assert decision["route"] == "public_onboarding_reply"


def test_public_lane_onboarded_photo_is_allowed(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=115)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(source=_source(115), event=_event(user_id=115, with_photo=True))

    assert decision["action"] == "allow"
    assert decision["route"] == "public_photo_flow"


def test_public_lane_diary_summary_text_is_allowed(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=116)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(
        source=_source(116),
        event=_event(user_id=116, text="что у меня сегодня в дневнике?"),
    )

    assert decision["action"] == "allow"
    assert decision["route"] == "public_diary_summary"


def test_public_lane_diary_correction_text_is_allowed(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=117)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(
        source=_source(117),
        event=_event(user_id=117, text="исправь последнюю запись на 400 ккал"),
    )

    assert decision["action"] == "allow"
    assert decision["route"] == "public_diary_correction"


def test_public_lane_pending_reply_is_allowed(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=118)
    _seed_pending(tmp_path, user_id=118)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr(
        "gateway.healbite_nutrition_diary.get_default_nutrition_diary",
        lambda: HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False),
    )
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(
        source=_source(118),
        event=_event(user_id=118, text="наверное тут ошибка"),
    )

    assert decision["action"] == "allow"
    assert decision["route"] == "public_pending_confirmation"


def test_public_lane_blocks_generic_tool_request_after_onboarding(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=119)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(
        source=_source(119),
        event=_event(user_id=119, text="прочитай файл и запусти terminal"),
    )

    assert decision["action"] == "reply"
    assert decision["route"] == "public_blocked_text"
    assert "фото еды" in decision["reply"]


def test_public_lane_admin_flow_is_unchanged_when_flag_disabled(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=120)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.delenv("HEALBITE_PUBLIC_ONBOARDING", raising=False)

    decision = _healbite_public_lane_decision(source=_source(120), event=_event(user_id=120, with_photo=True))

    assert decision["enabled"] is False


def test_public_lane_correlation_marker_is_hashed_and_has_no_pii():
    corr = _healbite_event_correlation_id(_event(user_id=968323641, text="/start"))

    assert corr
    assert "968323641" not in corr
    assert ":" not in corr
