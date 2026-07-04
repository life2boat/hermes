from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway.healbite_feature_gates import FeatureAvailabilityStatus, FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_weekly_menu_runtime import (
    HealBiteWeeklyMenuRuntimeService,
    WeeklyMenuRuntimeNotFoundError,
    WeeklyMenuRuntimeUnavailableError,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore, WeeklyMenuEntryInput, WeeklyMenuMealSlot


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE users (
                {identity_column} INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO users ({identity_column}, username) VALUES (?, ?)",
            (int(user_id), f"user-{user_id}"),
        )


class _CountingHouseholdStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self) -> HealBiteHouseholdStore:
        self.calls += 1
        return HealBiteHouseholdStore(db_path=self.db_path, ensure_schema_on_init=False)


class _CountingWeeklyStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self) -> HealBiteWeeklyMenuStore:
        self.calls += 1
        return HealBiteWeeklyMenuStore(db_path=self.db_path)


def _seed_runtime(db_path: Path, actor_user_id: int = 101) -> tuple[HealBiteWeeklyMenuStore, object]:
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(actor_user_id)
    context = household_store.resolve_actor_context(actor_user_id)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    return weekly_store, context


def _sample_entries() -> list[WeeklyMenuEntryInput]:
    return [
        WeeklyMenuEntryInput(local_date="2026-07-06", meal_slot=WeeklyMenuMealSlot.BREAKFAST, position=1, title="A"),
        WeeklyMenuEntryInput(local_date="2026-07-07", meal_slot=WeeklyMenuMealSlot.DINNER, position=1, title="B"),
    ]


def test_disabled_feature_does_not_open_household_or_weekly_menu_store(tmp_path):
    db_path = tmp_path / "runtime.db"
    household_factory = _CountingHouseholdStoreFactory(db_path)
    weekly_factory = _CountingWeeklyStoreFactory(db_path)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=False, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        weekly_menu_store_factory=weekly_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.DISABLED
    assert household_factory.calls == 0
    assert weekly_factory.calls == 0
    assert not db_path.exists()


def test_misconfigured_feature_fails_closed_without_any_store_open(tmp_path):
    db_path = tmp_path / "runtime.db"
    household_factory = _CountingHouseholdStoreFactory(db_path)
    weekly_factory = _CountingWeeklyStoreFactory(db_path)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=False, allowlist=frozenset(), configuration_valid=False),
        db_path=db_path,
        household_store_factory=household_factory,
        weekly_menu_store_factory=weekly_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.MISCONFIGURED
    assert household_factory.calls == 0
    assert weekly_factory.calls == 0


def test_non_allowlisted_actor_does_not_open_household_or_weekly_menu_store(tmp_path):
    db_path = tmp_path / "runtime.db"
    household_factory = _CountingHouseholdStoreFactory(db_path)
    weekly_factory = _CountingWeeklyStoreFactory(db_path)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({202}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        weekly_menu_store_factory=weekly_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.NOT_ALLOWLISTED
    assert household_factory.calls == 0
    assert weekly_factory.calls == 0


def test_allowlisted_actor_with_missing_household_never_opens_weekly_menu_store(tmp_path):
    db_path = tmp_path / "runtime.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    household_factory = _CountingHouseholdStoreFactory(db_path)
    weekly_factory = _CountingWeeklyStoreFactory(db_path)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        weekly_menu_store_factory=weekly_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE
    assert household_factory.calls == 1
    assert weekly_factory.calls == 0


def test_allowlisted_actor_with_missing_weekly_menu_schema_reports_schema_unavailable(tmp_path):
    db_path = tmp_path / "runtime.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(101)
    household_factory = _CountingHouseholdStoreFactory(db_path)
    weekly_factory = _CountingWeeklyStoreFactory(db_path)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        weekly_menu_store_factory=weekly_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE
    assert household_factory.calls == 1
    assert weekly_factory.calls == 1


def test_runtime_reads_week_and_revisions_when_feature_ready(tmp_path):
    db_path = tmp_path / "runtime.db"
    weekly_store, context = _seed_runtime(db_path)
    series = weekly_store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = weekly_store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key="draft-1",
    )
    ready = weekly_store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )
    published = weekly_store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="publish-1",
    )
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    availability = runtime.get_availability(101)
    week = runtime.get_weekly_menu_for_week(101, "2026-07-06")
    revisions = runtime.list_weekly_menu_revisions(101, "2026-07-06")
    revision = runtime.get_weekly_menu_revision(101, published.revision.id)

    assert availability.ready is True
    assert week is not None
    assert week.series.id == series.id
    assert len(week.revisions) == 1
    assert len(revisions) == 1
    assert revision.revision.id == published.revision.id
    assert len(revision.entries) == 2


def test_runtime_uses_existing_household_identity_column_variants(tmp_path):
    db_path = tmp_path / "runtime.db"
    _create_users_table(db_path, identity_column="telegram_id")
    _insert_user(db_path, 101, identity_column="telegram_id")
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(101)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    availability = runtime.get_availability(101)

    assert availability.ready is True


def test_get_weekly_menu_revision_maps_not_found_to_runtime_error(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    with pytest.raises(WeeklyMenuRuntimeNotFoundError):
        runtime.get_weekly_menu_revision(101, "33333333-3333-4333-8333-333333333333")


def test_disallowed_runtime_call_raises_unavailable_error(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({202}), configuration_valid=True),
        db_path=db_path,
    )

    with pytest.raises(WeeklyMenuRuntimeUnavailableError) as excinfo:
        runtime.get_weekly_menu_for_week(101, "2026-07-06")

    assert excinfo.value.availability.status is FeatureAvailabilityStatus.NOT_ALLOWLISTED



class _SpyWeeklyMenuStore(HealBiteWeeklyMenuStore):
    connect_calls = 0
    read_only_connect_calls = 0

    def _connect(self):
        type(self).connect_calls += 1
        return super()._connect()

    def _read_only_connect(self):
        type(self).read_only_connect_calls += 1
        return super()._read_only_connect()


def _table_count(db_path: Path, table: str) -> int:
    with _connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _install_abort_trigger(db_path: Path, table: str) -> None:
    with _connect(db_path) as conn:
        for op in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(
                f"CREATE TRIGGER trg_{table}_{op.lower()}_blocked BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'writes_blocked'); END"
            )


def test_allowed_actor_opens_weekly_store_only_after_gate_success(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    _SpyWeeklyMenuStore.connect_calls = 0
    _SpyWeeklyMenuStore.read_only_connect_calls = 0
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=lambda: _SpyWeeklyMenuStore(db_path=db_path),
    )

    availability = runtime.get_availability(101)

    assert availability.ready is True
    assert _SpyWeeklyMenuStore.connect_calls == 1
    assert _SpyWeeklyMenuStore.read_only_connect_calls == 0


def test_weekly_runtime_reads_do_not_mutate_rows(tmp_path):
    db_path = tmp_path / "runtime.db"
    weekly_store, context = _seed_runtime(db_path)
    series = weekly_store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = weekly_store.create_draft_revision(context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    ready = weekly_store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-1",
    )
    published = weekly_store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="publish-1",
    )
    before = {
        "series": _table_count(db_path, "household_weekly_menu_series"),
        "revisions": _table_count(db_path, "household_weekly_menus"),
        "entries": _table_count(db_path, "household_weekly_menu_entries"),
        "idempotency": _table_count(db_path, "household_weekly_menu_idempotency"),
    }
    for table in (
        "household_weekly_menu_series",
        "household_weekly_menus",
        "household_weekly_menu_entries",
        "household_weekly_menu_idempotency",
    ):
        _install_abort_trigger(db_path, table)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    week = runtime.get_weekly_menu_for_week(101, "2026-07-06")
    revisions = runtime.list_weekly_menu_revisions(101, "2026-07-06")
    revision = runtime.get_weekly_menu_revision(101, published.revision.id)
    after = {
        "series": _table_count(db_path, "household_weekly_menu_series"),
        "revisions": _table_count(db_path, "household_weekly_menus"),
        "entries": _table_count(db_path, "household_weekly_menu_entries"),
        "idempotency": _table_count(db_path, "household_weekly_menu_idempotency"),
    }

    assert week is not None
    assert len(revisions) == 1
    assert revision.revision.id == published.revision.id
    assert before == after


def test_cross_household_revision_access_does_not_leak_existence(tmp_path):
    db_path = tmp_path / "runtime.db"
    weekly_store, owner_context = _seed_runtime(db_path, actor_user_id=101)
    _insert_user(db_path, 202)
    second_store = HealBiteHouseholdStore(db_path=db_path)
    second_store.get_or_create_personal_household(202)
    series = weekly_store.create_or_get_weekly_menu_series(owner_context, owner_context.household_id, "2026-07-06")
    draft = weekly_store.create_draft_revision(owner_context, series.id, expected_series_version=series.version, idempotency_key="draft-1")
    ready = weekly_store.replace_draft_entries(owner_context, draft.revision.id, _sample_entries(), expected_revision_version=draft.revision.version, idempotency_key="replace-1")
    published = weekly_store.publish_weekly_menu_revision(owner_context, ready.revision.id, expected_series_version=ready.series.version, expected_revision_version=ready.revision.version, idempotency_key="publish-1")
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101, 202}), configuration_valid=True),
        db_path=db_path,
    )

    with pytest.raises(Exception) as excinfo:
        runtime.get_weekly_menu_revision(202, published.revision.id)

    assert type(excinfo.value).__name__ == 'WeeklyMenuRuntimeStateError'
