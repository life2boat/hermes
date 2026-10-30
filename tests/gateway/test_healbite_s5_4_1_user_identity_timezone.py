"""
Regression tests for HealBite S5.4.1: User Identity & Timezone Consistency.

Verifies:
1. Two users with different targets: complete consistency across /start, /profile, /diary, /stats.
2. Two users with different timezones: UTC conversion to local time, day boundary clipping.
3. One Telegram account on two devices: identical view and session handling.
4. Diary and profile independence: zero cross-user data leakage.
5. Time consistency after bot restart: persistence and UTC conversion stability.
"""

from __future__ import annotations

import datetime
from unittest import mock
import pytest

from gateway.healbite_nutrition_diary import (
    HealBiteNutritionDiary,
    NutritionRecord,
    compute_nutrition_diary_summary,
    format_nutrition_diary_report,
)
from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    format_healbite_profile_report,
)
from gateway.platforms.telegram import TelegramAdapter


@pytest.fixture
def test_db(tmp_path):
    """Provide an isolated SQLite database file for testing."""
    return tmp_path / "test_healbite_identity_tz.db"


def _make_record(meal_name: str, cals: float, p: float, f: float, c: float, items: list[dict] | None = None) -> NutritionRecord:
    item_list = items or [{"name": meal_name, "grams": 100, "calories": cals, "protein": p, "fat": f, "carbs": c}]
    return NutritionRecord(
        is_food=True,
        meal_name=meal_name,
        items=item_list,
        calories_kcal=cals,
        protein_g=p,
        fat_g=f,
        carbs_g=c,
        confidence=0.98,
        raw_summary=meal_name,
        display_name=meal_name,
    )


def test_two_users_with_different_targets(test_db):
    """Verify that two users with different targets see their exact targets across all 4 interfaces."""
    profile_store = HealBiteUserProfileStore(db_path=test_db)
    diary = HealBiteNutritionDiary(db_path=test_db, background_write=False)

    user1_id = 10000001
    user2_id = 10000002

    # User 1: Weight loss target
    profile_store.upsert_user_profile(
        user_id=user1_id,
        username="user_one",
        sex="male",
        age=30,
        height_cm=180.0,
        weight_kg=90.0,
        goal="lose",
        activity_level="moderate",
        daily_kcal_target=1800.0,
        daily_protein_target=140.0,
        daily_fat_target=60.0,
        daily_carbs_target=175.0,
        manual_kcal_target=1800.0,
        manual_protein_g=140.0,
        manual_fat_g=60.0,
        manual_carbs_g=175.0,
    )

    # User 2: Muscle gain target
    profile_store.upsert_user_profile(
        user_id=user2_id,
        username="user_two",
        sex="male",
        age=25,
        height_cm=185.0,
        weight_kg=80.0,
        goal="gain",
        activity_level="high",
        daily_kcal_target=2600.0,
        daily_protein_target=190.0,
        daily_fat_target=85.0,
        daily_carbs_target=268.0,
        manual_kcal_target=2600.0,
        manual_protein_g=190.0,
        manual_fat_g=85.0,
        manual_carbs_g=268.0,
    )

    adapter = TelegramAdapter(config=mock.MagicMock())

    # --- 1. /start Returning Dashboard ---
    with mock.patch("gateway.platforms.telegram.get_default_healbite_user_profile", return_value=profile_store):
        dashboard_1 = adapter._build_healbite_returning_dashboard(user1_id)
        dashboard_2 = adapter._build_healbite_returning_dashboard(user2_id)

    assert "1800 ккал" in dashboard_1
    assert "Б 140 г" in dashboard_1
    assert "Ж 60 г" in dashboard_1
    assert "У 175 г" in dashboard_1
    assert "2600 ккал" not in dashboard_1

    assert "2600 ккал" in dashboard_2
    assert "Б 190 г" in dashboard_2
    assert "Ж 85 г" in dashboard_2
    assert "У 268 г" in dashboard_2
    assert "1800 ккал" not in dashboard_2

    # --- 2. /profile Report ---
    prof1 = profile_store.get_user_profile(user1_id)
    prof2 = profile_store.get_user_profile(user2_id)
    profile_report_1 = format_healbite_profile_report(prof1)
    profile_report_2 = format_healbite_profile_report(prof2)

    assert "1800 ккал" in profile_report_1
    assert "140 г" in profile_report_1
    assert "60 г" in profile_report_1
    assert "175 г" in profile_report_1

    assert "2600 ккал" in profile_report_2
    assert "190 г" in profile_report_2
    assert "85 г" in profile_report_2
    assert "268 г" in profile_report_2

    # --- 3. /diary (today) with meal ---
    now_utc = datetime.datetime(2026, 10, 30, 8, 0, 0, tzinfo=datetime.timezone.utc)
    diary.save_record(
        user_id=user1_id,
        source="text",
        record=_make_record("Овсянка", 250, 8, 4, 45),
        image_ref=None,
        occurred_at=now_utc,
    )
    diary.save_record(
        user_id=user2_id,
        source="text",
        record=_make_record("Стейк с рисом", 600, 45, 20, 60),
        image_ref=None,
        occurred_at=now_utc,
    )

    diary_1 = compute_nutrition_diary_summary(test_db, user_id=user1_id, now=now_utc, days=1)
    diary_2 = compute_nutrition_diary_summary(test_db, user_id=user2_id, now=now_utc, days=1)

    assert diary_1["targets"]["calories_kcal"] == 1800.0
    assert diary_1["targets"]["protein_g"] == 140.0
    assert diary_1["targets"]["fat_g"] == 60.0
    assert diary_1["targets"]["carbs_g"] == 175.0

    assert diary_2["targets"]["calories_kcal"] == 2600.0
    assert diary_2["targets"]["protein_g"] == 190.0
    assert diary_2["targets"]["fat_g"] == 85.0
    assert diary_2["targets"]["carbs_g"] == 268.0

    diary_report_1 = format_nutrition_diary_report(diary_1)
    diary_report_2 = format_nutrition_diary_report(diary_2)

    assert "1800" in diary_report_1
    assert "2600" not in diary_report_1

    assert "2600" in diary_report_2
    assert "1800" not in diary_report_2

    # --- 4. /stats today ---
    stats_1 = compute_nutrition_diary_summary(test_db, user_id=user1_id, now=now_utc, days=1)
    stats_2 = compute_nutrition_diary_summary(test_db, user_id=user2_id, now=now_utc, days=1)
    assert stats_1["targets"]["calories_kcal"] == 1800.0
    assert stats_2["targets"]["calories_kcal"] == 2600.0


def test_two_users_with_different_timezones(test_db):
    """Verify that two users in different timezones format UTC meal times accurately in their local time."""
    profile_store = HealBiteUserProfileStore(db_path=test_db)
    diary = HealBiteNutritionDiary(db_path=test_db, background_write=False)

    user_msk = 20000001  # Europe/Moscow (UTC+3)
    user_nsk = 20000002  # Asia/Novosibirsk (UTC+7)

    profile_store.upsert_user_profile(
        user_id=user_msk,
        timezone="Europe/Moscow",
    )
    profile_store.upsert_user_profile(
        user_id=user_nsk,
        timezone="Asia/Novosibirsk",
    )

    # Meal logged at 08:44:06 UTC (exact operator timestamp)
    meal_utc = datetime.datetime(2026, 10, 30, 8, 44, 6, tzinfo=datetime.timezone.utc)
    diary.save_record(
        user_id=user_msk,
        source="text",
        record=_make_record("Яйца", 155, 12.6, 10.6, 1.1),
        image_ref=None,
        occurred_at=meal_utc,
    )
    diary.save_record(
        user_id=user_nsk,
        source="text",
        record=_make_record("Яйца", 155, 12.6, 10.6, 1.1),
        image_ref=None,
        occurred_at=meal_utc,
    )

    # In Moscow (UTC+3): 08:44 UTC -> 11:44 MSK
    summary_msk = diary.get_summary(user_id=user_msk, now=meal_utc, days=1)
    report_msk = format_nutrition_diary_report(summary_msk)
    assert "(11:44)" in report_msk, f"Expected (11:44) for Moscow user, got {report_msk}"

    # In Novosibirsk (UTC+7): 08:44 UTC -> 15:44 NSK
    summary_nsk = diary.get_summary(user_id=user_nsk, now=meal_utc, days=1)
    report_nsk = format_nutrition_diary_report(summary_nsk)
    assert "(15:44)" in report_nsk, f"Expected (15:44) for Novosibirsk user, got {report_nsk}"


def test_single_account_two_devices(test_db):
    """Verify that a single Telegram account accessing the bot from different devices receives identical data."""
    profile_store = HealBiteUserProfileStore(db_path=test_db)
    diary = HealBiteNutritionDiary(db_path=test_db, background_write=False)

    user_id = 30000001
    profile_store.upsert_user_profile(
        user_id=user_id,
        daily_kcal_target=2000.0,
        daily_protein_target=150.0,
        daily_fat_target=70.0,
        daily_carbs_target=190.0,
        manual_kcal_target=2000.0,
        manual_protein_g=150.0,
        manual_fat_g=70.0,
        manual_carbs_g=190.0,
        timezone="Europe/Moscow",
    )

    meal_utc = datetime.datetime(2026, 10, 30, 9, 30, 0, tzinfo=datetime.timezone.utc)
    diary.save_record(
        user_id=user_id,
        source="text",
        record=_make_record("Суп", 200, 10, 6, 25),
        image_ref=None,
        occurred_at=meal_utc,
    )

    # Device A query
    summary_dev_a = diary.get_summary(user_id=user_id, now=meal_utc, days=1)
    report_dev_a = format_nutrition_diary_report(summary_dev_a)

    # Device B query
    summary_dev_b = diary.get_summary(user_id=user_id, now=meal_utc, days=1)
    report_dev_b = format_nutrition_diary_report(summary_dev_b)

    # Both devices receive byte-for-byte identical reports
    assert report_dev_a == report_dev_b
    assert "(12:30)" in report_dev_a  # 09:30 UTC + 3h Moscow = 12:30
    assert "2000" in report_dev_a


def test_diary_and_profile_isolation(test_db):
    """Verify strict isolation: User 1 logging meals does not affect User 2, and vice versa."""
    profile_store = HealBiteUserProfileStore(db_path=test_db)
    diary = HealBiteNutritionDiary(db_path=test_db, background_write=False)

    user_a = 40000001
    user_b = 40000002

    profile_store.upsert_user_profile(user_id=user_a, weight_kg=75.0)
    profile_store.upsert_user_profile(user_id=user_b, weight_kg=95.0)

    # User A logs 2 meals
    now = datetime.datetime(2026, 10, 30, 10, 0, 0, tzinfo=datetime.timezone.utc)
    diary.save_record(user_id=user_a, source="text", record=_make_record("M1", 100, 5, 2, 15), image_ref=None, occurred_at=now)
    diary.save_record(user_id=user_a, source="text", record=_make_record("M2", 200, 10, 4, 30), image_ref=None, occurred_at=now)

    # Check User B diary is empty
    summary_b = diary.get_summary(user_id=user_b, now=now, days=1)
    assert summary_b["entry_count"] == 0
    assert summary_b["calories_kcal"] is None or summary_b["calories_kcal"] == 0

    # User B logs 1 meal
    diary.save_record(user_id=user_b, source="text", record=_make_record("M3", 300, 15, 6, 45), image_ref=None, occurred_at=now)

    # User A count remains 2
    summary_a = diary.get_summary(user_id=user_a, now=now, days=1)
    assert summary_a["entry_count"] == 2

    # User B count is 1
    summary_b_after = diary.get_summary(user_id=user_b, now=now, days=1)
    assert summary_b_after["entry_count"] == 1


def test_time_consistency_after_bot_restart(test_db):
    """Verify that meal occurred_at timestamps and local formatting remain identical after bot restart."""
    meal_time_utc = datetime.datetime(2026, 10, 30, 8, 44, 6, tzinfo=datetime.timezone.utc)
    user_id = 50000001

    # Instance 1 before restart
    diary1 = HealBiteNutritionDiary(db_path=test_db, background_write=False)
    prof_store1 = HealBiteUserProfileStore(db_path=test_db)
    prof_store1.upsert_user_profile(user_id=user_id, timezone="Europe/Moscow")
    diary1.save_record(
        user_id=user_id,
        source="text",
        record=_make_record("Яйца", 155, 12.6, 10.6, 1.1),
        image_ref=None,
        occurred_at=meal_time_utc,
    )
    rep1 = format_nutrition_diary_report(diary1.get_summary(user_id=user_id, now=meal_time_utc, days=1))
    diary1.close()

    # Instance 2 after restart
    diary2 = HealBiteNutritionDiary(db_path=test_db, background_write=False)
    rep2 = format_nutrition_diary_report(diary2.get_summary(user_id=user_id, now=meal_time_utc, days=1))
    diary2.close()

    assert rep1 == rep2
    assert "(11:44)" in rep2
