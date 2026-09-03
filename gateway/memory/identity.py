"""Immutable Memory identities. Migration consumes an externally pinned epoch.

No history is inferred from content, timestamps, or the SQLite sequence. A NULL
epoch is permitted only for a newly initialized, empty UUID-native database.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import closing

from gateway.memory.schema import (
    FACTS_TABLE,
    FACTS_CREATE_SQL,
    OUTBOX_TABLE,
    META_TABLE,
    MemorySchemaClassification,
    _columns,
    _normalized_schema_sql,
    _table_rows,
    migrate_memory_convergence_schema,
)

IDENTITY_TABLE = "memory_identity_metadata"
MIGRATION_ID = "memory-convergence-schema-v2"
UUID_COLUMN = ("fact_uuid", "TEXT", 0, None, 0)


def canonical_uuid(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("INVALID_MEMORY_UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError("INVALID_MEMORY_UUID") from exc
    if str(parsed) != value:
        raise ValueError("INVALID_MEMORY_UUID")
    return value


def canonical_fact_table_sql_variants() -> frozenset[str]:
    """Exact known v1 CREATE/ALTER histories, plus the single v2 UUID column.

    Column parity alone cannot authorize arbitrary extra CHECK constraints or
    other changed table SQL during target schema fingerprint validation.
    """
    variants = set()
    legacy = FACTS_CREATE_SQL.replace(
        "    vector_revision INTEGER NOT NULL DEFAULT 1,\n", ""
    )
    for create, append_revision in (
        (FACTS_CREATE_SQL, False),
        (legacy, True),
        (
            legacy.replace("source TEXT,", "source TEXT NOT NULL DEFAULT 'unknown',"),
            True,
        ),
    ):
        with closing(sqlite3.connect(":memory:")) as expected:
            expected.execute(create)
            if append_revision:
                expected.execute(
                    f"ALTER TABLE {FACTS_TABLE} ADD COLUMN vector_revision INTEGER NOT NULL DEFAULT 1"
                )
            expected.execute(f"ALTER TABLE {FACTS_TABLE} ADD COLUMN fact_uuid TEXT")
            sql = expected.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (FACTS_TABLE,)
            ).fetchone()[0]
            variants.add(_normalized_schema_sql(sql))
    return frozenset(variants)


def legacy_fact_uuid(epoch: str, user_id: int, sqlite_id: int) -> str:
    canonical_uuid(epoch)
    if any(type(v) is not int or v <= 0 for v in (user_id, sqlite_id)):
        raise ValueError("INVALID_MEMORY_OWNER_OR_ID")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"healbite-fact-legacy:{epoch}:{user_id}:{sqlite_id}",
        )
    )


def memory_point_id(user_id: int, fact_uuid: str) -> str:
    canonical_uuid(fact_uuid)
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("INVALID_MEMORY_OWNER")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"healbite-memory:{user_id}:{fact_uuid}"))


def _uuid_check(value: str) -> str:
    return (
        f"{value} IS NOT NULL AND typeof({value}) = 'text' "
        f"AND length({value}) = 36 AND lower({value}) = {value} "
        f"AND substr({value},9,1) = '-' AND substr({value},14,1) = '-' "
        f"AND substr({value},19,1) = '-' AND substr({value},24,1) = '-' "
        f"AND length(replace({value},'-','')) = 32 "
        f"AND replace({value},'-','') NOT GLOB '*[^0-9a-f]*'"
    )


SCHEMA_STATEMENTS = (
    f"CREATE TABLE IF NOT EXISTS {IDENTITY_TABLE} ("
    "singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1), "
    "legacy_epoch_uuid TEXT CHECK(legacy_epoch_uuid IS NULL OR ("
    + _uuid_check("legacy_epoch_uuid")
    + ")), schema_version INTEGER NOT NULL CHECK(schema_version = 2))",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{FACTS_TABLE}_fact_uuid ON {FACTS_TABLE}(fact_uuid)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{OUTBOX_TABLE}_uuid_revision "
    f"ON {OUTBOX_TABLE}(user_id, fact_uuid, fact_revision, operation)",
    *tuple(
        f"CREATE TRIGGER IF NOT EXISTS {table}_uuid_{event.lower()} "
        f"BEFORE {event} ON {table} WHEN NOT ({_uuid_check('NEW.fact_uuid')}) "
        "BEGIN SELECT RAISE(ABORT, 'INVALID_MEMORY_UUID'); END"
        for table in (FACTS_TABLE, OUTBOX_TABLE)
        for event in ("INSERT", "UPDATE")
    ),
    *tuple(
        f"CREATE TRIGGER IF NOT EXISTS {table}_uuid_immutable BEFORE UPDATE OF fact_uuid ON {table} "
        "WHEN OLD.fact_uuid IS NOT NEW.fact_uuid "
        "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_MEMORY_UUID'); END"
        for table in (FACTS_TABLE, OUTBOX_TABLE)
    ),
    f"CREATE TRIGGER IF NOT EXISTS {IDENTITY_TABLE}_immutable BEFORE UPDATE ON {IDENTITY_TABLE} "
    "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_MEMORY_EPOCH'); END",
    f"CREATE TRIGGER IF NOT EXISTS {IDENTITY_TABLE}_no_delete BEFORE DELETE ON {IDENTITY_TABLE} "
    "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_MEMORY_EPOCH'); END",
)
MIGRATION_SOURCE = (
    "\n".join((
        MIGRATION_ID,
        f"ALTER TABLE {FACTS_TABLE} ADD COLUMN fact_uuid TEXT",
        f"ALTER TABLE {OUTBOX_TABLE} ADD COLUMN fact_uuid TEXT",
        *SCHEMA_STATEMENTS,
        "CONSUME AUTHORITY legacy_epoch_uuid; REJECT EPOCH MISMATCH BEFORE MUTATION",
        "UUID5 NAMESPACE_URL healbite-fact-legacy:{legacy_epoch_uuid}:{user_id}:{sqlite_id}",
        "PRESERVE EXISTING ROW CONTENT; BACKFILL DELETE IDENTITY WITHOUT LIVE FACT",
        "RESEED CURRENT UPSERT INTENTS PENDING AT FIXED MIGRATION TIME",
    ))
    + "\n"
)
MIGRATION_SHA256 = hashlib.sha256(MIGRATION_SOURCE.encode()).hexdigest()


def stored_epoch(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (IDENTITY_TABLE,)
    ).fetchone():
        return False, None
    rows = conn.execute(
        f"SELECT singleton_id, legacy_epoch_uuid, schema_version FROM {IDENTITY_TABLE}"
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or rows[0][2] != 2:
        raise sqlite3.DatabaseError("MEMORY_IDENTITY_METADATA_INVALID")
    epoch = rows[0][1]
    if epoch is not None:
        canonical_uuid(epoch)
    return True, epoch


def validate_epoch(conn: sqlite3.Connection, epoch: str | None) -> None:
    if epoch is not None:
        canonical_uuid(epoch)
    exists, current = stored_epoch(conn)
    if exists:
        if current != epoch:
            raise sqlite3.DatabaseError("MIGRATION_EPOCH_MISMATCH")
        return
    if epoch is None:
        for table in (FACTS_TABLE, OUTBOX_TABLE):
            if (
                _columns(conn, table)
                and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            ):
                raise sqlite3.DatabaseError("LEGACY_EPOCH_AUTHORITY_REQUIRED")


def classify_identity_schema(conn: sqlite3.Connection) -> MemorySchemaClassification:
    columns = [_columns(conn, t) for t in (FACTS_TABLE, OUTBOX_TABLE)]
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ?", (IDENTITY_TABLE,)
    ).fetchone()
    if not exists and not any(any(c[0] == "fact_uuid" for c in cs) for cs in columns):
        return MemorySchemaClassification.ABSENT
    try:
        validate_identity_schema(conn)
    except (sqlite3.Error, ValueError):
        return MemorySchemaClassification.INCOMPATIBLE
    return MemorySchemaClassification.CURRENT


def validate_identity_schema(conn: sqlite3.Connection) -> None:
    exists, _ = stored_epoch(conn)
    if not exists:
        raise sqlite3.DatabaseError("MEMORY_IDENTITY_MIGRATION_REQUIRED")
    for table in (FACTS_TABLE, OUTBOX_TABLE):
        if not _columns(conn, table) or _columns(conn, table)[-1] != UUID_COLUMN:
            raise sqlite3.DatabaseError("MEMORY_IDENTITY_SCHEMA_INVALID")
        for (value,) in conn.execute(f"SELECT fact_uuid FROM {table}"):
            canonical_uuid(value)
    # Compare exact index/trigger/metadata definitions, not merely their names.
    with closing(sqlite3.connect(":memory:")) as expected:
        expected.execute(f"CREATE TABLE {FACTS_TABLE}(fact_uuid TEXT)")
        expected.execute(
            f"CREATE TABLE {OUTBOX_TABLE}(user_id, fact_uuid, fact_revision, operation)"
        )
        for statement in SCHEMA_STATEMENTS:
            expected.execute(statement)
        for name, sql in expected.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT IN (?, ?)",
            (FACTS_TABLE, OUTBOX_TABLE),
        ):
            actual = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
            ).fetchone()
            if not actual or _normalized_schema_sql(
                actual[0]
            ) != _normalized_schema_sql(sql):
                raise sqlite3.DatabaseError("MEMORY_IDENTITY_SCHEMA_INVALID")


def migrate_identity_schema(
    conn: sqlite3.Connection, *, legacy_epoch_uuid: str | None, now: float = 0.0
) -> None:
    """Caller owns transaction. Epoch checks precede even the v1 schema writes."""
    validate_epoch(conn, legacy_epoch_uuid)
    state = classify_identity_schema(conn)
    if state is MemorySchemaClassification.CURRENT:
        return
    if state is MemorySchemaClassification.INCOMPATIBLE:
        raise sqlite3.DatabaseError("MEMORY_IDENTITY_SCHEMA_INVALID")
    migrate_memory_convergence_schema(conn, now=now)
    for table in (FACTS_TABLE, OUTBOX_TABLE):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN fact_uuid TEXT")
        id_column = "id" if table == FACTS_TABLE else "fact_id"
        for row_id, user_id, fact_id in conn.execute(
            f"SELECT id, user_id, {id_column} FROM {table}"
        ).fetchall():
            if legacy_epoch_uuid is None:
                raise sqlite3.DatabaseError("LEGACY_EPOCH_AUTHORITY_REQUIRED")
            conn.execute(
                f"UPDATE {table} SET fact_uuid = ? WHERE id = ?",
                (legacy_fact_uuid(legacy_epoch_uuid, user_id, fact_id), row_id),
            )
    # Preserve old intent identities including deletes; reset only current UPSERTs
    # so derived vectors are republished under UUID identity, never old ID points.
    conn.execute(
        f"""INSERT INTO {OUTBOX_TABLE}(
        user_id, fact_id, fact_uuid, operation, fact_revision, created_at, updated_at)
        SELECT user_id, id, fact_uuid, 'UPSERT', vector_revision, ?, ? FROM {FACTS_TABLE} WHERE 1
        ON CONFLICT(user_id, fact_id, fact_revision, operation) DO UPDATE SET
        state='PENDING', attempt_count=0, next_attempt_at=0, last_error_class=NULL, updated_at=excluded.updated_at
        """,
        (float(now), float(now)),
    )
    # Insert metadata before installing the immutable triggers.
    conn.execute(SCHEMA_STATEMENTS[0])
    conn.execute(f"INSERT INTO {IDENTITY_TABLE} VALUES (1, ?, 2)", (legacy_epoch_uuid,))
    for statement in SCHEMA_STATEMENTS[1:]:
        conn.execute(statement)
    validate_identity_schema(conn)


def validate_identity_transition(
    before: sqlite3.Connection, after: sqlite3.Connection, *, epoch: str | None
) -> None:
    """Replay only Memory migration in an isolated DB; compare every persisted row."""
    validate_epoch(before, epoch)
    validate_identity_schema(after)
    validate_epoch(after, epoch)
    if classify_identity_schema(before) is MemorySchemaClassification.CURRENT:
        for table in (FACTS_TABLE, OUTBOX_TABLE, META_TABLE, IDENTITY_TABLE):
            if _table_rows(before, table) != _table_rows(after, table):
                raise sqlite3.DatabaseError("CURRENT_MEMORY_IDENTITY_DATA_CHANGED")
        return
    with closing(sqlite3.connect(":memory:")) as expected:
        for table in (FACTS_TABLE, OUTBOX_TABLE, META_TABLE):
            row = before.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row:
                expected.execute(row[0])
                columns, rows = _table_rows(before, table)
                expected.executemany(
                    f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})",
                    rows,
                )
        # Deleted/acknowledged rows still advance AUTOINCREMENT. Preserve the
        # counters so expected reseed IDs match the real staged copy exactly.
        if before.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'"
        ).fetchone():
            for name, seq in before.execute(
                "SELECT name,seq FROM sqlite_sequence WHERE name IN (?,?)",
                (FACTS_TABLE, OUTBOX_TABLE),
            ):
                expected.execute("DELETE FROM sqlite_sequence WHERE name=?", (name,))
                expected.execute(
                    "INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (name, seq)
                )
        migrate_identity_schema(expected, legacy_epoch_uuid=epoch)
        for table in (FACTS_TABLE, OUTBOX_TABLE, META_TABLE, IDENTITY_TABLE):
            if _table_rows(expected, table) != _table_rows(after, table):
                raise sqlite3.DatabaseError("MEMORY_IDENTITY_TRANSITION_INVALID")
