from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gateway.config import Platform
from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    FoodVisionInventory,
    FoodVisionItem,
    HealBiteNutritionDiary,
    calculate_inventory_nutrition,
    parse_pending_inventory_action,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource


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


def _source(user_id: str = "1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id=user_id,
        user_name="Test User",
    )


def test_pending_inventory_parser_rejects_invalid_inputs():
    oversized_name = "x" * 81
    invalid_cases = [
        "\u0432\u0435\u0441 1: 0 \u0433",
        "\u0432\u0435\u0441 1: -10 \u0433",
        f"\u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c: {oversized_name}, 20 \u0433",
        "\u0443\u0434\u0430\u043b\u0438\u0442\u044c 0",
    ]

    for value in invalid_cases:
        result = parse_pending_inventory_action(value)
        assert result.ok is False
        assert result.error


def test_inventory_confirmation_yes_requires_weights_before_calculation(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    inventory = _inventory(
        _food_item("\u0432\u0430\u0444\u043b\u0438", "\u0432\u0430\u0444\u043b\u0438", 45, 70),
        _food_item("\u0435\u0436\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u043c\u0443\u0441\u0441", "\u0435\u0436\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u043c\u0443\u0441\u0441", 90, 130),
    )
    diary.stage_pending_inventory(user_id=1, source="vision", inventory=inventory, image_ref="telegram:1:1", occurred_at=datetime.now(timezone.utc))

    result = diary.handle_pending_inventory_reply(1, "\u0434\u0430")

    assert result.status == "needs_weight_confirmation"
    assert result.missing_indexes == [2]
    assert diary.get_pending_inventory(1) is not None
    assert diary.get_pending_meal(1) is None
    assert diary.get_daily_summary(user_id=1)["entry_count"] == 0
    assert "\u0443\u043a\u0430\u0436\u0438\u0442\u0435 \u0432\u0435\u0441 \u0434\u043b\u044f \u043a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442\u0430 2" in result.reply_text.lower()


def test_inventory_replace_add_remove_and_weight_actions_update_state(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    inventory = _inventory(
        _food_item("\u0435\u0436\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u043c\u0443\u0441\u0441", "\u0435\u0436\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u043c\u0443\u0441\u0441", 90, 130),
        _food_item("\u043c\u0430\u0439\u043e\u043d\u0435\u0437", "\u043c\u0430\u0439\u043e\u043d\u0435\u0437", 10, 20, is_sauce=True),
    )
    staged = diary.stage_pending_inventory(user_id=2, source="vision", inventory=inventory, image_ref="telegram:2:1", occurred_at=datetime.now(timezone.utc))

    replace_result = diary.handle_pending_inventory_reply(2, "\u0438\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c 1: \u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430, 120 \u0433")
    weighted = diary.get_pending_inventory(2)
    assert replace_result.status == "updated"
    assert weighted is not None
    assert weighted.items[0].visible_name == "\u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430"
    assert weighted.items[0].selected_grams == pytest.approx(120.0)
    assert weighted.items[0].user_modified is True

    add_result = diary.handle_pending_inventory_reply(2, "\u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c: \u0441\u044b\u0440, 30 \u0433")
    added = diary.get_pending_inventory(2)
    assert add_result.status == "updated"
    assert added is not None
    assert [item.visible_name for item in added.items] == ["\u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430", "\u043c\u0430\u0439\u043e\u043d\u0435\u0437", "\u0441\u044b\u0440"]
    assert added.items[1].is_sauce is True

    remove_result = diary.handle_pending_inventory_reply(2, "\u0443\u0434\u0430\u043b\u0438\u0442\u044c 2")
    removed = diary.get_pending_inventory(2)
    assert remove_result.status == "updated"
    assert removed is not None
    assert [item.visible_name for item in removed.items] == ["\u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430", "\u0441\u044b\u0440"]
    assert staged.inventory_id == removed.inventory_id


def test_unknown_component_blocks_save_ready_pending_and_keeps_inventory(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    inventory = _inventory(_food_item("\u0435\u0436\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u043c\u0443\u0441\u0441", "\u0435\u0436\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u043c\u0443\u0441\u0441", 30, 40))
    diary.stage_pending_inventory(user_id=3, source="vision", inventory=inventory, image_ref="telegram:3:1", occurred_at=datetime.now(timezone.utc))

    result = diary.handle_pending_inventory_reply(3, "\u0434\u0430")

    assert result.status == "unknown_component"
    assert result.blocked_indexes == [1]
    assert diary.get_pending_inventory(3) is not None
    assert diary.get_pending_meal(3) is None
    assert diary.get_daily_summary(user_id=3)["entry_count"] == 0


def test_confirmed_inventory_transitions_to_save_confirmation_and_second_confirmation_saves_once(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    inventory = _inventory(
        _food_item("\u0432\u0430\u0444\u043b\u0438", "\u0432\u0430\u0444\u043b\u0438", 55, 65),
        _food_item("\u043d\u0430\u0440\u0435\u0437\u043a\u0430 \u043c\u044f\u0441\u0430", "\u043d\u0430\u0440\u0435\u0437\u043a\u0430 \u043c\u044f\u0441\u0430", 100, 120),
        _food_item("\u043e\u0433\u0443\u0440\u0446\u044b", "\u043e\u0433\u0443\u0440\u0446\u044b", 60, 80),
        _food_item("\u043c\u0430\u0439\u043e\u043d\u0435\u0437", "\u043c\u0430\u0439\u043e\u043d\u0435\u0437", 10, 20, is_sauce=True),
        _food_item("\u0433\u043e\u0440\u0447\u0438\u0446\u0430", "\u0433\u043e\u0440\u0447\u0438\u0446\u0430", 10, 15, is_sauce=True),
    )
    diary.stage_pending_inventory(user_id=4, source="vision", inventory=inventory, image_ref="telegram:4:photo-1", occurred_at=datetime.now(timezone.utc))

    phase_b = diary.handle_pending_inventory_reply(4, "\u0434\u0430")
    pending_meal = diary.get_pending_meal(4)

    assert phase_b.status == "awaiting_save_confirmation"
    assert pending_meal is not None
    assert diary.get_pending_inventory(4) is None
    assert diary.get_daily_summary(user_id=4)["entry_count"] == 0
    assert "\u042f \u0432\u0438\u0436\u0443 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0439 \u0441\u043e\u0441\u0442\u0430\u0432" in phase_b.reply_text
    assert "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a" in phase_b.reply_text

    first_save = diary.confirm_pending_meal(4)
    second_save = diary.confirm_pending_meal(4)
    summary = diary.get_daily_summary(user_id=4)

    assert first_save.status == "saved"
    assert second_save.status == "missing"
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == pytest.approx(first_save.record.calories_kcal)


def test_cross_user_cannot_mutate_other_inventory(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    inventory = _inventory(_food_item("\u0432\u0430\u0444\u043b\u0438", "\u0432\u0430\u0444\u043b\u0438", 55, 65))
    diary.stage_pending_inventory(user_id=5, source="vision", inventory=inventory, image_ref="telegram:5:1", occurred_at=datetime.now(timezone.utc))

    result = diary.handle_pending_inventory_reply(6, "\u0434\u0430")

    assert result.status == "missing"
    assert diary.get_pending_inventory(5) is not None
    assert diary.get_daily_summary(user_id=5)["entry_count"] == 0
    assert diary.get_daily_summary(user_id=6)["entry_count"] == 0


def test_stale_inventory_id_rejected_when_new_inventory_supersedes_old(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    first = diary.stage_pending_inventory(user_id=7, source="vision", inventory=_inventory(_food_item("\u0432\u0430\u0444\u043b\u0438", "\u0432\u0430\u0444\u043b\u0438", 55, 65)), image_ref="telegram:7:1", occurred_at=datetime.now(timezone.utc))
    second = diary.stage_pending_inventory(user_id=7, source="vision", inventory=_inventory(_food_item("\u043e\u0433\u0443\u0440\u0446\u044b", "\u043e\u0433\u0443\u0440\u0446\u044b", 60, 70)), image_ref="telegram:7:2", occurred_at=datetime.now(timezone.utc))

    result = diary.confirm_pending_inventory(7, expected_inventory_id=first.inventory_id)
    current = diary.get_pending_inventory(7)

    assert first.inventory_id != second.inventory_id
    assert result.status == "stale"
    assert current is not None
    assert current.inventory_id == second.inventory_id
    assert diary.get_daily_summary(user_id=7)["entry_count"] == 0


def test_calculate_inventory_nutrition_uses_component_sum_only(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    staged = diary.stage_pending_inventory(
        user_id=8,
        source="vision",
        inventory=_inventory(
            _food_item("\u0432\u0430\u0444\u043b\u0438", "\u0432\u0430\u0444\u043b\u0438", 55, 65),
            _food_item("\u0433\u043e\u0440\u0447\u0438\u0446\u0430", "\u0433\u043e\u0440\u0447\u0438\u0446\u0430", 10, 15, is_sauce=True),
        ),
        image_ref="telegram:8:1",
        occurred_at=datetime.now(timezone.utc),
    )

    calculation = calculate_inventory_nutrition(staged)

    assert calculation.status == "READY_FOR_NUTRITION_CALCULATION"
    assert calculation.record is not None
    assert calculation.record.items
    total_from_items = sum(float(item["calories_kcal"]) for item in calculation.record.items)
    assert calculation.record.calories_kcal == pytest.approx(total_from_items)


def test_runner_routes_inventory_confirmation_without_generic_agent(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "healbite.db", background_write=False)
    diary.stage_pending_inventory(
        user_id=9,
        source="vision",
        inventory=_inventory(
            _food_item("\u0432\u0430\u0444\u043b\u0438", "\u0432\u0430\u0444\u043b\u0438", 55, 65),
            _food_item("\u043e\u0433\u0443\u0440\u0446\u044b", "\u043e\u0433\u0443\u0440\u0446\u044b", 60, 70),
        ),
        image_ref="telegram:9:1",
        occurred_at=datetime.now(timezone.utc),
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._healbite_nutrition_diary = diary

    response = runner._maybe_handle_healbite_pending_meal_confirmation(message="\u0434\u0430", history=[], source=_source("9"), session_id="session-9", event=None)

    assert response is not None
    assert response["api_calls"] == 0
    assert response["tools"] == []
    assert "\u042f \u0432\u0438\u0436\u0443 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0439 \u0441\u043e\u0441\u0442\u0430\u0432" in response["final_response"]
    assert diary.get_pending_meal(9) is not None
    assert diary.get_daily_summary(user_id=9)["entry_count"] == 0
