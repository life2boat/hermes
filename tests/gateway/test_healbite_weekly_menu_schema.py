from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_SERIES_TABLE,
    WeeklyMenuSchemaState,
    detect_weekly_menu_schema_state,
    normalize_week_start,
    require_monday_week_start,
    week_dates,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore, WeeklyMenuSchemaError


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
        conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (int(user_id), "synthetic"))


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_schema_state_is_not_initialized_for_missing_db(tmp_path):
    db_path = tmp_path / "missing.db"

    store = HealBiteWeeklyMenuStore(db_path=db_path)

    assert store.schema_state() is WeeklyMenuSchemaState.NOT_INITIALIZED
    assert not db_path.exists()


def test_initialize_schema_creates_canonical_tables_idempotently(tmp_path):
    db_path = tmp_path / "weekly.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    store = HealBiteWeeklyMenuStore(db_path=db_path)

    assert store.initialize_schema() is WeeklyMenuSchemaState.CANONICAL
    assert store.initialize_schema() is WeeklyMenuSchemaState.CANONICAL

    with _connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {WEEKLY_MENU_SERIES_TABLE, WEEKLY_MENU_REVISIONS_TABLE, WEEKLY_MENU_ENTRIES_TABLE, WEEKLY_MENU_IDEMPOTENCY_TABLE}.issubset(tables)
        assert _count(conn, WEEKLY_MENU_SERIES_TABLE) == 0
        assert _count(conn, WEEKLY_MENU_REVISIONS_TABLE) == 0
        assert _count(conn, WEEKLY_MENU_ENTRIES_TABLE) == 0
        assert _count(conn, WEEKLY_MENU_IDEMPOTENCY_TABLE) == 0


def test_partial_schema_is_detected_and_initializer_refuses(tmp_path):
    db_path = tmp_path / "partial.db"
    with _connect(db_path) as conn:
        conn.execute(f"CREATE TABLE {WEEKLY_MENU_SERIES_TABLE} (id TEXT PRIMARY KEY)")
    store = HealBiteWeeklyMenuStore(db_path=db_path)

    assert store.schema_state() is WeeklyMenuSchemaState.PARTIAL
    with pytest.raises(WeeklyMenuSchemaError):
        store.initialize_schema()


def test_incompatible_schema_is_detected_and_initializer_refuses(tmp_path):
    db_path = tmp_path / "incompatible.db"
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE {WEEKLY_MENU_SERIES_TABLE} (
                id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE {WEEKLY_MENU_REVISIONS_TABLE} (
                id TEXT PRIMARY KEY,
                series_id TEXT NOT NULL,
                household_id TEXT NOT NULL,
                revision_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                source_revision_id TEXT NULL,
                created_by_member_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                published_at TEXT NULL,
                archived_at TEXT NULL,
                version INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE {WEEKLY_MENU_ENTRIES_TABLE} (
                id TEXT PRIMARY KEY,
                menu_id TEXT NOT NULL,
                household_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                meal_slot TEXT NOT NULL,
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE {WEEKLY_MENU_IDEMPOTENCY_TABLE} (
                id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL,
                actor_member_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_fingerprint TEXT NOT NULL,
                series_id TEXT NULL,
                revision_id TEXT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
    store = HealBiteWeeklyMenuStore(db_path=db_path)

    assert store.schema_state() is WeeklyMenuSchemaState.INCOMPATIBLE
    with pytest.raises(WeeklyMenuSchemaError):
        store.initialize_schema()


def test_detect_schema_state_returns_canonical_after_init(tmp_path):
    db_path = tmp_path / "state.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    store = HealBiteWeeklyMenuStore(db_path=db_path)
    store.initialize_schema()

    with _connect(db_path) as conn:
        assert detect_weekly_menu_schema_state(conn) is WeeklyMenuSchemaState.CANONICAL


def test_week_start_helpers_normalize_to_monday():
    assert normalize_week_start("2026-07-09") == "2026-07-06"
    assert require_monday_week_start("2026-07-06") == "2026-07-06"
    with pytest.raises(ValueError):
        require_monday_week_start("2026-07-09")
    assert week_dates("2026-07-06") == (
        "2026-07-06",
        "2026-07-07",
        "2026-07-08",
        "2026-07-09",
        "2026-07-10",
        "2026-07-11",
        "2026-07-12",
    )
