from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    NutritionRecord,
    compute_nutrition_diary_summary,
    format_nutrition_diary_report,
    normalize_nutrition_payload,
)
from gateway.platforms.telegram import TelegramAdapter
from gateway.run import GatewayRunner


def _seed_profile_timezone(db_path, user_id: int, tz_name: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                timezone TEXT
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO profiles(user_id, timezone) VALUES (?, ?)",
            (user_id, tz_name),
        )


def _build_test_record(
    *,
    meal_name: str,
    calories_kcal: float | None,
    protein_g: float | None,
    fat_g: float | None,
    carbs_g: float | None,
    items: list[dict] | None = None,
    confidence: float = 0.92,
    is_approximate: bool = True,
) -> NutritionRecord:
    totals = {}
    if calories_kcal is not None:
        totals["calories_kcal"] = calories_kcal
    if protein_g is not None:
        totals["protein_g"] = protein_g
    if fat_g is not None:
        totals["fat_g"] = fat_g
    if carbs_g is not None:
        totals["carbs_g"] = carbs_g

    raw_json = json.dumps(
        {
            "is_food": True,
            "meal_name": meal_name,
            "raw_summary": meal_name,
            "confidence": confidence,
            "totals": totals,
            "items": items or [],
            "is_approximate_range": is_approximate,
        },
        ensure_ascii=False,
    )
    return normalize_nutrition_payload(raw_json)


def _save(diary, *, user_id: int, record: NutritionRecord, occurred_at: datetime, seq: int = 1):
    return diary.save_record(
        user_id=user_id,
        source="vision",
        record=record,
        image_ref=f"telegram:{user_id}:{seq}",
        occurred_at=occurred_at,
    )


def test_daily_report_chronological_order(tmp_path):
    """AC-02: Display every saved meal in chronological order (occurred_at ASC, id ASC)."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 101

    base_time = datetime(2026, 10, 29, 6, 0, tzinfo=timezone.utc)
    # Insert in reverse order
    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Ужин", calories_kcal=600, protein_g=40, fat_g=20, carbs_g=50),
        occurred_at=base_time + timedelta(hours=12),  # 18:00
        seq=1,
    )
    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Завтрак", calories_kcal=400, protein_g=25, fat_g=15, carbs_g=40),
        occurred_at=base_time + timedelta(hours=2),  # 08:00
        seq=2,
    )
    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Обед", calories_kcal=700, protein_g=50, fat_g=25, carbs_g=60),
        occurred_at=base_time + timedelta(hours=7),  # 13:00
        seq=3,
    )

    summary = diary.get_summary(user_id=user_id, now=base_time + timedelta(hours=14))
    report = format_nutrition_diary_report(summary)

    # Verify chronological sequence in the report output
    breakfast_idx = report.find("Завтрак")
    lunch_idx = report.find("Обед")
    dinner_idx = report.find("Ужин")

    assert breakfast_idx != -1
    assert lunch_idx != -1
    assert dinner_idx != -1
    assert breakfast_idx < lunch_idx < dinner_idx


def test_daily_report_meal_and_item_breakdown(tmp_path):
    """AC-02: Display identified dishes and ingredients from items_json with local time and weights."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 102

    meal_time = datetime(2026, 10, 29, 9, 30, tzinfo=timezone.utc)
    items = [
        {
            "name": "Овсяная каша",
            "weight_g": 200,
            "calories_kcal": 220,
            "protein_g": 6,
            "fat_g": 4,
            "carbs_g": 40,
        },
        {
            "name": "Зелёное яблоко",
            "weight_g": 150,
            "calories_kcal": 78,
            "protein_g": 0.6,
            "fat_g": 0.6,
            "carbs_g": 19,
        },
    ]
    rec = _build_test_record(
        meal_name="Завтрак",
        calories_kcal=298,
        protein_g=6.6,
        fat_g=4.6,
        carbs_g=59,
        items=items,
    )
    _save(diary, user_id=user_id, record=rec, occurred_at=meal_time, seq=1)

    report = diary.get_daily_report(user_id=user_id, now=meal_time)

    assert "Завтрак" in report
    assert "Овсяная каша" in report
    assert "200 г" in report
    assert "220 ккал" in report
    assert "Зелёное яблоко" in report
    assert "150 г" in report
    assert "78 ккал" in report
    assert "Итого за приём:" in report
    assert "298 ккал" in report


def test_daily_report_missing_weights_do_not_invent_grams(tmp_path):
    """AC-02: Recorded portion weight in grams where available (do not invent weights if missing)."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 103

    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    items = [
        {
            "name": "Кофе чёрный",
            "weight_g": None,
            "calories_kcal": 2,
            "protein_g": 0.2,
            "fat_g": 0,
            "carbs_g": 0.3,
        }
    ]
    rec = _build_test_record(
        meal_name="Перекус",
        calories_kcal=2,
        protein_g=0.2,
        fat_g=0,
        carbs_g=0.3,
        items=items,
    )
    _save(diary, user_id=user_id, record=rec, occurred_at=now, seq=1)

    report = diary.get_daily_report(user_id=user_id, now=now)
    assert "Кофе чёрный" in report
    # Missing weight should not show fabricated "0 г" or "None г"
    assert "Кофе чёрный, 0 г" not in report
    assert "Кофе чёрный, None" not in report


def test_daily_report_daily_totals_strictly_from_persisted_records(tmp_path):
    """AC-03: Daily totals for Calories, Protein, Fat, Carbohydrates derived strictly from persisted records."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 104

    now = datetime(2026, 10, 29, 14, 0, tzinfo=timezone.utc)
    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Завтрак", calories_kcal=450, protein_g=30, fat_g=15, carbs_g=45),
        occurred_at=now - timedelta(hours=4),
        seq=1,
    )
    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Обед", calories_kcal=650, protein_g=45, fat_g=25, carbs_g=55),
        occurred_at=now,
        seq=2,
    )

    summary = diary.get_summary(user_id=user_id, now=now)
    report = format_nutrition_diary_report(summary)

    # 450 + 650 = 1100 kcal, 30 + 45 = 75 protein, 15 + 25 = 40 fat, 45 + 55 = 100 carbs
    assert summary["calories_kcal"] == 1100.0
    assert summary["protein_g"] == 75.0
    assert summary["fat_g"] == 40.0
    assert summary["carbs_g"] == 100.0

    assert "1100 ккал" in report
    assert "75 г" in report
    assert "40 г" in report
    assert "100 г" in report


def test_daily_report_composite_dish_deduplication(tmp_path):
    """AC-03: Prevent duplicate counting of composite dishes and ingredients (suppressed_by_item_id)."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 105

    now = datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc)
    # Composite dish: Plov is top-level; Rice and Meat are suppressed by plov
    items = [
        {
            "item_id": "plov_1",
            "name": "Плов с говядиной",
            "weight_g": 350,
            "calories_kcal": 650,
            "protein_g": 28,
            "fat_g": 26,
            "carbs_g": 72,
            "suppressed_by_item_id": None,
        },
        {
            "item_id": "rice_sub",
            "name": "Рис отварной",
            "weight_g": 200,
            "calories_kcal": 260,
            "protein_g": 5,
            "fat_g": 1,
            "carbs_g": 56,
            "suppressed_by_item_id": "plov_1",
        },
        {
            "item_id": "meat_sub",
            "name": "Говядина тушеная",
            "weight_g": 150,
            "calories_kcal": 390,
            "protein_g": 23,
            "fat_g": 25,
            "carbs_g": 16,
            "suppressed_by_item_id": "plov_1",
        },
    ]
    rec = _build_test_record(
        meal_name="Обед",
        calories_kcal=650,
        protein_g=28,
        fat_g=26,
        carbs_g=72,
        items=items,
    )
    _save(diary, user_id=user_id, record=rec, occurred_at=now, seq=1)

    report = diary.get_daily_report(user_id=user_id, now=now)

    assert "Плов с говядиной" in report
    # The suppressed ingredients must not be rendered as separate items
    assert "Рис отварной" not in report
    assert "Говядина тушеная" not in report
    # Row total remains 650 kcal, not doubled (650 + 260 + 390)
    assert "650 ккал" in report


def test_daily_report_user_corrections_priority(tmp_path):
    """AC-04: User overrides take precedence in item nutrition display."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 106

    now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    items = [
        {
            "name": "Творог",
            "weight_g": 150,
            "calories_kcal": 180,
            "protein_g": 24,
            "fat_g": 6,
            "carbs_g": 4,
            "user_nutrition": {
                "grams": 200,
                "calories_kcal": 240,
                "protein_g": 32,
                "fat_g": 8,
                "carbs_g": 5,
            },
        }
    ]
    rec = _build_test_record(
        meal_name="Полдник",
        calories_kcal=240,
        protein_g=32,
        fat_g=8,
        carbs_g=5,
        items=items,
    )
    _save(diary, user_id=user_id, record=rec, occurred_at=now, seq=1)

    report = diary.get_daily_report(user_id=user_id, now=now)
    assert "Творог, 200 г" in report
    assert "240 ккал" in report
    assert "Б 32 г" in report


def test_daily_report_honest_uncertainty_none_macros_never_zero(tmp_path):
    """AC-04 / AC-05: Unknown macronutrients must NEVER be represented as zero. Partial warning shown."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 107

    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    # Meal saved without KBZHU
    rec = _build_test_record(
        meal_name="Суп",
        calories_kcal=None,
        protein_g=None,
        fat_g=None,
        carbs_g=None,
    )
    _save(diary, user_id=user_id, record=rec, occurred_at=now, seq=1)

    summary = diary.get_summary(user_id=user_id, now=now)
    assert summary["has_incomplete_entries"] is True
    assert summary["calories_kcal"] is None
    assert summary["protein_g"] is None

    report = format_nutrition_diary_report(summary)
    # Check that missing calories and macros are not rendered as 0 ккал / 0 г
    assert "0 ккал" not in report
    assert "0 г" not in report
    assert "не указано" in report
    assert "учтено частично" in report
    assert "без КБЖУ" in report


def test_daily_report_approximate_markers_preserved(tmp_path):
    """AC-04: Preserve distinction between exact and approximate values (mark with ~)."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 108

    now = datetime(2026, 10, 29, 13, 0, tzinfo=timezone.utc)
    items = [
        {
            "name": "Шаурма",
            "weight_g": 300,
            "calories_kcal": 550,
            "protein_g": 25,
            "fat_g": 22,
            "carbs_g": 60,
            "is_approximate": True,
        }
    ]
    rec = _build_test_record(
        meal_name="Обед",
        calories_kcal=550,
        protein_g=25,
        fat_g=22,
        carbs_g=60,
        items=items,
        is_approximate=True,
    )
    _save(diary, user_id=user_id, record=rec, occurred_at=now, seq=1)

    report = diary.get_daily_report(user_id=user_id, now=now)
    assert "~550 ккал" in report


def test_daily_report_timezone_window_and_midnight_transitions(tmp_path):
    """AC-01: User's local calendar day window (local 00:00 to 24:00) using profiles.timezone."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 109

    # Set timezone to Asia/Vladivostok (UTC+10)
    _seed_profile_timezone(db_path, user_id=user_id, tz_name="Asia/Vladivostok")

    # In Vladivostok:
    # 2026-10-29 23:30 local is 2026-10-29 13:30 UTC -> inside Oct 29 local day
    # 2026-10-30 01:30 local is 2026-10-29 15:30 UTC -> inside Oct 30 local day (next day!)
    time_oct29_local = datetime(2026, 10, 29, 13, 30, tzinfo=timezone.utc)
    time_oct30_local = datetime(2026, 10, 29, 15, 30, tzinfo=timezone.utc)

    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Поздний ужин 29-го", calories_kcal=350, protein_g=20, fat_g=10, carbs_g=30),
        occurred_at=time_oct29_local,
        seq=1,
    )
    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Ночной перекус 30-го", calories_kcal=200, protein_g=10, fat_g=5, carbs_g=20),
        occurred_at=time_oct30_local,
        seq=2,
    )

    # Query for Oct 29 local day (now = 2026-10-29 23:45 Vladivostok time = 13:45 UTC)
    query_oct29 = datetime(2026, 10, 29, 13, 45, tzinfo=timezone.utc)
    summary_29 = diary.get_summary(user_id=user_id, now=query_oct29)

    assert summary_29["entry_count"] == 1
    assert summary_29["entries"][0]["meal_name"] == "Поздний ужин 29-го"
    assert summary_29["calories_kcal"] == 350.0

    # Query for Oct 30 local day (now = 2026-10-30 02:00 Vladivostok time = 16:00 UTC Oct 29)
    query_oct30 = datetime(2026, 10, 29, 16, 0, tzinfo=timezone.utc)
    summary_30 = diary.get_summary(user_id=user_id, now=query_oct30)

    assert summary_30["entry_count"] == 1
    assert summary_30["entries"][0]["meal_name"] == "Ночной перекус 30-го"
    assert summary_30["calories_kcal"] == 200.0


def test_daily_report_empty_day_message(tmp_path):
    """AC-05: Empty day shows clear message that no meals have been recorded today."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 110

    report = diary.get_daily_report(user_id=user_id)
    assert "📊 <b>Твой дневник за сегодня пока пуст.</b>" in report


def test_daily_report_user_isolation(tmp_path):
    """AC-06: Strictly filter user_id = ?. Zero cross-user leakage."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_a = 201
    user_b = 202

    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    _save(
        diary,
        user_id=user_a,
        record=_build_test_record(meal_name="Еда Пользователя А", calories_kcal=500, protein_g=30, fat_g=20, carbs_g=50),
        occurred_at=now,
        seq=1,
    )
    _save(
        diary,
        user_id=user_b,
        record=_build_test_record(meal_name="Еда Пользователя Б", calories_kcal=800, protein_g=50, fat_g=30, carbs_g=70),
        occurred_at=now,
        seq=2,
    )

    report_a = diary.get_daily_report(user_id=user_a, now=now)
    report_b = diary.get_daily_report(user_id=user_b, now=now)

    assert "Еда Пользователя А" in report_a
    assert "Еда Пользователя Б" not in report_a
    assert "500 ккал" in report_a
    assert "800 ккал" not in report_a

    assert "Еда Пользователя Б" in report_b
    assert "Еда Пользователя А" not in report_b
    assert "800 ккал" in report_b
    assert "500 ккал" not in report_b


def test_daily_report_idempotency_read_only(tmp_path):
    """AC-06: Repeated requests do not mutate or duplicate diary entries."""
    db_path = tmp_path / "healbite.db"
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    user_id = 112

    now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    _save(
        diary,
        user_id=user_id,
        record=_build_test_record(meal_name="Обед", calories_kcal=500, protein_g=30, fat_g=20, carbs_g=50),
        occurred_at=now,
        seq=1,
    )

    with sqlite3.connect(db_path) as conn:
        count_before = conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0]

    for _ in range(5):
        report = diary.get_daily_report(user_id=user_id, now=now)
        assert "500 ккал" in report

    with sqlite3.connect(db_path) as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM nutrition_log").fetchone()[0]

    assert count_before == count_after == 1


def test_parse_nutrition_diary_command_arguments():
    """AC-01: Command parsing for /diary and /stats supports today, 1d, 7d, and synonyms."""
    # TelegramAdapter parser
    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary")
    assert days == 1 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary today")
    assert days == 1 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary сегодня")
    assert days == 1 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary 1d")
    assert days == 1 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary 1")
    assert days == 1 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary 7d")
    assert days == 7 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary 7")
    assert days == 7 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/stats")
    assert days == 7 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/stats today")
    assert days == 1 and err is None

    days, err = TelegramAdapter._parse_nutrition_diary_args("/diary invalid_arg")
    assert days is None and "Использование:" in err

    # GatewayRunner parser
    days, err = GatewayRunner._parse_healbite_nutrition_diary_command_args("/diary")
    assert days == 1 and err is None

    days, err = GatewayRunner._parse_healbite_nutrition_diary_command_args("/diary today")
    assert days == 1 and err is None

    days, err = GatewayRunner._parse_healbite_nutrition_diary_command_args("/diary 7d")
    assert days == 7 and err is None
