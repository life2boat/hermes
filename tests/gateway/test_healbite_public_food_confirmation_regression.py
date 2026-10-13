from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from gateway.config import Platform
from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingMealPayload,
    normalize_nutrition_payload,
)
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import (
    GatewayRunner,
    _healbite_public_lane_decision,
)
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


def _food_item(
    visible_name: str,
    normalized_name: str,
    grams_min: float | None,
    grams_max: float | None,
    *,
    confidence: float = 0.9,
    is_sauce: bool = False,
    uncertainty: str = "",
    preparation: str = "",
) -> FoodVisionItem:
    return FoodVisionItem(
        visible_name=visible_name,
        normalized_name=normalized_name,
        confidence=confidence,
        estimated_grams_min=grams_min,
        estimated_grams_max=grams_max,
        preparation=preparation,
        is_sauce=is_sauce,
        uncertainty=uncertainty,
    )


def _inventory(*items: FoodVisionItem, overall_confidence: float = 0.91) -> FoodVisionInventory:
    return FoodVisionInventory(
        schema_version=FOOD_VISION_SCHEMA_VERSION,
        items=list(items),
        overall_confidence=overall_confidence,
        needs_user_confirmation=False,
        warnings=[],
    )


def _seed_pending_inventory(tmp_path, user_id: int = 100, items: list[FoodVisionItem] | None = None) -> PendingFoodVisionInventory:
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    if items is None:
        items = [
            _food_item("куриное филе", "куриное филе", 140, 160),
            _food_item("огурцы", "огурцы", 60, 80),
        ]
    inv = _inventory(*items)
    return diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv,
        image_ref=f"telegram:{user_id}:42",
        occurred_at=datetime.now(timezone.utc),
    )


def test_reproduce_real_user_photo_reply_cancel_failure_fixed(tmp_path, monkeypatch):
    """
    Reproduces the 2026-10-13 15:46 UTC+7 incident:
    Photo recognized -> pending inventory created -> user replies 'Нет' ->
    Public lane must allow the turn (public_pending_confirmation) and cancel the inventory.
    """
    store = _seed_profile(tmp_path, user_id=101)
    _seed_pending_inventory(tmp_path, user_id=101)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(
        source=_source(101),
        event=_event(user_id=101, text="Нет"),
    )

    assert decision["action"] == "allow"
    assert decision["route"] == "public_pending_confirmation"

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    response = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Нет",
        history=[],
        source=_source(101),
        session_id="session-101",
        event=None,
    )

    assert response is not None
    assert response["api_calls"] == 0
    assert response["tools"] == []
    assert "Отменено" in response["final_response"]
    assert diary.get_pending_inventory(101) is None
    assert diary.get_pending_meal(101) is None
    assert diary.get_daily_summary(user_id=101)["entry_count"] == 0


def test_cancel_variants_cancel_pending_inventory(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=102)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    for cancel_text in ["отмена", "cancel", "Неа", "no"]:
        _seed_pending_inventory(tmp_path, user_id=102)
        assert diary.get_pending_inventory(102) is not None

        decision = _healbite_public_lane_decision(
            source=_source(102),
            event=_event(user_id=102, text=cancel_text),
        )
        assert decision["action"] == "allow"
        assert decision["route"] == "public_pending_confirmation"

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._healbite_nutrition_diary = diary
        response = runner._maybe_handle_healbite_pending_meal_confirmation(
            message=cancel_text,
            history=[],
            source=_source(102),
            session_id="session-102",
            event=None,
        )
        assert response is not None
        assert diary.get_pending_inventory(102) is None
        assert diary.get_daily_summary(user_id=102)["entry_count"] == 0


def test_inventory_review_actions_allowed_and_mutated(tmp_path, monkeypatch):
    """
    Verifies that 'Вес N', 'Исправить N', 'Удалить N', 'Добавить' are allowed
    in public lane and correctly mutate the pending inventory.
    """
    store = _seed_profile(tmp_path, user_id=103)
    _seed_pending_inventory(tmp_path, user_id=103)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary

    # 1. Update weight of item 1
    decision = _healbite_public_lane_decision(
        source=_source(103),
        event=_event(user_id=103, text="Вес 1: 180"),
    )
    assert decision["action"] == "allow"
    assert decision["route"] == "public_pending_confirmation"

    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Вес 1: 180",
        history=[],
        source=_source(103),
        session_id="session-103",
        event=None,
    )
    assert resp is not None
    inv = diary.get_pending_inventory(103)
    assert inv is not None
    assert inv.items[0].selected_grams == 180

    # 2. Replace item 2
    decision = _healbite_public_lane_decision(
        source=_source(103),
        event=_event(user_id=103, text="Исправить 2: вафли, 150 г"),
    )
    assert decision["action"] == "allow"

    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Исправить 2: вафли, 150 г",
        history=[],
        source=_source(103),
        session_id="session-103",
        event=None,
    )
    assert resp is not None
    inv = diary.get_pending_inventory(103)
    assert inv.items[1].visible_name == "вафли"
    assert inv.items[1].selected_grams == 150

    # 3. Delete item 2
    decision = _healbite_public_lane_decision(
        source=_source(103),
        event=_event(user_id=103, text="Удалить 2"),
    )
    assert decision["action"] == "allow"

    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Удалить 2",
        history=[],
        source=_source(103),
        session_id="session-103",
        event=None,
    )
    assert resp is not None
    inv = diary.get_pending_inventory(103)
    assert len(inv.items) == 1

    # 4. Add item
    decision = _healbite_public_lane_decision(
        source=_source(103),
        event=_event(user_id=103, text="Добавить: огурцы, 50 г"),
    )
    assert decision["action"] == "allow"

    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Добавить: огурцы, 50 г",
        history=[],
        source=_source(103),
        session_id="session-103",
        event=None,
    )
    assert resp is not None
    inv = diary.get_pending_inventory(103)
    assert len(inv.items) == 2
    assert inv.items[1].visible_name == "огурцы"
    assert diary.get_daily_summary(user_id=103)["entry_count"] == 0


def test_missing_weight_blocks_confirmation_without_saving(tmp_path, monkeypatch):
    """
    Confirms that 'Да' does not bypass weight validation.
    If an item has no selected_grams, confirmation requires weight.
    """
    store = _seed_profile(tmp_path, user_id=104)
    items = [_food_item("суп", "суп", None, None)]
    _seed_pending_inventory(tmp_path, user_id=104, items=items)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(
        source=_source(104),
        event=_event(user_id=104, text="Да"),
    )
    assert decision["action"] == "allow"

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Да",
        history=[],
        source=_source(104),
        session_id="session-104",
        event=None,
    )
    assert resp is not None
    assert "укажите вес" in resp["final_response"].casefold()
    assert diary.get_pending_inventory(104) is not None
    assert diary.get_pending_meal(104) is None
    assert diary.get_daily_summary(user_id=104)["entry_count"] == 0


def test_unknown_component_blocks_confirmation(tmp_path, monkeypatch):
    store = _seed_profile(tmp_path, user_id=105)
    items = [_food_item("неведомая субстанция", "неведомая субстанция", 100, 100)]
    _seed_pending_inventory(tmp_path, user_id=105, items=items)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision = _healbite_public_lane_decision(
        source=_source(105),
        event=_event(user_id=105, text="Да"),
    )
    assert decision["action"] == "allow"

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Да",
        history=[],
        source=_source(105),
        session_id="session-105",
        event=None,
    )
    assert resp is not None
    assert diary.get_pending_inventory(105) is not None
    assert diary.get_pending_meal(105) is None
    assert diary.get_daily_summary(user_id=105)["entry_count"] == 0


def test_full_two_stage_confirmation_workflow_saves_to_diary(tmp_path, monkeypatch):
    """
    Tests complete lifecycle:
    1. Photo recognized -> PendingFoodVisionInventory created.
    2. User confirms inventory ('Да') -> PendingMealPayload staged (awaiting save confirmation).
    3. User confirms meal save ('Да') -> Saved to SQLite diary entry (nutrition_log).
    """
    store = _seed_profile(tmp_path, user_id=106)
    _seed_pending_inventory(tmp_path, user_id=106)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary

    # Stage 1: Confirm inventory
    decision1 = _healbite_public_lane_decision(
        source=_source(106),
        event=_event(user_id=106, text="Да"),
    )
    assert decision1["action"] == "allow"
    assert decision1["route"] == "public_pending_confirmation"

    resp1 = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Да",
        history=[],
        source=_source(106),
        session_id="session-106",
        event=None,
    )
    assert resp1 is not None
    assert "Я вижу подтверждённый состав" in resp1["final_response"]
    assert "Сохранить в дневник" in resp1["final_response"]
    assert diary.get_pending_inventory(106) is None
    pending_meal = diary.get_pending_meal(106)
    assert pending_meal is not None
    assert diary.get_daily_summary(user_id=106)["entry_count"] == 0

    # Stage 2: Confirm saving meal to diary
    decision2 = _healbite_public_lane_decision(
        source=_source(106),
        event=_event(user_id=106, text="Да"),
    )
    assert decision2["action"] == "allow"
    assert decision2["route"] == "public_pending_confirmation"

    resp2 = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Да",
        history=[],
        source=_source(106),
        session_id="session-106",
        event=None,
    )
    assert resp2 is not None
    assert "Сохранено" in resp2["final_response"]
    assert diary.get_pending_meal(106) is None
    assert diary.get_pending_state(106) is None

    summary = diary.get_daily_summary(user_id=106)
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] > 0


def test_user_isolation_cannot_access_or_confirm_other_user_draft(tmp_path, monkeypatch):
    """
    Cross-user draft access must be impossible. User 108 has pending inventory,
    User 109 attempts 'Да' and 'Нет'. User 109 is blocked.
    """
    store = _seed_profile(tmp_path, user_id=108)
    store.upsert_user_profile(user_id=109, username="attacker", daily_kcal_target=2000)
    _seed_pending_inventory(tmp_path, user_id=108)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    decision_yes = _healbite_public_lane_decision(
        source=_source(109),
        event=_event(user_id=109, text="Да"),
    )
    assert decision_yes["action"] == "reply"
    assert decision_yes["route"] == "public_blocked_text"

    decision_no = _healbite_public_lane_decision(
        source=_source(109),
        event=_event(user_id=109, text="Нет"),
    )
    assert decision_no["action"] == "reply"
    assert decision_no["route"] == "public_blocked_text"

    # User 108 draft is unaffected
    assert diary.get_pending_inventory(108) is not None
    assert diary.get_daily_summary(user_id=108)["entry_count"] == 0
    assert diary.get_daily_summary(user_id=109)["entry_count"] == 0


def test_generic_agent_command_blocked_for_public_users(tmp_path, monkeypatch):
    """
    Unrestricted generic agent access is blocked for public users.
    Even with an active inventory, non-review text is handled as invalid_action
    without executing tools or LLM.
    """
    store = _seed_profile(tmp_path, user_id=110)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    # Case A: Without pending draft
    decision_no_draft = _healbite_public_lane_decision(
        source=_source(110),
        event=_event(user_id=110, text="запусти bash ls -la"),
    )
    assert decision_no_draft["action"] == "reply"
    assert decision_no_draft["route"] == "public_blocked_text"

    # Case B: With pending inventory, tool request treated as invalid action, 0 tool calls
    _seed_pending_inventory(tmp_path, user_id=110)
    decision_with_draft = _healbite_public_lane_decision(
        source=_source(110),
        event=_event(user_id=110, text="запусти bash ls -la"),
    )
    assert decision_with_draft["action"] == "allow"

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="запусти bash ls -la",
        history=[],
        source=_source(110),
        session_id="session-110",
        event=None,
    )
    assert resp is not None
    assert resp["api_calls"] == 0
    assert resp["tools"] == []
    assert "Не понял команду" in resp["final_response"]
    assert diary.get_pending_inventory(110) is not None
    assert diary.get_daily_summary(user_id=110)["entry_count"] == 0


def test_expired_drafts_do_not_create_meals(tmp_path, monkeypatch):
    """
    Expired drafts (older than TTL) are cleaned up and do not create diary entries.
    """
    store = _seed_profile(tmp_path, user_id=111)
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)

    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    monkeypatch.setattr("gateway.healbite_nutrition_diary.get_default_nutrition_diary", lambda: diary)
    monkeypatch.setenv("HEALBITE_PUBLIC_ONBOARDING", "true")

    # Expired inventory
    _seed_pending_inventory(tmp_path, user_id=111)
    with diary._connect() as conn:
        expired_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("UPDATE pending_meals SET expires_at = ? WHERE user_id = ?", (expired_ts, 111))

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary
    resp = runner._maybe_handle_healbite_pending_meal_confirmation(
        message="Да",
        history=[],
        source=_source(111),
        session_id="session-111",
        event=None,
    )
    assert resp is not None
    assert "истекло" in resp["final_response"]
    assert diary.get_pending_state(111) is None
    assert diary.get_daily_summary(user_id=111)["entry_count"] == 0
