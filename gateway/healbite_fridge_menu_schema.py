from __future__ import annotations

import sqlite3

USER_INVENTORY_TABLE = "user_inventory"
WEEKLY_MENU_PLANS_TABLE = "weekly_menu_plans"
PLANNED_MEALS_TABLE = "planned_meals"
PLANNED_INGREDIENTS_TABLE = "planned_ingredients"

FRIDGE_MENU_SCHEMA_MIGRATION_ID = "healbite-fridge-menu-schema-v1"
FRIDGE_MENU_SCHEMA_MIGRATION_SHA256 = "52bba5ae2c3748f4e2955bef15676f9ec734e0b66100ee13917e0117d60db06f"

FRIDGE_MENU_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {USER_INVENTORY_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    user_id INTEGER NOT NULL,
    normalized_name TEXT NOT NULL CHECK (length(trim(normalized_name)) BETWEEN 1 AND 200),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) BETWEEN 1 AND 200),
    quantity_value TEXT NULL,
    quantity_unit TEXT NOT NULL DEFAULT 'unknown'
        CHECK (quantity_unit IN ('g', 'kg', 'ml', 'l', 'piece', 'package', 'unitless', 'unknown')),
    source_type TEXT NOT NULL CHECK (source_type IN ('text', 'vision')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, user_id),
    UNIQUE (user_id, normalized_name),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_inventory_user_name
    ON {USER_INVENTORY_TABLE} (user_id, normalized_name);

CREATE TABLE IF NOT EXISTS {WEEKLY_MENU_PLANS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    user_id INTEGER NOT NULL,
    week_start TEXT NOT NULL CHECK (
        length(week_start) = 10
        AND substr(week_start, 5, 1) = '-'
        AND substr(week_start, 8, 1) = '-'
        AND strftime('%w', week_start) = '1'
    ),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'generated', 'archived')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, user_id),
    UNIQUE (user_id, week_start),
    FOREIGN KEY (user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_weekly_menu_plans_user_week
    ON {WEEKLY_MENU_PLANS_TABLE} (user_id, week_start, status);

CREATE TABLE IF NOT EXISTS {PLANNED_MEALS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    plan_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    day_of_week TEXT NOT NULL
        CHECK (day_of_week IN ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')),
    meal_type TEXT NOT NULL CHECK (meal_type IN ('breakfast', 'lunch', 'dinner')),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
    instructions TEXT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (id, user_id),
    UNIQUE (plan_id, day_of_week, meal_type),
    FOREIGN KEY (plan_id, user_id)
        REFERENCES {WEEKLY_MENU_PLANS_TABLE}(id, user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_planned_meals_plan_day
    ON {PLANNED_MEALS_TABLE} (plan_id, day_of_week, meal_type);

CREATE TABLE IF NOT EXISTS {PLANNED_INGREDIENTS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    meal_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    normalized_name TEXT NOT NULL CHECK (length(trim(normalized_name)) BETWEEN 1 AND 200),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) BETWEEN 1 AND 200),
    quantity_value TEXT NULL,
    quantity_unit TEXT NOT NULL DEFAULT 'unknown'
        CHECK (quantity_unit IN ('g', 'kg', 'ml', 'l', 'piece', 'package', 'unitless', 'unknown')),
    is_in_inventory INTEGER NOT NULL DEFAULT 0 CHECK (is_in_inventory IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (meal_id, normalized_name),
    FOREIGN KEY (meal_id, user_id)
        REFERENCES {PLANNED_MEALS_TABLE}(id, user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_planned_ingredients_meal
    ON {PLANNED_INGREDIENTS_TABLE} (meal_id, normalized_name);
CREATE INDEX IF NOT EXISTS idx_planned_ingredients_missing
    ON {PLANNED_INGREDIENTS_TABLE} (user_id, normalized_name)
    WHERE is_in_inventory = 0;
"""


def fridge_menu_schema_statements() -> tuple[str, ...]:
    """Return authoritative additive fridge-menu DDL without applying it."""

    return tuple(
        statement.strip()
        for statement in FRIDGE_MENU_SCHEMA_SQL.split(";")
        if statement.strip()
    )


def apply_fridge_menu_schema(connection: sqlite3.Connection) -> None:
    """Apply the additive schema to a borrowed SQLite connection."""

    for statement in fridge_menu_schema_statements():
        connection.execute(statement)
