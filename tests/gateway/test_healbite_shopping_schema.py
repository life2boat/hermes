from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_shopping import HealBiteShoppingStore, ShoppingSchemaError
from gateway.healbite_shopping_schema import (
    SHOPPING_IDEMPOTENCY_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
    ShoppingSchemaState,
    detect_shopping_schema_state,
    is_valid_quantity_value,
    normalize_quantity_value,
    normalize_shopping_unit,
    quantity_contract_is_valid,
    shopping_unit_family,
    units_are_compatible,
)
from gateway.healbite_water_tracker import HealBiteWaterTracker, WATER_INTAKE_TABLE
from gateway.healbite_weekly_menu_schema import WeeklyMenuSchemaState
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore
from gateway.healbite_weight_tracker import HealBiteWeightTracker, WEIGHT_ENTRIES_TABLE


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path) -> None:
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


def _insert_user(db_path: Path, user_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (int(user_id), f"user-{user_id}"))


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _seed_dependencies(db_path: Path, *, actor_user_id: int = 101) -> None:
    _create_users_table(db_path)
    _insert_user(db_path, actor_user_id)
    household_store = HealBiteHouseholdStore(db_path=db_path)
    household_store.get_or_create_personal_household(actor_user_id)
    weekly_store = HealBiteWeeklyMenuStore(db_path=db_path)
    assert weekly_store.initialize_schema() is WeeklyMenuSchemaState.CANONICAL


def test_schema_state_reports_dependency_missing_before_household_and_weekly_menu(tmp_path):
    db_path = tmp_path / "shopping.db"
    store = HealBiteShoppingStore(db_path=db_path)

    assert store.schema_state() is ShoppingSchemaState.DEPENDENCY_MISSING
    assert not db_path.exists()


def test_initialize_schema_refuses_when_weekly_menu_dependency_missing(tmp_path):
    db_path = tmp_path / "shopping.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    store = HealBiteShoppingStore(db_path=db_path)

    with pytest.raises(ShoppingSchemaError):
        store.initialize_schema()
    assert store.schema_state() is ShoppingSchemaState.DEPENDENCY_MISSING


def test_initialize_schema_creates_canonical_tables_idempotently(tmp_path):
    db_path = tmp_path / "shopping.db"
    _seed_dependencies(db_path)
    store = HealBiteShoppingStore(db_path=db_path)

    assert store.initialize_schema() is ShoppingSchemaState.CANONICAL
    assert store.initialize_schema() is ShoppingSchemaState.CANONICAL

    with _connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _count(conn, SHOPPING_LISTS_TABLE) == 0
        assert _count(conn, SHOPPING_ITEMS_TABLE) == 0
        assert _count(conn, SHOPPING_IDEMPOTENCY_TABLE) == 0
        assert detect_shopping_schema_state(conn) is ShoppingSchemaState.CANONICAL


def test_malformed_partial_schema_is_incompatible_and_initializer_refuses(tmp_path):
    db_path = tmp_path / "partial.db"
    _seed_dependencies(db_path)
    with _connect(db_path) as conn:
        conn.execute(f"CREATE TABLE {SHOPPING_LISTS_TABLE} (id TEXT PRIMARY KEY)")
    store = HealBiteShoppingStore(db_path=db_path)

    assert store.schema_state() is ShoppingSchemaState.INCOMPATIBLE
    with pytest.raises(ShoppingSchemaError):
        store.initialize_schema()


def test_valid_nonprefix_partial_schema_is_incompatible(tmp_path):
    db_path = tmp_path / "nonprefix.db"
    _seed_dependencies(db_path)
    idempotency_statement = next(
        statement
        for statement in HealBiteShoppingStore.schema_statements()
        if statement.lstrip().startswith("CREATE TABLE IF NOT EXISTS household_shopping_idempotency")
    )
    with _connect(db_path) as conn:
        conn.execute(idempotency_statement)
    store = HealBiteShoppingStore(db_path=db_path)

    assert store.schema_state() is ShoppingSchemaState.INCOMPATIBLE
    with pytest.raises(ShoppingSchemaError):
        store.initialize_schema()


def test_incompatible_schema_is_detected_and_initializer_refuses(tmp_path):
    db_path = tmp_path / "incompatible.db"
    _seed_dependencies(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE {SHOPPING_LISTS_TABLE} (
                id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL,
                week_start TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE {SHOPPING_ITEMS_TABLE} (
                id TEXT PRIMARY KEY,
                shopping_list_id TEXT NOT NULL,
                household_id TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE {SHOPPING_IDEMPOTENCY_TABLE} (
                id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL,
                actor_member_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_fingerprint TEXT NOT NULL,
                shopping_list_id TEXT NULL,
                shopping_item_id TEXT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    store = HealBiteShoppingStore(db_path=db_path)

    assert store.schema_state() is ShoppingSchemaState.INCOMPATIBLE
    with pytest.raises(ShoppingSchemaError):
        store.initialize_schema()


def test_schema_initialization_rolls_back_mid_ddl_failure(tmp_path, monkeypatch):
    db_path = tmp_path / "rollback.db"
    _seed_dependencies(db_path)
    store = HealBiteShoppingStore(db_path=db_path)
    original = store._schema_statements
    monkeypatch.setattr(
        HealBiteShoppingStore,
        "_schema_statements",
        staticmethod(lambda: ("CREATE TABLE shopping_probe(id TEXT PRIMARY KEY)", "THIS IS NOT SQL")),
    )

    with pytest.raises(sqlite3.OperationalError):
        store.initialize_schema()

    with _connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "shopping_probe" not in tables
        assert SHOPPING_LISTS_TABLE not in tables
        assert SHOPPING_ITEMS_TABLE not in tables
    monkeypatch.setattr(HealBiteShoppingStore, "_schema_statements", original)


def test_schema_preserves_existing_household_weekly_menu_and_health_rows(tmp_path):
    db_path = tmp_path / "preserve.db"
    _seed_dependencies(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nutrition_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                meal_name TEXT NOT NULL,
                calories_kcal REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "INSERT INTO nutrition_log (user_id, meal_name, calories_kcal) VALUES (?, ?, ?)",
            (101, "synthetic", 100.0),
        )
    HealBiteWeightTracker(db_path=db_path).add_weight_entry(101, 80.0, source="test")
    HealBiteWaterTracker(db_path=db_path).add_water_intake(101, 250, source="test")

    with _connect(db_path) as conn:
        before = {
            "households": _count(conn, "households"),
            "household_members": _count(conn, "household_members"),
            "weekly_series": _count(conn, "household_weekly_menu_series"),
            "weekly_revisions": _count(conn, "household_weekly_menus"),
            "weekly_entries": _count(conn, "household_weekly_menu_entries"),
            "nutrition_log": _count(conn, "nutrition_log"),
            WEIGHT_ENTRIES_TABLE: _count(conn, WEIGHT_ENTRIES_TABLE),
            WATER_INTAKE_TABLE: _count(conn, WATER_INTAKE_TABLE),
        }

    store = HealBiteShoppingStore(db_path=db_path)
    assert store.initialize_schema() is ShoppingSchemaState.CANONICAL

    with _connect(db_path) as conn:
        for table, count in before.items():
            mapped = {
                "weekly_series": "household_weekly_menu_series",
                "weekly_revisions": "household_weekly_menus",
                "weekly_entries": "household_weekly_menu_entries",
            }.get(table, table)
            assert _count(conn, mapped) == count


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", "0"),
        ("001.230", "1.23"),
        ("12.500", "12.5"),
        ("1.0", "1"),
        ("01", "1"),
        ("999999999.999", "999999999.999"),
    ],
)
def test_quantity_normalization_accepts_valid_values(value, expected):
    assert normalize_quantity_value(value) == expected
    assert is_valid_quantity_value(value)


@pytest.mark.parametrize(
    "value",
    ["", " 1 ", "1,2", "1e3", "-1", "+1", "1.2345", "1000000000000", "NaN", "Infinity"],
)
def test_quantity_normalization_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        normalize_quantity_value(value)
    assert not is_valid_quantity_value(value)


def test_unit_helpers_report_family_and_compatibility():
    assert normalize_shopping_unit("KG").value == "kg"
    assert shopping_unit_family("kg").value == "mass"
    assert units_are_compatible("g", "kg")
    assert units_are_compatible("ml", "l")
    assert not units_are_compatible("g", "piece")
    assert not units_are_compatible("unknown", "g")
    assert quantity_contract_is_valid("1.5", "kg")
    assert quantity_contract_is_valid(None, "unknown")
    assert not quantity_contract_is_valid(None, "kg")


def test_imports_do_not_create_database_or_start_schema(tmp_path):
    db_path = tmp_path / "no-import.db"
    env = {
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "HEALBITE_DB_PATH": str(db_path),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import gateway.healbite_shopping_schema; import gateway.healbite_shopping",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert not db_path.exists()
