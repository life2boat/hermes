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
    profile = profile_store.get_user_profile(101)

    assert first.entry.weight_grams == 80000
    assert second.entry.weight_grams == 82400
    assert summary.latest is not None
    assert summary.latest.weight_grams == 82400
    assert summary.delta_7d_grams == 2400
    assert tracker.get_summary(202, now=now).latest.weight_grams == 70000
    assert profile is not None and profile.weight_kg == 82.4
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

    assert result.weight_saved is True
    assert result.profile_updated is True
    assert result.recalculation_attempted is True
    assert result.targets_recalculated is True
    assert result.targets_changed is True
    assert after is not None
    assert after.weight_kg == 82.4
    assert after.daily_kcal_target == 2000
    assert before is not None and after.daily_protein_g != before.daily_protein_g
    assert after.daily_protein_g == 132


def test_incomplete_profile_weight_update_does_not_force_macro_recalculation(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    profile_store.upsert_user_profile(user_id=101, username="tester")
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: profile_store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    result = tracker.add_weight_entry(101, 82.4, source="test")
    profile = profile_store.get_user_profile(101)

    assert result.weight_saved is True
    assert result.profile_updated is True
    assert result.recalculation_attempted is False
    assert result.targets_recalculated is False
    assert result.profile_incomplete is True
    assert profile is not None and profile.weight_kg == 82.4
    assert profile.daily_kcal_target is None


def test_weight_saved_notice_distinguishes_all_outcomes():
    now = "2026-06-29 00:00:00"
    entry = WeightEntry(id=1, user_id=101, recorded_at_utc=now, local_date="2026-06-29", weight_grams=82400, created_at=now)

    assert format_weight_saved_notice(WeightAddResult(entry, True, True, True, True, True, False, False)) == "Вес записан. КБЖУ пересчитаны."
    assert format_weight_saved_notice(WeightAddResult(entry, True, True, True, True, False, False, False)) == "Вес записан. Нормы КБЖУ не изменились."
    assert format_weight_saved_notice(WeightAddResult(entry, True, True, False, False, False, True, False)) == "Вес записан. Для пересчёта КБЖУ заполните /profile."
    assert format_weight_saved_notice(WeightAddResult(entry, True, True, True, False, False, False, True)) == "Вес сохранён, но пересчитать нормы сейчас не удалось."


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


def test_history_insert_failure_does_not_update_profile_or_targets(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(store, user_id=101)
    before = store.get_user_profile(101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("PII_S70C_WEIGHT_TRANSACTION")

    monkeypatch.setattr(tracker, "_insert_weight_entry", _boom)

    with caplog.at_level("INFO", logger="gateway.healbite_weight_tracker"):
        with pytest.raises(RuntimeError):
            tracker.add_weight_entry(101, 82.4, source="test")

    after = store.get_user_profile(101)
    with tracker._connect() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM {WEIGHT_ENTRIES_TABLE} WHERE user_id = ?", (101,)).fetchone()[0]

    assert count == 0
    assert before is not None and after is not None
    assert after.weight_kg == before.weight_kg
    assert after.daily_kcal_target == before.daily_kcal_target
    assert "PII_S70C_WEIGHT_TRANSACTION" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_profile_update_failure_rolls_back_history_and_preserves_previous_weight(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(store, user_id=101)
    before = store.get_user_profile(101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("PII_S70C_PROFILE_UPDATE")

    monkeypatch.setattr(tracker, "_update_canonical_profile_weight", _boom)

    with caplog.at_level("INFO", logger="gateway.healbite_weight_tracker"):
        with pytest.raises(RuntimeError):
            tracker.add_weight_entry(101, 82.4, source="test")

    after = store.get_user_profile(101)
    with tracker._connect() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM {WEIGHT_ENTRIES_TABLE} WHERE user_id = ?", (101,)).fetchone()[0]

    assert count == 0
    assert before is not None and after is not None
    assert after.weight_kg == before.weight_kg
    assert "PII_S70C_PROFILE_UPDATE" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_commit_failure_leaves_no_partial_history_or_profile_state(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(store, user_id=101)
    before = store.get_user_profile(101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("PII_S70C_WEIGHT_TRANSACTION")

    monkeypatch.setattr(tracker, "_commit_weight_write", _boom)

    with caplog.at_level("INFO", logger="gateway.healbite_weight_tracker"):
        with pytest.raises(RuntimeError):
            tracker.add_weight_entry(101, 82.4, source="test")

    after = store.get_user_profile(101)
    with tracker._connect() as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM {WEIGHT_ENTRIES_TABLE} WHERE user_id = ?", (101,)).fetchone()[0]

    assert count == 0
    assert before is not None and after is not None
    assert after.weight_kg == before.weight_kg
    assert "PII_S70C_WEIGHT_TRANSACTION" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_calculator_exception_keeps_saved_weight_and_old_targets(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(store, user_id=101)
    before = store.get_user_profile(101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("PII_S70C_CALCULATOR_EXCEPTION")

    monkeypatch.setattr("gateway.healbite_user_profile.calculate_nutrition_targets", _boom)

    with caplog.at_level("INFO", logger="gateway.healbite_weight_tracker"):
        result = tracker.add_weight_entry(101, 82.4, source="test")

    after = store.get_user_profile(101)
    assert before is not None and after is not None
    assert result.weight_saved is True
    assert result.profile_updated is True
    assert result.recalculation_attempted is True
    assert result.recalculation_failed is True
    assert after.weight_kg == 82.4
    assert (after.daily_kcal_target, after.daily_protein_g, after.daily_fat_g, after.daily_carbs_g) == (
        before.daily_kcal_target,
        before.daily_protein_g,
        before.daily_fat_g,
        before.daily_carbs_g,
    )
    assert format_weight_saved_notice(result) == "Вес сохранён, но пересчитать нормы сейчас не удалось."
    assert "PII_S70C_CALCULATOR_EXCEPTION" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_target_update_failure_keeps_old_targets_fully_intact(tmp_path, monkeypatch, caplog):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(store, user_id=101)
    before = store.get_user_profile(101)
    tracker = HealBiteWeightTracker(db_path=db_path)
    original_upsert = HealBiteUserProfileStore.upsert_user_profile

    def _patched_upsert(self, **kwargs):
        if kwargs.get("daily_kcal_target") is not None:
            raise RuntimeError("PII_S70C_TARGET_UPDATE")
        return original_upsert(self, **kwargs)

    monkeypatch.setattr(HealBiteUserProfileStore, "upsert_user_profile", _patched_upsert)

    with caplog.at_level("INFO", logger="gateway.healbite_weight_tracker"):
        result = tracker.add_weight_entry(101, 82.4, source="test")

    after = store.get_user_profile(101)
    assert before is not None and after is not None
    assert result.weight_saved is True
    assert result.recalculation_failed is True
    assert after.weight_kg == 82.4
    assert (after.daily_kcal_target, after.daily_protein_g, after.daily_fat_g, after.daily_carbs_g) == (
        before.daily_kcal_target,
        before.daily_protein_g,
        before.daily_fat_g,
        before.daily_carbs_g,
    )
    assert "PII_S70C_TARGET_UPDATE" not in caplog.text
    assert "error_type=RuntimeError" in caplog.text


def test_unchanged_rounded_targets_report_as_unchanged(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(store, user_id=101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    tracker = HealBiteWeightTracker(db_path=db_path)

    result = tracker.add_weight_entry(101, 80.0, source="test")

    assert result.recalculation_attempted is True
    assert result.recalculation_completed is True
    assert result.targets_changed is False
    assert format_weight_saved_notice(result) == "Вес записан. Нормы КБЖУ не изменились."


def test_multiple_entries_same_day_remain_append_only_and_profile_tracks_latest(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    store = HealBiteUserProfileStore(db_path=db_path)
    _complete_profile(store, user_id=101)
    monkeypatch.setattr("gateway.healbite_user_profile.get_default_healbite_user_profile", lambda: store)
    tracker = HealBiteWeightTracker(db_path=db_path)
    now = datetime(2026, 6, 28, 12, 0, tzinfo=timezone.utc)

    tracker.add_weight_entry(101, 80.0, recorded_at=now, source="test")
    tracker.add_weight_entry(101, 81.0, recorded_at=now + timedelta(hours=2), source="test")
    tracker.add_weight_entry(101, 82.0, recorded_at=now + timedelta(hours=3), source="test")

    summary = tracker.get_summary(101, now=now + timedelta(hours=4))
    profile = store.get_user_profile(101)

    assert [entry.weight_grams for entry in summary.entries] == [80000, 81000, 82000]
    assert summary.latest is not None and summary.latest.weight_grams == 82000
    assert profile is not None and profile.weight_kg == 82.0
