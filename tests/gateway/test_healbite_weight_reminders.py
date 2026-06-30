from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weight_reminders import (
    DEFAULT_MISSED_GRACE_HOURS,
    DEFAULT_SCAN_INTERVAL_SECONDS,
    ReminderDeliveryState,
    ReminderDeliveryStatus,
    ReminderScheduleAction,
    ReminderSchedulingError,
    HealBiteWeightReminderStore,
    WEIGHT_REMINDER_DELIVERIES_TABLE,
    WEIGHT_REMINDER_SETTINGS_TABLE,
    calculate_next_occurrence_utc,
    classify_missed_reminder,
    load_weight_reminder_config,
    make_delivery_key,
    normalize_local_time,
    validate_timezone,
    validate_weekday,
)
from gateway.healbite_weight_tracker import HealBiteWeightTracker, WEIGHT_ENTRIES_TABLE


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_schema_initializes_pristine_db_idempotently_without_rows(tmp_path):
    db_path = tmp_path / "healbite.db"

    HealBiteWeightReminderStore(db_path=db_path)
    HealBiteWeightReminderStore(db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert WEIGHT_REMINDER_SETTINGS_TABLE in _tables(conn)
        assert WEIGHT_REMINDER_DELIVERIES_TABLE in _tables(conn)
        indexes = _indexes(conn)
        assert "idx_weight_reminder_settings_due" in indexes
        assert "idx_weight_reminder_deliveries_status_claim" in indexes
        assert "idx_weight_reminder_deliveries_user_scheduled" in indexes
        assert _count(conn, WEIGHT_REMINDER_SETTINGS_TABLE) == 0
        assert _count(conn, WEIGHT_REMINDER_DELIVERIES_TABLE) == 0


def test_schema_initializes_production_shaped_db_without_touching_existing_counts(tmp_path):
    db_path = tmp_path / "healbite.db"
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    profile_store.upsert_user_profile(
        user_id=101,
        username="tester",
        sex="male",
        age=35,
        height_cm=180,
        weight_kg=80,
        goal="maintain",
        activity_level="moderate",
        manual_kcal_target=2000,
    )
    tracker = HealBiteWeightTracker(db_path=db_path)
    tracker.add_weight_entry(101, 80.0, source="test")

    with sqlite3.connect(db_path) as conn:
        before_weight = _count(conn, WEIGHT_ENTRIES_TABLE)
        before_users = _count(conn, "users")
        before_profiles = _count(conn, "profiles")

    HealBiteWeightReminderStore(db_path=db_path)
    HealBiteWeightReminderStore(db_path=db_path)

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert _count(conn, WEIGHT_ENTRIES_TABLE) == before_weight
        assert _count(conn, "users") == before_users
        assert _count(conn, "profiles") == before_profiles
        assert _count(conn, WEIGHT_REMINDER_SETTINGS_TABLE) == 0
        assert _count(conn, WEIGHT_REMINDER_DELIVERIES_TABLE) == 0


@pytest.mark.parametrize("timezone_name", ["UTC", "Europe/Berlin", "Asia/Almaty"])
def test_validate_timezone_accepts_iana_names(timezone_name):
    assert validate_timezone(timezone_name) == timezone_name


@pytest.mark.parametrize("timezone_name", ["", "Berlin", "Europe/NotAZone"])
def test_validate_timezone_rejects_invalid_names(timezone_name):
    with pytest.raises(ReminderSchedulingError):
        validate_timezone(timezone_name)


@pytest.mark.parametrize("weekday", [0, 3, 6])
def test_validate_weekday_accepts_monday_zero_convention(weekday):
    assert validate_weekday(weekday) == weekday


@pytest.mark.parametrize("weekday", [-1, 7, True])
def test_validate_weekday_rejects_invalid_values(weekday):
    with pytest.raises(ReminderSchedulingError):
        validate_weekday(weekday)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09:00", "09:00"),
        ("9:05", "09:05"),
        ("23:59", "23:59"),
    ],
)
def test_normalize_local_time_accepts_hh_mm_only(raw, expected):
    assert normalize_local_time(raw) == expected


@pytest.mark.parametrize("raw", ["24:00", "10:60", "10:30:00", "abc", ""])
def test_normalize_local_time_rejects_invalid_or_seconds(raw):
    with pytest.raises(ReminderSchedulingError):
        normalize_local_time(raw)


def test_calculate_next_occurrence_is_strictly_next_same_week_future():
    now = datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc)  # Monday

    occurrence = calculate_next_occurrence_utc(now, "UTC", 0, "09:00")

    assert occurrence.scheduled_utc == datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)
    assert occurrence.scheduled_utc > now
    assert occurrence.scheduled_local.weekday() == 0


def test_calculate_next_occurrence_rolls_to_next_week_when_equal():
    now = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)  # Monday

    occurrence = calculate_next_occurrence_utc(now, "UTC", 0, "09:00")

    assert occurrence.scheduled_utc == datetime(2026, 7, 6, 9, 0, tzinfo=timezone.utc)


def test_calculate_next_occurrence_rejects_naive_datetime():
    with pytest.raises(ReminderSchedulingError):
        calculate_next_occurrence_utc(datetime(2026, 6, 29, 9, 0), "UTC", 0, "09:00")


def test_calculate_next_occurrence_rejects_non_utc_aware_datetime():
    with pytest.raises(ReminderSchedulingError):
        calculate_next_occurrence_utc(
            datetime(2026, 6, 29, 11, 0, tzinfo=timezone(timedelta(hours=2))),
            "UTC",
            0,
            "09:00",
        )


def test_spring_forward_nonexistent_wall_time_shifts_to_first_valid_local_instant():
    now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)

    occurrence = calculate_next_occurrence_utc(now, "Europe/Berlin", 6, "02:30")

    assert occurrence.scheduled_local.strftime("%Y-%m-%d %H:%M") == "2026-03-29 03:00"
    assert occurrence.shifted_for_dst_gap is True


def test_fall_back_repeated_wall_time_uses_fold_zero_and_single_key():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)

    occurrence = calculate_next_occurrence_utc(now, "Europe/Berlin", 6, "02:30")
    key1 = make_delivery_key(user_id=101, occurrence=occurrence, schedule_version=1)
    key2 = make_delivery_key(user_id=101, occurrence=occurrence, schedule_version=1)

    assert occurrence.scheduled_local.strftime("%Y-%m-%d %H:%M") == "2026-10-25 02:30"
    assert occurrence.fold == 0
    assert key1 == key2


def test_missed_policy_distinguishes_wait_deliver_and_skip_boundaries():
    scheduled = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)

    wait = classify_missed_reminder(
        now_utc=scheduled - timedelta(minutes=1),
        scheduled_utc=scheduled,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
    )
    due = classify_missed_reminder(
        now_utc=scheduled,
        scheduled_utc=scheduled,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
    )
    boundary = classify_missed_reminder(
        now_utc=scheduled + timedelta(hours=DEFAULT_MISSED_GRACE_HOURS),
        scheduled_utc=scheduled,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
    )
    expired = classify_missed_reminder(
        now_utc=scheduled + timedelta(hours=DEFAULT_MISSED_GRACE_HOURS, seconds=1),
        scheduled_utc=scheduled,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
    )

    assert wait.action is ReminderScheduleAction.WAIT
    assert wait.occurrence is None
    assert due.action is ReminderScheduleAction.DELIVER
    assert boundary.action is ReminderScheduleAction.DELIVER
    assert expired.action is ReminderScheduleAction.SKIP_EXPIRED
    assert expired.skip_reason == "missed_window"
    assert expired.next_occurrence.scheduled_utc > expired.occurrence.scheduled_utc


def test_delivery_key_is_deterministic_and_uses_identity_timezone_and_version():
    occurrence = calculate_next_occurrence_utc(
        datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc),
        "Europe/Berlin",
        0,
        "09:00",
    )
    same = make_delivery_key(user_id=101, occurrence=occurrence, schedule_version=1)

    assert len(same) == 64
    assert same == make_delivery_key(user_id=101, occurrence=occurrence, schedule_version=1)
    assert same != make_delivery_key(user_id=202, occurrence=occurrence, schedule_version=1)
    assert same != make_delivery_key(user_id=101, occurrence=occurrence, schedule_version=2)
    assert same != make_delivery_key(
        user_id=101,
        occurrence=calculate_next_occurrence_utc(
            datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc),
            "UTC",
            0,
            "09:00",
        ),
        schedule_version=1,
    )


def test_settings_repository_requires_explicit_opt_in_and_isolates_users(tmp_path):
    store = HealBiteWeightReminderStore(db_path=tmp_path / "healbite.db")
    now = datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc)

    assert store.get_settings(101) is None
    setting = store.create_or_update_settings(
        user_id=101,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
        enabled=True,
        now_utc=now,
    )

    assert setting.enabled is True
    assert setting.delivery_state is ReminderDeliveryState.ACTIVE
    assert setting.schedule_version == 1
    assert setting.next_due_at_utc == "2026-06-29 09:00:00"
    assert store.get_settings(202) is None


def test_schedule_version_increments_only_when_schedule_changes(tmp_path):
    store = HealBiteWeightReminderStore(db_path=tmp_path / "healbite.db")
    now = datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc)
    store.create_or_update_settings(
        user_id=101,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
        enabled=True,
        now_utc=now,
    )

    same = store.create_or_update_settings(
        user_id=101,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
        enabled=False,
        now_utc=now + timedelta(minutes=1),
    )
    changed = store.create_or_update_settings(
        user_id=101,
        timezone_name="UTC",
        weekday=1,
        local_time="09:00",
        enabled=True,
        now_utc=now + timedelta(minutes=2),
    )

    assert same.schedule_version == 1
    assert same.enabled is False
    assert changed.schedule_version == 2


def test_disable_suspend_and_resume_contracts(tmp_path):
    store = HealBiteWeightReminderStore(db_path=tmp_path / "healbite.db")
    now = datetime(2026, 6, 29, 7, 0, tzinfo=timezone.utc)
    store.create_or_update_settings(
        user_id=101,
        timezone_name="UTC",
        weekday=0,
        local_time="09:00",
        enabled=True,
        now_utc=now,
    )

    suspended = store.suspend_delivery(101, safe_reason="blocked_user", now_utc=now)
    unsafe = store.suspend_delivery(101, safe_reason="blocked user PII_SHOULD_NOT_SURVIVE", now_utc=now)
    disabled = store.disable_settings(101, now_utc=now)
    resumed = store.resume_delivery(101, now_utc=now)

    assert suspended is not None
    assert suspended.enabled is True
    assert suspended.delivery_state is ReminderDeliveryState.SUSPENDED
    assert suspended.suspension_reason == "blocked_user"
    assert unsafe is not None
    assert unsafe.suspension_reason == "unknown"
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.next_due_at_utc is None
    assert resumed is not None
    assert resumed.delivery_state is ReminderDeliveryState.ACTIVE
    assert resumed.suspended_at_utc is None
    assert resumed.suspension_reason is None
    assert resumed.next_due_at_utc is not None


def test_outbox_insert_is_idempotent_and_constraints_are_enforced(tmp_path):
    store = HealBiteWeightReminderStore(db_path=tmp_path / "healbite.db")
    scheduled = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)

    first = store.insert_delivery_if_absent(
        user_id=101,
        scheduled_for_utc=scheduled,
        delivery_key="abc123",
    )
    second = store.insert_delivery_if_absent(
        user_id=101,
        scheduled_for_utc=scheduled,
        delivery_key="abc123",
    )

    assert first.id == second.id
    assert first.status is ReminderDeliveryStatus.PENDING
    assert len(store.list_deliveries(101)) == 1
    with sqlite3.connect(tmp_path / "healbite.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {WEIGHT_REMINDER_DELIVERIES_TABLE}
                    (user_id, scheduled_for_utc, delivery_key, status, attempt_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (101, "2026-06-29 09:00:00", "bad-status", "not_a_state", 0, "2026-06-29 00:00:00", "2026-06-29 00:00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {WEIGHT_REMINDER_DELIVERIES_TABLE}
                    (user_id, scheduled_for_utc, delivery_key, status, attempt_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (101, "2026-06-29 09:00:00", "bad-attempt", "pending", -1, "2026-06-29 00:00:00", "2026-06-29 00:00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {WEIGHT_REMINDER_SETTINGS_TABLE}
                    (user_id, enabled, timezone, weekday, local_time, schedule_version,
                     delivery_state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (303, 1, "UTC", 0, "24:00", 1, "active", "2026-06-29 00:00:00", "2026-06-29 00:00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {WEIGHT_REMINDER_SETTINGS_TABLE}
                    (user_id, enabled, timezone, weekday, local_time, schedule_version,
                     delivery_state, suspension_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    404,
                    1,
                    "UTC",
                    0,
                    "09:00",
                    1,
                    "suspended",
                    "raw_user_text_should_not_store",
                    "2026-06-29 00:00:00",
                    "2026-06-29 00:00:00",
                ),
            )


def test_feature_config_defaults_are_safe_and_allowlist_is_parsed_without_enabling():
    default = load_weight_reminder_config({})
    configured = load_weight_reminder_config(
        {
            "WEIGHT_REMINDERS_ENABLED": "true",
            "WEIGHT_REMINDERS_ALLOWLIST": "101, 202, nope",
            "WEIGHT_REMINDER_SCAN_INTERVAL_SECONDS": "15",
            "WEIGHT_REMINDER_MISSED_GRACE_HOURS": "6",
        }
    )
    invalid = load_weight_reminder_config(
        {
            "WEIGHT_REMINDERS_ENABLED": "definitely",
            "WEIGHT_REMINDER_SCAN_INTERVAL_SECONDS": "-1",
            "WEIGHT_REMINDER_MISSED_GRACE_HOURS": "nope",
        }
    )

    assert default.enabled is False
    assert default.allowlist == frozenset()
    assert default.scan_interval_seconds == DEFAULT_SCAN_INTERVAL_SECONDS
    assert default.missed_grace_hours == DEFAULT_MISSED_GRACE_HOURS
    assert configured.enabled is True
    assert configured.allowlist == frozenset({101, 202})
    assert configured.scan_interval_seconds == 15
    assert configured.missed_grace_hours == 6
    assert invalid.enabled is False
    assert invalid.scan_interval_seconds == DEFAULT_SCAN_INTERVAL_SECONDS
    assert invalid.missed_grace_hours == DEFAULT_MISSED_GRACE_HOURS
