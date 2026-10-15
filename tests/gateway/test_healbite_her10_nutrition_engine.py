from datetime import datetime, timedelta, timezone
import pytest

from gateway.healbite_food_composition import (
    FOOD_COMPOSITION_PROVENANCE,
    FOOD_COMPOSITION_VERSION,
    resolve_food_composition,
)
from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    PendingFoodVisionInventory,
    PendingFoodVisionItem,
    calculate_inventory_nutrition,
    format_nutrition_diary_report,
    _INVENTORY_AMBIGUOUS_ERROR,
)


def _food_item(
    index: int,
    visible_name: str,
    normalized_name: str,
    grams: float | None = None,
    min_g: float | None = 100.0,
    max_g: float | None = 100.0,
    is_sauce: bool = False,
) -> PendingFoodVisionItem:
    if grams is None:
        min_g = None
        max_g = None
    return PendingFoodVisionItem(
        index=index,
        visible_name=visible_name,
        normalized_name=normalized_name,
        confidence=0.95,
        estimated_grams_min=min_g,
        estimated_grams_max=max_g,
        selected_grams=grams,
        preparation="",
        is_sauce=is_sauce,
        uncertainty="",
        user_modified=False,
    )


def _inventory(user_id: int, *items: PendingFoodVisionItem) -> PendingFoodVisionInventory:
    now = datetime.now(timezone.utc)
    return PendingFoodVisionInventory(
        user_id=user_id,
        inventory_id=f"inv-{user_id}",
        schema_version="food_vision_inventory_v1",
        items=list(items),
        overall_confidence=0.95,
        warnings=[],
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        source="vision",
        state="PENDING_INVENTORY_CONFIRMATION",
        image_ref=f"telegram:{user_id}:1",
        occurred_at=now,
    )


def test_her10_provenance_and_version():
    assert "Russian National Food Composition Tables" in FOOD_COMPOSITION_PROVENANCE
    assert "USDA" in FOOD_COMPOSITION_PROVENANCE
    assert FOOD_COMPOSITION_VERSION == "2026.1"


def test_her10_incident_draniki_and_salmon_calculates_and_saves(tmp_path):
    """
    Point 1: Exact incident test.
    Photo with 'Драники 200 г' and 'Лосось 100 г' must calculate KBZHU and save successfully.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 101

    item1 = _food_item(1, "Драники", "драники", grams=200.0, min_g=190.0, max_g=210.0)
    item2 = _food_item(2, "Лосось", "лосось", grams=100.0, min_g=95.0, max_g=105.0)
    inv = _inventory(user_id, item1, item2)

    # 1. Calculation check
    calc = calculate_inventory_nutrition(inv)
    assert calc.status == "READY_FOR_NUTRITION_CALCULATION"
    assert len(calc.blocked_indexes) == 0
    assert len(calc.components) == 2

    # Draniki: 205 kcal/100g * 2 = 410 kcal
    draniki_comp = calc.components[0]
    assert draniki_comp.nutrition_available is True
    assert draniki_comp.calories_kcal == pytest.approx(410.0, abs=1.0)

    # Salmon: 142 kcal/100g * 1 = 142 kcal
    salmon_comp = calc.components[1]
    assert salmon_comp.nutrition_available is True
    assert salmon_comp.calories_kcal == pytest.approx(142.0, abs=1.0)

    assert calc.record is not None
    assert calc.record.calories_kcal == pytest.approx(552.0, abs=2.0)

    # 2. Stage inventory and one-tap save
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv,
        image_ref=inv.image_ref,
        occurred_at=inv.occurred_at,
    )

    result = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")
    assert result.status == "saved"
    assert result.saved is True
    assert "✅ Сохранено" in result.reply_text

    # Pending state cleared
    assert diary.get_pending_inventory(user_id) is None
    assert diary.get_pending_meal(user_id) is None

    # Diary entry verified
    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == pytest.approx(552.0, abs=2.0)
    assert summary["has_incomplete_entries"] is False
    assert summary["is_complete"] is True


def test_her10_one_tap_save_without_intermediate_confirmation(tmp_path):
    """
    Point 2: Direct One-Tap save.
    '✅ Сохранить' or 'сохранить' saves directly without intermediate 'Да' roundtrip.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 102

    item = _food_item(1, "Куриная грудка", "куриная грудка", grams=150.0)
    inv = _inventory(user_id, item)
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv,
        image_ref=inv.image_ref,
        occurred_at=inv.occurred_at,
    )

    # User clicks "Сохранить" in one tap
    result = diary.handle_pending_inventory_reply(user_id, "сохранить")
    assert result.status == "saved"
    assert result.saved is True
    assert diary.get_pending_meal(user_id) is None
    assert diary.get_pending_inventory(user_id) is None

    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1


def test_her10_fallback_save_without_kbzhu_preserves_food_and_weight_as_none(tmp_path):
    """
    Point 3: Fallback save without KBZHU.
    Save meal with unmapped component, preserving food and weight, with None in calories/macros.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 103

    item = _food_item(1, "Марсианский пирог", "марсианский пирог", grams=250.0)
    inv = _inventory(user_id, item)

    calc = calculate_inventory_nutrition(inv)
    assert calc.status == "UNKNOWN_COMPONENT_BLOCKED"
    assert 1 in calc.blocked_indexes

    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv,
        image_ref=inv.image_ref,
        occurred_at=inv.occurred_at,
    )

    # User clicks "Сохранить без КБЖУ"
    result = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить без КБЖУ")
    assert result.status == "saved_without_kbzhu"
    assert result.saved is True
    assert "✅ Сохранено без КБЖУ" in result.reply_text

    # Verify database persistence: macros must be NULL (None), food and weight preserved!
    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1
    assert summary["has_incomplete_entries"] is True
    assert summary["is_complete"] is False

    entry = summary["entries"][0]
    assert entry["calories_kcal"] is None
    assert entry["protein_g"] is None
    assert entry["fat_g"] is None
    assert entry["carbs_g"] is None
    assert "Марсианский пирог" in entry["meal_name"]


def test_her10_multiline_atomic_batch_correction(tmp_path):
    """
    Point 4: Multiline atomic batch correction.
    'Исправить 1: Драники домашние, 200 г\nВес 2: 150 г'
    Both items updated together without partial mutations if one line fails.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 104

    item1 = _food_item(1, "Драники", "драники", grams=100.0)
    item2 = _food_item(2, "Лосось", "лосось", grams=100.0)
    inv = _inventory(user_id, item1, item2)
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv,
        image_ref=inv.image_ref,
        occurred_at=inv.occurred_at,
    )

    # 1. Successful multiline batch
    cmd = "Исправить 1: Драники, 200 г\nВес 2: 150 г"
    res = diary.handle_pending_inventory_reply(user_id, cmd)
    assert res.status == "updated"

    updated_inv = diary.get_pending_inventory(user_id)
    assert updated_inv is not None
    assert updated_inv.items[0].selected_grams == 200.0
    assert updated_inv.items[1].selected_grams == 150.0

    # 2. Failing multiline batch: second line has invalid index 99
    bad_cmd = "Исправить 1: Драники, 300 г\nВес 99: 50 г"
    bad_res = diary.handle_pending_inventory_reply(user_id, bad_cmd)
    assert bad_res.status == "invalid_action"

    # Verify atomicity: item 1 was NOT updated to 300g! Remains 200g.
    current_inv = diary.get_pending_inventory(user_id)
    assert current_inv.items[0].selected_grams == 200.0
    assert current_inv.items[1].selected_grams == 150.0


def test_her10_greedy_ambiguous_command_fails_closed_her9_preserved(tmp_path):
    """
    Point 5: Greedy ambiguous input rejection (HER-9 fix preserved).
    'Исправить 2: лосось слабосоленый 100 грамм яблоко 200 грамм' fails-closed.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 105

    item1 = _food_item(1, "Драники", "драники", grams=200.0)
    item2 = _food_item(2, "Лосось", "лосось", grams=100.0)
    inv = _inventory(user_id, item1, item2)
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv,
        image_ref=inv.image_ref,
        occurred_at=inv.occurred_at,
    )

    ambiguous_cmd = "Исправить 2: лосось слабосоленый 100 грамм яблоко 200 грамм"
    res = diary.handle_pending_inventory_reply(user_id, ambiguous_cmd)
    assert res.status == "invalid_action"
    assert _INVENTORY_AMBIGUOUS_ERROR in res.reply_text

    # Preserved without change
    current_inv = diary.get_pending_inventory(user_id)
    assert len(current_inv.items) == 2
    assert current_inv.items[1].visible_name == "Лосось"
    assert current_inv.items[1].selected_grams == 100.0


def test_her10_russian_inflections_and_aliases():
    """
    Point 6: Russian inflection and alias coverage.
    Драники, драник, деруны, семга, лосось слабосоленый, сыр творожный/сливочный, руккола/рукола, яблоко.
    """
    test_cases = [
        ("драники", "draniki"),
        ("драник", "draniki"),
        ("деруны", "draniki"),
        ("картофельные драники", "draniki"),
        ("лосось", "salmon"),
        ("семга", "salmon"),
        ("сёмга", "salmon"),
        ("лосось слабосоленый", "salmon_lightly_salted"),
        ("слабосоленый лосось", "salmon_lightly_salted"),
        ("сливочный сыр", "cream_cheese"),
        ("сыр сливочный", "cream_cheese"),
        ("творожный сыр", "cream_cheese"),
        ("рукола", "arugula"),
        ("руккола", "arugula"),
        ("свежая руккола", "arugula"),
        ("яблоко", "apple"),
        ("яблоки", "apple"),
        ("хлеб", "bread"),
        ("белый хлеб", "bread"),
        ("черный хлеб", "bread"),
    ]

    for query, expected_id in test_cases:
        res = resolve_food_composition(query)
        assert res.matched is True, f"Failed to match query '{query}'"
        assert res.profile is not None
        assert res.profile.canonical_id == expected_id, f"Expected {expected_id} for '{query}', got {res.profile.canonical_id}"
        assert res.profile.calories_kcal > 0


def test_her10_conservative_variance_handling():
    """
    Point 7: Conservative variance handling.
    Draniki variance note / reference confidence separated from vision confidence.
    """
    res = resolve_food_composition("драники")
    assert res.matched is True
    assert res.profile.variance_note != ""
    assert "масла" in res.profile.variance_note.lower()
    # Reference confidence is 0.85 (conservative due to preparation variance)
    assert res.profile.reference_confidence == 0.85


def test_her10_daily_summary_with_incomplete_entries(tmp_path):
    """
    Point 8: Daily summary calculation with incomplete entries.
    Null values do not distort numeric totals or throw exceptions; summary marked incomplete.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 108

    # 1. Complete meal with 500 kcal
    item_known = _food_item(1, "Гречка", "гречка", grams=200.0)
    inv_known = _inventory(user_id, item_known)
    diary.stage_pending_inventory(user_id=user_id, source="vision", inventory=inv_known, image_ref="ref:1", occurred_at=datetime.now(timezone.utc))
    diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")

    # 2. Incomplete meal without KBZHU
    item_unknown = _food_item(1, "Неизвестный суп", "неизвестный суп", grams=300.0)
    inv_unknown = _inventory(user_id, item_unknown)
    diary.stage_pending_inventory(user_id=user_id, source="vision", inventory=inv_unknown, image_ref="ref:2", occurred_at=datetime.now(timezone.utc))
    diary.handle_pending_inventory_reply(user_id, "✅ Сохранить без КБЖУ")

    # 3. Summary check
    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 2
    assert summary["has_incomplete_entries"] is True
    assert summary["is_complete"] is False
    # Known calories sum correctly without crashing on None
    assert summary["calories_kcal"] > 0

    report = format_nutrition_diary_report(summary)
    assert "Некоторые приёмы пищи сохранены без КБЖУ" in report


def test_her10_correction_after_save_no_duplicates(tmp_path):
    """
    Point 9: Correction after save.
    Updating meal via update_last_meal does not create duplicate entries.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 109

    # Save a meal without KBZHU
    item = _food_item(1, "Домашний пирог", "домашний пирог", grams=200.0)
    inv = _inventory(user_id, item)
    diary.stage_pending_inventory(user_id=user_id, source="vision", inventory=inv, image_ref="ref:109", occurred_at=datetime.now(timezone.utc))
    diary.handle_pending_inventory_reply(user_id, "✅ Сохранить без КБЖУ")

    assert diary.get_daily_summary(user_id=user_id)["entry_count"] == 1

    # Update with manual values later
    update_res = diary.update_last_meal(
        user_id=str(user_id),
        new_calories=380,
        new_protein=8.0,
        new_fat=15.0,
        new_carbs=45.0,
    )
    assert update_res.updated is True

    # Count must remain 1
    summary = diary.get_daily_summary(user_id=user_id)
    assert summary["entry_count"] == 1
    assert summary["calories_kcal"] == 380.0
    assert summary["has_incomplete_entries"] is False
    assert summary["is_complete"] is True


def test_her10_cross_user_isolation(tmp_path):
    """
    Point 10: Cross-user isolation.
    User A's pending inventory / saved diary completely isolated from User B.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_a = 110
    user_b = 111

    item = _food_item(1, "Драники", "драники", grams=200.0)
    inv = _inventory(user_a, item)
    diary.stage_pending_inventory(user_id=user_a, source="vision", inventory=inv, image_ref="ref:110", occurred_at=datetime.now(timezone.utc))

    # User B tries to confirm
    res_b = diary.handle_pending_inventory_reply(user_b, "✅ Сохранить")
    assert res_b.status == "missing"

    assert diary.get_daily_summary(user_id=user_b)["entry_count"] == 0
    assert diary.get_daily_summary(user_id=user_a)["entry_count"] == 0
    assert diary.get_pending_inventory(user_a) is not None


def test_her10_stale_inventory_rejection(tmp_path):
    """
    Point 11: Stale inventory rejection.
    New photo supersedes previous pending inventory ID.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 112

    inv1 = _inventory(user_id, _food_item(1, "Драники", "драники", grams=200.0))
    staged1 = diary.stage_pending_inventory(user_id=user_id, source="vision", inventory=inv1, image_ref="ref:1", occurred_at=datetime.now(timezone.utc))

    inv2 = _inventory(user_id, _food_item(1, "Лосось", "лосось", grams=100.0))
    staged2 = diary.stage_pending_inventory(user_id=user_id, source="vision", inventory=inv2, image_ref="ref:2", occurred_at=datetime.now(timezone.utc))

    # Try saving with old expected ID
    res = diary.save_pending_inventory_directly(user_id=user_id, expected_inventory_id=staged1.inventory_id)
    assert res.status == "stale"
    assert diary.get_daily_summary(user_id=user_id)["entry_count"] == 0

    # Current inventory remains staged2
    assert diary.get_pending_inventory(user_id).inventory_id == staged2.inventory_id


def test_her10_missing_weight_and_expired_safety(tmp_path):
    """
    Point 12: Zero-state & missing weight safety.
    Clear prompts when weight missing or inventory expired.
    """
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 113

    # 1. Missing weight
    inv_no_weight = _inventory(user_id, _food_item(1, "Драники", "драники", grams=None))
    diary.stage_pending_inventory(user_id=user_id, source="vision", inventory=inv_no_weight, image_ref="ref:113", occurred_at=datetime.now(timezone.utc))

    res_missing = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")
    assert res_missing.status == "needs_weight_confirmation"
    assert "укажите вес" in res_missing.reply_text.lower()
    assert diary.get_daily_summary(user_id=user_id)["entry_count"] == 0

    # 2. Expired inventory
    inv_expired = _inventory(user_id, _food_item(1, "Драники", "драники", grams=200.0))
    inv_expired.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    diary.stage_pending_inventory(
        user_id=user_id,
        source="vision",
        inventory=inv_expired,
        image_ref="ref:113_exp",
        occurred_at=datetime.now(timezone.utc),
        expires_at=inv_expired.expires_at,
    )

    res_expired = diary.handle_pending_inventory_reply(user_id, "✅ Сохранить")
    assert res_expired.status == "expired"
    assert diary.get_pending_inventory(user_id) is None
