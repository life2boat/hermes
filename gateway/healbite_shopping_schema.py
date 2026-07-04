from __future__ import annotations

import re
import sqlite3
import uuid
from enum import Enum

from gateway.healbite_household_schema import (
    HOUSEHOLD_MEMBERS_TABLE,
    HOUSEHOLDS_TABLE,
    is_canonical_uuid4,
    require_canonical_uuid4,
)
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WeeklyMenuSchemaState,
    detect_weekly_menu_schema_state,
    is_valid_week_start,
)

SHOPPING_LISTS_TABLE = "household_shopping_lists"
SHOPPING_ITEMS_TABLE = "household_shopping_items"
SHOPPING_IDEMPOTENCY_TABLE = "household_shopping_idempotency"

_QUANTITY_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]{1,3})?$")
_MAX_QUANTITY_PRECISION = 12
_MAX_QUANTITY_SCALE = 3


class ShoppingSchemaState(str, Enum):
    NOT_INITIALIZED = "not_initialized"
    CANONICAL = "canonical"
    PARTIAL = "partial"
    INCOMPATIBLE = "incompatible"
    DEPENDENCY_MISSING = "dependency_missing"


class ShoppingListStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class ShoppingItemOrigin(str, Enum):
    MENU_GENERATED = "menu_generated"
    MANUAL = "manual"


class ShoppingItemOverrideState(str, Enum):
    NONE = "none"
    MANUALIZED = "manualized"


class ShoppingUnit(str, Enum):
    G = "g"
    KG = "kg"
    ML = "ml"
    L = "l"
    PIECE = "piece"
    PACKAGE = "package"
    UNITLESS = "unitless"
    UNKNOWN = "unknown"


class ShoppingUnitFamily(str, Enum):
    MASS = "mass"
    VOLUME = "volume"
    COUNT = "count"
    PACKAGE = "package"
    UNITLESS = "unitless"
    UNKNOWN = "unknown"


class ShoppingIdempotencyOperation(str, Enum):
    CREATE_LIST = "create_list"
    ACTIVATE_LIST = "activate_list"
    COMPLETE_LIST = "complete_list"
    ARCHIVE_LIST = "archive_list"
    ADD_MANUAL_ITEM = "add_manual_item"
    UPDATE_ITEM = "update_item"
    SET_ITEM_CHECKED = "set_item_checked"
    REGENERATE_GENERATED_ITEMS = "regenerate_generated_items"


SHOPPING_LIST_STATUSES = tuple(item.value for item in ShoppingListStatus)
SHOPPING_ITEM_ORIGINS = tuple(item.value for item in ShoppingItemOrigin)
SHOPPING_ITEM_OVERRIDE_STATES = tuple(item.value for item in ShoppingItemOverrideState)
SHOPPING_UNITS = tuple(item.value for item in ShoppingUnit)
SHOPPING_IDEMPOTENCY_OPERATIONS = tuple(item.value for item in ShoppingIdempotencyOperation)

EXPECTED_TABLES = {
    SHOPPING_LISTS_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_IDEMPOTENCY_TABLE,
}
EXPECTED_INDEXES = {
    "idx_household_shopping_lists_id_household",
    "idx_household_shopping_lists_active_per_household_week",
    "idx_household_shopping_lists_household_week_status",
    "idx_household_shopping_items_list_position_unique",
    "idx_household_shopping_items_list_origin",
    "idx_household_shopping_items_generated_dedup_unique",
    "idx_household_shopping_idempotency_unique",
}
EXPECTED_LIST_COLUMNS = {
    "id",
    "household_id",
    "week_start",
    "source_menu_id",
    "source_menu_revision",
    "status",
    "created_by_member_id",
    "created_at",
    "updated_at",
    "completed_at",
    "archived_at",
    "version",
}
EXPECTED_ITEM_COLUMNS = {
    "id",
    "shopping_list_id",
    "household_id",
    "normalized_name",
    "display_name",
    "quantity_value",
    "quantity_unit_normalized",
    "quantity_unit_display",
    "category",
    "position",
    "checked_state",
    "origin",
    "override_state",
    "source_menu_entry_id",
    "normalization_version",
    "dedup_fingerprint",
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
    "shopping_list_id",
    "shopping_item_id",
    "created_at",
}
EXPECTED_LIST_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "household_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "week_start": {"type": "TEXT", "notnull": 1, "pk": 0},
    "source_menu_id": {"type": "TEXT", "notnull": 0, "pk": 0},
    "source_menu_revision": {"type": "INTEGER", "notnull": 0, "pk": 0},
    "status": {"type": "TEXT", "notnull": 1, "pk": 0},
    "created_by_member_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "updated_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "completed_at": {"type": "TEXT", "notnull": 0, "pk": 0},
    "archived_at": {"type": "TEXT", "notnull": 0, "pk": 0},
    "version": {"type": "INTEGER", "notnull": 1, "pk": 0, "default": "1"},
}
EXPECTED_ITEM_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "shopping_list_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "household_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "normalized_name": {"type": "TEXT", "notnull": 1, "pk": 0},
    "display_name": {"type": "TEXT", "notnull": 1, "pk": 0},
    "quantity_value": {"type": "TEXT", "notnull": 0, "pk": 0},
    "quantity_unit_normalized": {"type": "TEXT", "notnull": 1, "pk": 0},
    "quantity_unit_display": {"type": "TEXT", "notnull": 1, "pk": 0},
    "category": {"type": "TEXT", "notnull": 0, "pk": 0},
    "position": {"type": "INTEGER", "notnull": 1, "pk": 0},
    "checked_state": {"type": "INTEGER", "notnull": 1, "pk": 0, "default": "0"},
    "origin": {"type": "TEXT", "notnull": 1, "pk": 0},
    "override_state": {"type": "TEXT", "notnull": 1, "pk": 0, "default": "'none'"},
    "source_menu_entry_id": {"type": "TEXT", "notnull": 0, "pk": 0},
    "normalization_version": {"type": "INTEGER", "notnull": 1, "pk": 0, "default": "1"},
    "dedup_fingerprint": {"type": "TEXT", "notnull": 1, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "updated_at": {"type": "TEXT", "notnull": 1, "pk": 0},
    "version": {"type": "INTEGER", "notnull": 1, "pk": 0, "default": "1"},
}
EXPECTED_IDEMPOTENCY_COLUMN_DETAILS = {
    "id": {"type": "TEXT", "notnull": 0, "pk": 1},
    "household_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "actor_member_id": {"type": "TEXT", "notnull": 1, "pk": 0},
    "operation": {"type": "TEXT", "notnull": 1, "pk": 0},
    "idempotency_key": {"type": "TEXT", "notnull": 1, "pk": 0},
    "payload_fingerprint": {"type": "TEXT", "notnull": 1, "pk": 0},
    "shopping_list_id": {"type": "TEXT", "notnull": 0, "pk": 0},
    "shopping_item_id": {"type": "TEXT", "notnull": 0, "pk": 0},
    "created_at": {"type": "TEXT", "notnull": 1, "pk": 0},
}
EXPECTED_FOREIGN_KEYS = {
    SHOPPING_LISTS_TABLE: {
        (HOUSEHOLDS_TABLE, ("household_id",), ("id",), "RESTRICT"),
        (HOUSEHOLD_MEMBERS_TABLE, ("created_by_member_id",), ("id",), "RESTRICT"),
        (WEEKLY_MENU_REVISIONS_TABLE, ("source_menu_id", "household_id"), ("id", "household_id"), "RESTRICT"),
    },
    SHOPPING_ITEMS_TABLE: {
        (SHOPPING_LISTS_TABLE, ("shopping_list_id", "household_id"), ("id", "household_id"), "RESTRICT"),
    },
    SHOPPING_IDEMPOTENCY_TABLE: {
        (HOUSEHOLDS_TABLE, ("household_id",), ("id",), "RESTRICT"),
        (HOUSEHOLD_MEMBERS_TABLE, ("actor_member_id",), ("id",), "RESTRICT"),
        (SHOPPING_LISTS_TABLE, ("shopping_list_id",), ("id",), "RESTRICT"),
        (SHOPPING_ITEMS_TABLE, ("shopping_item_id",), ("id",), "RESTRICT"),
    },
}
EXPECTED_INDEX_DETAILS = {
    "idx_household_shopping_lists_id_household": {
        "table": SHOPPING_LISTS_TABLE,
        "unique": 1,
        "partial": 0,
        "columns": ("id", "household_id"),
        "where": None,
    },
    "idx_household_shopping_lists_active_per_household_week": {
        "table": SHOPPING_LISTS_TABLE,
        "unique": 1,
        "partial": 1,
        "columns": ("household_id", "week_start"),
        "where": "status = 'active'",
    },
    "idx_household_shopping_lists_household_week_status": {
        "table": SHOPPING_LISTS_TABLE,
        "unique": 0,
        "partial": 0,
        "columns": ("household_id", "week_start", "status", "created_at"),
        "where": None,
    },
    "idx_household_shopping_items_list_position_unique": {
        "table": SHOPPING_ITEMS_TABLE,
        "unique": 1,
        "partial": 0,
        "columns": ("shopping_list_id", "position"),
        "where": None,
    },
    "idx_household_shopping_items_list_origin": {
        "table": SHOPPING_ITEMS_TABLE,
        "unique": 0,
        "partial": 0,
        "columns": ("shopping_list_id", "origin", "override_state", "position"),
        "where": None,
    },
    "idx_household_shopping_items_generated_dedup_unique": {
        "table": SHOPPING_ITEMS_TABLE,
        "unique": 1,
        "partial": 1,
        "columns": ("shopping_list_id", "dedup_fingerprint"),
        "where": "origin = 'menu_generated' AND override_state = 'none'",
    },
    "idx_household_shopping_idempotency_unique": {
        "table": SHOPPING_IDEMPOTENCY_TABLE,
        "unique": 1,
        "partial": 0,
        "columns": ("household_id", "actor_member_id", "operation", "idempotency_key"),
        "where": None,
    },
}
EXPECTED_CHECK_SNIPPETS = {
    SHOPPING_LISTS_TABLE: (
        "strftime('%w', week_start) = '1'",
        "source_menu_revision >= 1",
        "status IN ('draft', 'active', 'completed', 'archived')",
        "CHECK (version >= 1)",
        "status = 'draft'",
        "status = 'active'",
        "status = 'completed'",
        "status = 'archived'",
    ),
    SHOPPING_ITEMS_TABLE: (
        "length(trim(display_name)) > 0",
        "length(trim(normalized_name)) > 0",
        "checked_state IN (0, 1)",
        "origin IN ('menu_generated', 'manual')",
        "override_state IN ('none', 'manualized')",
        "normalization_version >= 1",
        "length(dedup_fingerprint) = 64",
        "position >= 1",
        "quantity_unit_normalized IN ('g', 'kg', 'ml', 'l', 'piece', 'package', 'unitless', 'unknown')",
        "quantity_value IS NULL OR",
    ),
    SHOPPING_IDEMPOTENCY_TABLE: (
        "operation IN ('create_list', 'activate_list', 'complete_list', 'archive_list', 'add_manual_item', 'update_item', 'set_item_checked', 'regenerate_generated_items')",
        "length(trim(idempotency_key)) BETWEEN 1 AND 128",
        "length(payload_fingerprint) = 64",
    ),
}


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


SHOPPING_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {SHOPPING_LISTS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    household_id TEXT NOT NULL,
    week_start TEXT NOT NULL CHECK (
        length(week_start) = 10
        AND substr(week_start, 5, 1) = '-'
        AND substr(week_start, 8, 1) = '-'
        AND strftime('%w', week_start) = '1'
    ),
    source_menu_id TEXT NULL,
    source_menu_revision INTEGER NULL CHECK (source_menu_revision IS NULL OR source_menu_revision >= 1),
    status TEXT NOT NULL CHECK (status IN ({_quoted(SHOPPING_LIST_STATUSES)})),
    created_by_member_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NULL,
    archived_at TEXT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (household_id) REFERENCES {HOUSEHOLDS_TABLE}(id) ON DELETE RESTRICT,
    FOREIGN KEY (created_by_member_id) REFERENCES {HOUSEHOLD_MEMBERS_TABLE}(id) ON DELETE RESTRICT,
    FOREIGN KEY (source_menu_id, household_id)
        REFERENCES {WEEKLY_MENU_REVISIONS_TABLE}(id, household_id)
        ON DELETE RESTRICT,
    CHECK (
        (source_menu_id IS NULL AND source_menu_revision IS NULL)
        OR source_menu_id IS NOT NULL
    ),
    CHECK (
        (status = 'draft' AND completed_at IS NULL AND archived_at IS NULL)
        OR (status = 'active' AND completed_at IS NULL AND archived_at IS NULL)
        OR (status = 'completed' AND completed_at IS NOT NULL AND archived_at IS NULL)
        OR (status = 'archived' AND archived_at IS NOT NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_shopping_lists_id_household
    ON {SHOPPING_LISTS_TABLE} (id, household_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_shopping_lists_active_per_household_week
    ON {SHOPPING_LISTS_TABLE} (household_id, week_start)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_household_shopping_lists_household_week_status
    ON {SHOPPING_LISTS_TABLE} (household_id, week_start, status, created_at);

CREATE TABLE IF NOT EXISTS {SHOPPING_ITEMS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    shopping_list_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    normalized_name TEXT NOT NULL CHECK (length(trim(normalized_name)) > 0),
    display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
    quantity_value TEXT NULL CHECK (
        quantity_value IS NULL OR (
            trim(quantity_value) = quantity_value
            AND quantity_value NOT LIKE '%,%'
            AND quantity_value NOT LIKE '%e%'
            AND quantity_value NOT LIKE '%E%'
            AND quantity_value NOT LIKE '%+%'
            AND quantity_value NOT LIKE '%-%'
            AND replace(quantity_value, '.', '') GLOB '[0-9]*'
            AND length(replace(quantity_value, '.', '')) BETWEEN 1 AND 12
            AND (instr(quantity_value, '.') = 0 OR length(substr(quantity_value, instr(quantity_value, '.') + 1)) BETWEEN 1 AND 3)
            AND (instr(quantity_value, '.') = 0 OR substr(quantity_value, -1) != '0')
        )
    ),
    quantity_unit_normalized TEXT NOT NULL CHECK (quantity_unit_normalized IN ({_quoted(SHOPPING_UNITS)})),
    quantity_unit_display TEXT NOT NULL CHECK (length(trim(quantity_unit_display)) BETWEEN 1 AND 32),
    category TEXT NULL,
    position INTEGER NOT NULL CHECK (position >= 1),
    checked_state INTEGER NOT NULL DEFAULT 0 CHECK (checked_state IN (0, 1)),
    origin TEXT NOT NULL CHECK (origin IN ({_quoted(SHOPPING_ITEM_ORIGINS)})),
    override_state TEXT NOT NULL DEFAULT 'none' CHECK (override_state IN ({_quoted(SHOPPING_ITEM_OVERRIDE_STATES)})),
    source_menu_entry_id TEXT NULL,
    normalization_version INTEGER NOT NULL DEFAULT 1 CHECK (normalization_version >= 1),
    dedup_fingerprint TEXT NOT NULL CHECK (length(dedup_fingerprint) = 64),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (shopping_list_id, household_id)
        REFERENCES {SHOPPING_LISTS_TABLE}(id, household_id)
        ON DELETE RESTRICT,
    CHECK (
        quantity_value IS NOT NULL OR quantity_unit_normalized = 'unknown'
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_shopping_items_list_position_unique
    ON {SHOPPING_ITEMS_TABLE} (shopping_list_id, position);
CREATE INDEX IF NOT EXISTS idx_household_shopping_items_list_origin
    ON {SHOPPING_ITEMS_TABLE} (shopping_list_id, origin, override_state, position);
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_shopping_items_generated_dedup_unique
    ON {SHOPPING_ITEMS_TABLE} (shopping_list_id, dedup_fingerprint)
    WHERE origin = 'menu_generated' AND override_state = 'none';

CREATE TABLE IF NOT EXISTS {SHOPPING_IDEMPOTENCY_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    household_id TEXT NOT NULL,
    actor_member_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ({_quoted(SHOPPING_IDEMPOTENCY_OPERATIONS)})),
    idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) BETWEEN 1 AND 128),
    payload_fingerprint TEXT NOT NULL CHECK (length(payload_fingerprint) = 64),
    shopping_list_id TEXT NULL,
    shopping_item_id TEXT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (household_id) REFERENCES {HOUSEHOLDS_TABLE}(id) ON DELETE RESTRICT,
    FOREIGN KEY (actor_member_id) REFERENCES {HOUSEHOLD_MEMBERS_TABLE}(id) ON DELETE RESTRICT,
    FOREIGN KEY (shopping_list_id) REFERENCES {SHOPPING_LISTS_TABLE}(id) ON DELETE RESTRICT,
    FOREIGN KEY (shopping_item_id) REFERENCES {SHOPPING_ITEMS_TABLE}(id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_shopping_idempotency_unique
    ON {SHOPPING_IDEMPOTENCY_TABLE} (household_id, actor_member_id, operation, idempotency_key);
"""


def new_shopping_list_id() -> str:
    return str(uuid.uuid4())


def new_shopping_item_id() -> str:
    return str(uuid.uuid4())


def new_shopping_idempotency_id() -> str:
    return str(uuid.uuid4())


def require_shopping_list_id(value: str) -> str:
    return require_canonical_uuid4(value)


def require_shopping_item_id(value: str) -> str:
    return require_canonical_uuid4(value)


def require_shopping_idempotency_id(value: str) -> str:
    return require_canonical_uuid4(value)


def normalize_quantity_value(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("invalid quantity")
    raw = str(value)
    if raw.strip() != raw:
        raise ValueError("invalid quantity")
    text = raw
    if not text:
        raise ValueError("invalid quantity")
    if "," in text or "e" in text.lower() or text.startswith(("+", "-")):
        raise ValueError("invalid quantity")
    if not _QUANTITY_PATTERN.fullmatch(text):
        raise ValueError("invalid quantity")
    integer, dot, fraction = text.partition(".")
    integer = integer.lstrip("0") or "0"
    if fraction:
        fraction = fraction.rstrip("0")
    canonical = integer if not fraction else f"{integer}.{fraction}"
    if canonical == "":
        canonical = "0"
    digits = canonical.replace(".", "")
    scale = len(canonical.partition(".")[2])
    if len(digits) > _MAX_QUANTITY_PRECISION or scale > _MAX_QUANTITY_SCALE:
        raise ValueError("invalid quantity")
    if not _QUANTITY_PATTERN.fullmatch(canonical):
        raise ValueError("invalid quantity")
    return canonical


def is_valid_quantity_value(value: object | None) -> bool:
    try:
        normalize_quantity_value(value)
    except ValueError:
        return False
    return True


def normalize_shopping_unit(value: ShoppingUnit | str) -> ShoppingUnit:
    if isinstance(value, ShoppingUnit):
        return value
    try:
        return ShoppingUnit(str(value).strip().lower())
    except ValueError as exc:
        raise ValueError("invalid shopping unit") from exc


def shopping_unit_family(unit: ShoppingUnit | str) -> ShoppingUnitFamily:
    normalized = normalize_shopping_unit(unit)
    if normalized in (ShoppingUnit.G, ShoppingUnit.KG):
        return ShoppingUnitFamily.MASS
    if normalized in (ShoppingUnit.ML, ShoppingUnit.L):
        return ShoppingUnitFamily.VOLUME
    if normalized is ShoppingUnit.PIECE:
        return ShoppingUnitFamily.COUNT
    if normalized is ShoppingUnit.PACKAGE:
        return ShoppingUnitFamily.PACKAGE
    if normalized is ShoppingUnit.UNITLESS:
        return ShoppingUnitFamily.UNITLESS
    return ShoppingUnitFamily.UNKNOWN


def units_are_compatible(left: ShoppingUnit | str, right: ShoppingUnit | str) -> bool:
    left_unit = normalize_shopping_unit(left)
    right_unit = normalize_shopping_unit(right)
    if left_unit is ShoppingUnit.UNKNOWN or right_unit is ShoppingUnit.UNKNOWN:
        return False
    return shopping_unit_family(left_unit) is shopping_unit_family(right_unit)


def quantity_contract_is_valid(quantity_value: object | None, unit: ShoppingUnit | str) -> bool:
    try:
        canonical_quantity = normalize_quantity_value(quantity_value)
        normalized_unit = normalize_shopping_unit(unit)
    except ValueError:
        return False
    if canonical_quantity is None:
        return normalized_unit is ShoppingUnit.UNKNOWN
    return True


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _index_rows(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for name, table_name, sql in conn.execute(
        "SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_autoindex_%'"
    ).fetchall():
        rows[str(name)] = {
            "table": str(table_name),
            "sql": str(sql) if sql is not None else "",
        }
    return rows


def _table_info(conn: sqlite3.Connection, table_name: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall():
        result[str(row[1])] = {
            "type": str(row[2]).upper(),
            "notnull": int(row[3]),
            "default": None if row[4] is None else str(row[4]),
            "pk": int(row[5]),
        }
    return result


def _foreign_keys(conn: sqlite3.Connection, table_name: str) -> set[tuple[str, tuple[str, ...], tuple[str, ...], str]]:
    grouped: dict[int, dict[str, object]] = {}
    for row in conn.execute(f"PRAGMA foreign_key_list({table_name})").fetchall():
        group = grouped.setdefault(
            int(row[0]),
            {"table": str(row[2]), "from": [], "to": [], "on_delete": str(row[6]).upper()},
        )
        group["from"].append(str(row[3]))
        group["to"].append(str(row[4]))
    return {
        (
            str(data["table"]),
            tuple(data["from"]),
            tuple(data["to"]),
            str(data["on_delete"]),
        )
        for data in grouped.values()
    }


def _index_spec(conn: sqlite3.Connection, index_name: str, table_name: str, sql: str) -> dict[str, object]:
    index_list_row = None
    for row in conn.execute(f"PRAGMA index_list({table_name})").fetchall():
        if str(row[1]) == index_name:
            index_list_row = row
            break
    if index_list_row is None:
        raise ValueError(f"missing index {index_name}")
    columns = tuple(str(row[2]) for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall())
    where = None
    upper_sql = sql.upper()
    if " WHERE " in upper_sql:
        where = sql.split(" WHERE ", 1)[1].strip()
    return {
        "table": table_name,
        "unique": int(index_list_row[2]),
        "partial": int(index_list_row[4]),
        "columns": columns,
        "where": where,
    }


def _sql_for_table(conn: sqlite3.Connection, table_name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?",
        (table_name,),
    ).fetchone()
    return "" if row is None or row[0] is None else str(row[0])


def _has_expected_columns(
    actual: dict[str, dict[str, object]],
    expected_names: set[str],
    expected_details: dict[str, dict[str, object]],
) -> bool:
    if set(actual) != expected_names:
        return False
    for name, details in expected_details.items():
        actual_details = actual.get(name)
        if actual_details is None:
            return False
        for key, expected_value in details.items():
            if actual_details.get(key) != expected_value:
                return False
    return True


def detect_shopping_schema_state(conn: sqlite3.Connection) -> ShoppingSchemaState:
    tables = _table_names(conn)
    required_household_tables = {HOUSEHOLDS_TABLE, HOUSEHOLD_MEMBERS_TABLE}
    if not required_household_tables.issubset(tables):
        return ShoppingSchemaState.DEPENDENCY_MISSING
    if detect_weekly_menu_schema_state(conn) is not WeeklyMenuSchemaState.CANONICAL:
        return ShoppingSchemaState.DEPENDENCY_MISSING
    present_tables = EXPECTED_TABLES & tables
    if not present_tables:
        return ShoppingSchemaState.NOT_INITIALIZED
    if present_tables != EXPECTED_TABLES:
        return ShoppingSchemaState.PARTIAL
    if not _has_expected_columns(_table_info(conn, SHOPPING_LISTS_TABLE), EXPECTED_LIST_COLUMNS, EXPECTED_LIST_COLUMN_DETAILS):
        return ShoppingSchemaState.INCOMPATIBLE
    if not _has_expected_columns(_table_info(conn, SHOPPING_ITEMS_TABLE), EXPECTED_ITEM_COLUMNS, EXPECTED_ITEM_COLUMN_DETAILS):
        return ShoppingSchemaState.INCOMPATIBLE
    if not _has_expected_columns(
        _table_info(conn, SHOPPING_IDEMPOTENCY_TABLE),
        EXPECTED_IDEMPOTENCY_COLUMNS,
        EXPECTED_IDEMPOTENCY_COLUMN_DETAILS,
    ):
        return ShoppingSchemaState.INCOMPATIBLE
    if _foreign_keys(conn, SHOPPING_LISTS_TABLE) != EXPECTED_FOREIGN_KEYS[SHOPPING_LISTS_TABLE]:
        return ShoppingSchemaState.INCOMPATIBLE
    if _foreign_keys(conn, SHOPPING_ITEMS_TABLE) != EXPECTED_FOREIGN_KEYS[SHOPPING_ITEMS_TABLE]:
        return ShoppingSchemaState.INCOMPATIBLE
    if _foreign_keys(conn, SHOPPING_IDEMPOTENCY_TABLE) != EXPECTED_FOREIGN_KEYS[SHOPPING_IDEMPOTENCY_TABLE]:
        return ShoppingSchemaState.INCOMPATIBLE
    index_rows = _index_rows(conn)
    if not EXPECTED_INDEXES.issubset(index_rows):
        return ShoppingSchemaState.INCOMPATIBLE
    for index_name, details in EXPECTED_INDEX_DETAILS.items():
        actual = _index_spec(conn, index_name, str(details["table"]), str(index_rows[index_name]["sql"]))
        if actual != details:
            return ShoppingSchemaState.INCOMPATIBLE
    for table_name, snippets in EXPECTED_CHECK_SNIPPETS.items():
        table_sql = _sql_for_table(conn, table_name)
        if not table_sql:
            return ShoppingSchemaState.INCOMPATIBLE
        for snippet in snippets:
            if snippet not in table_sql:
                return ShoppingSchemaState.INCOMPATIBLE
    return ShoppingSchemaState.CANONICAL
