from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from gateway.healbite_water_tracker import (
    HealBiteWaterTracker,
    WATER_INTAKE_TABLE,
    WATER_TARGET_MISSING_HINT,
    format_water_tracker_report,
    parse_water_amount,
)


def _seed_water_target(db_path, *, user_id: int, target_ml: int = 2200) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE profiles (
                telegram_id INTEGER PRIMARY KEY,
                water_target_ml INTEGER
            )
            """
        )
        conn.execute(
            "INSERT INTO profiles(telegram_id, water_target_ml) VALUES (?, ?)",
            (user_id, target_ml),
        )
        conn.commit()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("300", 300),
        ("300 мл", 300),
        ("0.5 л", 500),
        ("0,5 л", 500),
    ],
)
def test_parse_water_amount_accepts_supported_forms(raw, expected):
    assert parse_water_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "-1", "4000", "стакан", "abc", "1 литр воды"])
def test_parse_water_amount_rejects_invalid_or_ambiguous_values(raw):
    assert parse_water_amount(raw) is None


def test_water_tracker_schema_add_sum_idempotency_and_persistence(tmp_path):
    db_path = tmp_path / "healbite.db"
    _seed_water_target(db_path, user_id=101, target_ml=2200)
    tracker = HealBiteWaterTracker(db_path=db_path)

    first = tracker.add_water_intake(101, 250, idempotency_key="callback-1")
    duplicate = tracker.add_water_intake(101, 250, idempotency_key="callback-1")
    tracker.add_water_intake(101, 500, idempotency_key="callback-2")
    tracker.add_water_intake(202, 1000, idempotency_key="other-user")

    summary = tracker.get_water_summary(101)
    reloaded = HealBiteWaterTracker(db_path=db_path)

    assert first.added is True
    assert duplicate.duplicate is True
    assert summary.consumed_ml == 750
    assert summary.target_ml == 2200
    assert summary.remaining_ml == 1450
    assert summary.progress_percent == 34
    assert reloaded.get_water_intake_today(101) == 750
    assert reloaded.get_water_intake_today(202) == 1000
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (WATER_INTAKE_TABLE,),
        ).fetchone()[0] == 1


def test_water_tracker_today_window_and_undo_are_user_scoped(tmp_path):
    db_path = tmp_path / "healbite.db"
    tracker = HealBiteWaterTracker(db_path=db_path)
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)

    tracker.add_water_intake(101, 250, consumed_at=now - timedelta(days=1), idempotency_key="old")
    tracker.add_water_intake(101, 500, consumed_at=now, idempotency_key="today-1")
    tracker.add_water_intake(101, 300, consumed_at=now + timedelta(minutes=5), idempotency_key="today-2")
    tracker.add_water_intake(202, 700, consumed_at=now + timedelta(minutes=10), idempotency_key="other")

    deleted = tracker.undo_last_water_intake_today(101, now=now)

    assert deleted.deleted is True
    assert deleted.entry is not None
    assert deleted.entry.amount_ml == 300
    assert tracker.get_water_intake_today(101, now=now) == 500
    assert tracker.get_water_intake_today(202, now=now) == 700


def test_water_tracker_missing_target_formats_soft_hint(tmp_path):
    tracker = HealBiteWaterTracker(db_path=tmp_path / "healbite.db")
    tracker.add_water_intake(101, 250)

    report = format_water_tracker_report(tracker.get_water_summary(101))

    assert WATER_TARGET_MISSING_HINT in report
    assert "250 мл" in report


def test_water_tracker_custom_pending_state_expires_and_clears(tmp_path):
    tracker = HealBiteWaterTracker(db_path=tmp_path / "healbite.db")
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)

    tracker.stage_custom_amount(101, now=now)
    assert tracker.get_pending_state(101, now=now + timedelta(minutes=1)) == "water_custom_amount"
    assert tracker.get_pending_state(101, now=now + timedelta(minutes=11)) is None

    tracker.stage_custom_amount(101, now=now)
    tracker.clear_pending_state(101)
    assert tracker.get_pending_state(101, now=now) is None
