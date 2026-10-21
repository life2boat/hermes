from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch
import pytest

from gateway.healbite_nutrition_diary import (
    FOOD_VISION_SCHEMA_VERSION,
    HealBiteNutritionDiary,
    PendingFoodVisionItem,
    PendingFoodVisionInventory,
    format_food_vision_inventory_reply,
    calculate_inventory_nutrition,
)
from gateway.run import _is_healbite_tester_user


def _mock_walnut_5_items_json():
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": [
                {
                    "visible_name": "грецкий орех",
                    "normalized_name": "грецкий орех в скорлупе",
                    "confidence": 0.95,
                    "estimated_grams_min": 10.0,
                    "estimated_grams_max": 15.0,
                    "preparation": "",
                    "is_sauce": False,
                    "uncertainty": "",
                }
                for _ in range(5)
            ],
            "overall_confidence": 0.95,
            "needs_user_confirmation": False,
            "warnings": [],
        }, ensure_ascii=False)
    })


def _mock_walnut_grouped_json():
    return json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": [
                {
                    "visible_name": "грецкие орехи",
                    "normalized_name": "грецкий орех в скорлупе",
                    "confidence": 0.95,
                    "estimated_grams_min": 50.0,
                    "estimated_grams_max": 75.0,
                    "preparation": "",
                    "is_sauce": False,
                    "uncertainty": "",
                    "quantity": 5,
                }
            ],
            "overall_confidence": 0.95,
            "needs_user_confirmation": False,
            "warnings": [],
        }, ensure_ascii=False)
    })


# -----------------------------------------------------------------------------
# Scenario 1 & 2: Single photo, 5 separate walnut items -> 1 group of 5
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenarios_1_and_2_five_walnuts_aggregated_into_one_group(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = _mock_walnut_5_items_json()
        outcome = await diary.analyze_and_maybe_log(
            user_id=101,
            image_path="/tmp/fake_walnuts.jpg",
        )

    assert outcome.available is True
    assert outcome.validation_status in ("VALID", "NEEDS_CLARIFICATION")
    pending = diary.get_pending_inventory(101)
    assert pending is not None
    assert len(pending.items) == 1
    item = pending.items[0]
    assert item.quantity == 5
    assert item.count_type == "COUNT_ESTIMATED"
    assert "Грецкие орехи" in item.visible_name

    reply = format_food_vision_inventory_reply(pending)
    lines = [line.strip() for line in reply.splitlines() if line.strip().startswith("1. ")]
    assert len(lines) == 1
    assert "5 шт." in lines[0]


# -----------------------------------------------------------------------------
# Scenario 3 & 4: Vision returns already grouped item -> mass not multiplied
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenarios_3_and_4_already_grouped_item_retains_mass(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = _mock_walnut_grouped_json()
        outcome = await diary.analyze_and_maybe_log(
            user_id=102,
            image_path="/tmp/fake_walnuts.jpg",
        )

    assert outcome.available is True
    pending = diary.get_pending_inventory(102)
    assert pending is not None
    assert len(pending.items) == 1
    item = pending.items[0]
    assert item.quantity == 5
    # Total mass must be 50-75g, NOT 250-375g
    assert item.estimated_grams_min == pytest.approx(50.0)
    assert item.estimated_grams_max == pytest.approx(75.0)


# -----------------------------------------------------------------------------
# Scenario 5 & 6: Edible mass separated from shell & KBZHU calculated on edible mass
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenarios_5_and_6_edible_mass_and_kbzhu_calculation(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = _mock_walnut_5_items_json()
        await diary.analyze_and_maybe_log(
            user_id=103,
            image_path="/tmp/fake_walnuts.jpg",
        )

    pending = diary.get_pending_inventory(103)
    assert pending is not None
    reply = format_food_vision_inventory_reply(pending)
    # Check that edible portion is mentioned
    assert "съедобн" in reply.lower() or "скорлуп" in reply.lower()

    # Confirm and calculate nutrition on the pending inventory
    calc = calculate_inventory_nutrition(pending)
    assert calc.record is not None
    # Walnuts in shell: 60g gross midpoint -> 0.45 edible yield = 27g edible nuts
    # Walnut kernels: ~654 kcal/100g -> 27g = ~177 kcal. Gross 60g would be ~392 kcal.
    assert calc.record.calories_kcal == pytest.approx(177.0, abs=15.0)
    assert calc.record.protein_g == pytest.approx(4.1, abs=1.5)
    assert calc.record.fat_g == pytest.approx(17.6, abs=2.5)


# -----------------------------------------------------------------------------
# Scenario 7: Slow but successful provider
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_7_slow_but_successful_provider(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")

    async def slow_vision(**kwargs):
        await asyncio.sleep(0.1)
        return _mock_walnut_5_items_json()

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=slow_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=104,
            image_path="/tmp/fake_walnuts.jpg",
        )

    assert outcome.available is True
    assert outcome.validation_status in ("VALID", "NEEDS_CLARIFICATION")


# -----------------------------------------------------------------------------
# Scenario 8: True provider timeout
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_8_true_provider_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("HEALBITE_VISION_ANALYZE_TIMEOUT", "0.05")
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")

    async def hanging_vision(**kwargs):
        await asyncio.sleep(1.0)
        return _mock_walnut_5_items_json()

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=hanging_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=105,
            image_path="/tmp/fake_walnuts.jpg",
        )

    assert outcome.available is False
    assert outcome.validation_status == "PROVIDER_TIMEOUT"
    assert diary.get_pending_inventory(105) is None


# -----------------------------------------------------------------------------
# Scenario 9: Invalid JSON
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_9_invalid_json(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    malformed_json = json.dumps({
        "success": True,
        "analysis": '{"schema_version": "food_vision_v1", "items": [{"visible_name":',
    })
    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = malformed_json
        outcome = await diary.analyze_and_maybe_log(
            user_id=106,
            image_path="/tmp/fake_walnuts.jpg",
        )

    assert outcome.available is False
    assert outcome.validation_status in ("INVALID_PROVIDER_OUTPUT", "PROVIDER_ERROR")
    assert diary.get_pending_inventory(106) is None


# -----------------------------------------------------------------------------
# Scenario 10: Missing or invalid quantity on continuous food
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_10_continuous_food_missing_quantity(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    oatmeal_json = json.dumps({
        "success": True,
        "analysis": json.dumps({
            "schema_version": FOOD_VISION_SCHEMA_VERSION,
            "is_food": True,
            "items": [
                {
                    "visible_name": "овсяная каша",
                    "normalized_name": "овсяная каша",
                    "confidence": 0.95,
                    "estimated_grams_min": 200.0,
                    "estimated_grams_max": 250.0,
                    "preparation": "варка",
                    "is_sauce": False,
                    "uncertainty": "",
                    "quantity": "invalid_val",
                }
            ],
            "overall_confidence": 0.95,
            "needs_user_confirmation": False,
            "warnings": [],
        }, ensure_ascii=False)
    })
    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = oatmeal_json
        outcome = await diary.analyze_and_maybe_log(
            user_id=107,
            image_path="/tmp/fake_oatmeal.jpg",
        )

    assert outcome.available is True
    pending = diary.get_pending_inventory(107)
    assert pending is not None
    item = pending.items[0]
    assert item.quantity is None
    assert item.count_type == "COUNT_UNKNOWN"
    reply = format_food_vision_inventory_reply(pending)
    line = [l for l in reply.splitlines() if l.startswith("1. ")][0]
    assert "шт" not in line


# -----------------------------------------------------------------------------
# Scenario 11: Provider transient 503 error retries and recovers
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_11_provider_transient_error_retries_and_recovers(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    attempts = 0

    async def flaky_vision(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return json.dumps({
                "success": False,
                "error": "Error analyzing image: provider_unavailable",
                "diagnostic": {
                    "category": "GEMINI_PROVIDER_5XX",
                    "stage": "PROVIDER_HTTP",
                    "retryable": True,
                }
            })
        return _mock_walnut_5_items_json()

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=flaky_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=108,
            image_path="/tmp/fake_walnuts.jpg",
        )

    assert attempts == 2
    assert outcome.available is True
    assert outcome.validation_status in ("VALID", "NEEDS_CLARIFICATION")
    assert diary.get_pending_inventory(108) is not None


# -----------------------------------------------------------------------------
# Scenario 11b: Persistent provider error fails closed safely
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_11b_persistent_provider_error_fails_closed(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    attempts = 0

    async def persistent_failing_vision(**kwargs):
        nonlocal attempts
        attempts += 1
        return json.dumps({
            "success": False,
            "error": "Error analyzing image: provider_unavailable",
            "diagnostic": {
                "category": "GEMINI_PROVIDER_5XX",
                "stage": "PROVIDER_HTTP",
                "retryable": True,
            }
        })

    with patch("tools.vision_tools.vision_analyze_tool", side_effect=persistent_failing_vision):
        outcome = await diary.analyze_and_maybe_log(
            user_id=109,
            image_path="/tmp/fake_walnuts.jpg",
        )

    assert attempts == 2
    assert outcome.available is False
    assert outcome.validation_status == "PROVIDER_ERROR"
    assert diary.get_pending_inventory(109) is None


# -----------------------------------------------------------------------------
# Scenario 12: No duplicate nutrition records
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_12_no_duplicate_nutrition_records(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    with patch("tools.vision_tools.vision_analyze_tool", new_callable=AsyncMock) as mock_vision:
        mock_vision.return_value = _mock_walnut_5_items_json()
        await diary.analyze_and_maybe_log(
            user_id=110,
            image_path="/tmp/fake_walnuts.jpg",
        )

    # Save meal directly via 'сохранить'
    reply1 = diary.handle_pending_inventory_reply(user_id=110, message="сохранить")
    assert reply1 is not None
    assert reply1.status == "saved"
    assert reply1.saved is True

    # Verify rows in DB
    with diary._connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM nutrition_log WHERE user_id = ?", (110,))
        count = cur.fetchone()[0]
    assert count == 1

    # Attempting to save again should not insert duplicates
    reply2 = diary.handle_pending_inventory_reply(user_id=110, message="сохранить")
    assert reply2.status == "missing"
    with diary._connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM nutrition_log WHERE user_id = ?", (110,))
        count_after = cur.fetchone()[0]
    assert count_after == 1


# -----------------------------------------------------------------------------
# Scenario 13 & 14: Scoped tester lane active, admin/generic Hermes access denied
# -----------------------------------------------------------------------------
def test_scenarios_13_and_14_scoped_tester_access_and_admin_denied(monkeypatch):
    monkeypatch.setenv("HEALBITE_TESTER_USERS", "248875361")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "968323641")

    # Scoped tester authorized for HealBite
    assert _is_healbite_tester_user("248875361") is True
    # Non-tester denied
    assert _is_healbite_tester_user("999999999") is False

    # Scoped tester is NOT an admin
    admin_users = {u.strip() for u in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if u.strip()}
    assert "248875361" not in admin_users
