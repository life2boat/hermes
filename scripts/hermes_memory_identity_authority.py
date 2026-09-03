"""Planning-only epoch assignment, before any staged copying.

The caller persists the return value once in the new immutable approval. Retries
consume that approval, not this function. Independent legacy authorities acquire
different epochs; UUID-native databases retain their existing namespace.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from gateway.memory.identity import (
    stored_epoch,
    validate_identity_schema,
    validate_epoch,
)


def plan_memory_epoch(db_path: Path) -> str | None:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        exists, epoch = stored_epoch(conn)
        if exists:
            validate_identity_schema(conn)
            return epoch
        return str(uuid.uuid4())


def verify_memory_epoch(db_path: Path, epoch: str | None) -> None:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        validate_epoch(conn, epoch)
