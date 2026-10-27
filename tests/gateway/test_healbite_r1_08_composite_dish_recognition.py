"""
Tests for HEALBITE R1-08: Composite Dish Recognition & Nutrition Integrity.

Verification of all 12 mandatory scenarios:
1. Plov is identified as a finished prepared dish, not just rice and meat.
2. Borscht does not get double-counted calories from repeated ingredient accounting.
3. Olivier salad is not counted simultaneously as a whole salad and duplicate ingredient sum.
4. Casserole or pie does not turn into a set of speculative microcomponents.
5. Pizza does not simultaneously count the whole pizza and all its toppings again.
6. Chicken, buckwheat, and salad on one plate remain three distinct independent items.
7. Separate drink, bread, or garnish does not disappear when processing a main dish.
8. Ambiguous composition preserves honest uncertainty and does not invent recipes.
9. Mass and KBZHU are recalculated without duplication of components.
10. Existing user modifications and confirmed values are not overwritten.
11. Data of different users remains strictly isolated.
12. Existing food aggregation and multi-photo tests do not regress.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from datetime import datetime

from gateway.healbite_nutrition_diary import (
    FoodVisionItem,
    FoodVisionInventory,
    PendingFoodVisionItem,
    PendingFoodVisionInventory,
    HealBiteNutritionDiary,
    PendingInventoryAction,
    apply_dish_first_policy_to_items,
    calculate_food_vision_meal_nutrition,
    _build_inventory_calculation_snapshot,
    _detect_and_deduplicate_composite_inventory,
    format_food_vision_inventory_reply,
)
from gateway.healbite_food_composition import resolve_food_composition


# -----------------------------------------------------------------------------
# Scenario 1: Plov is identified as a prepared dish, not just rice and meat
# -----------------------------------------------------------------------------
def test_scenario_1_plov_identified_as_prepared_dish():
    # When vision decomposes plov into rice, meat, and carrots:
    raw_items = [
        FoodVisionItem("Рис для плова", "рис", 0.88, 150.0, 200.0, "тушеный"),
        FoodVisionItem("Говядина кусочками", "говядина", 0.85, 80.0, 120.0, "тушеная"),
        FoodVisionItem("Морковь", "морковь", 0.85, 20.0, 40.0, "тушеная"),
    ]
    consolidated = apply_dish_first_policy_to_items(raw_items)
    assert len(consolidated) == 1
    dish = consolidated[0]
    assert dish.normalized_name == "плов"
    assert "плов" in dish.visible_name.lower()
    assert dish.estimated_grams_min == pytest.approx(250.0, abs=1.0)
    assert dish.estimated_grams_max == pytest.approx(360.0, abs=1.0)

    # Resolution of plov profile
    res = resolve_food_composition("плов")
    assert res.matched is True
    assert res.profile is not None
    assert res.profile.canonical_id == "plov"
    assert res.profile.calories_kcal == pytest.approx(215.0, abs=1.0)


# -----------------------------------------------------------------------------
# Scenario 2: Borscht does not get double-counted calories from ingredients
# -----------------------------------------------------------------------------
def test_scenario_2_borscht_no_double_counting_from_ingredients():
    inv = PendingFoodVisionInventory(
        user_id=1,
        inventory_id="test_borscht_inv",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Борщ", "борщ", 0.92, 300.0, 400.0, 350.0, "вареный", False, ""),
            PendingFoodVisionItem(2, "Говядина вареная", "говядина", 0.85, 50.0, 70.0, 60.0, "вареная", False, ""),
            PendingFoodVisionItem(3, "Свекла", "свекла", 0.85, 40.0, 60.0, 50.0, "вареная", False, ""),
            PendingFoodVisionItem(4, "Капуста", "капуста", 0.85, 30.0, 50.0, 40.0, "вареная", False, ""),
        ],
        overall_confidence=0.92,
        needs_user_confirmation=False,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None

    # Only Borscht should be in calculated components
    calculated_names = [c["name"] for c in snap["components"]]
    assert calculated_names == ["Борщ"]

    # Items 2, 3, 4 should be marked as suppressed by Borscht
    assert inv.items[0].suppressed_by_item_id is None
    assert inv.items[1].suppressed_by_item_id == inv.items[0].item_id
    assert inv.items[2].suppressed_by_item_id == inv.items[0].item_id
    assert inv.items[3].suppressed_by_item_id == inv.items[0].item_id

    # Borscht: 350g * 58 kcal/100g = 203 kcal. Total kcal must NOT include beef (+120 kcal) or beets (+25 kcal).
    assert snap["calories_kcal"] == pytest.approx(203.0, abs=2.0)

    # User message formatting reflects "в составе «Борщ» (учтено в блюде)"
    reply = format_food_vision_inventory_reply(inv)
    assert "Борщ" in reply
    assert "в составе «Борщ» (учтено в блюде)" in reply


# -----------------------------------------------------------------------------
# Scenario 3: Olivier salad not counted as whole salad + ingredient sum
# -----------------------------------------------------------------------------
def test_scenario_3_olivier_not_double_counted():
    inv = PendingFoodVisionInventory(
        user_id=2,
        inventory_id="test_olv_inv",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Салат оливье", "салат оливье", 0.90, 180.0, 220.0, 200.0, "готовый", False, ""),
            PendingFoodVisionItem(2, "Вареная колбаса", "вареная колбаса", 0.85, 40.0, 60.0, 50.0, "", False, ""),
            PendingFoodVisionItem(3, "Зеленый горошек", "зеленый горошек", 0.85, 20.0, 40.0, 30.0, "", False, ""),
            PendingFoodVisionItem(4, "Майонез", "майонез", 0.85, 20.0, 30.0, 25.0, "", True, ""),
        ],
        overall_confidence=0.90,
        needs_user_confirmation=False,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None

    calculated_names = [c["name"] for c in snap["components"]]
    assert calculated_names == ["Салат оливье"]

    # Olivier 200g * 198 kcal/100g = 396 kcal
    assert snap["calories_kcal"] == pytest.approx(396.0, abs=2.0)
    assert inv.items[1].suppressed_by_item_id == inv.items[0].item_id
    assert inv.items[2].suppressed_by_item_id == inv.items[0].item_id
    assert inv.items[3].suppressed_by_item_id == inv.items[0].item_id


# -----------------------------------------------------------------------------
# Scenario 4: Casserole or pie does not turn into speculative microcomponents
# -----------------------------------------------------------------------------
def test_scenario_4_casserole_and_pie_not_speculative_microcomponents():
    # Casserole with speculative crumbs/sprinkles
    casserole_items = [
        FoodVisionItem("Творожная запеканка", "творожная запеканка", 0.92, 180.0, 220.0, "запеченный"),
        FoodVisionItem("Крошки теста", "крошки", 0.35, 2.0, 5.0, ""),
        FoodVisionItem("Неопознанная посыпка", "посыпка", 0.40, 5.0, 10.0, ""),
    ]
    pruned_cas = apply_dish_first_policy_to_items(casserole_items)
    assert len(pruned_cas) == 1
    assert "запеканка" in pruned_cas[0].visible_name.lower()
    assert "включает:" in pruned_cas[0].uncertainty.lower()

    # Pie over-decomposed
    pie_items = [
        FoodVisionItem("Печёное тесто", "печеное тесто", 0.90, 80.0, 120.0, "запеченный"),
        FoodVisionItem("Ломтики яблока", "ломтики яблока", 0.90, 40.0, 60.0, "запеченный"),
        FoodVisionItem("Темная добавка", "темная добавка", 0.30, 2.0, 5.0, ""),
    ]
    pruned_pie = apply_dish_first_policy_to_items(pie_items)
    assert len(pruned_pie) == 1
    assert "пирог" in pruned_pie[0].visible_name.lower() or "выпечка" in pruned_pie[0].visible_name.lower()


# -----------------------------------------------------------------------------
# Scenario 5: Pizza does not count pizza and all toppings again
# -----------------------------------------------------------------------------
def test_scenario_5_pizza_no_duplicate_toppings():
    inv = PendingFoodVisionInventory(
        user_id=3,
        inventory_id="test_pizza_inv",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Пицца Пепперони", "пицца", 0.92, 180.0, 240.0, 200.0, "запеченный", False, ""),
            PendingFoodVisionItem(2, "Сыр моцарелла", "сыр моцарелла", 0.85, 40.0, 60.0, 50.0, "", False, ""),
            PendingFoodVisionItem(3, "Пепперони", "пепперони", 0.85, 20.0, 40.0, 30.0, "", False, ""),
            PendingFoodVisionItem(4, "Томатный соус", "томатный соус", 0.85, 20.0, 30.0, 25.0, "", True, ""),
        ],
        overall_confidence=0.92,
        needs_user_confirmation=False,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None

    calculated_names = [c["name"] for c in snap["components"]]
    assert calculated_names == ["Пицца Пепперони"]
    # Pizza 200g * 260 kcal/100g = 520 kcal
    assert snap["calories_kcal"] == pytest.approx(520.0, abs=2.0)
    assert inv.items[1].suppressed_by_item_id == inv.items[0].item_id
    assert inv.items[2].suppressed_by_item_id == inv.items[0].item_id


# -----------------------------------------------------------------------------
# Scenario 6: Chicken, buckwheat, and salad remain 3 separate items
# -----------------------------------------------------------------------------
def test_scenario_6_chicken_buckwheat_salad_three_separate_items():
    items = [
        FoodVisionItem("Куриное филе", "куриное филе", 0.90, 120.0, 160.0, "гриль"),
        FoodVisionItem("Гречка", "гречка", 0.90, 130.0, 170.0, "отварная"),
        FoodVisionItem("Салат овощной", "салат овощной", 0.90, 80.0, 120.0, "свежий"),
    ]
    res = apply_dish_first_policy_to_items(items)
    # Must remain 3 separate items!
    assert len(res) == 3
    names = [it.visible_name for it in res]
    assert "Куриное филе" in names
    assert "Гречка" in names
    assert "Салат овощной" in names

    # In Pending Inventory calculation:
    inv = PendingFoodVisionInventory(
        user_id=4,
        inventory_id="test_plate_inv",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Куриное филе", "куриное филе", 0.90, 120.0, 160.0, 140.0, "гриль", False, ""),
            PendingFoodVisionItem(2, "Гречка", "гречка", 0.90, 130.0, 170.0, 150.0, "отварная", False, ""),
            PendingFoodVisionItem(3, "Салат овощной", "салат овощной", 0.90, 80.0, 120.0, 100.0, "свежий", False, ""),
        ],
        overall_confidence=0.90,
        needs_user_confirmation=False,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None
    # All 3 must be active and calculated!
    assert len(snap["components"]) == 3
    assert all(it.suppressed_by_item_id is None for it in inv.items)


# -----------------------------------------------------------------------------
# Scenario 7: Separate drink, bread, or garnish does not disappear
# -----------------------------------------------------------------------------
def test_scenario_7_separate_drink_bread_garnish_not_lost():
    inv = PendingFoodVisionInventory(
        user_id=5,
        inventory_id="test_meal_bread_drink_inv",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Борщ", "борщ", 0.92, 300.0, 400.0, 350.0, "вареный", False, ""),
            PendingFoodVisionItem(2, "Свекла", "свекла", 0.85, 40.0, 60.0, 50.0, "вареная", False, ""),
            PendingFoodVisionItem(3, "Хлеб ржаной", "хлеб", 0.90, 30.0, 50.0, 40.0, "", False, ""),
            PendingFoodVisionItem(4, "Чай черный", "чай", 0.90, 180.0, 220.0, 200.0, "", False, ""),
        ],
        overall_confidence=0.92,
        needs_user_confirmation=False,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None

    active_names = [c["name"] for c in snap["components"]]
    assert "Борщ" in active_names
    assert "Хлеб ржаной" in active_names
    assert "Чай черный" in active_names
    # Beet is suppressed, but Bread and Tea are completely preserved!
    assert inv.items[1].suppressed_by_item_id == inv.items[0].item_id
    assert inv.items[2].suppressed_by_item_id is None
    assert inv.items[3].suppressed_by_item_id is None


# -----------------------------------------------------------------------------
# Scenario 8: Ambiguous composition preserves honest uncertainty
# -----------------------------------------------------------------------------
def test_scenario_8_ambiguous_composition_preserves_honest_uncertainty():
    # Ambiguous stew without distinct identifiable components
    items = [
        FoodVisionItem(
            "Рагу из мяса и овощей",
            "рагу",
            0.65,
            200.0,
            300.0,
            preparation="тушеный",
            uncertainty="Точный состав овощей и вид мяса не определены",
        ),
    ]
    res = apply_dish_first_policy_to_items(items)
    assert len(res) == 1
    # Does not invent a hallucinated exact ingredient list
    assert "Точный состав" in res[0].uncertainty
    assert res[0].normalized_name == "рагу"


# -----------------------------------------------------------------------------
# Scenario 9: Mass and KBZHU recalculated without duplication
# -----------------------------------------------------------------------------
def test_scenario_9_mass_and_kbzhu_recalculated_without_duplication():
    # Vegetable ragout + components
    inv = PendingFoodVisionInventory(
        user_id=6,
        inventory_id="test_ragout_inv",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Овощное рагу", "овощное рагу", 0.90, 200.0, 300.0, 250.0, "тушеный", False, ""),
            PendingFoodVisionItem(2, "Кабачки", "кабачки", 0.85, 80.0, 120.0, 100.0, "", False, ""),
            PendingFoodVisionItem(3, "Картофель", "картофель", 0.85, 60.0, 100.0, 80.0, "", False, ""),
        ],
        overall_confidence=0.90,
        needs_user_confirmation=False,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None
    # 250g ragout * 72 kcal/100g = 180 kcal
    assert snap["calories_kcal"] == pytest.approx(180.0, abs=2.0)
    assert len(snap["components"]) == 1

    # Recalculate via calculate_food_vision_meal_nutrition
    meal_est = calculate_food_vision_meal_nutrition(inv)
    assert meal_est.total_intervals.calories_kcal is not None
    assert meal_est.total_intervals.calories_kcal.center_value == pytest.approx(180.0, abs=2.0)


# -----------------------------------------------------------------------------
# Scenario 10: Existing user modifications and confirmed values not overwritten
# -----------------------------------------------------------------------------
def test_scenario_10_user_modified_values_not_overwritten():
    user_item = PendingFoodVisionItem(
        index=1,
        visible_name="Плов домашний",
        normalized_name="плов",
        confidence=0.95,
        estimated_grams_min=200.0,
        estimated_grams_max=300.0,
        selected_grams=350.0,
        preparation="тушеный",
        is_sauce=False,
        uncertainty="",
        user_modified=True,
    )
    inv = PendingFoodVisionInventory(
        user_id=7,
        inventory_id="test_user_mod_inv",
        schema_version="food_vision_inventory_v1",
        items=[user_item],
        overall_confidence=0.95,
        needs_user_confirmation=False,
        warnings=[],
    )
    snap = _build_inventory_calculation_snapshot(inv)
    assert snap is not None
    # Must use user confirmed 350g, not mid of min/max: 350g * 215 kcal/100g = 752.5 kcal
    assert snap["calories_kcal"] == pytest.approx(752.5, abs=2.0)
    assert inv.items[0].selected_grams == 350.0
    assert inv.items[0].user_modified is True

    # apply_dish_first_policy_to_items does not collapse user-modified items
    items_list = [user_item, FoodVisionItem("Хлеб", "хлеб", 0.9, 30.0, 40.0)]
    unchanged = apply_dish_first_policy_to_items(items_list)
    assert len(unchanged) == 2
    assert unchanged[0].selected_grams == 350.0


# -----------------------------------------------------------------------------
# Scenario 11: Multi-user isolation
# -----------------------------------------------------------------------------
def test_scenario_11_multi_user_isolation(tmp_path: Path):
    db_path = tmp_path / "multi_user.db"
    diary = HealBiteNutritionDiary(db_path=db_path)

    inv_user_a = PendingFoodVisionInventory(
        user_id=101,
        inventory_id="inv_user_101",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Борщ", "борщ", 0.9, 300.0, 400.0, 350.0, "", False, ""),
            PendingFoodVisionItem(2, "Свекла", "свекла", 0.85, 40.0, 60.0, 50.0, "", False, ""),
        ],
        overall_confidence=0.9,
        needs_user_confirmation=False,
        warnings=[],
    )
    inv_user_b = PendingFoodVisionInventory(
        user_id=102,
        inventory_id="inv_user_102",
        schema_version="food_vision_inventory_v1",
        items=[
            PendingFoodVisionItem(1, "Пицца", "пицца", 0.9, 150.0, 250.0, 200.0, "", False, ""),
            PendingFoodVisionItem(2, "Пепперони", "пепперони", 0.85, 20.0, 40.0, 30.0, "", False, ""),
        ],
        overall_confidence=0.9,
        needs_user_confirmation=False,
        warnings=[],
    )

    diary._store_pending_inventory(inv_user_a)
    diary._store_pending_inventory(inv_user_b)

    retrieved_a = diary.get_pending_inventory(101)
    retrieved_b = diary.get_pending_inventory(102)

    assert retrieved_a is not None
    assert retrieved_a.user_id == 101
    assert retrieved_a.items[0].visible_name == "Борщ"

    assert retrieved_b is not None
    assert retrieved_b.user_id == 102
    assert retrieved_b.items[0].visible_name == "Пицца"

    # Action on user A does not touch user B
    action = PendingInventoryAction(kind="weight", index=1, grams=400.0)
    res_a = diary.apply_pending_inventory_action(101, action)
    assert res_a.status == "updated"
    assert diary.get_pending_inventory(101).items[0].selected_grams == 400.0
    # User B remains 200.0
    assert diary.get_pending_inventory(102).items[0].selected_grams == 200.0


# -----------------------------------------------------------------------------
# Scenario 12: Existing aggregation and multi-photo no regressions
# -----------------------------------------------------------------------------
def test_scenario_12_existing_aggregation_and_multiphoto_no_regressions():
    from gateway.healbite_nutrition_diary import (
        _aggregate_identical_food_vision_items,
        _reconcile_multi_photo_inventories,
    )

    # Identical items aggregation
    items = [
        FoodVisionItem("Яблоко", "яблоко", 0.9, 100.0, 150.0, quantity=1),
        FoodVisionItem("Яблоко", "яблоко", 0.9, 100.0, 150.0, quantity=1),
    ]
    aggregated = _aggregate_identical_food_vision_items(items)
    assert len(aggregated) == 1
    assert aggregated[0].quantity == 2
    assert aggregated[0].estimated_grams_min == 200.0
    assert aggregated[0].estimated_grams_max == 300.0

    # Multi-photo reconciliation with composite dish
    photo_1 = [FoodVisionItem("Плов", "плов", 0.9, 250.0, 350.0)]
    photo_2 = [FoodVisionItem("Салат овощной", "салат овощной", 0.9, 80.0, 120.0)]
    res = _reconcile_multi_photo_inventories([photo_1, photo_2], decision="distinct_portions")
    reconciled_items = res[0]
    assert len(reconciled_items) == 2
    names = [it.visible_name for it in reconciled_items]
    assert "Плов" in names
    assert "Салат овощной" in names
