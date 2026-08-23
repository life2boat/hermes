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
_LEGACY_FACTS_COLUMNS_NOT_NULL = tuple(
    ("source", "TEXT", 1, "'unknown'", 0) if c[0] == "source" else c
    for c in _LEGACY_FACTS_COLUMNS
)
_VECTOR_REVISION_COLUMN = next(
    column for column in _EXPECTED_COLUMNS[FACTS_TABLE] if column[0] == "vector_revision"
)
# SQLite appends an additive ALTER TABLE column. Both layouts are canonical:
# fresh databases use target CREATE order, while legacy upgrades preserve every
# pre-existing column position and append vector_revision.
_MIGRATED_LEGACY_FACTS_COLUMNS = (*_LEGACY_FACTS_COLUMNS, _VECTOR_REVISION_COLUMN)
_MIGRATED_LEGACY_FACTS_COLUMNS_NOT_NULL = (*_LEGACY_FACTS_COLUMNS_NOT_NULL, _VECTOR_REVISION_COLUMN)

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
        _LEGACY_FACTS_COLUMNS_NOT_NULL,
        _MIGRATED_LEGACY_FACTS_COLUMNS_NOT_NULL,
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
    if set(objects) != expected_names or facts_columns in (_LEGACY_FACTS_COLUMNS, _LEGACY_FACTS_COLUMNS_NOT_NULL):
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
    elif _columns(conn, FACTS_TABLE) in (_LEGACY_FACTS_COLUMNS, _LEGACY_FACTS_COLUMNS_NOT_NULL):
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


def _table_rows(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    columns = tuple(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")'))
    if not columns:
        return (), ()
    projection = ", ".join(f'"{column}"' for column in columns)
    order_column = "id" if "id" in columns else "singleton_id"
    rows = tuple(
        tuple(row)
        for row in conn.execute(
            f'SELECT {projection} FROM "{table}" ORDER BY "{order_column}"'
        )
    )
    return columns, rows


def validate_memory_convergence_staged_transition(
    before: sqlite3.Connection,
    after: sqlite3.Connection,
    *,
    seed_timestamp: float,
) -> None:
    """Prove that staged migration changed only canonical Memory seed state."""

    before_state = classify_memory_convergence_schema(before)
    if before_state is MemorySchemaClassification.INCOMPATIBLE:
        raise sqlite3.DatabaseError("memory convergence source schema is incompatible")
    validate_memory_convergence_schema(after)

    before_facts_columns, before_facts = _table_rows(before, FACTS_TABLE)
    after_facts_columns, after_facts = _table_rows(after, FACTS_TABLE)
    if before_facts_columns:
        preserved_columns = tuple(
            column for column in before_facts_columns if column != "vector_revision"
        )
        before_projection = tuple(before_facts_columns.index(column) for column in preserved_columns)
        after_projection = tuple(after_facts_columns.index(column) for column in preserved_columns)
        if tuple(
            tuple(row[index] for index in before_projection) for row in before_facts
        ) != tuple(tuple(row[index] for index in after_projection) for row in after_facts):
            raise sqlite3.DatabaseError("memory convergence facts changed during migration")
    elif after_facts:
        raise sqlite3.DatabaseError("memory convergence migration created facts")

    before_outbox_columns, before_outbox = _table_rows(before, OUTBOX_TABLE)
    after_outbox_columns, after_outbox = _table_rows(after, OUTBOX_TABLE)
    before_meta_columns, before_meta = _table_rows(before, META_TABLE)
    after_meta_columns, after_meta = _table_rows(after, META_TABLE)

    if before_state is MemorySchemaClassification.CURRENT:
        if (
            before_facts != after_facts
            or before_outbox != after_outbox
            or before_meta != after_meta
        ):
            raise sqlite3.DatabaseError("current memory convergence data changed")
        return

    seed_was_complete = False
    if before_meta_columns and before_meta:
        seeded_index = before_meta_columns.index("schema_seeded")
        seed_was_complete = int(before_meta[0][seeded_index]) == 1

    if before_outbox and not set(before_outbox).issubset(set(after_outbox)):
        raise sqlite3.DatabaseError("memory convergence migration changed existing outbox rows")

    if seed_was_complete:
        if before_outbox != after_outbox:
            raise sqlite3.DatabaseError("seeded memory convergence outbox changed")
    else:
        fact_id_index = after_facts_columns.index("id")
        fact_user_index = after_facts_columns.index("user_id")
        fact_revision_index = after_facts_columns.index("vector_revision")
        outbox_indexes = {
            column: after_outbox_columns.index(column)
            for column in (
                "user_id", "fact_id", "operation", "fact_revision",
                "state", "attempt_count", "next_attempt_at", "last_error_class",
                "created_at", "updated_at",
            )
        }
        expected_signatures = {
            (row[fact_user_index], row[fact_id_index], "UPSERT", row[fact_revision_index])
            for row in after_facts
        }
        before_signatures = {
            (
                row[before_outbox_columns.index("user_id")],
                row[before_outbox_columns.index("fact_id")],
                row[before_outbox_columns.index("operation")],
                row[before_outbox_columns.index("fact_revision")],
            )
            for row in before_outbox
        }
        after_by_signature = {
            (
                row[outbox_indexes["user_id"]], row[outbox_indexes["fact_id"]],
                row[outbox_indexes["operation"]], row[outbox_indexes["fact_revision"]],
            ): row
            for row in after_outbox
        }
        if len(after_outbox) != len(before_outbox) + len(
            expected_signatures - before_signatures
        ) or not expected_signatures.issubset(after_by_signature):
            raise sqlite3.DatabaseError("memory convergence seed count is invalid")
        for signature in expected_signatures - before_signatures:
            row = after_by_signature[signature]
            if (
                row[outbox_indexes["state"]] != "PENDING"
                or int(row[outbox_indexes["attempt_count"]]) != 0
                or float(row[outbox_indexes["next_attempt_at"]]) != 0.0
                or row[outbox_indexes["last_error_class"]] is not None
                or float(row[outbox_indexes["created_at"]]) != float(seed_timestamp)
                or float(row[outbox_indexes["updated_at"]]) != float(seed_timestamp)
            ):
                raise sqlite3.DatabaseError("memory convergence seed payload is invalid")

    if len(after_meta) != 1:
        raise sqlite3.DatabaseError("memory convergence meta singleton is invalid")
    after_meta_map = dict(zip(after_meta_columns, after_meta[0], strict=True))
    if int(after_meta_map["singleton_id"]) != 1 or int(after_meta_map["schema_seeded"]) != 1:
        raise sqlite3.DatabaseError("memory convergence completion marker is invalid")
    if before_meta:
        before_meta_map = dict(zip(before_meta_columns, before_meta[0], strict=True))
        before_meta_map["schema_seeded"] = 1
        if before_meta_map != after_meta_map:
            raise sqlite3.DatabaseError("memory convergence meta counters changed")
    elif any(
        after_meta_map[column] != expected
        for column, expected in (
            ("processed_count", 0), ("succeeded_count", 0),
            ("failed_count", 0), ("superseded_count", 0),
            ("last_success_at", None), ("last_reconciliation_at", None),
            ("last_error_class", None),
        )
    ):
        raise sqlite3.DatabaseError("memory convergence meta defaults are invalid")


def schema_statements() -> Sequence[str]:
    return MEMORY_CONVERGENCE_SCHEMA_STATEMENTS
