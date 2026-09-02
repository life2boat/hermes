from __future__ import annotations

import sqlite3

import pytest

from gateway.healbite_fridge_menu_schema import (
    PLANNED_INGREDIENTS_TABLE,
    PLANNED_MEALS_TABLE,
    USER_INVENTORY_TABLE,
    WEEKLY_MENU_PLANS_TABLE,
    apply_fridge_menu_schema,
)


USER_ONE = 101
USER_TWO = 202
INVENTORY_ID = "11111111-1111-1111-1111-111111111111"
PLAN_ID = "22222222-2222-2222-2222-222222222222"
MEAL_ID = "33333333-3333-3333-3333-333333333333"
INGREDIENT_ONE_ID = "44444444-4444-4444-4444-444444444444"
INGREDIENT_TWO_ID = "55555555-5555-5555-5555-555555555555"


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT)"
    )
    connection.executemany(
        "INSERT INTO users (telegram_id, username) VALUES (?, ?)",
        ((USER_ONE, "one"), (USER_TWO, "two")),
    )
    return connection


def _seed_plan(connection: sqlite3.Connection, *, user_id: int = USER_ONE) -> None:
    connection.execute(
        f"""
        INSERT INTO {WEEKLY_MENU_PLANS_TABLE}
            (id, user_id, week_start, status)
        VALUES (?, ?, ?, ?)
        """,
        (PLAN_ID, user_id, "2026-08-10", "generated"),
    )
    connection.execute(
        f"""
        INSERT INTO {PLANNED_MEALS_TABLE}
            (id, plan_id, user_id, day_of_week, meal_type, title)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            MEAL_ID,
            PLAN_ID,
            user_id,
            "monday",
            "breakfast",
            "\u041e\u043c\u043b\u0435\u0442",
        ),
    )


def test_schema_is_additive_idempotent_and_preserves_existing_data() -> None:
    connection = _connection()
    connection.execute("CREATE TABLE existing_rows (value TEXT NOT NULL)")
    connection.execute("INSERT INTO existing_rows (value) VALUES ('keep')")

    apply_fridge_menu_schema(connection)
    apply_fridge_menu_schema(connection)

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        USER_INVENTORY_TABLE,
        WEEKLY_MENU_PLANS_TABLE,
        PLANNED_MEALS_TABLE,
        PLANNED_INGREDIENTS_TABLE,
    } <= tables
    assert connection.execute("SELECT value FROM existing_rows").fetchone()[0] == "keep"


# ---------------------------------------------------------------------------
# PR corrective regression: fridge-menu foreign keys must target the
# production users identity column (users.telegram_id).
# ---------------------------------------------------------------------------


def _fk_map(connection: sqlite3.Connection, table: str) -> dict[str, tuple[str, str]]:
    """Map child column -> (parent table, parent column) via SQLite introspection."""
    rows = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    mapping: dict[str, tuple[str, str]] = {}
    for row in rows:
        child_column = str(row[3])
        parent_table = str(row[2])
        parent_column = str(row[4]) if row[4] is not None else ""
        mapping[child_column] = (parent_table, parent_column)
    return mapping


def test_user_identity_fk_targets_users_telegram_id() -> None:
    connection = _connection()
    apply_fridge_menu_schema(connection)

    inventory_fk = _fk_map(connection, USER_INVENTORY_TABLE)
    assert inventory_fk["user_id"] == ("users", "telegram_id")

    plans_fk = _fk_map(connection, WEEKLY_MENU_PLANS_TABLE)
    assert plans_fk["user_id"] == ("users", "telegram_id")


def test_planned_tables_reference_parent_fridge_tables_not_users() -> None:
    connection = _connection()
    apply_fridge_menu_schema(connection)

    meals_fk = _fk_map(connection, PLANNED_MEALS_TABLE)
    assert meals_fk["plan_id"] == (WEEKLY_MENU_PLANS_TABLE, "id")
    assert meals_fk["user_id"] == (WEEKLY_MENU_PLANS_TABLE, "user_id")

    ingredients_fk = _fk_map(connection, PLANNED_INGREDIENTS_TABLE)
    assert ingredients_fk["meal_id"] == (PLANNED_MEALS_TABLE, "id")
    assert ingredients_fk["user_id"] == (PLANNED_MEALS_TABLE, "user_id")


def test_migration_succeeds_on_production_users_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO users (telegram_id, username) VALUES (101, 'prod-one');
        INSERT INTO users (telegram_id, username) VALUES (202, 'prod-two');
        """
    )

    apply_fridge_menu_schema(connection)
    apply_fridge_menu_schema(connection)

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        USER_INVENTORY_TABLE,
        WEEKLY_MENU_PLANS_TABLE,
        PLANNED_MEALS_TABLE,
        PLANNED_INGREDIENTS_TABLE,
    } <= tables
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    connection.execute(
        f"""
        INSERT INTO {USER_INVENTORY_TABLE}
            (id, user_id, normalized_name, display_name, source_type)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            INVENTORY_ID,
            101,
            "eggs",
            "Eggs",
            "text",
        ),
    )
    connection.execute("DELETE FROM users WHERE telegram_id = ?", (101,))
    assert connection.execute(f"SELECT COUNT(*) FROM {USER_INVENTORY_TABLE}").fetchone()[0] == 0


def test_parent_without_telegram_id_cannot_satisfy_fridge_user_fk() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT)"
    )
    apply_fridge_menu_schema(connection)

    with pytest.raises(sqlite3.OperationalError, match="foreign key mismatch"):
        connection.execute(
            f"""
            INSERT INTO {USER_INVENTORY_TABLE}
                (id, user_id, normalized_name, display_name, source_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (INVENTORY_ID, 101, "eggs", "Eggs", "text"),
        )


def test_missing_ingredients_query_is_driven_by_is_in_inventory() -> None:
    connection = _connection()
    apply_fridge_menu_schema(connection)
    connection.execute(
        f"""
        INSERT INTO {USER_INVENTORY_TABLE}
            (id, user_id, normalized_name, display_name, quantity_value, quantity_unit, source_type)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            INVENTORY_ID,
            USER_ONE,
            "\u044f\u0439\u0446\u0430",
            "\u042f\u0439\u0446\u0430",
            "6",
            "piece",
            "vision",
        ),
    )
    _seed_plan(connection)
    connection.executemany(
        f"""
        INSERT INTO {PLANNED_INGREDIENTS_TABLE}
            (id, meal_id, user_id, normalized_name, display_name, quantity_value, quantity_unit, is_in_inventory)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                INGREDIENT_ONE_ID,
                MEAL_ID,
                USER_ONE,
                "\u044f\u0439\u0446\u0430",
                "\u042f\u0439\u0446\u0430",
                "2",
                "piece",
                1,
            ),
            (
                INGREDIENT_TWO_ID,
                MEAL_ID,
                USER_ONE,
                "\u043c\u043e\u043b\u043e\u043a\u043e",
                "\u041c\u043e\u043b\u043e\u043a\u043e",
                "200",
                "ml",
                0,
            ),
        ),
    )

    missing = connection.execute(
        f"""
        SELECT display_name
        FROM {PLANNED_INGREDIENTS_TABLE}
        WHERE user_id = ? AND is_in_inventory = 0
        ORDER BY normalized_name
        """,
        (USER_ONE,),
    ).fetchall()

    assert [str(row[0]) for row in missing] == [
        "\u041c\u043e\u043b\u043e\u043a\u043e"
    ]


def test_composite_foreign_keys_prevent_cross_user_plan_access() -> None:
    connection = _connection()
    apply_fridge_menu_schema(connection)
    connection.execute(
        f"""
        INSERT INTO {WEEKLY_MENU_PLANS_TABLE}
            (id, user_id, week_start)
        VALUES (?, ?, ?)
        """,
        (PLAN_ID, USER_ONE, "2026-08-10"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"""
            INSERT INTO {PLANNED_MEALS_TABLE}
                (id, plan_id, user_id, day_of_week, meal_type, title)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (MEAL_ID, PLAN_ID, USER_TWO, "monday", "breakfast", "Forbidden"),
        )


def test_inventory_flag_is_strict_and_user_delete_cascades() -> None:
    connection = _connection()
    apply_fridge_menu_schema(connection)
    _seed_plan(connection)

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"""
            INSERT INTO {PLANNED_INGREDIENTS_TABLE}
                (id, meal_id, user_id, normalized_name, display_name, is_in_inventory)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                INGREDIENT_ONE_ID,
                MEAL_ID,
                USER_ONE,
                "\u044f\u0439\u0446\u0430",
                "\u042f\u0439\u0446\u0430",
                2,
            ),
        )

    connection.execute(
        f"""
        INSERT INTO {PLANNED_INGREDIENTS_TABLE}
            (id, meal_id, user_id, normalized_name, display_name, is_in_inventory)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            INGREDIENT_ONE_ID,
            MEAL_ID,
            USER_ONE,
            "\u044f\u0439\u0446\u0430",
            "\u042f\u0439\u0446\u0430",
            1,
        ),
    )
    connection.execute("DELETE FROM users WHERE telegram_id = ?", (USER_ONE,))

    assert connection.execute(
        f"SELECT COUNT(*) FROM {WEEKLY_MENU_PLANS_TABLE}"
    ).fetchone()[0] == 0
    assert connection.execute(
        f"SELECT COUNT(*) FROM {PLANNED_MEALS_TABLE}"
    ).fetchone()[0] == 0
    assert connection.execute(
        f"SELECT COUNT(*) FROM {PLANNED_INGREDIENTS_TABLE}"
    ).fetchone()[0] == 0
