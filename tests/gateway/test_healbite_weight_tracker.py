from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weight_tracker import (
    HealBiteWeightTracker,
    WEIGHT_CUSTOM_STATE,
    WEIGHT_ENTRIES_TABLE,
    WeightAddResult,
    WeightEntry,
    format_weight_saved_notice,
    format_weight_tracker_report,
    parse_weight_kg,
)


def _complete_profile(store: HealBiteUserProfileStore, *, user_id: int = 101) -> None:
    store.upsert_user_profile(
        user_id=user_id,
        username="tester",
        sex="male",
        age=35,
        height_cm=180,
        weight_kg=80,
        goal="maintain",
        activity_level="moderate",
        manual_kcal_target=2000,
    )
    store.recalculate_profile_targets(user_id=user_id, target_source="manual", manual_kcal_target=2000)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("82", 82.0),
        ("82.4", 82.4),
        ("82,4 кг", 82.4),
        ("82.45 kg", 82.5),
    ],
)
def test_parse_weight_kg_accepts_supported_forms(raw, expected):
    assert parse_weight_kg(raw) == expected


@pytest.mark.parametrize("raw", ["", "0", "34.9", "301", "82 кг утром", "abc"])
def test_parse_weight_kg_rejects_invalid_or_ambiguous_values(raw):
    assert parse_weight_kg(raw) is None


def test_weight_tracker_appends_history_and_isolates_users(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(profile_store, user_id=101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: profile_store)
    tracker = HealBiteWeightTracker(db_path=db_path)
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    first = tracker.add_weight_entry(101, 80.0, recorded_at=now - timedelta(days=6), source="test")
    second = tracker.add_weight_entry(101, 82.4, recorded_at=now, source="test")
    tracker.add_weight_entry(202, 70.0, recorded_at=now, source="test")
    summary = tracker.get_summary(101, now=now)

    assert first.entry.weight_grams == 80000
    assert second.entry.weight_grams == 82400
    assert summary.latest is not None
    assert summary.latest.weight_grams == 82400
    assert summary.delta_7d_grams == 2400
    assert tracker.get_summary(202, now=now).latest.weight_grams == 70000
    with tracker._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (WEIGHT_ENTRIES_TABLE,),
        ).fetchone()[0] == 1


def test_weight_entry_updates_profile_and_recalculates_macro_targets(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(profile_store, user_id=101)
    before = profile_store.get_user_profile(101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: profile_store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    result = tracker.add_weight_entry(101, 82.4, source="test")
    after = profile_store.get_user_profile(101)

    assert result.profile_updated is True
    assert result.targets_recalculated is True
    assert after.weight_kg == 82.4
    assert after.daily_kcal_target == 2000
    assert after.daily_protein_g != before.daily_protein_g
    assert after.daily_protein_g == 132


def test_incomplete_profile_weight_update_does_not_force_macro_recalculation(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    profile_store.upsert_user_profile(user_id=101, username="tester")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: profile_store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    result = tracker.add_weight_entry(101, 82.4, source="test")
    profile = profile_store.get_user_profile(101)

    assert result.profile_updated is True
    assert result.targets_recalculated is False
    assert profile.weight_kg == 82.4
    assert profile.daily_kcal_target is None


def test_weight_saved_notice_distinguishes_profile_gap_from_recalculation_error():
    now = "2026-06-29 00:00:00"
    entry = WeightEntry(id=1, user_id=101, recorded_at_utc=now, local_date="2026-06-29", weight_grams=82400, created_at=now)

    assert format_weight_saved_notice(WeightAddResult(entry, True, True, False)) == "Вес записан. КБЖУ пересчитаны."
    assert format_weight_saved_notice(WeightAddResult(entry, True, False, False)) == "Вес записан. Для пересчёта КБЖУ заполните /profile."
    assert format_weight_saved_notice(WeightAddResult(entry, True, False, True)) == "Вес записан. Пересчитать КБЖУ сейчас не удалось."


def test_weight_report_and_pending_state(tmp_path):
    tracker = HealBiteWeightTracker(db_path=tmp_path / "healbite.db")
    tracker.add_weight_entry(101, 82.4)

    report = format_weight_tracker_report(tracker.get_summary(101))
    tracker.stage_custom_weight(101)

    assert "Вес" in report
    assert "82,4 кг" in report
    assert tracker.get_pending_state(101) == WEIGHT_CUSTOM_STATE
    tracker.clear_pending_state(101)
    assert tracker.get_pending_state(101) is None
