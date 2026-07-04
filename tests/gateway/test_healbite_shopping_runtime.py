from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from gateway.healbite_feature_gates import FeatureAvailabilityStatus, FeatureGateConfig
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_runtime_resources import borrowed_runtime_resource
from gateway.healbite_shopping import (
    HealBiteShoppingStore,
    ManualShoppingItemInput,
    ShoppingAccessError,
    ShoppingListView,
    ShoppingNotFoundError,
)
from gateway.healbite_shopping_runtime import (
    HealBiteShoppingRuntimeService,
    ShoppingListFilters,
    ShoppingRuntimeCleanupError,
    ShoppingRuntimeNotFoundError,
    ShoppingRuntimeStateError,
    ShoppingRuntimeUnavailableError,
)
from gateway.healbite_shopping_schema import ShoppingSchemaState
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


class _CountingShoppingStoreFactory:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return borrowed_runtime_resource(HealBiteShoppingStore(db_path=self.db_path))


def _seed_runtime(db_path: Path, actor_user_id: int = 101):
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    personal = household_store.get_or_create_personal_household(actor_user_id)
    context = household_store.resolve_actor_context(actor_user_id)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    return personal, context, weekly_store, shopping_store


def _publish_menu_revision(db_path: Path):
    personal, context, weekly_store, _shopping_store = _seed_runtime(db_path)
    series = weekly_store.create_or_get_weekly_menu_series(context, personal.household.id, "2026-07-06")
    draft = weekly_store.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key="menu-draft-1",
    )
    ready = weekly_store.replace_draft_entries(
        context,
        draft.revision.id,
        [
            WeeklyMenuEntryInput(local_date="2026-07-06", meal_slot=WeeklyMenuMealSlot.LUNCH, position=1, title="A"),
            WeeklyMenuEntryInput(local_date="2026-07-07", meal_slot=WeeklyMenuMealSlot.DINNER, position=1, title="B"),
        ],
        expected_revision_version=draft.revision.version,
        idempotency_key="menu-replace-1",
    )
    published = weekly_store.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key="menu-publish-1",
    )
    return personal, context, published


def test_disabled_feature_does_not_open_household_or_shopping_store(tmp_path):
    db_path = tmp_path / "runtime.db"
    household_factory = _CountingHouseholdStoreFactory(db_path)
    shopping_factory = _CountingShoppingStoreFactory(db_path)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=False, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        shopping_store_factory=shopping_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.DISABLED
    assert household_factory.calls == 0
    assert shopping_factory.calls == 0
    assert not db_path.exists()


def test_actor_denied_never_opens_household_or_shopping_store(tmp_path):
    db_path = tmp_path / "runtime.db"
    household_factory = _CountingHouseholdStoreFactory(db_path)
    shopping_factory = _CountingShoppingStoreFactory(db_path)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({202}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        shopping_store_factory=shopping_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.NOT_ALLOWLISTED
    assert household_factory.calls == 0
    assert shopping_factory.calls == 0


def test_household_unavailable_never_opens_shopping_store(tmp_path):
    db_path = tmp_path / "runtime.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    household_factory = _CountingHouseholdStoreFactory(db_path)
    shopping_factory = _CountingShoppingStoreFactory(db_path)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        shopping_store_factory=shopping_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.HOUSEHOLD_UNAVAILABLE
    assert household_factory.calls == 1
    assert shopping_factory.calls == 0


def test_schema_unavailable_is_fail_closed_after_household_resolution(tmp_path):
    db_path = tmp_path / "runtime.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(101)
    household_factory = _CountingHouseholdStoreFactory(db_path)
    shopping_factory = _CountingShoppingStoreFactory(db_path)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        household_store_factory=household_factory,
        shopping_store_factory=shopping_factory,
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE
    assert household_factory.calls == 1
    assert shopping_factory.calls == 1


def test_runtime_reads_shopping_lists_and_items_when_feature_ready(tmp_path):
    db_path = tmp_path / "runtime.db"
    personal, context, _published = _publish_menu_revision(db_path)
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    created = shopping_store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    with_item = shopping_store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Tomatoes", quantity_value="2", quantity_unit_normalized="piece"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="manual-1",
    )
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    availability = runtime.get_availability(101)
    lists = runtime.list_shopping_lists(101, ShoppingListFilters(week_start="2026-07-06"))
    view = runtime.get_shopping_list(101, with_item.shopping_list.id)
    items = runtime.list_shopping_items(101, with_item.shopping_list.id)

    assert availability.ready is True
    assert len(lists) == 1
    assert view.shopping_list.id == with_item.shopping_list.id
    assert len(items) == 1


def test_runtime_uses_existing_household_identity_column_variants(tmp_path):
    db_path = tmp_path / "runtime.db"
    _create_users_table(db_path, identity_column="telegram_id")
    _insert_user(db_path, 101, identity_column="telegram_id")
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(101)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly_store.initialize_schema()
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    shopping_store.initialize_schema()
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    availability = runtime.get_availability(101)

    assert availability.ready is True


def test_get_shopping_list_maps_not_found_to_runtime_error(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    with pytest.raises(ShoppingRuntimeNotFoundError):
        runtime.get_shopping_list(101, "33333333-3333-4333-8333-333333333333")


def test_disallowed_runtime_call_raises_unavailable_error(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({202}), configuration_valid=True),
        db_path=db_path,
    )

    with pytest.raises(ShoppingRuntimeUnavailableError) as excinfo:
        runtime.list_shopping_lists(101)

    assert excinfo.value.availability.status is FeatureAvailabilityStatus.NOT_ALLOWLISTED



class _SpyShoppingStore(HealBiteShoppingStore):
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


def test_allowed_actor_opens_shopping_store_only_after_gate_success(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    _SpyShoppingStore.connect_calls = 0
    _SpyShoppingStore.read_only_connect_calls = 0
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        shopping_store_factory=lambda: borrowed_runtime_resource(_SpyShoppingStore(db_path=db_path)),
    )

    availability = runtime.get_availability(101)

    assert availability.ready is True
    assert _SpyShoppingStore.connect_calls == 1
    assert _SpyShoppingStore.read_only_connect_calls == 0


def test_shopping_runtime_reads_do_not_mutate_rows(tmp_path):
    db_path = tmp_path / "runtime.db"
    personal, context, _published = _publish_menu_revision(db_path)
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    created = shopping_store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    with_item = shopping_store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Tomatoes", quantity_value="2", quantity_unit_normalized="piece"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="manual-1",
    )
    before = {
        "lists": _table_count(db_path, "household_shopping_lists"),
        "items": _table_count(db_path, "household_shopping_items"),
        "idempotency": _table_count(db_path, "household_shopping_idempotency"),
    }
    for table in (
        "household_shopping_lists",
        "household_shopping_items",
        "household_shopping_idempotency",
    ):
        _install_abort_trigger(db_path, table)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
    )

    lists = runtime.list_shopping_lists(101, ShoppingListFilters(week_start="2026-07-06"))
    view = runtime.get_shopping_list(101, with_item.shopping_list.id)
    items = runtime.list_shopping_items(101, with_item.shopping_list.id)
    after = {
        "lists": _table_count(db_path, "household_shopping_lists"),
        "items": _table_count(db_path, "household_shopping_items"),
        "idempotency": _table_count(db_path, "household_shopping_idempotency"),
    }

    assert len(lists) == 1
    assert view.shopping_list.id == with_item.shopping_list.id
    assert len(items) == 1
    assert before == after


def test_cross_household_shopping_access_does_not_leak_existence(tmp_path):
    db_path = tmp_path / "runtime.db"
    personal, context, _published = _publish_menu_revision(db_path)
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    created = shopping_store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-1",
    )
    _insert_user(db_path, 202)
    second_store = HealBiteHouseholdStore(db_path=db_path)
    second_store.get_or_create_personal_household(202)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101, 202}), configuration_valid=True),
        db_path=db_path,
    )

    with pytest.raises(ShoppingRuntimeNotFoundError) as excinfo:
        runtime.get_shopping_list(202, created.shopping_list.id)

    assert type(excinfo.value).__name__ == "ShoppingRuntimeNotFoundError"


@dataclass
class _ShoppingResourceStats:
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


class _OwnedShoppingStore:
    def __init__(
        self,
        stats: _ShoppingResourceStats,
        *,
        schema_state: ShoppingSchemaState = ShoppingSchemaState.CANONICAL,
        shopping_view: ShoppingListView | None = None,
        shopping_lists=(),
        operation_error: Exception | None = None,
        list_not_found: bool = False,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._stats = stats
        self._schema_state = schema_state
        self._shopping_view = shopping_view
        self._shopping_lists = shopping_lists
        self._operation_error = operation_error
        self._list_not_found = list_not_found
        self._connection = connection

    def schema_state(self):
        self._stats.schema_state_calls += 1
        return self._schema_state

    def get_shopping_list(self, _context, _shopping_list_id: str):
        self._stats.operation_calls += 1
        if self._operation_error is not None:
            raise self._operation_error
        if self._list_not_found:
            raise ShoppingNotFoundError("hidden")
        assert self._shopping_view is not None
        return self._shopping_view

    def list_shopping_lists(self, _context, _household_id: str, *, week_start: str | None = None):
        self._stats.operation_calls += 1
        if self._operation_error is not None:
            raise self._operation_error
        return self._shopping_lists

    def rollback_owned(self) -> None:
        if self._connection is not None and self._connection.in_transaction:
            self._stats.in_transaction_before_cleanup = True
            self._connection.rollback()
            self._stats.rollback_count += 1
            self._stats.in_transaction_after_cleanup = self._connection.in_transaction

    def close_owned(self) -> None:
        if self._connection is not None:
            self._connection.close()


class _ShoppingResourceLease:
    def __init__(
        self,
        store: _OwnedShoppingStore,
        stats: _ShoppingResourceStats,
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


class _ShoppingResourceFactory:
    def __init__(self, builder, stats: _ShoppingResourceStats, *, owned: bool, close_error: Exception | None = None) -> None:
        self._builder = builder
        self._stats = stats
        self._owned = owned
        self._close_error = close_error

    def __call__(self):
        self._stats.factory_calls += 1
        return _ShoppingResourceLease(
            self._builder(),
            self._stats,
            owned=self._owned,
            close_error=self._close_error,
        )


def _shopping_view_artifact(db_path: Path):
    personal, context, _published = _publish_menu_revision(db_path)
    shopping_store = HealBiteShoppingStore(db_path=db_path)
    created = shopping_store.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="list-lifecycle",
    )
    with_item = shopping_store.add_manual_item(
        context,
        created.shopping_list.id,
        ManualShoppingItemInput(display_name="Tomatoes", quantity_value="2", quantity_unit_normalized="piece"),
        expected_list_version=created.shopping_list.version,
        idempotency_key="manual-lifecycle",
    )
    return shopping_store.get_shopping_list(context, with_item.shopping_list.id)


def test_owned_shopping_resource_closes_after_successful_read(tmp_path):
    db_path = tmp_path / "runtime.db"
    shopping_view = _shopping_view_artifact(db_path)
    stats = _ShoppingResourceStats()
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        shopping_store_factory=_ShoppingResourceFactory(
            lambda: _OwnedShoppingStore(stats, shopping_view=shopping_view, shopping_lists=(shopping_view.shopping_list,)),
            stats,
            owned=True,
        ),
    )

    view = runtime.get_shopping_list(101, shopping_view.shopping_list.id)

    assert view.shopping_list.id == shopping_view.shopping_list.id
    assert stats.entered_count == 1
    assert stats.exited_count == 1
    assert stats.closed_count == 1
    assert stats.active_count == 0


def test_owned_shopping_resource_closes_after_schema_unavailable(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    stats = _ShoppingResourceStats()
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        shopping_store_factory=_ShoppingResourceFactory(
            lambda: _OwnedShoppingStore(stats, schema_state=ShoppingSchemaState.PARTIAL),
            stats,
            owned=True,
        ),
    )

    availability = runtime.get_availability(101)

    assert availability.status is FeatureAvailabilityStatus.SCHEMA_UNAVAILABLE
    assert stats.closed_count == 1
    assert stats.operation_calls == 0


def test_shopping_runtime_maps_unexpected_store_error_and_closes_owned_resource(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_runtime(db_path)
    stats = _ShoppingResourceStats()
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        shopping_store_factory=_ShoppingResourceFactory(
            lambda: _OwnedShoppingStore(stats, operation_error=RuntimeError("boom")),
            stats,
            owned=True,
        ),
    )

    with pytest.raises(ShoppingRuntimeStateError):
        runtime.list_shopping_lists(101)

    assert stats.closed_count == 1


def test_shopping_runtime_cleanup_failure_after_success_is_typed(tmp_path):
    db_path = tmp_path / "runtime.db"
    shopping_view = _shopping_view_artifact(db_path)
    stats = _ShoppingResourceStats()
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        shopping_store_factory=_ShoppingResourceFactory(
            lambda: _OwnedShoppingStore(stats, shopping_view=shopping_view, shopping_lists=(shopping_view.shopping_list,)),
            stats,
            owned=True,
            close_error=RuntimeError("synthetic close failure"),
        ),
    )

    with pytest.raises(ShoppingRuntimeCleanupError):
        runtime.get_shopping_list(101, shopping_view.shopping_list.id)

    assert stats.closed_count == 1


def test_borrowed_shopping_resource_is_not_closed_and_remains_usable(tmp_path):
    db_path = tmp_path / "runtime.db"
    shopping_view = _shopping_view_artifact(db_path)
    stats = _ShoppingResourceStats()
    borrowed_store = _OwnedShoppingStore(stats, shopping_view=shopping_view, shopping_lists=(shopping_view.shopping_list,))
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        shopping_store_factory=_ShoppingResourceFactory(lambda: borrowed_store, stats, owned=False),
    )

    view = runtime.get_shopping_list(101, shopping_view.shopping_list.id)

    assert view.shopping_list.id == shopping_view.shopping_list.id
    assert stats.closed_count == 0
    assert borrowed_store.get_shopping_list(None, shopping_view.shopping_list.id).shopping_list.id == shopping_view.shopping_list.id


def test_shopping_runtime_owned_resource_rolls_back_and_releases_sqlite_lock(tmp_path):
    db_path = tmp_path / "runtime.db"
    shopping_view = _shopping_view_artifact(db_path)
    lock_conn = sqlite3.connect(db_path, timeout=0.5, check_same_thread=False)
    lock_conn.execute("BEGIN IMMEDIATE")
    stats = _ShoppingResourceStats()
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(enabled=True, allowlist=frozenset({101}), configuration_valid=True),
        db_path=db_path,
        shopping_store_factory=_ShoppingResourceFactory(
            lambda: _OwnedShoppingStore(
                stats,
                shopping_view=shopping_view,
                shopping_lists=(shopping_view.shopping_list,),
                connection=lock_conn,
            ),
            stats,
            owned=True,
        ),
    )

    view = runtime.get_shopping_list(101, shopping_view.shopping_list.id)

    assert view.shopping_list.id == shopping_view.shopping_list.id
    assert stats.in_transaction_before_cleanup is True
    assert stats.rollback_count == 1
    assert stats.in_transaction_after_cleanup is False
    second = sqlite3.connect(db_path, timeout=0.5, check_same_thread=False)
    try:
        second.execute("BEGIN EXCLUSIVE")
        second.rollback()
    finally:
        second.close()
