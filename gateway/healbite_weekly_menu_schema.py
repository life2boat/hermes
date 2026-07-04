from __future__ import annotations

import sqlite3
import uuid
from datetime import date, timedelta
from enum import Enum

from gateway.healbite_household_schema import is_canonical_uuid4, require_canonical_uuid4

WEEKLY_MENU_SERIES_TABLE = "household_weekly_menu_series"
WEEKLY_MENU_REVISIONS_TABLE = "household_weekly_menus"
WEEKLY_MENU_ENTRIES_TABLE = "household_weekly_menu_entries"
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


def detect_weekly_menu_schema_state(conn: sqlite3.Connection) -> WeeklyMenuSchemaState:
    tables = _table_names(conn)
    present = EXPECTED_TABLES.intersection(tables)
    if not present:
        return WeeklyMenuSchemaState.NOT_INITIALIZED
    if present != EXPECTED_TABLES:
        return WeeklyMenuSchemaState.PARTIAL
    if not EXPECTED_SERIES_COLUMNS.issubset(_table_columns(conn, WEEKLY_MENU_SERIES_TABLE)):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not EXPECTED_REVISION_COLUMNS.issubset(_table_columns(conn, WEEKLY_MENU_REVISIONS_TABLE)):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not EXPECTED_ENTRY_COLUMNS.issubset(_table_columns(conn, WEEKLY_MENU_ENTRIES_TABLE)):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not EXPECTED_IDEMPOTENCY_COLUMNS.issubset(_table_columns(conn, WEEKLY_MENU_IDEMPOTENCY_TABLE)):
        return WeeklyMenuSchemaState.INCOMPATIBLE
    if not EXPECTED_INDEXES.issubset(_index_names(conn)):
        return WeeklyMenuSchemaState.PARTIAL
    return WeeklyMenuSchemaState.CANONICAL


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
