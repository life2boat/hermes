from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import pytest

from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_feature_gates import FeatureGateConfig
from gateway.healbite_shopping import (
    HealBiteShoppingStore,
    ManualShoppingItemInput,
    ShoppingAccessError,
    ShoppingConflictError,
    ShoppingItemOrigin,
    ShoppingItemOverrideState,
    ShoppingNotFoundError,
    ShoppingStateError,
    ShoppingValidationError,
    _derived_base_unit,
    _rounded_quantity,
)
from gateway.healbite_shopping_runtime import HealBiteShoppingRuntimeService
from gateway.healbite_shopping_schema import (
    SHOPPING_CONTRIBUTIONS_TABLE,
    SHOPPING_IDEMPOTENCY_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
)
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_TABLE,
    WEEKLY_MENU_INGREDIENTS_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_SERIES_TABLE,
)
from gateway.healbite_weekly_menus import (
    HealBiteWeeklyMenuStore,
    WeeklyMenuEntryInput,
    WeeklyMenuIngredientInput,
    WeeklyMenuMealSlot,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_actor(db_path: Path, actor_user_id: int = 101):
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
            (actor_user_id, "actor"),
        )
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(actor_user_id)
    context = households.resolve_actor_context(actor_user_id)
    weekly = HealBiteWeeklyMenuStore(db_path=db_path)
    weekly.initialize_schema()
    shopping = HealBiteShoppingStore(db_path=db_path)
    shopping.initialize_schema()
    return households, weekly, shopping, personal, context


def _ingredient(
    name: str,
    quantity: str,
    unit: str,
    *,
    base_servings: str,
    position: int,
) -> WeeklyMenuIngredientInput:
    return WeeklyMenuIngredientInput(
        position=position,
        display_name=name,
        quantity_value=quantity,
        quantity_unit=unit,
        recipe_base_servings=base_servings,
    )


def _entries(*, flour_quantity: str = "0.5") -> list[WeeklyMenuEntryInput]:
    return [
        WeeklyMenuEntryInput(
            local_date="2026-07-06",
            meal_slot=WeeklyMenuMealSlot.LUNCH,
            position=1,
            title="Первое блюдо",
            servings="4",
            ingredients=(
                _ingredient(" Мука ", flour_quantity, "kg", base_servings="2", position=1),
                _ingredient("Молоко", "0.25", "l", base_servings="2", position=2),
                _ingredient("Яйцо", "2", "piece", base_servings="2", position=3),
            ),
        ),
        WeeklyMenuEntryInput(
            local_date="2026-07-07",
            meal_slot=WeeklyMenuMealSlot.DINNER,
            position=1,
            title="Второе блюдо",
            servings="2",
            ingredients=(
                _ingredient("мука", "250", "g", base_servings="1", position=1),
                _ingredient("Молоко", "100", "ml", base_servings="1", position=2),
                _ingredient("Яйцо", "50", "g", base_servings="1", position=3),
            ),
        ),
    ]


def _publish(weekly, context, household_id: str, entries, *, suffix: str):
    series = weekly.create_or_get_weekly_menu_series(context, household_id, "2026-07-06")
    draft = weekly.create_draft_revision(
        context,
        series.id,
        expected_series_version=series.version,
        idempotency_key=f"draft-{suffix}",
    )
    ready = weekly.replace_draft_entries(
        context,
        draft.revision.id,
        entries,
        expected_revision_version=draft.revision.version,
        idempotency_key=f"replace-{suffix}",
    )
    return weekly.publish_weekly_menu_revision(
        context,
        ready.revision.id,
        expected_series_version=ready.series.version,
        expected_revision_version=ready.revision.version,
        idempotency_key=f"publish-{suffix}",
    )


def _seed_published(db_path: Path):
    households, weekly, shopping, personal, context = _seed_actor(db_path)
    published = _publish(weekly, context, personal.household.id, _entries(), suffix="one")
    return households, weekly, shopping, personal, context, published


def _table_snapshot(conn: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
    order = "id" if "id" in columns else columns[0]
    return tuple(tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"))


def test_derivation_scales_normalizes_and_aggregates_without_mutating_menu(tmp_path):
    db_path = tmp_path / "derive.db"
    _households, _weekly, shopping, _personal, context, published = _seed_published(db_path)
    weekly_tables = (
        WEEKLY_MENU_SERIES_TABLE,
        WEEKLY_MENU_REVISIONS_TABLE,
        WEEKLY_MENU_ENTRIES_TABLE,
        WEEKLY_MENU_INGREDIENTS_TABLE,
        WEEKLY_MENU_IDEMPOTENCY_TABLE,
    )
    with _connect(db_path) as conn:
        before = {table: _table_snapshot(conn, table) for table in weekly_tables}

    result = shopping.generate_shopping_list_from_weekly_menu(
        context,
        "2026-07-06",
        expected_list_version=None,
        idempotency_key="derive-1",
    )

    assert result.shopping_list.source_menu_id == published.revision.id
    assert result.shopping_list.source_menu_revision == published.revision.revision_number
    assert result.shopping_list.status.value == "active"
    actual = {(item.normalized_name, item.quantity_unit_normalized.value): item.quantity_value for item in result.items}
    assert actual == {
        ("молоко", "ml"): "700",
        ("мука", "g"): "1500",
        ("яйцо", "g"): "100",
        ("яйцо", "piece"): "4",
    }
    assert [item.position for item in result.items] == list(range(1, len(result.items) + 1))
    with _connect(db_path) as conn:
        after = {table: _table_snapshot(conn, table) for table in weekly_tables}
        assert before == after
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_CONTRIBUTIONS_TABLE}").fetchone()[0] == 6


def test_runtime_exposes_actor_scoped_derivation_api(tmp_path):
    db_path = tmp_path / "runtime.db"
    _households, _weekly, _shopping, _personal, _context, _published = _seed_published(db_path)
    runtime = HealBiteShoppingRuntimeService(
        config=FeatureGateConfig(
            enabled=True,
            allowlist=frozenset({101}),
            configuration_valid=True,
        ),
        db_path=db_path,
    )

    result = runtime.generate_shopping_list_from_weekly_menu(
        101,
        "2026-07-06",
        "runtime-derive",
        None,
    )

    assert result.shopping_list.household_id
    assert len(result.items) == 4


def test_additive_schema_migrations_preserve_existing_rows(tmp_path):
    db_path = tmp_path / "migration.db"
    _households, _weekly, _shopping, _personal, _context, _published = _seed_published(db_path)
    with _connect(db_path) as conn:
        weekly_entry_count = conn.execute(
            f"SELECT COUNT(*) FROM {WEEKLY_MENU_ENTRIES_TABLE}"
        ).fetchone()[0]
        conn.execute(f"DROP TABLE {SHOPPING_CONTRIBUTIONS_TABLE}")
    shopping = HealBiteShoppingStore(db_path=db_path)
    shopping.initialize_schema()
    with _connect(db_path) as conn:
        assert conn.execute(
            f"SELECT COUNT(*) FROM {WEEKLY_MENU_ENTRIES_TABLE}"
        ).fetchone()[0] == weekly_entry_count
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
            (SHOPPING_CONTRIBUTIONS_TABLE,),
        ).fetchone()[0] == 1

    second_db = tmp_path / "weekly-migration.db"
    _households, weekly, _shopping, _personal, _context = _seed_actor(second_db)
    with _connect(second_db) as conn:
        conn.execute(f"DROP TABLE {SHOPPING_CONTRIBUTIONS_TABLE}")
        conn.execute(f"DROP TABLE {SHOPPING_ITEMS_TABLE}")
        conn.execute(f"DROP TABLE {SHOPPING_IDEMPOTENCY_TABLE}")
        conn.execute(f"DROP TABLE {SHOPPING_LISTS_TABLE}")
        conn.execute(f"DROP TABLE {WEEKLY_MENU_INGREDIENTS_TABLE}")
    weekly.initialize_schema()
    with _connect(second_db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?",
            (WEEKLY_MENU_INGREDIENTS_TABLE,),
        ).fetchone()[0] == 1


def test_derivation_fails_closed_for_incomplete_structured_ingredients(tmp_path):
    db_path = tmp_path / "incomplete.db"
    _households, weekly, shopping, personal, context = _seed_actor(db_path)
    entries = _entries()
    entries[1] = WeeklyMenuEntryInput(
        local_date="2026-07-07",
        meal_slot=WeeklyMenuMealSlot.DINNER,
        position=1,
        title="Без ингредиентов",
        servings="2",
    )
    _publish(weekly, context, personal.household.id, entries, suffix="incomplete")

    with pytest.raises(ShoppingStateError):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=None,
            idempotency_key="derive-incomplete",
        )
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_LISTS_TABLE}").fetchone()[0] == 0
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_ITEMS_TABLE}").fetchone()[0] == 0
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_IDEMPOTENCY_TABLE}").fetchone()[0] == 0


def test_missing_published_menu_does_not_touch_existing_shopping_state(tmp_path):
    db_path = tmp_path / "missing-menu.db"
    _households, _weekly, shopping, personal, context = _seed_actor(db_path)
    existing = shopping.create_shopping_list(
        context,
        personal.household.id,
        week_start="2026-07-06",
        idempotency_key="existing-list",
    )
    existing = shopping.add_manual_item(
        context,
        existing.shopping_list.id,
        ManualShoppingItemInput(display_name="Сохранить", quantity_value="1", quantity_unit_normalized="piece"),
        expected_list_version=existing.shopping_list.version,
        idempotency_key="existing-manual",
    )
    with _connect(db_path) as conn:
        before = {
            table: _table_snapshot(conn, table)
            for table in (SHOPPING_LISTS_TABLE, SHOPPING_ITEMS_TABLE, SHOPPING_IDEMPOTENCY_TABLE)
        }

    with pytest.raises(ShoppingNotFoundError):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=existing.shopping_list.version,
            idempotency_key="missing-source",
        )
    with _connect(db_path) as conn:
        after = {
            table: _table_snapshot(conn, table)
            for table in (SHOPPING_LISTS_TABLE, SHOPPING_ITEMS_TABLE, SHOPPING_IDEMPOTENCY_TABLE)
        }
    assert before == after


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (Decimal("0.0005"), "0.001"),
        (Decimal("0.5"), "0.5"),
        (Decimal("1"), "1"),
        (Decimal("12.3454"), "12.345"),
        (Decimal("12.3455"), "12.346"),
        (Decimal("999999999999"), "999999999999"),
    ),
)
def test_quantity_rounding_contract_is_deterministic(value, expected):
    assert _rounded_quantity(value) == expected


@pytest.mark.parametrize("value", (Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), Decimal("0.0004")))
def test_quantity_rounding_rejects_invalid_or_unrepresentable_values(value):
    with pytest.raises(ShoppingValidationError):
        _rounded_quantity(value)


@pytest.mark.parametrize("unit", ("unknown", "package", "tablespoon", ""))
def test_derivation_rejects_unapproved_units(unit):
    with pytest.raises(ShoppingValidationError):
        _derived_base_unit(unit)


def test_derivation_revalidates_actor_inside_transaction(tmp_path):
    db_path = tmp_path / "authorization.db"
    _households, _weekly, shopping, _personal, context, _published = _seed_published(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE household_members SET status = 'disabled' WHERE id = ?",
            (context.household_member_id,),
        )

    with pytest.raises(ShoppingAccessError):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=None,
            idempotency_key="derive-denied",
        )
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_LISTS_TABLE}").fetchone()[0] == 0


def test_foreign_household_cannot_derive_from_another_households_menu(tmp_path):
    db_path = tmp_path / "foreign.db"
    _households, _weekly, shopping, _personal, _owner_context, _published = _seed_published(db_path)
    _other_households, _other_weekly, _other_shopping, _other_personal, other_context = _seed_actor(
        db_path,
        actor_user_id=202,
    )

    with pytest.raises(ShoppingNotFoundError):
        shopping.generate_shopping_list_from_weekly_menu(
            other_context,
            "2026-07-06",
            expected_list_version=None,
            idempotency_key="foreign-derive",
        )
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_LISTS_TABLE}").fetchone()[0] == 0


def test_inactive_household_is_rejected_inside_transaction(tmp_path):
    db_path = tmp_path / "inactive-household.db"
    _households, _weekly, shopping, personal, context, _published = _seed_published(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE households SET status = 'disabled' WHERE id = ?",
            (personal.household.id,),
        )

    with pytest.raises(ShoppingAccessError):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=None,
            idempotency_key="inactive-household",
        )
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_LISTS_TABLE}").fetchone()[0] == 0


def test_idempotency_replay_is_authorized_and_source_sensitive(tmp_path):
    db_path = tmp_path / "idempotency.db"
    _households, weekly, shopping, personal, context, _published = _seed_published(db_path)
    first = shopping.generate_shopping_list_from_weekly_menu(
        context,
        "2026-07-06",
        expected_list_version=None,
        idempotency_key="derive-once",
    )
    replay = shopping.generate_shopping_list_from_weekly_menu(
        context,
        "2026-07-06",
        expected_list_version=None,
        idempotency_key="derive-once",
    )
    assert replay == first

    _publish(weekly, context, personal.household.id, _entries(flour_quantity="0.75"), suffix="changed")
    with pytest.raises(ShoppingConflictError):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=None,
            idempotency_key="derive-once",
        )
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE household_members SET status = 'disabled' WHERE id = ?",
            (context.household_member_id,),
        )
    with pytest.raises(ShoppingAccessError):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=None,
            idempotency_key="derive-once",
        )


def test_stale_list_version_rolls_back_without_new_idempotency(tmp_path):
    db_path = tmp_path / "stale.db"
    _households, _weekly, shopping, _personal, context, _published = _seed_published(db_path)
    first = shopping.generate_shopping_list_from_weekly_menu(
        context,
        "2026-07-06",
        expected_list_version=None,
        idempotency_key="derive-first",
    )
    with _connect(db_path) as conn:
        before = _table_snapshot(conn, SHOPPING_ITEMS_TABLE)
        idempotency_before = conn.execute(
            f"SELECT COUNT(*) FROM {SHOPPING_IDEMPOTENCY_TABLE}"
        ).fetchone()[0]
    with pytest.raises(ShoppingConflictError):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=first.shopping_list.version + 1,
            idempotency_key="derive-stale",
        )
    with _connect(db_path) as conn:
        assert _table_snapshot(conn, SHOPPING_ITEMS_TABLE) == before
        assert conn.execute(
            f"SELECT COUNT(*) FROM {SHOPPING_IDEMPOTENCY_TABLE}"
        ).fetchone()[0] == idempotency_before


def test_regeneration_preserves_manual_and_checked_and_restores_deleted_generated(tmp_path):
    db_path = tmp_path / "regenerate.db"
    _households, weekly, shopping, personal, context, _published = _seed_published(db_path)
    first = shopping.generate_shopping_list_from_weekly_menu(
        context,
        "2026-07-06",
        expected_list_version=None,
        idempotency_key="derive-first",
    )
    checked_target = next(
        item
        for item in first.items
        if item.normalized_name == "мука" and item.quantity_unit_normalized.value == "g"
    )
    checked = shopping.set_item_checked(
        context,
        checked_target.id,
        True,
        expected_item_version=checked_target.version,
        idempotency_key="check-one",
    )
    with_manual = shopping.add_manual_item(
        context,
        checked.shopping_list.id,
        ManualShoppingItemInput(display_name="Ручная позиция", quantity_value="1", quantity_unit_normalized="piece"),
        expected_list_version=checked.shopping_list.version,
        idempotency_key="manual-one",
    )
    manual_before = next(item for item in with_manual.items if item.display_name == "Ручная позиция")
    deleted_target = next(
        item
        for item in with_manual.items
        if item.origin is ShoppingItemOrigin.MENU_GENERATED and item.id != checked_target.id
    )
    after_delete = shopping.delete_item(
        context,
        deleted_target.id,
        expected_item_version=deleted_target.version,
        idempotency_key="delete-generated",
    )
    regenerated = shopping.generate_shopping_list_from_weekly_menu(
        context,
        "2026-07-06",
        expected_list_version=after_delete.shopping_list.version,
        idempotency_key="derive-same-source",
    )
    assert any(item.display_name == "Ручная позиция" for item in regenerated.items)
    manual_after = next(item for item in regenerated.items if item.display_name == "Ручная позиция")
    assert (
        manual_after.display_name,
        manual_after.quantity_value,
        manual_after.quantity_unit_normalized,
        manual_after.checked_state,
        manual_after.version,
    ) == (
        manual_before.display_name,
        manual_before.quantity_value,
        manual_before.quantity_unit_normalized,
        manual_before.checked_state,
        manual_before.version,
    )
    assert any(item.dedup_fingerprint == deleted_target.dedup_fingerprint for item in regenerated.items)
    assert next(
        item for item in regenerated.items if item.dedup_fingerprint == checked_target.dedup_fingerprint
    ).checked_state

    _publish(weekly, context, personal.household.id, _entries(flour_quantity="0.75"), suffix="two")
    changed = shopping.generate_shopping_list_from_weekly_menu(
        context,
        "2026-07-06",
        expected_list_version=regenerated.shopping_list.version,
        idempotency_key="derive-changed-source",
    )
    assert any(item.display_name == "Ручная позиция" for item in changed.items)
    old_checked = next(item for item in changed.items if item.dedup_fingerprint == checked_target.dedup_fingerprint)
    assert old_checked.override_state is ShoppingItemOverrideState.MANUALIZED
    assert old_checked.checked_state
    assert any(
        item.origin is ShoppingItemOrigin.MENU_GENERATED
        and item.normalized_name == "мука"
        and item.quantity_value == "2000"
        and not item.checked_state
        for item in changed.items
    )


@pytest.mark.parametrize(
    "phase",
    (
        "after_authorization",
        "after_source_read",
        "after_validation",
        "after_aggregation",
        "after_generated_deletion",
        "after_first_generated_insert",
        "after_generated_mutation",
        "after_list_version_update",
        "after_idempotency_write",
        "before_commit",
    ),
)
def test_derivation_faults_roll_back_every_write(phase, tmp_path):
    db_path = tmp_path / f"fault-{phase}.db"
    _households, _weekly, _shopping, _personal, context, _published = _seed_published(db_path)

    def fail(selected: str) -> None:
        if selected == phase:
            raise RuntimeError("injected derivation failure")

    shopping = HealBiteShoppingStore(db_path=db_path, derivation_fault_hook=fail)
    with pytest.raises(RuntimeError, match="injected derivation failure"):
        shopping.generate_shopping_list_from_weekly_menu(
            context,
            "2026-07-06",
            expected_list_version=None,
            idempotency_key=f"fault-{phase}",
        )
    with _connect(db_path) as conn:
        for table in (
            SHOPPING_LISTS_TABLE,
            SHOPPING_ITEMS_TABLE,
            SHOPPING_CONTRIBUTIONS_TABLE,
            SHOPPING_IDEMPOTENCY_TABLE,
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_idempotent_derivation_creates_one_logical_list(tmp_path):
    db_path = tmp_path / "concurrent.db"
    _households, _weekly, _shopping, _personal, context, _published = _seed_published(db_path)
    barrier = threading.Barrier(2)
    results = []
    failures = []

    def worker() -> None:
        try:
            store = HealBiteShoppingStore(db_path=db_path)
            barrier.wait(timeout=5)
            results.append(
                store.generate_shopping_list_from_weekly_menu(
                    context,
                    "2026-07-06",
                    expected_list_version=None,
                    idempotency_key="same-concurrent-key",
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert len(results) == 2
    assert results[0].shopping_list.id == results[1].shopping_list.id
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_LISTS_TABLE}").fetchone()[0] == 1
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_ITEMS_TABLE}").fetchone()[0] == 4
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_IDEMPOTENCY_TABLE}").fetchone()[0] == 1
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_concurrent_different_keys_serialize_to_one_success(tmp_path):
    db_path = tmp_path / "concurrent-different.db"
    _households, _weekly, _shopping, _personal, context, _published = _seed_published(db_path)
    barrier = threading.Barrier(2)
    results = []
    failures = []

    def worker(key: str) -> None:
        try:
            store = HealBiteShoppingStore(db_path=db_path)
            barrier.wait(timeout=5)
            results.append(
                store.generate_shopping_list_from_weekly_menu(
                    context,
                    "2026-07-06",
                    expected_list_version=None,
                    idempotency_key=key,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=worker, args=("different-a",)),
        threading.Thread(target=worker, args=("different-b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ShoppingConflictError)
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_LISTS_TABLE}").fetchone()[0] == 1
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_ITEMS_TABLE}").fetchone()[0] == 4
        assert conn.execute(f"SELECT COUNT(*) FROM {SHOPPING_IDEMPOTENCY_TABLE}").fetchone()[0] == 1
