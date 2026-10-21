import json
from datetime import datetime, timezone
from pathlib import Path
import pytest

from gateway.healbite_nutrition_diary import (
    FoodVisionItem,
    FoodVisionInventory,
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    _aggregate_identical_food_vision_items,
    _aggregate_multi_photo_batch,
    _build_inventory_calculation_snapshot,
    _deserialize_pending_inventory,
    _serialize_pending_inventory,
    calculate_inventory_nutrition,
    format_food_vision_inventory_reply,
    parse_pending_inventory_action,
    validate_food_vision_inventory,
)


# -----------------------------------------------------------------------------
# 1. Five identical walnuts -> one group
# -----------------------------------------------------------------------------
def test_req1_five_walnuts_one_group():
    raw_payload = json.dumps({
        "schema_version": "food_vision_inventory_v1",
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
    })
    result = validate_food_vision_inventory(raw_payload)
    assert result.status == "VALID"
    assert result.inventory is not None
    assert len(result.inventory.items) == 1
    assert "Грецкие орехи" in result.inventory.items[0].visible_name


# -----------------------------------------------------------------------------
# 2. Quantity = 5, not 1
# -----------------------------------------------------------------------------
def test_req2_quantity_equals_five():
    raw_payload = json.dumps({
        "schema_version": "food_vision_inventory_v1",
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
    })
    result = validate_food_vision_inventory(raw_payload)
    assert result.inventory is not None
    item = result.inventory.items[0]
    assert item.quantity == 5
    assert item.count_type == "COUNT_ESTIMATED"


# -----------------------------------------------------------------------------
# 3. Total mass = sum of individual masses
# -----------------------------------------------------------------------------
def test_req3_total_mass_equals_sum_of_five_masses():
    raw_payload = json.dumps({
        "schema_version": "food_vision_inventory_v1",
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
    })
    result = validate_food_vision_inventory(raw_payload)
    assert result.inventory is not None
    item = result.inventory.items[0]
    assert item.estimated_grams_min == pytest.approx(50.0)
    assert item.estimated_grams_max == pytest.approx(75.0)
    assert item.piece_grams == pytest.approx(12.5)


# -----------------------------------------------------------------------------
# 4. KBZHU calculated on total edible mass (not shell mass)
# -----------------------------------------------------------------------------
def test_req4_kbzhu_calculated_on_edible_mass():
    # 5 walnuts in shell, 10-15g each -> 50-75g gross, selected midpoint = 60g
    # Edible yield = 0.45 -> edible mass center = 27g
    # Walnut kernels: 654 kcal/100g, 15.2g P, 65.2g F, 13.7g C
    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="грецкий орех в скорлупе",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=60.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        count_type="COUNT_CONFIRMED",
        piece_grams=12.0,
        edible_yield_ratio=0.45,
    )
    inv = PendingFoodVisionInventory(
        user_id=101,
        inventory_id="inv-walnuts",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    snapshot = _build_inventory_calculation_snapshot(inv)
    assert snapshot is not None
    assert snapshot.get("has_unmapped_components") is False

    # Check component mass and KBZHU
    comp = snapshot["components"][0]
    # Edible mass should be ~27g (not 60g)
    assert comp["estimated_weight_g"] == pytest.approx(27.0, abs=1.0)
    # Calories for 27g of walnut kernels should be ~176.6 kcal (not ~392 kcal from 60g)
    assert comp["calories_kcal"] == pytest.approx(27.0 * 6.54, rel=0.08)
    assert 160.0 <= comp["calories_kcal"] <= 195.0

    # Total calories in snapshot must match edible portion
    assert snapshot["calories_kcal"] == pytest.approx(comp["calories_kcal"])

    # Reply format must mention shell and edible part
    reply = format_food_vision_inventory_reply(inv)
    assert "Грецкие орехи в скорлупе — 5 шт." in reply
    assert "со скорлупой" in reply
    assert "Съедобная часть:" in reply
    assert "КБЖУ" in reply


# -----------------------------------------------------------------------------
# 5. Three tomatoes -> one group of 3
# -----------------------------------------------------------------------------
def test_req5_three_tomatoes_one_group():
    items = [
        FoodVisionItem(
            visible_name="помидор",
            normalized_name="помидоры",
            confidence=0.9,
            estimated_grams_min=90.0,
            estimated_grams_max=110.0,
        )
        for _ in range(3)
    ]
    grouped = _aggregate_identical_food_vision_items(items)
    assert len(grouped) == 1
    assert grouped[0].quantity == 3
    assert grouped[0].estimated_grams_min == pytest.approx(270.0)
    assert grouped[0].estimated_grams_max == pytest.approx(330.0)
    assert "Помидоры" in grouped[0].visible_name


# -----------------------------------------------------------------------------
# 6. Different products -> separate groups
# -----------------------------------------------------------------------------
def test_req6_different_products_separate_groups():
    items = [
        FoodVisionItem(
            visible_name="грецкий орех",
            normalized_name="грецкий орех в скорлупе",
            confidence=0.9,
            estimated_grams_min=10.0,
            estimated_grams_max=15.0,
        ),
        FoodVisionItem(
            visible_name="грецкий орех",
            normalized_name="грецкий орех в скорлупе",
            confidence=0.9,
            estimated_grams_min=10.0,
            estimated_grams_max=15.0,
        ),
        FoodVisionItem(
            visible_name="помидор",
            normalized_name="помидоры",
            confidence=0.9,
            estimated_grams_min=100.0,
            estimated_grams_max=120.0,
        ),
        FoodVisionItem(
            visible_name="сыр",
            normalized_name="сыр",
            confidence=0.95,
            estimated_grams_min=30.0,
            estimated_grams_max=40.0,
        ),
    ]
    grouped = _aggregate_identical_food_vision_items(items)
    assert len(grouped) == 3
    # First is walnuts (count 2)
    assert grouped[0].quantity == 2
    # Second is tomato (count 1)
    assert "помидор" in grouped[1].visible_name.casefold()
    # Third is cheese (count 1)
    assert "сыр" in grouped[2].visible_name.casefold()


# -----------------------------------------------------------------------------
# 7. Different cooking preparations -> not merged
# -----------------------------------------------------------------------------
def test_req7_different_cooking_preparations_not_merged():
    items = [
        FoodVisionItem(
            visible_name="сырая курица",
            normalized_name="куриная грудка",
            confidence=0.9,
            estimated_grams_min=100.0,
            estimated_grams_max=120.0,
            preparation="сырая",
        ),
        FoodVisionItem(
            visible_name="жареная курица",
            normalized_name="куриная грудка",
            confidence=0.9,
            estimated_grams_min=100.0,
            estimated_grams_max=120.0,
            preparation="жареная",
        ),
        FoodVisionItem(
            visible_name="свежие огурцы",
            normalized_name="огурцы",
            confidence=0.9,
            estimated_grams_min=50.0,
            estimated_grams_max=60.0,
            preparation="свежие",
        ),
        FoodVisionItem(
            visible_name="соленые огурцы",
            normalized_name="огурцы",
            confidence=0.9,
            estimated_grams_min=50.0,
            estimated_grams_max=60.0,
            preparation="соленые",
        ),
    ]
    grouped = _aggregate_identical_food_vision_items(items)
    assert len(grouped) == 4
    # Neither raw/fried chicken nor fresh/pickled cucumbers should be merged
    preps = [it.preparation for it in grouped]
    assert preps == ["сырая", "жареная", "свежие", "соленые"]


# -----------------------------------------------------------------------------
# 8. Composite dishes -> no double counting
# -----------------------------------------------------------------------------
def test_req8_composite_dishes_no_double_counting():
    items = [
        PendingFoodVisionItem(
            index=1,
            visible_name="Плов",
            normalized_name="плов",
            confidence=0.95,
            estimated_grams_min=250.0,
            estimated_grams_max=350.0,
            selected_grams=300.0,
            preparation="готовое блюдо",
            is_sauce=False,
            uncertainty="",
            item_id="item_plov",
        ),
        PendingFoodVisionItem(
            index=2,
            visible_name="Мясо",
            normalized_name="говядина",
            confidence=0.9,
            estimated_grams_min=80.0,
            estimated_grams_max=120.0,
            selected_grams=100.0,
            preparation="в плове",
            is_sauce=False,
            uncertainty="",
            item_id="item_meat",
        ),
        PendingFoodVisionItem(
            index=3,
            visible_name="Рис",
            normalized_name="рис",
            confidence=0.9,
            estimated_grams_min=120.0,
            estimated_grams_max=180.0,
            selected_grams=150.0,
            preparation="в плове",
            is_sauce=False,
            uncertainty="",
            item_id="item_rice",
        ),
        PendingFoodVisionItem(
            index=4,
            visible_name="Помидоры",
            normalized_name="помидоры",
            confidence=0.9,
            estimated_grams_min=70.0,
            estimated_grams_max=90.0,
            selected_grams=80.0,
            preparation="свежие",
            is_sauce=False,
            uncertainty="",
            item_id="item_tomatoes",
        ),
    ]
    inv = PendingFoodVisionInventory(
        user_id=102,
        inventory_id="inv-plov",
        schema_version="food_vision_inventory_v1",
        items=items,
        overall_confidence=0.95,
        warnings=[],
    )
    snapshot = _build_inventory_calculation_snapshot(inv)
    assert snapshot is not None
    # Meat and rice are suppressed as ingredients of plov; tomatoes remain external
    comp_names = [c["canonical_name"].casefold() for c in snapshot["components"]]
    assert any("плов" in name for name in comp_names)
    assert any("помидор" in name for name in comp_names)
    assert not any("говядина" in name for name in comp_names)
    assert not any("рис" in name for name in comp_names)


# -----------------------------------------------------------------------------
# 9. Already grouped Vision output -> no double multiplication
# -----------------------------------------------------------------------------
def test_req9_already_grouped_vision_output_no_double_multiplication():
    # Vision already returned 1 item with quantity=5 and combined weight 50-75g
    raw_payload = json.dumps({
        "schema_version": "food_vision_inventory_v1",
        "is_food": True,
        "items": [
            {
                "visible_name": "Грецкие орехи",
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
    })
    result = validate_food_vision_inventory(raw_payload)
    assert result.status == "VALID"
    assert len(result.inventory.items) == 1
    item = result.inventory.items[0]
    assert item.quantity == 5
    # Must NOT multiply by 5 again (which would falsely give 250-375g)
    assert item.estimated_grams_min == pytest.approx(50.0)
    assert item.estimated_grams_max == pytest.approx(75.0)
    assert item.piece_grams == pytest.approx(12.5)


# -----------------------------------------------------------------------------
# 10. Quantity correction -> full recalculation
# -----------------------------------------------------------------------------
def test_req10_quantity_correction_recalculates_mass_and_kbzhu(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    user_id = 201

    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="грецкий орех в скорлупе",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=60.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        count_type="COUNT_CONFIRMED",
        piece_grams=12.0,
        edible_yield_ratio=0.45,
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-corr",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    initial_cal = inv.calculation_snapshot["calories_kcal"]

    # User adjusts count: "Исправить 1: 7 штук"
    res = diary.handle_pending_inventory_reply(user_id, "Исправить 1: 7 штук")
    assert res.status == "updated"
    assert res.inventory is not None

    upd_item = res.inventory.items[0]
    assert upd_item.quantity == 7
    assert upd_item.count_type == "COUNT_CONFIRMED"
    # 7 * 12g = 84g
    assert upd_item.selected_grams == pytest.approx(84.0)

    # Recalculated KBZHU: 84g * 0.45 = 37.8g edible -> ~247 kcal
    upd_cal = res.inventory.calculation_snapshot["calories_kcal"]
    assert upd_cal > initial_cal
    assert upd_cal == pytest.approx(37.8 * 6.54, rel=0.08)
    assert "7 шт" in res.reply_text


# -----------------------------------------------------------------------------
# 11. Total weight correction -> full recalculation
# -----------------------------------------------------------------------------
def test_req11_weight_correction_recalculates_group_and_kbzhu(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    user_id = 202

    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="грецкий орех в скорлупе",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=60.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        count_type="COUNT_CONFIRMED",
        piece_grams=12.0,
        edible_yield_ratio=0.45,
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-weight",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # User adjusts weight for group: "Вес 1: 80 г"
    res = diary.handle_pending_inventory_reply(user_id, "Вес 1: 80 г")
    assert res.status == "updated"
    assert res.inventory is not None

    upd_item = res.inventory.items[0]
    assert upd_item.selected_grams == pytest.approx(80.0)
    assert upd_item.piece_grams == pytest.approx(16.0)

    # Recalculated KBZHU: 80g * 0.45 = 36.0g edible -> ~235.4 kcal
    upd_cal = res.inventory.calculation_snapshot["calories_kcal"]
    assert upd_cal == pytest.approx(36.0 * 6.54, rel=0.08)


# -----------------------------------------------------------------------------
# 12. Group deletion -> correct totals
# -----------------------------------------------------------------------------
def test_req12_remove_group_updates_totals_correctly(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    user_id = 203

    item1 = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="грецкий орех в скорлупе",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=60.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        count_type="COUNT_CONFIRMED",
        piece_grams=12.0,
        edible_yield_ratio=0.45,
    )
    item2 = PendingFoodVisionItem(
        index=2,
        visible_name="Яблоки",
        normalized_name="яблоко",
        confidence=0.95,
        estimated_grams_min=180.0,
        estimated_grams_max=220.0,
        selected_grams=200.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        quantity=1,
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-del",
        schema_version="food_vision_inventory_v1",
        items=[item1, item2],
        overall_confidence=0.95,
        warnings=[],
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # Delete item 1
    res = diary.handle_pending_inventory_reply(user_id, "Удалить 1")
    assert res.status == "updated"
    assert res.inventory is not None
    assert len(res.inventory.items) == 1

    rem = res.inventory.items[0]
    assert rem.index == 1
    assert "Яблоки" in rem.visible_name

    # Snapshot totals must match apple alone (~104 kcal for 200g)
    snap_cal = res.inventory.calculation_snapshot["calories_kcal"]
    assert snap_cal == pytest.approx(200.0 * 0.52, rel=0.05)


# -----------------------------------------------------------------------------
# 13. Old pending inventory backward compatibility
# -----------------------------------------------------------------------------
def test_req13_backward_compatibility_old_pending_inventory():
    # Legacy serialized JSON with no quantity, piece_grams, edible_yield_ratio
    old_json = {
        "kind": "vision_inventory_confirmation",
        "state": "awaiting_inventory_confirmation",
        "inventory_id": "legacy-inv-1",
        "schema_version": "food_vision_inventory_v1",
        "user_id": 999,
        "source": "vision",
        "items": [
            {
                "index": 1,
                "visible_name": "Гречневая каша",
                "normalized_name": "гречка",
                "confidence": 0.9,
                "estimated_grams_min": 150.0,
                "estimated_grams_max": 200.0,
                "selected_grams": 175.0,
                "preparation": "отварная",
                "is_sauce": False,
                "uncertainty": "",
                "user_modified": False,
                "item_id": "item_1",
                "suppressed_by_item_id": None,
            }
        ],
        "overall_confidence": 0.9,
        "needs_user_confirmation": False,
        "warnings": [],
        "image_ref": "photo_123",
        "occurred_at": "2026-10-21 07:00:00",
        "calculation_snapshot": None,
        "household_id": None,
    }
    now = datetime.now(timezone.utc)
    deserialized = _deserialize_pending_inventory(old_json, created_at=now, expires_at=now)
    assert deserialized is not None
    assert len(deserialized.items) == 1
    item = deserialized.items[0]
    assert item.quantity is None
    assert item.count_type == "COUNT_UNKNOWN"
    assert item.piece_grams is None

    # Reply formatting works without crash
    reply = format_food_vision_inventory_reply(deserialized)
    assert "1. Гречневая каша" in reply


# -----------------------------------------------------------------------------
# 14. Save without duplication
# -----------------------------------------------------------------------------
def test_req14_save_without_duplication(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    user_id = 301

    item = PendingFoodVisionItem(
        index=1,
        visible_name="Грецкие орехи в скорлупе",
        normalized_name="грецкий орех в скорлупе",
        confidence=0.95,
        estimated_grams_min=50.0,
        estimated_grams_max=75.0,
        selected_grams=60.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        quantity=5,
        count_type="COUNT_CONFIRMED",
        piece_grams=12.0,
        edible_yield_ratio=0.45,
    )
    inv = PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id="inv-save",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.95,
        warnings=[],
        image_ref="photo_walnuts_01",
    )
    inv.calculation_snapshot = _build_inventory_calculation_snapshot(inv)
    diary._store_pending_inventory(inv)

    # First save
    save_res = diary.save_pending_inventory_directly(user_id)
    assert save_res.status == "saved"
    assert save_res.saved is True
    assert save_res.sqlite_id is not None

    # Staged pending must be cleared
    assert diary.get_pending_inventory(user_id) is None

    # Repeated save attempt with same image_ref detects duplicate
    sqlite_id2, duplicate = diary.save_record(
        user_id=user_id,
        source="vision",
        record=save_res.record,
        image_ref="photo_walnuts_01",
        occurred_at=datetime.now(timezone.utc),
    )
    assert duplicate is True


# -----------------------------------------------------------------------------
# 15. User and household isolation
# -----------------------------------------------------------------------------
def test_req15_user_and_household_isolation(tmp_path):
    diary = HealBiteNutritionDiary(db_path=tmp_path / "test.db")
    user_a = 401
    user_b = 402

    inv_a = PendingFoodVisionInventory(
        user_id=user_a,
        inventory_id="inv-a",
        schema_version="food_vision_inventory_v1",
        items=[PendingFoodVisionItem(
            index=1, visible_name="Яблоки", normalized_name="яблоко",
            confidence=0.9, estimated_grams_min=100.0, estimated_grams_max=150.0,
            selected_grams=120.0, preparation="", is_sauce=False, uncertainty="",
        )],
        overall_confidence=0.9,
        warnings=[],
    )
    inv_b = PendingFoodVisionInventory(
        user_id=user_b,
        inventory_id="inv-b",
        schema_version="food_vision_inventory_v1",
        items=[PendingFoodVisionItem(
            index=1, visible_name="Сыр", normalized_name="сыр",
            confidence=0.95, estimated_grams_min=40.0, estimated_grams_max=60.0,
            selected_grams=50.0, preparation="", is_sauce=False, uncertainty="",
        )],
        overall_confidence=0.95,
        warnings=[],
    )
    diary._store_pending_inventory(inv_a)
    diary._store_pending_inventory(inv_b)

    # User A updates quantity on item 1
    diary.handle_pending_inventory_reply(user_a, "Исправить 1: 3 шт")

    # User B's state must remain completely isolated and unaffected
    loaded_b = diary.get_pending_inventory(user_b)
    assert loaded_b is not None
    assert loaded_b.items[0].visible_name == "Сыр"
    assert loaded_b.items[0].quantity is None


# -----------------------------------------------------------------------------
# 16. Unknown count -> does not fabricate item count
# -----------------------------------------------------------------------------
def test_req16_unknown_count_does_not_invent_quantity():
    # Continuous food (e.g. shredded cheese or oatmeal) without determinable count
    item = PendingFoodVisionItem(
        index=1,
        visible_name="Тертый сыр",
        normalized_name="сыр",
        confidence=0.9,
        estimated_grams_min=40.0,
        estimated_grams_max=60.0,
        selected_grams=50.0,
        preparation="",
        is_sauce=False,
        uncertainty="",
        quantity=None,
        count_type="COUNT_UNKNOWN",
    )
    inv = PendingFoodVisionInventory(
        user_id=501,
        inventory_id="inv-unknown-count",
        schema_version="food_vision_inventory_v1",
        items=[item],
        overall_confidence=0.9,
        warnings=[],
    )
    reply = format_food_vision_inventory_reply(inv)
    # Must NOT invent "1 шт" or other piece count in item line
    item_line = [line for line in reply.splitlines() if line.startswith("1. ")][0]
    assert "шт" not in item_line
    assert "1. Тертый сыр — " in reply


# -----------------------------------------------------------------------------
# 17. Multiple photos in a batch -> no double counting
# -----------------------------------------------------------------------------
def test_req17_multiple_photos_batch_no_double_counting():
    # Photos in one batch detecting the same walnuts from two angles
    items_photo1 = [
        FoodVisionItem(
            visible_name="грецкий орех",
            normalized_name="грецкий орех в скорлупе",
            confidence=0.92,
            estimated_grams_min=10.0,
            estimated_grams_max=15.0,
        )
        for _ in range(5)
    ]
    # Photo 2 also sees the same walnuts as a group from a second angle
    items_photo2 = [
        FoodVisionItem(
            visible_name="грецкие орехи",
            normalized_name="грецкий орех в скорлупе",
            confidence=0.95,
            estimated_grams_min=50.0,
            estimated_grams_max=75.0,
            quantity=5,
        )
    ]
    # Case B: Both photos participate in multi-photo batch pipeline -> no double counting!
    grouped = _aggregate_multi_photo_batch([items_photo1, items_photo2])
    assert len(grouped) == 1
    assert grouped[0].quantity == 5
    assert grouped[0].estimated_grams_min == pytest.approx(50.0)
    assert grouped[0].estimated_grams_max == pytest.approx(75.0)

    # Case C: Photo 1 sees walnuts, Photo 2 sees distinct food (e.g. 2 apples) -> both retained!
    items_photo2_distinct = [
        FoodVisionItem(
            visible_name="яблоки",
            normalized_name="яблоко",
            confidence=0.94,
            estimated_grams_min=250.0,
            estimated_grams_max=350.0,
            quantity=2,
        )
    ]
    combined = _aggregate_multi_photo_batch([items_photo1, items_photo2_distinct])
    assert len(combined) == 2
    names = {it.normalized_name for it in combined}
    assert "грецкий орех в скорлупе" in names
    assert "яблоко" in names
