from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


OUTBOX_TABLE = "memory_os_vector_sync_outbox"
META_TABLE = "memory_os_vector_sync_meta"
FACTS_TABLE = "memory_os_facts"

_MAX_BATCH_SIZE = 100
_DEFAULT_MAX_ATTEMPTS = 8
_MAX_BACKOFF_SECONDS = 300.0
_VALID_OPERATIONS = frozenset({"UPSERT", "DELETE"})
_READY_STATES = ("PENDING", "RETRY")


@dataclass(frozen=True, slots=True)
class VectorSyncBatchResult:
    status: str
    processed: int
    succeeded: int
    retried: int
    blocked: int
    superseded: int
    remaining: int


@dataclass(frozen=True, slots=True)
class VectorSyncStatus:
    status: str
    vector_enabled: bool
    pending_count: int
    retryable_count: int
    blocked_count: int
    oldest_pending_age_seconds: float | None
    last_success_at: float | None
    last_error_class: str | None
    processed_count: int
    succeeded_count: int
    failed_count: int
    superseded_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryVectorConvergence:
    """Durable SQLite-authoritative reconciliation for derived vector state."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        qdrant_adapter: Any | None,
        vector_enabled: bool,
        fact_text: Callable[[dict[str, Any]], str],
        clock: Callable[[], float] = time.time,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self.db_path = Path(db_path)
        self.qdrant_adapter = qdrant_adapter
        self.vector_enabled = bool(vector_enabled)
        self.fact_text = fact_text
        self.clock = clock
        self.max_attempts = max(1, int(max_attempts))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        columns = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({FACTS_TABLE})").fetchall()
        }
        if "vector_revision" not in columns:
            conn.execute(
                f"ALTER TABLE {FACTS_TABLE} "
                "ADD COLUMN vector_revision INTEGER NOT NULL DEFAULT 1"
            )

        conn.executescript(
            f"""
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
            );
            CREATE INDEX IF NOT EXISTS idx_{OUTBOX_TABLE}_ready
                ON {OUTBOX_TABLE}(state, next_attempt_at, id);
            CREATE INDEX IF NOT EXISTS idx_{OUTBOX_TABLE}_fact_revision
                ON {OUTBOX_TABLE}(user_id, fact_id, fact_revision);

            CREATE TABLE IF NOT EXISTS {META_TABLE} (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                schema_seeded INTEGER NOT NULL DEFAULT 0 CHECK(schema_seeded IN (0, 1)),
                processed_count INTEGER NOT NULL DEFAULT 0,
                succeeded_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                superseded_count INTEGER NOT NULL DEFAULT 0,
                last_success_at REAL,
                last_error_class TEXT
            );
            INSERT OR IGNORE INTO {META_TABLE}(singleton_id) VALUES (1);
            """
        )

        seeded = conn.execute(
            f"SELECT schema_seeded FROM {META_TABLE} WHERE singleton_id = 1"
        ).fetchone()
        if seeded is not None and int(seeded[0]) == 0:
            now = float(self.clock())
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
                (now, now),
            )
            conn.execute(
                f"UPDATE {META_TABLE} SET schema_seeded = 1 WHERE singleton_id = 1"
            )

    def enqueue_upsert(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        fact_id: int,
        fact_revision: int,
    ) -> None:
        self._enqueue(
            conn,
            user_id=user_id,
            fact_id=fact_id,
            fact_revision=fact_revision,
            operation="UPSERT",
        )

    def enqueue_delete(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        fact_id: int,
        fact_revision: int,
    ) -> None:
        self._enqueue(
            conn,
            user_id=user_id,
            fact_id=fact_id,
            fact_revision=fact_revision,
            operation="DELETE",
        )

    def _enqueue(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        fact_id: int,
        fact_revision: int,
        operation: str,
    ) -> None:
        now = float(self.clock())
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {OUTBOX_TABLE}(
                user_id, fact_id, operation, fact_revision,
                state, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?)
            """,
            (user_id, fact_id, operation, fact_revision, now, now),
        )

    def process_batch(
        self,
        *,
        batch_size: int = 25,
        time_budget_seconds: float = 2.0,
        retry_blocked: bool = False,
    ) -> VectorSyncBatchResult:
        if not self.vector_enabled or self.qdrant_adapter is None:
            status = self.get_status()
            return VectorSyncBatchResult(
                status=status.status,
                processed=0,
                succeeded=0,
                retried=0,
                blocked=0,
                superseded=0,
                remaining=(
                    status.pending_count + status.retryable_count + status.blocked_count
                ),
            )

        limit = min(_MAX_BATCH_SIZE, max(1, int(batch_size)))
        budget = max(0.001, float(time_budget_seconds))
        started = time.perf_counter()
        now = float(self.clock())
        selected_states = (*_READY_STATES, "BLOCKED") if retry_blocked else _READY_STATES
        placeholders = ", ".join("?" for _ in selected_states)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {OUTBOX_TABLE}
                WHERE state IN ({placeholders})
                  AND (state = 'BLOCKED' OR next_attempt_at <= ?)
                ORDER BY id ASC
                LIMIT ?
                """,
                (*selected_states, now, limit),
            ).fetchall()

        processed = succeeded = retried = blocked = superseded = 0
        for row in rows:
            if time.perf_counter() - started >= budget:
                break
            processed += 1
            outcome = self._process_operation(dict(row), now=float(self.clock()))
            if outcome == "SUCCEEDED":
                succeeded += 1
            elif outcome == "SUPERSEDED":
                superseded += 1
            elif outcome == "RETRY":
                retried += 1
            else:
                blocked += 1

        status = self.get_status()
        return VectorSyncBatchResult(
            status=status.status,
            processed=processed,
            succeeded=succeeded,
            retried=retried,
            blocked=blocked,
            superseded=superseded,
            remaining=status.pending_count + status.retryable_count + status.blocked_count,
        )

    def _process_operation(self, operation: dict[str, Any], *, now: float) -> str:
        validated = self._validate_operation(operation)
        if validated is not None:
            self._record_failure(operation, error_class=validated, now=now, terminal=True)
            return "BLOCKED"

        operation_id = int(operation["id"])
        user_id = int(operation["user_id"])
        fact_id = int(operation["fact_id"])
        revision = int(operation["fact_revision"])
        operation_type = str(operation["operation"])

        reset_connection = getattr(self.qdrant_adapter, "reset_connection_for_retry", None)
        if callable(reset_connection):
            reset_connection()

        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {FACTS_TABLE} WHERE id = ?",
                (fact_id,),
            ).fetchone()
        fact = dict(row) if row is not None else None

        if fact is not None and int(fact["user_id"]) != user_id:
            self._record_failure(
                operation,
                error_class="OWNER_MISMATCH",
                now=now,
                terminal=True,
            )
            return "BLOCKED"

        if operation_type == "UPSERT":
            if fact is None:
                success = self._call_adapter(
                    self.qdrant_adapter.delete_fact,
                    sqlite_id=fact_id,
                    user_id=user_id,
                    wait=True,
                )
            else:
                canonical_revision = int(fact["vector_revision"])
                if canonical_revision > revision:
                    self._ack(operation_id, now=now, superseded=True)
                    return "SUPERSEDED"
                if canonical_revision < revision:
                    self._record_failure(
                        operation,
                        error_class="CANONICAL_REVISION_REGRESSION",
                        now=now,
                        terminal=True,
                    )
                    return "BLOCKED"
                success = self._call_adapter(
                    self.qdrant_adapter.upsert_fact,
                    sqlite_id=fact_id,
                    user_id=user_id,
                    text=self.fact_text(fact),
                    payload={
                        "sqlite_id": fact_id,
                        "user_id": user_id,
                        "entity": fact["entity"],
                        "key": fact["key"],
                        "value": fact["value"],
                        "source": fact.get("source"),
                        "trust_score": fact.get("trust_score"),
                        "updated_at": fact.get("updated_at"),
                        "vector_revision": canonical_revision,
                    },
                    wait=True,
                )
        else:
            if fact is not None:
                self._ack(operation_id, now=now, superseded=True)
                return "SUPERSEDED"
            success = self._call_adapter(
                self.qdrant_adapter.delete_fact,
                sqlite_id=fact_id,
                user_id=user_id,
                wait=True,
            )

        if success:
            became_stale = self._enqueue_post_mutation_correction(operation)
            self._ack(operation_id, now=now, superseded=became_stale)
            return "SUPERSEDED" if became_stale else "SUCCEEDED"
        return self._record_retry(operation, now=now)

    def _enqueue_post_mutation_correction(self, operation: dict[str, Any]) -> bool:
        fact_id = int(operation["fact_id"])
        user_id = int(operation["user_id"])
        revision = int(operation["fact_revision"])
        operation_type = str(operation["operation"])
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT user_id, vector_revision FROM {FACTS_TABLE} WHERE id = ?",
                (fact_id,),
            ).fetchone()
            fact = dict(row) if row is not None else None
            if fact is not None and int(fact["user_id"]) != user_id:
                return True
            if operation_type == "UPSERT":
                if fact is None:
                    self.enqueue_delete(
                        conn,
                        user_id=user_id,
                        fact_id=fact_id,
                        fact_revision=revision + 1,
                    )
                    return True
                canonical_revision = int(fact["vector_revision"])
                if canonical_revision != revision:
                    self.enqueue_upsert(
                        conn,
                        user_id=user_id,
                        fact_id=fact_id,
                        fact_revision=canonical_revision,
                    )
                    return True
                return False
            if fact is not None:
                self.enqueue_upsert(
                    conn,
                    user_id=user_id,
                    fact_id=fact_id,
                    fact_revision=int(fact["vector_revision"]),
                )
                return True
            return False

    @staticmethod
    def _call_adapter(method: Callable[..., Any], **kwargs: Any) -> bool:
        try:
            return bool(method(**kwargs))
        except Exception:
            return False

    @staticmethod
    def _validate_operation(operation: dict[str, Any]) -> str | None:
        if operation.get("operation") not in _VALID_OPERATIONS:
            return "INVALID_OPERATION"
        for field in ("id", "user_id", "fact_id", "fact_revision", "attempt_count"):
            value = operation.get(field)
            if type(value) is not int:
                return "MALFORMED_OPERATION"
        if operation["id"] <= 0 or operation["user_id"] <= 0 or operation["fact_id"] <= 0:
            return "MALFORMED_OPERATION"
        if operation["fact_revision"] <= 0 or operation["attempt_count"] < 0:
            return "MALFORMED_OPERATION"
        return None

    def _record_retry(self, operation: dict[str, Any], *, now: float) -> str:
        attempts = int(operation["attempt_count"]) + 1
        terminal = attempts >= self.max_attempts
        self._record_failure(
            operation,
            error_class="QDRANT_OPERATION_FAILED",
            now=now,
            terminal=terminal,
            attempts=attempts,
        )
        return "BLOCKED" if terminal else "RETRY"

    def _record_failure(
        self,
        operation: dict[str, Any],
        *,
        error_class: str,
        now: float,
        terminal: bool,
        attempts: int | None = None,
    ) -> None:
        operation_id = operation.get("id")
        if not isinstance(operation_id, int):
            return
        attempts = int(operation.get("attempt_count") or 0) + 1 if attempts is None else attempts
        state = "BLOCKED" if terminal else "RETRY"
        backoff = min(_MAX_BACKOFF_SECONDS, float(2 ** min(attempts, 8)))
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE {OUTBOX_TABLE}
                SET state = ?, attempt_count = ?, next_attempt_at = ?,
                    last_error_class = ?, updated_at = ?
                WHERE id = ? AND state IN ('PENDING', 'RETRY', 'BLOCKED')
                """,
                (state, attempts, now + backoff, error_class, now, operation_id),
            )
            if cursor.rowcount:
                conn.execute(
                    f"""
                    UPDATE {META_TABLE}
                    SET processed_count = processed_count + 1,
                        failed_count = failed_count + 1,
                        last_error_class = ?
                    WHERE singleton_id = 1
                    """,
                    (error_class,),
                )

    def _ack(self, operation_id: int, *, now: float, superseded: bool) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {OUTBOX_TABLE} WHERE id = ? AND state IN ('PENDING', 'RETRY', 'BLOCKED')",
                (operation_id,),
            )
            if cursor.rowcount:
                conn.execute(
                    f"""
                    UPDATE {META_TABLE}
                    SET processed_count = processed_count + 1,
                        succeeded_count = succeeded_count + ?,
                        superseded_count = superseded_count + ?,
                        last_success_at = ?,
                        last_error_class = NULL
                    WHERE singleton_id = 1
                    """,
                    (0 if superseded else 1, 1 if superseded else 0, now),
                )

    def get_status(self) -> VectorSyncStatus:
        now = float(self.clock())
        with self._connect() as conn:
            counts = {
                str(row["state"]): int(row["count"])
                for row in conn.execute(
                    f"SELECT state, COUNT(*) AS count FROM {OUTBOX_TABLE} GROUP BY state"
                ).fetchall()
            }
            oldest = conn.execute(
                f"SELECT MIN(created_at) FROM {OUTBOX_TABLE}"
            ).fetchone()[0]
            meta = conn.execute(
                f"SELECT * FROM {META_TABLE} WHERE singleton_id = 1"
            ).fetchone()
            unresolved_error = conn.execute(
                f"""
                SELECT last_error_class FROM {OUTBOX_TABLE}
                WHERE last_error_class IS NOT NULL
                ORDER BY updated_at DESC, id DESC LIMIT 1
                """
            ).fetchone()

        pending = counts.get("PENDING", 0)
        retryable = counts.get("RETRY", 0)
        blocked = counts.get("BLOCKED", 0)
        if blocked:
            status = "BLOCKED"
        elif retryable:
            status = "DEGRADED"
        elif pending:
            status = "PENDING"
        else:
            status = "CONVERGED"
        return VectorSyncStatus(
            status=status,
            vector_enabled=self.vector_enabled,
            pending_count=pending,
            retryable_count=retryable,
            blocked_count=blocked,
            oldest_pending_age_seconds=(None if oldest is None else max(0.0, now - float(oldest))),
            last_success_at=(None if meta["last_success_at"] is None else float(meta["last_success_at"])),
            last_error_class=(
                unresolved_error["last_error_class"]
                if unresolved_error is not None
                else meta["last_error_class"]
            ),
            processed_count=int(meta["processed_count"]),
            succeeded_count=int(meta["succeeded_count"]),
            failed_count=int(meta["failed_count"]),
            superseded_count=int(meta["superseded_count"]),
        )
