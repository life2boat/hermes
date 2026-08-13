from __future__ import annotations

import hashlib
import re
import sqlite3
from enum import Enum
from typing import Callable, Sequence


FACTS_TABLE = "memory_os_facts"
OUTBOX_TABLE = "memory_os_vector_sync_outbox"
META_TABLE = "memory_os_vector_sync_meta"

MEMORY_CONVERGENCE_MIGRATION_ID = "memory-convergence-schema-v1"


class MemorySchemaClassification(str, Enum):
    ABSENT = "ABSENT"
    KNOWN_COMPATIBLE_PARTIAL = "KNOWN_COMPATIBLE_PARTIAL"
    CURRENT = "CURRENT"
    INCOMPATIBLE = "INCOMPATIBLE"


FACTS_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {FACTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    entity TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    vector_revision INTEGER NOT NULL DEFAULT 1,
    source TEXT,
    trust_score REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
""".strip()

FACTS_USER_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{FACTS_TABLE}_user_id ON {FACTS_TABLE}(user_id)"
)
FACTS_KEY_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{FACTS_TABLE}_user_entity_key "
    f"ON {FACTS_TABLE}(user_id, entity, key)"
)

OUTBOX_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {OUTBOX_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('UPSERT', 'DELETE')),
    fact_revision INTEGER NOT NULL CHECK(fact_revision > 0),
    state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK(state IN ('PENDING', 'RETRY', 'BLOCKED')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_error_class TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(user_id, fact_id, fact_revision, operation)
)
""".strip()

OUTBOX_READY_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{OUTBOX_TABLE}_ready "
    f"ON {OUTBOX_TABLE}(state, next_attempt_at, id)"
)
OUTBOX_FACT_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{OUTBOX_TABLE}_fact_revision "
    f"ON {OUTBOX_TABLE}(user_id, fact_id, fact_revision)"
)

META_CREATE_SQL = f"""
CREATE TABLE IF NOT EXISTS {META_TABLE} (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    schema_seeded INTEGER NOT NULL DEFAULT 0 CHECK(schema_seeded IN (0, 1)),
    processed_count INTEGER NOT NULL DEFAULT 0,
    succeeded_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    superseded_count INTEGER NOT NULL DEFAULT 0,
    last_success_at REAL,
    last_reconciliation_at REAL,
    last_error_class TEXT
)
""".strip()

MEMORY_CONVERGENCE_SCHEMA_STATEMENTS: tuple[str, ...] = (
    FACTS_CREATE_SQL,
    FACTS_USER_INDEX_SQL,
    FACTS_KEY_INDEX_SQL,
    OUTBOX_CREATE_SQL,
    OUTBOX_READY_INDEX_SQL,
    OUTBOX_FACT_INDEX_SQL,
    META_CREATE_SQL,
)

_MIGRATION_RECIPE = "\n".join(
    (
        f"migration_id={MEMORY_CONVERGENCE_MIGRATION_ID}",
        *MEMORY_CONVERGENCE_SCHEMA_STATEMENTS,
        f"ALTER TABLE {FACTS_TABLE} ADD COLUMN vector_revision INTEGER NOT NULL DEFAULT 1",
        "INSERT META SINGLETON IF ABSENT",
        "SEED ONE UPSERT INTENT PER LEGACY FACT AT ITS CANONICAL REVISION",
        "MARK META SCHEMA_SEEDED ONLY AFTER SEED",
    )
)
MEMORY_CONVERGENCE_MIGRATION_SOURCE = _MIGRATION_RECIPE + "\n"
MEMORY_CONVERGENCE_MIGRATION_SHA256 = hashlib.sha256(
    MEMORY_CONVERGENCE_MIGRATION_SOURCE.encode("utf-8")
).hexdigest()

_EXPECTED_COLUMNS: dict[str, tuple[tuple[str, str, int, str | None, int], ...]] = {
    FACTS_TABLE: (
        ("id", "INTEGER", 0, None, 1),
        ("user_id", "INTEGER", 1, None, 0),
        ("entity", "TEXT", 1, None, 0),
        ("key", "TEXT", 1, None, 0),
        ("value", "TEXT", 1, None, 0),
        ("vector_revision", "INTEGER", 1, "1", 0),
        ("source", "TEXT", 0, None, 0),
        ("trust_score", "REAL", 1, "0.5", 0),
        ("created_at", "TEXT", 1, "CURRENT_TIMESTAMP", 0),
        ("updated_at", "TEXT", 1, "CURRENT_TIMESTAMP", 0),
    ),
    OUTBOX_TABLE: (
        ("id", "INTEGER", 0, None, 1),
        ("user_id", "INTEGER", 1, None, 0),
        ("fact_id", "INTEGER", 1, None, 0),
        ("operation", "TEXT", 1, None, 0),
        ("fact_revision", "INTEGER", 1, None, 0),
        ("state", "TEXT", 1, "'PENDING'", 0),
        ("attempt_count", "INTEGER", 1, "0", 0),
        ("next_attempt_at", "REAL", 1, "0", 0),
        ("last_error_class", "TEXT", 0, None, 0),
        ("created_at", "REAL", 1, None, 0),
        ("updated_at", "REAL", 1, None, 0),
    ),
    META_TABLE: (
        ("singleton_id", "INTEGER", 0, None, 1),
        ("schema_seeded", "INTEGER", 1, "0", 0),
        ("processed_count", "INTEGER", 1, "0", 0),
        ("succeeded_count", "INTEGER", 1, "0", 0),
        ("failed_count", "INTEGER", 1, "0", 0),
        ("superseded_count", "INTEGER", 1, "0", 0),
        ("last_success_at", "REAL", 0, None, 0),
        ("last_reconciliation_at", "REAL", 0, None, 0),
        ("last_error_class", "TEXT", 0, None, 0),
    ),
}

_LEGACY_FACTS_COLUMNS = tuple(
    column for column in _EXPECTED_COLUMNS[FACTS_TABLE] if column[0] != "vector_revision"
)
_VECTOR_REVISION_COLUMN = next(
    column for column in _EXPECTED_COLUMNS[FACTS_TABLE] if column[0] == "vector_revision"
)
# SQLite appends an additive ALTER TABLE column. Both layouts are canonical:
# fresh databases use target CREATE order, while legacy upgrades preserve every
# pre-existing column position and append vector_revision.
_MIGRATED_LEGACY_FACTS_COLUMNS = (*_LEGACY_FACTS_COLUMNS, _VECTOR_REVISION_COLUMN)

_EXPECTED_INDEX_COLUMNS = {
    f"idx_{FACTS_TABLE}_user_id": ("user_id",),
    f"idx_{FACTS_TABLE}_user_entity_key": ("user_id", "entity", "key"),
    f"idx_{OUTBOX_TABLE}_ready": ("state", "next_attempt_at", "id"),
    f"idx_{OUTBOX_TABLE}_fact_revision": ("user_id", "fact_id", "fact_revision"),
}


def _columns(conn: sqlite3.Connection, table: str) -> tuple[tuple[str, str, int, str | None, int], ...]:
    return tuple(
        (
            str(row[1]),
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    )


def _object_types(conn: sqlite3.Connection) -> dict[str, str]:
    names = (*_EXPECTED_COLUMNS, *_EXPECTED_INDEX_COLUMNS)
    placeholders = ",".join("?" for _ in names)
    return {
        str(row[1]): str(row[0]).lower()
        for row in conn.execute(
            f"SELECT type, name FROM sqlite_master WHERE name IN ({placeholders})",
            names,
        ).fetchall()
    }


def _index_columns(conn: sqlite3.Connection, index: str) -> tuple[str, ...]:
    return tuple(
        str(row[2])
        for row in conn.execute(f'PRAGMA index_info("{index}")').fetchall()
    )


def _normalized_schema_sql(value: str) -> str:
    normalized = re.sub(r"\bif\s+not\s+exists\b", "", value, flags=re.IGNORECASE)
    return " ".join(normalized.lower().split())


def _object_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    return "" if row is None or row[0] is None else _normalized_schema_sql(str(row[0]))


def classify_memory_convergence_schema(
    conn: sqlite3.Connection,
) -> MemorySchemaClassification:
    objects = _object_types(conn)
    if not objects:
        return MemorySchemaClassification.ABSENT
    if FACTS_TABLE not in objects or objects[FACTS_TABLE] != "table":
        return MemorySchemaClassification.INCOMPATIBLE

    facts_columns = _columns(conn, FACTS_TABLE)
    if facts_columns not in {
        _EXPECTED_COLUMNS[FACTS_TABLE],
        _LEGACY_FACTS_COLUMNS,
        _MIGRATED_LEGACY_FACTS_COLUMNS,
    }:
        return MemorySchemaClassification.INCOMPATIBLE

    for table in (OUTBOX_TABLE, META_TABLE):
        if table in objects and (
            objects[table] != "table"
            or _columns(conn, table) != _EXPECTED_COLUMNS[table]
            or _object_sql(conn, table)
            != _normalized_schema_sql(
                OUTBOX_CREATE_SQL if table == OUTBOX_TABLE else META_CREATE_SQL
            )
        ):
            return MemorySchemaClassification.INCOMPATIBLE
    for index, expected_columns in _EXPECTED_INDEX_COLUMNS.items():
        if index in objects and (
            objects[index] != "index" or _index_columns(conn, index) != expected_columns
        ):
            return MemorySchemaClassification.INCOMPATIBLE

    expected_names = set(_EXPECTED_COLUMNS) | set(_EXPECTED_INDEX_COLUMNS)
    if set(objects) != expected_names or facts_columns == _LEGACY_FACTS_COLUMNS:
        return MemorySchemaClassification.KNOWN_COMPATIBLE_PARTIAL

    meta = conn.execute(
        f"SELECT schema_seeded FROM {META_TABLE} WHERE singleton_id = 1"
    ).fetchone()
    if meta is None or int(meta[0]) != 1:
        return MemorySchemaClassification.KNOWN_COMPATIBLE_PARTIAL
    if conn.execute(
        f"SELECT 1 FROM {META_TABLE} WHERE singleton_id <> 1 LIMIT 1"
    ).fetchone() is not None:
        return MemorySchemaClassification.INCOMPATIBLE
    return MemorySchemaClassification.CURRENT


def migrate_memory_convergence_schema(
    conn: sqlite3.Connection,
    *,
    now: float,
    before_seed: Callable[[], None] | None = None,
) -> None:
    classification = classify_memory_convergence_schema(conn)
    if classification is MemorySchemaClassification.INCOMPATIBLE:
        raise sqlite3.DatabaseError("memory convergence schema is incompatible")
    if classification is MemorySchemaClassification.CURRENT:
        return

    if classification is MemorySchemaClassification.ABSENT:
        conn.execute(FACTS_CREATE_SQL)
    elif _columns(conn, FACTS_TABLE) == _LEGACY_FACTS_COLUMNS:
        conn.execute(
            f"ALTER TABLE {FACTS_TABLE} "
            "ADD COLUMN vector_revision INTEGER NOT NULL DEFAULT 1"
        )

    for statement in MEMORY_CONVERGENCE_SCHEMA_STATEMENTS[1:]:
        conn.execute(statement)
    conn.execute(f"INSERT OR IGNORE INTO {META_TABLE}(singleton_id) VALUES (1)")
    seeded = conn.execute(
        f"SELECT schema_seeded FROM {META_TABLE} WHERE singleton_id = 1"
    ).fetchone()
    if seeded is None:
        raise sqlite3.DatabaseError("memory convergence seed marker is unavailable")
    if int(seeded[0]) == 0:
        if before_seed is not None:
            before_seed()
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {OUTBOX_TABLE}(
                user_id, fact_id, operation, fact_revision,
                state, next_attempt_at, created_at, updated_at
            )
            SELECT user_id, id, 'UPSERT', vector_revision,
                   'PENDING', 0, ?, ?
            FROM {FACTS_TABLE}
            """,
            (float(now), float(now)),
        )
        conn.execute(
            f"UPDATE {META_TABLE} SET schema_seeded = 1 WHERE singleton_id = 1"
        )

    if classify_memory_convergence_schema(conn) is not MemorySchemaClassification.CURRENT:
        raise sqlite3.DatabaseError("memory convergence schema did not reach current state")


def validate_memory_convergence_schema(conn: sqlite3.Connection) -> None:
    if classify_memory_convergence_schema(conn) is not MemorySchemaClassification.CURRENT:
        raise sqlite3.DatabaseError("memory convergence staged migration is required")


def schema_statements() -> Sequence[str]:
    return MEMORY_CONVERGENCE_SCHEMA_STATEMENTS
