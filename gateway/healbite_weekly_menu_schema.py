from __future__ import annotations

import sqlite3
import uuid
from datetime import date, timedelta
from enum import Enum

from gateway.healbite_household_schema import is_canonical_uuid4, require_canonical_uuid4

WEEKLY_MENU_SERIES_TABLE = "household_weekly_menu_series"
WEEKLY_MENU_REVISIONS_TABLE = "household_weekly_menus"
WEEKLY_MENU_ENTRIES_TABLE = "household_weekly_menu_entries"
WEEKLY_MENU_INGREDIENTS_TABLE = "household_weekly_menu_entry_ingredients"
WEEKLY_MENU_IDEMPOTENCY_TABLE = "household_weekly_menu_idempotency"


class WeeklyMenuSchemaState(str, Enum):
    NOT_INITIALIZED = "not_initialized"
    CANONICAL = "canonical"
    PARTIAL = "partial"
    INCOMPATIBLE = "incompatible"


class WeeklyMenuRevisionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class WeeklyMenuMealSlot(str, Enum):
    BREAKFAST = "breakfast"
    LUNCH = "lunch"
    DINNER = "dinner"
    SNACK = "snack"


class WeeklyMenuEntryOrigin(str, Enum):
    GENERATED = "generated"
    MANUAL = "manual"
    COPIED = "copied"


class WeeklyMenuIdempotencyOperation(str, Enum):
    CREATE_DRAFT = "create_draft"
    REPLACE_DRAFT_ENTRIES = "replace_draft_entries"
    PUBLISH_REVISION = "publish_revision"
    ARCHIVE_REVISION = "archive_revision"


WEEKLY_MENU_REVISION_STATUSES = tuple(item.value for item in WeeklyMenuRevisionStatus)
WEEKLY_MENU_MEAL_SLOTS = tuple(item.value for item in WeeklyMenuMealSlot)
WEEKLY_MENU_ENTRY_ORIGINS = tuple(item.value for item in WeeklyMenuEntryOrigin)
WEEKLY_MENU_IDEMPOTENCY_OPERATIONS = tuple(item.value for item in WeeklyMenuIdempotencyOperation)
MEAL_SLOT_ORDER = {
    WeeklyMenuMealSlot.BREAKFAST.value: 0,
    WeeklyMenuMealSlot.LUNCH.value: 1,
    WeeklyMenuMealSlot.DINNER.value: 2,
    WeeklyMenuMealSlot.SNACK.value: 3,
}

EXPECTED_TABLES = {
    WEEKLY_MENU_SERIES_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_INGREDIENTS_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_TABLE,
}
EXPECTED_INDEXES = {
    "idx_weekly_menu_series_household_week_unique",
    "idx_weekly_menu_series_id_household",
    "idx_weekly_menu_revisions_series_revision_unique",
    "idx_weekly_menu_revisions_id_household",
    "idx_weekly_menu_revisions_single_draft",
    "idx_weekly_menu_revisions_single_published",
    "idx_weekly_menu_entries_menu_slot_position_unique",
    "idx_weekly_menu_entries_menu_local_date_slot_position",
    "idx_weekly_menu_ingredients_entry_position_unique",
    "idx_weekly_menu_idempotency_unique",
}
EXPECTED_SERIES_COLUMNS = {
    "id",
    "household_id",
    "week_start",
    "created_at",
    "updated_at",
    "version",
}
EXPECTED_REVISION_COLUMNS = {
    "id",
    "series_id",
    "household_id",
    "revision_number",
    "status",
    "source_revision_id",
    "created_by_member_id",
    "created_at",
    "updated_at",
    "published_at",
    "archived_at",
    "version",
}
EXPECTED_ENTRY_COLUMNS = {
    "id",
    "menu_id",
    "household_id",
    "local_date",
    "meal_slot",
    "position",
    "title",
    "description",
    "servings",
    "origin",
    "created_at",
    "updated_at",
    "version",
}
EXPECTED_INGREDIENT_COLUMNS = {
    "id",
    "menu_entry_id",
    "position",
    "display_name",
    "quantity_value",
    "quantity_unit",
    "recipe_base_servings",
    "created_at",
}
EXPECTED_IDEMPOTENCY_COLUMNS = {
    "id",
    "household_id",
    "actor_member_id",
    "operation",
    "idempotency_key",
    "payload_fingerprint",
    "series_id",
    "revision_id",
    "created_at",
}
EXPECTED_SERIES_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "household_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "week_start": {"type": "TEXT", "notnull": 1, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "updated_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "version": {"type": "INTEGER", "notnull": 1, "pk": 0, "default": "1"},
}
EXPECTED_REVISION_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "series_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "household_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "revision_number": {"type": "INTEGER", "notnull": 1, "pk": 0},
    "status": {"type": "TEXT", "notnull": 1, "pk": 0},
    "source_revision_id": {"type": "TEXT", "notnull": 0, "pk": 0},
    "created_by_member_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "updated_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "published_at": {"type": "TEXT", "notnull": 0, "pk": 0},
    "archived_at": {"type": "TEXT", "notnull": 0, "pk": 0},
    "version": {"type": "INTEGER", "notnull": 1, "pk": 0, "default": "1"},
}
EXPECTED_ENTRY_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "menu_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "household_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "local_date": {"type": "TEXT", "notnull": 1, "pk": 0},
    "meal_slot": {"type": "TEXT", "notnull": 1, "pk": 0},
    "position": {"type": "INTEGER", "notnull": 1, "pk": 0},
    "title": {"type": "TEXT", "notnull": 1, "pk": 0},
    "description": {"type": "TEXT", "notnull": 0, "pk": 0},
    "servings": {"type": "TEXT", "notnull": 0, "pk": 0},
    "origin": {"type": "TEXT", "notnull": 1, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "updated_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "version": {"type": "INTEGER", "notnull": 1, "pk": 0, "default": "1"},
}
EXPECTED_INGREDIENT_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "menu_entry_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "position": {"type": "INTEGER", "notnull": 1, "pk": 0},
    "display_name": {"type": "TEXT", "notnull": 1, "pk": 0},
    "quantity_value": {"type": "TEXT", "notnull": 1, "pk": 0},
    "quantity_unit": {"type": "TEXT", "notnull": 1, "pk": 0},
    "recipe_base_servings": {"type": "TEXT", "notnull": 1, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
}
EXPECTED_IDEMPOTENCY_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "household_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "actor_member_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "operation": {"type": "TEXT", "notnull": 1, "pk": 0},
    "idempotency_key": {"type": "TEXT", "notnull": 1, "pk": 0},
    "payload_fingerprint": {"type": "TEXT", "notnull": 1, "pk": 0},
    "series_id": {"type": "TEXT", "notnull": 0, "pk": 0},
    "revision_id": {"type": "TEXT", "notnull": 0, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
}
EXPECTED_FOREIGN_KEYS = {
    WEEKLY_MENU_SERIES_TABLE: {
        ("household_id", "households", "id", "RESTRICT"),
    },
    WEEKLY_MENU_REVISIONS_TABLE: {
        ("source_revision_id", WEEKLY_MENU_REVISIONS_TABLE, "id", "RESTRICT"),
        ("created_by_member_id", "household_members", "id", "RESTRICT"),
        ("series_id|household_id", WEEKLY_MENU_SERIES_TABLE, "id|household_id", "RESTRICT"),
    },
    WEEKLY_MENU_ENTRIES_TABLE: {
        ("menu_id|household_id", WEEKLY_MENU_REVISIONS_TABLE, "id|household_id", "RESTRICT"),
    },
    WEEKLY_MENU_INGREDIENTS_TABLE: {
        ("menu_entry_id", WEEKLY_MENU_ENTRIES_TABLE, "id", "CASCADE"),
    },
    WEEKLY_MENU_IDEMPOTENCY_TABLE: {
        ("household_id", "households", "id", "RESTRICT"),
        ("actor_member_id", "household_members", "id", "RESTRICT"),
        ("series_id", WEEKLY_MENU_SERIES_TABLE, "id", "RESTRICT"),
        ("revision_id", WEEKLY_MENU_REVISIONS_TABLE, "id", "RESTRICT"),
    },
}
EXPECTED_INDEX_DETAILS = {
    "idx_weekly_menu_series_household_week_unique": {"table": WEEKLY_MENU_SERIES_TABLE, "unique": 1, "partial": 0, "columns": ("household_id", "week_start"), "where": None},
    "idx_weekly_menu_series_id_household": {"table": WEEKLY_MENU_SERIES_TABLE, "unique": 1, "partial": 0, "columns": ("id", "household_id"), "where": None},
    "idx_weekly_menu_revisions_series_revision_unique": {"table": WEEKLY_MENU_REVISIONS_TABLE, "unique": 1, "partial": 0, "columns": ("series_id", "revision_number"), "where": None},
    "idx_weekly_menu_revisions_id_household": {"table": WEEKLY_MENU_REVISIONS_TABLE, "unique": 1, "partial": 0, "columns": ("id", "household_id"), "where": None},
    "idx_weekly_menu_revisions_single_draft": {"table": WEEKLY_MENU_REVISIONS_TABLE, "unique": 1, "partial": 1, "columns": ("series_id",), "where": "status = 'draft'"},
    "idx_weekly_menu_revisions_single_published": {"table": WEEKLY_MENU_REVISIONS_TABLE, "unique": 1, "partial": 1, "columns": ("series_id",), "where": "status = 'published'"},
    "idx_weekly_menu_entries_menu_slot_position_unique": {"table": WEEKLY_MENU_ENTRIES_TABLE, "unique": 1, "partial": 0, "columns": ("menu_id", "local_date", "meal_slot", "position"), "where": None},
    "idx_weekly_menu_entries_menu_local_date_slot_position": {"table": WEEKLY_MENU_ENTRIES_TABLE, "unique": 0, "partial": 0, "columns": ("menu_id", "local_date", "meal_slot", "position"), "where": None},
    "idx_weekly_menu_ingredients_entry_position_unique": {"table": WEEKLY_MENU_INGREDIENTS_TABLE, "unique": 1, "partial": 0, "columns": ("menu_entry_id", "position"), "where": None},
    "idx_weekly_menu_idempotency_unique": {"table": WEEKLY_MENU_IDEMPOTENCY_TABLE, "unique": 1, "partial": 0, "columns": ("household_id", "actor_member_id", "operation", "idempotency_key"), "where": None},
}
EXPECTED_CHECK_SNIPPETS = {
    WEEKLY_MENU_SERIES_TABLE: ("strftime('%w', week_start) = '1'", "CHECK (version >= 1)"),
    WEEKLY_MENU_REVISIONS_TABLE: ("status IN ('draft', 'published', 'archived')", "revision_number >= 1", "status = 'draft'", "status = 'published'", "status = 'archived'"),
    WEEKLY_MENU_ENTRIES_TABLE: ("meal_slot IN ('breakfast', 'lunch', 'dinner', 'snack')", "position >= 1", "length(trim(title)) > 0", "origin IN ('generated', 'manual', 'copied')"),
    WEEKLY_MENU_INGREDIENTS_TABLE: (
        "position >= 1",
        "length(trim(display_name)) > 0",
        "quantity_unit IN ('g', 'kg', 'ml', 'l', 'piece', 'unitless')",
        "length(trim(quantity_value)) > 0",
        "length(trim(recipe_base_servings)) > 0",
    ),
    WEEKLY_MENU_IDEMPOTENCY_TABLE: ("operation IN ('create_draft', 'replace_draft_entries', 'publish_revision', 'archive_revision')", "length(trim(idempotency_key)) BETWEEN 1 AND 128", "length(payload_fingerprint) = 64"),
}


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


WEEKLY_MENU_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {WEEKLY_MENU_SERIES_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    household_id TEXT NOT NULL,
    week_start TEXT NOT NULL CHECK (
        length(week_start) = 10
        AND substr(week_start, 5, 1) = '-'
        AND substr(week_start, 8, 1) = '-'
        AND strftime('%w', week_start) = '1'
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_series_household_week_unique
    ON {WEEKLY_MENU_SERIES_TABLE} (household_id, week_start);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_series_id_household
    ON {WEEKLY_MENU_SERIES_TABLE} (id, household_id);

CREATE TABLE IF NOT EXISTS {WEEKLY_MENU_REVISIONS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    series_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    status TEXT NOT NULL CHECK (status IN ({_quoted(WEEKLY_MENU_REVISION_STATUSES)})),
    source_revision_id TEXT NULL,
    created_by_member_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    published_at TEXT NULL,
    archived_at TEXT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (series_id, household_id)
        REFERENCES {WEEKLY_MENU_SERIES_TABLE}(id, household_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (source_revision_id) REFERENCES {WEEKLY_MENU_REVISIONS_TABLE}(id) ON DELETE RESTRICT,
    FOREIGN KEY (created_by_member_id) REFERENCES household_members(id) ON DELETE RESTRICT,
    CHECK (
        (status = 'draft' AND published_at IS NULL AND archived_at IS NULL)
        OR (status = 'published' AND published_at IS NOT NULL AND archived_at IS NULL)
        OR (status = 'archived' AND archived_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_revisions_series_revision_unique
    ON {WEEKLY_MENU_REVISIONS_TABLE} (series_id, revision_number);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_revisions_id_household
    ON {WEEKLY_MENU_REVISIONS_TABLE} (id, household_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_revisions_single_draft
    ON {WEEKLY_MENU_REVISIONS_TABLE} (series_id)
    WHERE status = 'draft';
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_revisions_single_published
    ON {WEEKLY_MENU_REVISIONS_TABLE} (series_id)
    WHERE status = 'published';

CREATE TABLE IF NOT EXISTS {WEEKLY_MENU_ENTRIES_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    menu_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    local_date TEXT NOT NULL CHECK (
        length(local_date) = 10
        AND substr(local_date, 5, 1) = '-'
        AND substr(local_date, 8, 1) = '-'
    ),
    meal_slot TEXT NOT NULL CHECK (meal_slot IN ({_quoted(WEEKLY_MENU_MEAL_SLOTS)})),
    position INTEGER NOT NULL CHECK (position >= 1),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NULL,
    servings TEXT NULL,
    origin TEXT NOT NULL CHECK (origin IN ({_quoted(WEEKLY_MENU_ENTRY_ORIGINS)})),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (menu_id, household_id)
        REFERENCES {WEEKLY_MENU_REVISIONS_TABLE}(id, household_id)
        ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_entries_menu_slot_position_unique
    ON {WEEKLY_MENU_ENTRIES_TABLE} (menu_id, local_date, meal_slot, position);
CREATE INDEX IF NOT EXISTS idx_weekly_menu_entries_menu_local_date_slot_position
    ON {WEEKLY_MENU_ENTRIES_TABLE} (menu_id, local_date, meal_slot, position);

CREATE TABLE IF NOT EXISTS {WEEKLY_MENU_INGREDIENTS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    menu_entry_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 1),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    quantity_value TEXT NOT NULL CHECK (length(trim(quantity_value)) > 0),
    quantity_unit TEXT NOT NULL CHECK (quantity_unit IN ('g', 'kg', 'ml', 'l', 'piece', 'unitless')),
    recipe_base_servings TEXT NOT NULL CHECK (length(trim(recipe_base_servings)) > 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (menu_entry_id) REFERENCES {WEEKLY_MENU_ENTRIES_TABLE}(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_ingredients_entry_position_unique
    ON {WEEKLY_MENU_INGREDIENTS_TABLE} (menu_entry_id, position);

CREATE TABLE IF NOT EXISTS {WEEKLY_MENU_IDEMPOTENCY_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    household_id TEXT NOT NULL,
    actor_member_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ({_quoted(WEEKLY_MENU_IDEMPOTENCY_OPERATIONS)})),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) BETWEEN 1 AND 128),
    payload_fingerprint TEXT NOT NULL CHECK (length(payload_fingerprint) = 64),
    series_id TEXT NULL,
    revision_id TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (household_id) REFERENCES households(id) ON DELETE RESTRICT,
    FOREIGN KEY (actor_member_id) REFERENCES household_members(id) ON DELETE RESTRICT,
    FOREIGN KEY (series_id) REFERENCES {WEEKLY_MENU_SERIES_TABLE}(id) ON DELETE RESTRICT,
    FOREIGN KEY (revision_id) REFERENCES {WEEKLY_MENU_REVISIONS_TABLE}(id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_menu_idempotency_unique
    ON {WEEKLY_MENU_IDEMPOTENCY_TABLE} (household_id, actor_member_id, operation, idempotency_key);
"""


def new_weekly_menu_series_id() -> str:
    return str(uuid.uuid4())


def new_weekly_menu_revision_id() -> str:
    return str(uuid.uuid4())


def new_weekly_menu_entry_id() -> str:
    return str(uuid.uuid4())


def new_weekly_menu_ingredient_id() -> str:
    return str(uuid.uuid4())


def new_weekly_menu_idempotency_id() -> str:
    return str(uuid.uuid4())


def parse_iso_local_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("expected ISO local date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expected ISO local date") from exc


def normalize_week_start(value: str | date) -> str:
    current = parse_iso_local_date(value)
    monday = current - timedelta(days=current.weekday())
    return monday.isoformat()


def require_monday_week_start(value: str | date) -> str:
    week_start = parse_iso_local_date(value)
    if week_start.weekday() != 0:
        raise ValueError("week_start must be Monday")
    return week_start.isoformat()


def week_dates(week_start: str | date) -> tuple[str, ...]:
    monday = parse_iso_local_date(require_monday_week_start(week_start))
    return tuple((monday + timedelta(days=offset)).isoformat() for offset in range(7))


def require_weekly_menu_series_id(value: str) -> str:
    return require_canonical_uuid4(value)


def require_weekly_menu_revision_id(value: str) -> str:
    return require_canonical_uuid4(value)


def require_weekly_menu_entry_id(value: str) -> str:
    return require_canonical_uuid4(value)


def require_weekly_menu_idempotency_id(value: str) -> str:
    return require_canonical_uuid4(value)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()}


def _table_column_details(conn: sqlite3.Connection, table: str) -> dict[str, dict[str, object]]:
    escaped = table.replace('"', '""')
    result: dict[str, dict[str, object]] = {}
    for row in conn.execute(f'PRAGMA table_info("{escaped}")').fetchall():
        result[str(row[1])] = {
            "type": str(row[2]).upper(),
            "notnull": int(row[3]),
            "default": None if row[4] is None else str(row[4]).strip("'\""),
            "pk": int(row[5]),
        }
    return result


def _foreign_keys(conn: sqlite3.Connection, table: str) -> set[tuple[str, str, str, str]]:
    escaped = table.replace('"', '""')
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(f'PRAGMA foreign_key_list("{escaped}")').fetchall():
        grouped.setdefault(int(row[0]), []).append(row)
    result: set[tuple[str, str, str, str]] = set()
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda item: int(item[1]))
        from_cols = "|".join(str(item[3]) for item in ordered)
        to_cols = "|".join(str(item[4]) for item in ordered)
        target = str(ordered[0][2])
        on_delete = str(ordered[0][6]).upper()
        result.add((from_cols, target, to_cols, on_delete))
    return result


def _index_metadata(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    rows = conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name IS NOT NULL"
    ).fetchall()
    metadata: dict[str, dict[str, object]] = {}
    for row in rows:
        name = str(row[0])
        pragma = conn.execute(f'PRAGMA index_list("{str(row[1]).replace(chr(34), chr(34) * 2)}")').fetchall()
        index_row = next((item for item in pragma if str(item[1]) == name), None)
        if index_row is None:
            continue
        columns = tuple(
            str(info[2])
            for info in conn.execute(f'PRAGMA index_info("{name.replace(chr(34), chr(34) * 2)}")').fetchall()
        )
        sql = None if row[2] is None else str(row[2]).lower()
        metadata[name] = {
            "table": str(row[1]),
            "unique": int(index_row[2]),
            "partial": int(index_row[4]) if len(index_row) > 4 else 0,
            "columns": columns,
            "sql": sql,
        }
    return metadata


def _normalized_create_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return "" if row is None or row[0] is None else " ".join(str(row[0]).lower().split())


def _matches_column_details(actual: dict[str, dict[str, object]], expected: dict[str, dict[str, object]]) -> bool:
    if set(actual) != set(expected):
        return False
    for name, spec in expected.items():
        row = actual.get(name)
        if row is None:
            return False
        for field, value in spec.items():
            if row.get(field) != value:
                return False
    return True


def _matches_index_metadata(actual: dict[str, dict[str, object]], expected: dict[str, dict[str, object]]) -> bool:
    for name, spec in expected.items():
        row = actual.get(name)
        if row is None:
            return False
        if row["table"] != spec["table"] or row["unique"] != spec["unique"] or row["partial"] != spec["partial"]:
            return False
        if tuple(row["columns"]) != tuple(spec["columns"]):
            return False
        where = spec["where"]
        sql = row["sql"]
        if where is None:
            if sql is not None and " where " in sql:
                return False
        else:
            if sql is None or where.lower() not in sql:
                return False
    return True


def _matches_check_snippets(conn: sqlite3.Connection, table: str) -> bool:
    sql = _normalized_create_sql(conn, table)
    return all(" ".join(snippet.lower().split()) in sql for snippet in EXPECTED_CHECK_SNIPPETS[table])


def detect_weekly_menu_schema_state(conn: sqlite3.Connection) -> WeeklyMenuSchemaState:
    tables = _table_names(conn)
    present = EXPECTED_TABLES.intersection(tables)
    if not present:
        return WeeklyMenuSchemaState.NOT_INITIALIZED
    if present != EXPECTED_TABLES:
        legacy_tables = EXPECTED_TABLES - {WEEKLY_MENU_INGREDIENTS_TABLE}
        if present == legacy_tables:
            return (
                WeeklyMenuSchemaState.PARTIAL
                if is_legacy_weekly_menu_schema_without_ingredients(conn)
                else WeeklyMenuSchemaState.INCOMPATIBLE
            )
        return WeeklyMenuSchemaState.PARTIAL
    if not _matches_column_details(_table_column_details(conn, WEEKLY_MENU_SERIES_TABLE), EXPECTED_SERIES_COLUMN_DETAILS):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_column_details(_table_column_details(conn, WEEKLY_MENU_REVISIONS_TABLE), EXPECTED_REVISION_COLUMN_DETAILS):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_column_details(_table_column_details(conn, WEEKLY_MENU_ENTRIES_TABLE), EXPECTED_ENTRY_COLUMN_DETAILS):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_column_details(
        _table_column_details(conn, WEEKLY_MENU_INGREDIENTS_TABLE),
        EXPECTED_INGREDIENT_COLUMN_DETAILS,
    ):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_column_details(_table_column_details(conn, WEEKLY_MENU_IDEMPOTENCY_TABLE), EXPECTED_IDEMPOTENCY_COLUMN_DETAILS):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not EXPECTED_INDEXES.issubset(_index_names(conn)):
        return WeeklyMenuSchemaState.PARTIAL
    if _foreign_keys(conn, WEEKLY_MENU_SERIES_TABLE) != EXPECTED_FOREIGN_KEYS[WEEKLY_MENU_SERIES_TABLE]:
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if _foreign_keys(conn, WEEKLY_MENU_REVISIONS_TABLE) != EXPECTED_FOREIGN_KEYS[WEEKLY_MENU_REVISIONS_TABLE]:
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if _foreign_keys(conn, WEEKLY_MENU_ENTRIES_TABLE) != EXPECTED_FOREIGN_KEYS[WEEKLY_MENU_ENTRIES_TABLE]:
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if _foreign_keys(conn, WEEKLY_MENU_INGREDIENTS_TABLE) != EXPECTED_FOREIGN_KEYS[WEEKLY_MENU_INGREDIENTS_TABLE]:
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if _foreign_keys(conn, WEEKLY_MENU_IDEMPOTENCY_TABLE) != EXPECTED_FOREIGN_KEYS[WEEKLY_MENU_IDEMPOTENCY_TABLE]:
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_index_metadata(_index_metadata(conn), EXPECTED_INDEX_DETAILS):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_check_snippets(conn, WEEKLY_MENU_SERIES_TABLE):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_check_snippets(conn, WEEKLY_MENU_REVISIONS_TABLE):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_check_snippets(conn, WEEKLY_MENU_ENTRIES_TABLE):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_check_snippets(conn, WEEKLY_MENU_INGREDIENTS_TABLE):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not _matches_check_snippets(conn, WEEKLY_MENU_IDEMPOTENCY_TABLE):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    return WeeklyMenuSchemaState.CANONICAL


def is_legacy_weekly_menu_schema_without_ingredients(conn: sqlite3.Connection) -> bool:
    legacy_tables = EXPECTED_TABLES - {WEEKLY_MENU_INGREDIENTS_TABLE}
    if EXPECTED_TABLES.intersection(_table_names(conn)) != legacy_tables:
        return False
    table_specs = (
        (WEEKLY_MENU_SERIES_TABLE, EXPECTED_SERIES_COLUMN_DETAILS),
        (WEEKLY_MENU_REVISIONS_TABLE, EXPECTED_REVISION_COLUMN_DETAILS),
        (WEEKLY_MENU_ENTRIES_TABLE, EXPECTED_ENTRY_COLUMN_DETAILS),
        (WEEKLY_MENU_IDEMPOTENCY_TABLE, EXPECTED_IDEMPOTENCY_COLUMN_DETAILS),
    )
    if any(
        not _matches_column_details(_table_column_details(conn, table), details)
        for table, details in table_specs
    ):
        return False
    if any(
        _foreign_keys(conn, table) != EXPECTED_FOREIGN_KEYS[table]
        for table, _details in table_specs
    ):
        return False
    legacy_indexes = EXPECTED_INDEXES - {
        "idx_weekly_menu_ingredients_entry_position_unique"
    }
    if not legacy_indexes.issubset(_index_names(conn)):
        return False
    metadata = _index_metadata(conn)
    if not _matches_index_metadata(
        metadata,
        {name: EXPECTED_INDEX_DETAILS[name] for name in legacy_indexes},
    ):
        return False
    return all(_matches_check_snippets(conn, table) for table, _details in table_specs)


def weekly_menu_schema_tables_present(conn: sqlite3.Connection) -> set[str]:
    return EXPECTED_TABLES.intersection(_table_names(conn))


def is_valid_week_start(value: str) -> bool:
    try:
        require_monday_week_start(value)
    except ValueError:
        return False
    return True


def is_valid_local_date(value: str) -> bool:
    try:
        parse_iso_local_date(value)
    except ValueError:
        return False
    return True


def validate_weekly_menu_audit_row_ids(*values: str) -> bool:
    return all(is_canonical_uuid4(value) for value in values)
