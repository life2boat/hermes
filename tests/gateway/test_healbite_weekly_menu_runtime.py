from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from gateway.healbite_feature_gates import FeatureAvailabilityStatus, FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_runtime_resources import borrowed_runtime_resource
from gateway.healbite_weekly_menu_runtime import (
    HealBiteWeeklyMenuRuntimeService,
    WeeklyMenuRuntimeCleanupError,
    WeeklyMenuRuntimeNotFoundError,
    WeeklyMenuRuntimeStateError,
    WeeklyMenuRuntimeUnavailableError,
)
from gateway.healbite_weekly_menu_schema import WeeklyMenuSchemaState
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuAccessError,
    WeeklyMenuEntryInput,
    WeeklyMenuMealSlot,
    WeeklyMenuNotFoundError,
    WeeklyMenuRevisionView,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS users (
                {identity_column} INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"INSERT OR IGNORE INTO users ({identity_column}, username) VALUES (?, ?)",
            (int(user_id), f"user-{user_id}"),
        )


class _CountingHouseholdStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return borrowed_runtime_resource(HealBiteHouseholdStore(db_path=self.db_path, ensure_schema_on_init=False))


class _CountingWeeklyStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return borrowed_runtime_resource(HealBiteWeeklyMenuStore(db_path=self.db_path))


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
        weekly_menu_store_factory=lambda: borrowed_runtime_resource(_SpyWeeklyMenuStore(db_path=db_path)),
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

    with pytest.raises(WeeklyMenuRuntimeNotFoundError) as excinfo:
        runtime.get_weekly_menu_revision(202, published.revision.id)

    assert type(excinfo.value).__name__ == "WeeklyMenuRuntimeNotFoundError"


@dataclass
class _WeeklyResourceStats:
    factory_calls: int = 0
    entered_count: int = 0
    exited_count: int = 0
    opened_count: int = 0
    closed_count: int = 0
    rollback_count: int = 0
    active_count: int = 0
    double_close_count: int = 0
    operation_calls: int = 0
    schema_state_calls: int = 0
    in_transaction_before_cleanup: bool = False
    in_transaction_after_cleanup: bool = False


class _WeeklyOwnedStore:
    def __init__(
        self,
        stats: _WeeklyResourceStats,
        *,
        schema_state: WeeklyMenuSchemaState = WeeklyMenuSchemaState.CANONICAL,
        series=None,
        revisions=(),
        revision_view: WeeklyMenuRevisionView | None = None,
        missing_week: bool = False,
        revision_not_found: bool = False,
        operation_error: Exception | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._stats = stats
        self._schema_state = schema_state
        self._series = series
        self._revisions = revisions
        self._revision_view = revision_view
        self._missing_week = missing_week
        self._revision_not_found = revision_not_found
        self._operation_error = operation_error
        self._connection = connection

    def schema_state(self):
        self._stats.schema_state_calls += 1
        return self._schema_state

    def get_weekly_menu_series(self, _context, _household_id: str, _week_start: str):
        self._stats.operation_calls += 1
        if self._operation_error is not None:
            raise self._operation_error
        if self._missing_week:
            return None
        return self._series

    def list_weekly_menu_revisions(self, _context, _series_id: str):
        self._stats.operation_calls += 1
        if self._operation_error is not None:
            raise self._operation_error
        return self._revisions

    def get_weekly_menu_revision(self, _context, _revision_id: str):
        self._stats.operation_calls += 1
        if self._operation_error is not None:
            raise self._operation_error
        if self._revision_not_found:
            raise WeeklyMenuNotFoundError("hidden")
        assert self._revision_view is not None
        return self._revision_view

    def rollback_owned(self) -> None:
        if self._connection is not None and self._connection.in_transaction:
            self._stats.in_transaction_before_cleanup = True
            self._connection.rollback()
            self._stats.rollback_count += 1
            self._stats.in_transaction_after_cleanup = self._connection.in_transaction

    def close_owned(self) -> None:
        if self._connection is not None:
            self._connection.close()


class _WeeklyResourceLease:
    def __init__(
        self,
        store: _WeeklyOwnedStore,
        stats: _WeeklyResourceStats,
        *,
        owned: bool,
        close_error: Exception | None = None,
    ) -> None:
        self._store = store
        self._stats = stats
        self._owned = owned
        self._close_error = close_error
        self._closed = False
        self.cleanup_error: Exception | None = None
        self._stats.opened_count += 1
        self._stats.active_count += 1

    def __enter__(self):
        self._stats.entered_count += 1
        return self._store

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._stats.exited_count += 1
        if not self._owned:
            return False
        if self._closed:
            self._stats.double_close_count += 1
            return False
        self._closed = True
        self._store.rollback_owned()
        self._store.close_owned()
        self._stats.closed_count += 1
        self._stats.active_count -= 1
        if self._close_error is not None:
            self.cleanup_error = self._close_error
        return False


class _WeeklyResourceFactory:
    def __init__(self, builder, stats: _WeeklyResourceStats, *, owned: bool, close_error: Exception | None = None) -> None:
        self._builder = builder
        self._stats = stats
        self._owned = owned
        self._close_error = close_error

    def __call__(self):
        self._stats.factory_calls += 1
        return _WeeklyResourceLease(
            self._builder(),
            self._stats,
            owned=self._owned,
            close_error=self._close_error,
        )


def _published_weekly_artifacts(db_path: Path):
    weekly_store, context = _seed_runtime(db_path)
    series = weekly_store.create_or_get_weekly_menu_series(context, context.household_id, "2026-07-06")
    draft = weekly_store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key="draft-lifecycle",
    )
    ready = weekly_store.replace_draft_entries(
        context,
        draft.revision.id,
        _sample_entries(),
        expected_revision_version=draft.revision.version,
        idempotency_key="replace-lifecycle",
    )
    published = weekly_store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="publish-lifecycle",
    )
    revision_view = weekly_store.get_weekly_menu_revision(context, published.revision.id)
    revisions = weekly_store.list_weekly_menu_revisions(context, series.id)
    return series, revisions, revision_view


def test_owned_weekly_resource_closes_after_successful_read(tmp_path):
    db_path = tmp_path / "runtime.db"
    series, revisions, _revision_view = _published_weekly_artifacts(db_path)
    stats = _WeeklyResourceStats()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(
            lambda: _WeeklyOwnedStore(stats, series=series, revisions=revisions),
            stats,
            owned=True,
        ),
    )

    week = runtime.get_weekly_menu_for_week(101, "2026-07-06")

    assert week is not None
    assert stats.factory_calls == 1
    assert stats.entered_count == 1
    assert stats.exited_count == 1
    assert stats.closed_count == 1
    assert stats.active_count == 0
    assert stats.double_close_count == 0


def test_owned_weekly_resource_closes_after_missing_week(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    stats = _WeeklyResourceStats()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(
            lambda: _WeeklyOwnedStore(stats, missing_week=True),
            stats,
            owned=True,
        ),
    )

    assert runtime.get_weekly_menu_for_week(101, "2026-07-06") is None
    assert stats.closed_count == 1
    assert stats.active_count == 0


def test_owned_weekly_resource_closes_after_schema_unavailable(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    stats = _WeeklyResourceStats()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(
            lambda: _WeeklyOwnedStore(stats, schema_state=WeeklyMenuSchemaState.PARTIAL),
            stats,
            owned=True,
        ),
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE
    assert stats.schema_state_calls == 1
    assert stats.closed_count == 1
    assert stats.operation_calls == 0


def test_weekly_runtime_maps_unexpected_store_error_and_closes_owned_resource(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    stats = _WeeklyResourceStats()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(
            lambda: _WeeklyOwnedStore(stats, operation_error=RuntimeError("boom")),
            stats,
            owned=True,
        ),
    )

    with pytest.raises(WeeklyMenuRuntimeStateError):
        runtime.get_weekly_menu_for_week(101, "2026-07-06")

    assert stats.closed_count == 1
    assert stats.active_count == 0


def test_weekly_runtime_cleanup_failure_after_success_is_typed(tmp_path):
    db_path = tmp_path / "runtime.db"
    series, revisions, _revision_view = _published_weekly_artifacts(db_path)
    stats = _WeeklyResourceStats()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(
            lambda: _WeeklyOwnedStore(stats, series=series, revisions=revisions),
            stats,
            owned=True,
            close_error=RuntimeError("synthetic close failure"),
        ),
    )

    with pytest.raises(WeeklyMenuRuntimeCleanupError):
        runtime.get_weekly_menu_for_week(101, "2026-07-06")

    assert stats.closed_count == 1


def test_weekly_runtime_primary_error_wins_over_cleanup_failure(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    stats = _WeeklyResourceStats()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(
            lambda: _WeeklyOwnedStore(stats, operation_error=WeeklyMenuAccessError("denied")),
            stats,
            owned=True,
            close_error=RuntimeError("synthetic close failure"),
        ),
    )

    with pytest.raises(WeeklyMenuRuntimeStateError):
        runtime.get_weekly_menu_for_week(101, "2026-07-06")

    assert stats.closed_count == 1


def test_borrowed_weekly_resource_is_not_closed_and_remains_usable(tmp_path):
    db_path = tmp_path / "runtime.db"
    _series, _revisions, revision_view = _published_weekly_artifacts(db_path)
    stats = _WeeklyResourceStats()
    borrowed_store = _WeeklyOwnedStore(stats, revision_view=revision_view)
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(lambda: borrowed_store, stats, owned=False),
    )

    revision = runtime.get_weekly_menu_revision(101, revision_view.revision.id)

    assert revision.revision.id == revision_view.revision.id
    assert stats.closed_count == 0
    assert borrowed_store.get_weekly_menu_revision(None, revision_view.revision.id).revision.id == revision_view.revision.id


def test_weekly_runtime_owned_resource_rolls_back_and_releases_sqlite_lock(tmp_path):
    db_path = tmp_path / "runtime.db"
    series, revisions, _revision_view = _published_weekly_artifacts(db_path)
    lock_conn = sqlite3.connect(db_path, timeout=0.5, check_same_thread=False)
    lock_conn.execute("BEGIN IMMEDIATE")
    stats = _WeeklyResourceStats()
    runtime = HealBiteWeeklyMenuRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        weekly_menu_store_factory=_WeeklyResourceFactory(
            lambda: _WeeklyOwnedStore(stats, series=series, revisions=revisions, connection=lock_conn),
            stats,
            owned=True,
        ),
    )

    week = runtime.get_weekly_menu_for_week(101, "2026-07-06")

    assert week is not None
    assert stats.in_transaction_before_cleanup is True
    assert stats.rollback_count == 1
    assert stats.in_transaction_after_cleanup is False
    second = sqlite3.connect(db_path, timeout=0.5, check_same_thread=False)
    try:
        second.execute("BEGIN EXCLUSIVE")
        second.rollback()
    finally:
        second.close()
