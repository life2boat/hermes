from __future__ import annotations

from datetime import datetime, timezone

from gateway.healbite_weight_tracker import HealBiteWeightTracker


def test_weekly_weight_reminder_due_and_dedupes_by_local_date(tmp_path):
    tracker = HealBiteWeightTracker(db_path=tmp_path / "healbite.db")
    now = datetime(2026, 6, 29, 9, 5, tzinfo=timezone.utc)  # Monday

    setting = tracker.set_weekly_reminder(101, enabled=True, weekday=0, time_local="09:00", timezone_name="UTC")
    due = tracker.due_weekly_reminders(now=now)
    tracker.mark_reminder_sent(101, local_date="2026-06-29")

    assert setting.enabled is True
    assert [item.user_id for item in due] == [101]
    assert tracker.due_weekly_reminders(now=now) == []


def test_weekly_weight_reminder_disabled_is_not_due(tmp_path):
    tracker = HealBiteWeightTracker(db_path=tmp_path / "healbite.db")
    tracker.set_weekly_reminder(101, enabled=False, weekday=0, time_local="09:00", timezone_name="UTC")

    assert tracker.due_weekly_reminders(now=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc)) == []
