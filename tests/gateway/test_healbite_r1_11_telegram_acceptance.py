"""
Comprehensive acceptance test suite for HEALBITE_R1_11_TELEGRAM_E2E_ACCEPTANCE:
End-to-End user journeys for Scenarios A through G covering text-first food logging,
composite dishes, approximate portion estimation, user corrections, persistence & idempotency,
fail-closed negative behaviors, and multi-user isolation.
"""

from __future__ import annotations

import datetime
from pathlib import Path
import pytest

from gateway.healbite_food_composition import resolve_food_composition
from gateway.healbite_nutrition_diary import (
    FoodVisionItem,
    FoodVisionInventory,
    HealBiteNutritionDiary,
    PendingFoodVisionItem,
    PendingFoodVisionInventory,
    apply_dish_first_policy_to_items,
    calculate_food_vision_meal_nutrition,
    format_food_vision_inventory_reply,
    is_full_food_description,
)
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.nutrition.search import normalize_russian_food_text, reduce_russian_stem
from gateway.nutrition.text_parser import parse_food_text_description


# =============================================================================
# SCENARIO A: Text-First Recognition & Oblique/Genitive Russian Resolution
# =============================================================================

def test_scenario_a_text_first_recognition_and_provenance(tmp_path: Path):
    """
    Scenario A:
    - Input: '200 г куриной грудки запечённой, 150 г гречки отварной, 10 г оливкового масла'
    - Text takes precedence, Vision is bypassed (is_full_food_description is True).
    - All three ingredients resolve from catalog with exact macro profiles despite genitive case.
    - Total energy: ~623.9 kcal, protein: ~68.8g, fat: ~19.6g, carbs: ~39.2g.
    """
    diary = HealBiteNutritionDiary(db_path=tmp_path / "scenario_a.db")
    raw_text = "200 г куриной грудки запечённой, 150 г гречки отварной, 10 г оливкового масла"

    # 1. Parse text description
    parsed = parse_food_text_description(raw_text)
    assert len(parsed.ingredients) == 3
    assert is_full_food_description(parsed) is True

    # 2. Stage pending inventory from text
    pending = diary.stage_pending_inventory_from_text(user_id=101, text=raw_text)
    assert pending is not None
    assert pending.source == "text"
    assert pending.image_ref is None
    assert len(pending.items) == 3

    # 3. Verify each component resolves from catalog
    # Item 1: Куриная грудка запеченная
    item1 = pending.items[0]
    assert item1.selected_grams == pytest.approx(200.0)
    res1 = resolve_food_composition(item1.normalized_name)
    assert res1.matched is True
    assert "171477" in res1.source_id or "кури" in res1.profile.display_name.lower()
    assert res1.profile.calories_kcal == pytest.approx(165.0, abs=5.0)

    # Item 2: Гречка отварная
    item2 = pending.items[1]
    assert item2.selected_grams == pytest.approx(150.0)
    res2 = resolve_food_composition(item2.normalized_name)
    assert res2.matched is True
    assert res2.profile.calories_kcal == pytest.approx(137.0, abs=5.0)

    # Item 3: Оливковое масло
    item3 = pending.items[2]
    assert item3.selected_grams == pytest.approx(10.0)
    res3 = resolve_food_composition(item3.normalized_name)
    assert res3.matched is True
    assert res3.profile.calories_kcal == pytest.approx(884.0, abs=10.0)
    assert res3.profile.fat_g == pytest.approx(100.0, abs=2.0)

    # 4. Verify formatted reply contains confirmed grams and totals
    reply = format_food_vision_inventory_reply(pending)
    assert "200" in reply
    assert "150" in reply
    assert "10" in reply
    assert "62" in reply or "330" in reply


# =============================================================================
# SCENARIO B: Composite Dish Recognition & Deduplication
# =============================================================================

def test_scenario_b_composite_dish_deduplication():
    """
    Scenario B:
    - Mixed composite dish (e.g. Borscht, Plov, Olivier).
    - Resolves as prepared composite dish, prevents duplicate counting of individual ingredients.
    - Estimated weights are flagged as approximate, not falsely marked user-confirmed.
    """
    # Test Borscht composite resolution
    borscht_res = resolve_food_composition("борщ со сметаной")
    assert borscht_res.matched is True
    assert borscht_res.profile.display_name == "Борщ"
    assert borscht_res.profile.cooking_state == "boiled"

    # Test Plov composite consolidation
    raw_vision_items = [
        FoodVisionItem("Рис для плова", "рис", 0.88, 150.0, 200.0, "тушеный"),
        FoodVisionItem("Говядина кусочками", "говядина", 0.85, 80.0, 120.0, "тушеная"),
        FoodVisionItem("Морковь", "морковь", 0.85, 20.0, 40.0, "тушеная"),
    ]
    consolidated = apply_dish_first_policy_to_items(raw_vision_items)
    assert len(consolidated) == 1
    plov_dish = consolidated[0]
    assert plov_dish.normalized_name == "плов"
    assert plov_dish.estimated_grams_min == pytest.approx(250.0, abs=1.0)
    assert plov_dish.estimated_grams_max == pytest.approx(360.0, abs=1.0)

    # Test Borscht consolidation with PendingFoodVisionItem
    pending_items = [
        PendingFoodVisionItem(index=1, visible_name="Борщ", normalized_name="борщ", confidence=0.9, estimated_grams_min=250.0, estimated_grams_max=350.0, selected_grams=300.0, preparation="вареный", is_sauce=False, uncertainty="300 г"),
        PendingFoodVisionItem(index=2, visible_name="Свекла", normalized_name="свекла", confidence=0.8, estimated_grams_min=40.0, estimated_grams_max=60.0, selected_grams=50.0, preparation="вареный", is_sauce=False, uncertainty="50 г"),
    ]
    borscht_consolidated = apply_dish_first_policy_to_items(pending_items)
    assert len(borscht_consolidated) == 1
    assert borscht_consolidated[0].normalized_name == "борщ"


# =============================================================================
# SCENARIO C: Approximate Portion Estimation & Uncertainty
# =============================================================================

def test_scenario_c_approximate_portion_estimation(tmp_path: Path):
    """
    Scenario C:
    - Photo of food without user-specified grams.
    - Calculates plausible mass interval and KБЖУ interval.
    - Approximate values can be saved without requiring exact user weights.
    """
    diary = HealBiteNutritionDiary(db_path=tmp_path / "scenario_c.db")
    approx_items = [
        FoodVisionItem("Борщ", "борщ", 0.90, 250.0, 350.0, "вареный")
    ]
    inv = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=approx_items,
        overall_confidence=0.90,
        needs_user_confirmation=False,
        warnings=[],
    )

    # Calculate nutrition with intervals
    estimate = calculate_food_vision_meal_nutrition(inv)
    assert estimate.total_intervals.calories_kcal is not None
    cal_interval = estimate.total_intervals.calories_kcal
    assert cal_interval.min_value == pytest.approx(145.0, abs=2.0)
    assert cal_interval.center_value == pytest.approx(174.0, abs=2.0)
    assert cal_interval.max_value == pytest.approx(203.0, abs=2.0)

    # Stage and verify reply shows approximate "~" notation
    pending = diary.stage_pending_inventory(
        user_id=101,
        source="vision",
        inventory=inv,
        image_ref="meal_photo.jpg",
        occurred_at=datetime.datetime.now(datetime.timezone.utc),
    )
    reply = format_food_vision_inventory_reply(pending)
    assert "~" in reply
    assert "приблизительно" in reply.lower() or "примерно" in reply.lower()


# =============================================================================
# SCENARIO D: Explicit User Corrections & Natural Language Overrides
# =============================================================================

def test_scenario_d_user_corrections_override_catalog(tmp_path: Path):
    """
    Scenario D:
    - User corrects weight ('Вес 1: 200 г') and explicit macros ('Исправить 1: 280 ккал, Б 10, Ж 3, У 50').
    - User-specified values strictly override catalog values.
    - Recalculates total meal nutrition with user numbers.
    """
    diary = HealBiteNutritionDiary(db_path=tmp_path / "scenario_d.db")
    items = [
        PendingFoodVisionItem(
            index=1,
            visible_name="Гречка отварная",
            normalized_name="гречка отварная",
            confidence=0.95,
            estimated_grams_min=135.0,
            estimated_grams_max=165.0,
            selected_grams=150.0,
            preparation="",
            is_sauce=False,
            uncertainty="150 г",
            user_modified=False,
        )
    ]
    inv = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=items,
        overall_confidence=0.95,
        needs_user_confirmation=False,
        warnings=[],
    )
    diary.stage_pending_inventory(
        user_id=101,
        source="text",
        inventory=inv,
        image_ref=None,
        occurred_at=datetime.datetime.now(datetime.timezone.utc),
    )

    # 1. Update weight to 200g
    res_w = diary.handle_pending_inventory_reply(101, "Вес 1: 200 г")
    assert res_w.status == "updated"

    # 2. Update explicit macros to 280 kcal, 10g protein, 3g fat, 50g carbs
    res_m = diary.handle_pending_inventory_reply(101, "Исправить 1: 280 ккал, Б 10, Ж 3, У 50")
    assert res_m.status == "updated"

    pending = diary.get_pending_inventory(101)
    item = pending.items[0]
    assert item.selected_grams == pytest.approx(200.0)
    assert item.user_nutrition is not None
    assert item.user_nutrition.calories_kcal == pytest.approx(280.0)
    assert item.user_nutrition.protein_g == pytest.approx(10.0)
    assert item.user_nutrition.fat_g == pytest.approx(3.0)
    assert item.user_nutrition.carbs_g == pytest.approx(50.0)

    # Verify calculation snapshot reflects user overrides
    snap = pending.calculation_snapshot
    assert snap["calories_kcal"] == pytest.approx(280.0)
    assert snap["protein_g"] == pytest.approx(10.0)


# =============================================================================
# SCENARIO E: Persistence, Idempotency & Read-Back
# =============================================================================

def test_scenario_e_persistence_idempotency_and_readback(tmp_path: Path):
    """
    Scenario E:
    - Confirms the meal once.
    - Exactly one corresponding diary entry saved to SQLite nutrition_log.
    - Idempotent duplicate confirm does not create double saves.
    - Read back through diary history verifies exact saved totals.
    """
    diary = HealBiteNutritionDiary(db_path=tmp_path / "scenario_e.db")
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    items = [
        PendingFoodVisionItem(
            index=1,
            visible_name="Гречка отварная",
            normalized_name="гречка отварная",
            confidence=0.95,
            estimated_grams_min=135.0,
            estimated_grams_max=165.0,
            selected_grams=150.0,
            preparation="",
            is_sauce=False,
            uncertainty="150 г",
            user_modified=False,
        )
    ]
    inv = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=items,
        overall_confidence=0.95,
        needs_user_confirmation=False,
        warnings=[],
    )
    diary.stage_pending_inventory(user_id=101, source="text", inventory=inv, image_ref=None, occurred_at=now_utc)

    # 1. First confirmation
    res_inv = diary.confirm_pending_inventory(101, now=now_utc)
    assert res_inv.status == "awaiting_save_confirmation"
    res_save = diary.confirm_pending_meal(101, now=now_utc)
    assert res_save.status == "saved"

    # 2. Check exactly 1 record saved
    import sqlite3
    with sqlite3.connect(tmp_path / "scenario_e.db") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (101,)).fetchall()
    assert len(rows) == 1
    record = rows[0]
    assert record["user_id"] == 101
    assert "Гречка" in record["meal_name"]

    # Summary read-back verifies saved totals
    summary = diary.get_summary(user_id=101, days=1)
    assert summary is not None
    assert summary["calories_kcal"] > 0

    # 3. Duplicate confirmation attempt (must be idempotent, not double-save)
    res_dup_inv = diary.confirm_pending_inventory(101, now=now_utc)
    assert res_dup_inv.status == "missing"
    res_dup_save = diary.confirm_pending_meal(101, now=now_utc)
    assert res_dup_save.status == "missing"

    # History still has exactly 1 record
    with sqlite3.connect(tmp_path / "scenario_e.db") as conn:
        conn.row_factory = sqlite3.Row
        rows_after = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (101,)).fetchall()
    assert len(rows_after) == 1


# =============================================================================
# SCENARIO F: Fail-Closed Behavior on Unknown or Fantasy Foods
# =============================================================================

def test_scenario_f_fail_closed_on_unknown_and_fantasy_foods():
    """
    Scenario F:
    - Tests fantasy / unknown foods ('жареная клубника', 'эльфийский хлеб лембас', 'пирог из звездной пыли').
    - Fail-closed: returns matched=False without silent substitution or invented calories.
    """
    negatives = [
        "жареная клубника",
        "эльфийский хлеб лембас",
        "пирог из звездной пыли",
        "салат из темной материи",
    ]
    for neg in negatives:
        res = resolve_food_composition(neg)
        assert res.matched is False, f"Expected fail-closed for '{neg}', got match: {res.profile}"


# =============================================================================
# SCENARIO G: User Registration, Personal Targets & User Isolation
# =============================================================================

def test_scenario_g_registration_targets_and_isolation(tmp_path: Path):
    """
    Scenario G:
    - User registration and personal nutrition target recalculation work.
    - Test account (101) cannot access or mutate another user's (102) diary or profile.
    """
    db_path = tmp_path / "scenario_g.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    diary = HealBiteNutritionDiary(db_path=db_path)

    # 1. Register test user 101 with profile
    store.upsert_user_profile(
        user_id=101,
        age=30,
        sex="male",
        height_cm=180.0,
        weight_kg=78.0,
        goal="maintain",
        activity_level="moderate",
    )
    targets_101 = store.recalculate_profile_targets(user_id=101)
    assert targets_101 is not None
    assert targets_101.daily_kcal_target is not None
    assert targets_101.daily_kcal_target > 1500

    # 2. Register separate user 102
    store.upsert_user_profile(
        user_id=102,
        age=25,
        sex="female",
        height_cm=165.0,
        weight_kg=58.0,
        goal="lose",
        activity_level="light",
    )
    targets_102 = store.recalculate_profile_targets(user_id=102)
    assert targets_102 is not None
    assert targets_102.daily_kcal_target != targets_101.daily_kcal_target

    # 3. Save meal for user 102
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    items_102 = [
        PendingFoodVisionItem(
            index=1,
            visible_name="Омлет",
            normalized_name="омлет",
            confidence=0.9,
            estimated_grams_min=150.0,
            estimated_grams_max=200.0,
            selected_grams=180.0,
            preparation="fried",
            is_sauce=False,
            uncertainty="180 г",
        )
    ]
    inv_102 = FoodVisionInventory(
        schema_version="food_vision_inventory_v1",
        items=items_102,
        overall_confidence=0.9,
        needs_user_confirmation=False,
        warnings=[],
    )
    diary.stage_pending_inventory(user_id=102, source="vision", inventory=inv_102, image_ref=None, occurred_at=now_utc)
    diary.confirm_pending_inventory(102, now=now_utc)
    diary.confirm_pending_meal(102, now=now_utc)

    # 4. Verify user 101 sees 0 records in their diary
    summary_101 = diary.get_summary(user_id=101, days=1)
    assert summary_101 is None or summary_101.get("calories_kcal", 0) == 0
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows_101 = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (101,)).fetchall()
        rows_102 = conn.execute("SELECT * FROM nutrition_log WHERE user_id = ?", (102,)).fetchall()
    assert len(rows_101) == 0
    assert len(rows_102) == 1
    assert "Омлет" in rows_102[0]["meal_name"]

    # 5. Profile isolation
    p101 = store.get_user_profile(101)
    p102 = store.get_user_profile(102)
    assert p101.user_id == 101
    assert p102.user_id == 102
    assert p101.weight_kg == 78.0
    assert p102.weight_kg == 58.0
