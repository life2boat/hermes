from __future__ import annotations

from datetime import datetime, timezone
import pytest

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    NutritionRecord,
)


def test_complete_components_null_row_totals(tmp_path):
    """Test 1 — Complete components, NULL row totals.

    - All active components have all four macros/calories.
    - Row-level totals are NULL.
    - Assert incomplete_entry_count == 0.
    - Assert has_incomplete_entries is False.
    - Assert is_complete is True.
    """
    db_path = tmp_path / "healbite_semantics.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 101
    occurred_at = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)

    items = [
        {
            "name": "Варёное яйцо",
            "weight_g": 100.0,
            "calories_kcal": 155.0,
            "protein_g": 13.0,
            "fat_g": 11.0,
            "carbs_g": 1.1,
        },
        {
            "name": "Овсянка",
            "weight_g": 200.0,
            "calories_kcal": 220.0,
            "protein_g": 6.0,
            "fat_g": 4.0,
            "carbs_g": 40.0,
        },
    ]

    record = NutritionRecord(
        is_food=True,
        meal_name="Завтрак",
        items=items,
        calories_kcal=None,
        protein_g=None,
        fat_g=None,
        carbs_g=None,
        confidence=1.0,
        raw_summary="Завтрак summary",
    )
    diary.save_record(
        user_id=user_id,
        source="text",
        record=record,
        image_ref=None,
        occurred_at=occurred_at,
    )

    summary = diary.get_summary(user_id=user_id, now=occurred_at, days=1)

    assert summary["incomplete_entry_count"] == 0
    assert summary["has_incomplete_entries"] is False
    assert summary["is_complete"] is True


def test_incomplete_component_complete_row_totals(tmp_path):
    """Test 2 — Incomplete component, complete row totals.

    - Row-level totals contain all four nutritional values.
    - An active component has all four values NULL.
    - Assert incomplete_entry_count == 1.
    - Assert has_incomplete_entries is True.
    - Assert is_complete is False.
    - Assert unknown component values remain NULL (never fabricated as zero).
    """
    db_path = tmp_path / "healbite_semantics.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 102
    occurred_at = datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)

    items = [
        {
            "name": "Неизвестный соус",
            "weight_g": 50.0,
            "calories_kcal": None,
            "protein_g": None,
            "fat_g": None,
            "carbs_g": None,
        },
    ]

    record = NutritionRecord(
        is_food=True,
        meal_name="Обед",
        items=items,
        calories_kcal=500.0,
        protein_g=30.0,
        fat_g=20.0,
        carbs_g=50.0,
        confidence=1.0,
        raw_summary="Обед summary",
    )
    diary.save_record(
        user_id=user_id,
        source="text",
        record=record,
        image_ref=None,
        occurred_at=occurred_at,
    )

    summary = diary.get_summary(user_id=user_id, now=occurred_at, days=1)

    assert summary["incomplete_entry_count"] == 1
    assert summary["has_incomplete_entries"] is True
    assert summary["is_complete"] is False

    entry = summary["entries"][0]
    active_component = entry["items"][0]
    assert active_component["calories_kcal"] is None
    assert active_component["protein_g"] is None
    assert active_component["fat_g"] is None
    assert active_component["carbs_g"] is None
