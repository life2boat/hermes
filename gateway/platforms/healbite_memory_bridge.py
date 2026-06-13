from __future__ import annotations

import logging
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterator

from gateway.memory.analytics import MemoryAnalyticsLogger
from gateway.memory.settings import env_flag
from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter, QdrantMemoryHit

logger = logging.getLogger(__name__)

_FACTS_TABLE = "memory_os_facts"
_FTS_TABLE = "memory_os_facts_fts"
_FTS_TOKEN_RE = re.compile(r"[\w-]+", re.UNICODE)

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {_FACTS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    entity TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT,
    trust_score REAL NOT NULL DEFAULT 0.5,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_{_FACTS_TABLE}_user_id ON {_FACTS_TABLE}(user_id);
CREATE INDEX IF NOT EXISTS idx_{_FACTS_TABLE}_user_entity_key ON {_FACTS_TABLE}(user_id, entity, key);
"""

_FTS_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5(
    entity,
    key,
    value,
    content='{_FACTS_TABLE}',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS {_FTS_TABLE}_ai AFTER INSERT ON {_FACTS_TABLE} BEGIN
    INSERT INTO {_FTS_TABLE}(rowid, entity, key, value)
    VALUES (new.id, new.entity, new.key, new.value);
END;
CREATE TRIGGER IF NOT EXISTS {_FTS_TABLE}_ad AFTER DELETE ON {_FACTS_TABLE} BEGIN
    INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, entity, key, value)
    VALUES ('delete', old.id, old.entity, old.key, old.value);
END;
CREATE TRIGGER IF NOT EXISTS {_FTS_TABLE}_au AFTER UPDATE ON {_FACTS_TABLE} BEGIN
    INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, entity, key, value)
    VALUES ('delete', old.id, old.entity, old.key, old.value);
    INSERT INTO {_FTS_TABLE}(rowid, entity, key, value)
    VALUES (new.id, new.entity, new.key, new.value);
END;
"""


def require_memory_user_id(user_id: Any) -> int:
    if user_id is None or (isinstance(user_id, str) and not user_id.strip()):
        raise ValueError("Memory Bridge requires a non-null user_id")
    try:
        return int(user_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Memory Bridge received invalid user_id: {user_id!r}") from exc


def build_memory_session_id(
    user_id: Any,
    chat_id: Any | None = None,
    thread_id: Any | None = None,
) -> str:
    normalized_user_id = require_memory_user_id(user_id)
    parts = [f"tg:{normalized_user_id}"]
    if chat_id is not None:
        parts.append(f"chat:{int(chat_id)}")
    if thread_id is not None:
        parts.append(f"thread:{int(thread_id)}")
    return "|".join(parts)


class HealBiteMemoryBridge:
    """SQLite-backed memory facts store with optional semantic Qdrant recall."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        qdrant_adapter: QdrantMemoryAdapter | None = None,
        embedding_adapter: EmbeddingAdapter | None = None,
        analytics_logger: MemoryAnalyticsLogger | None = None,
        background_write: bool = True,
        min_trust_score: float = 0.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.embedding_adapter = embedding_adapter or EmbeddingAdapter()
        self.qdrant_adapter = qdrant_adapter
        self._owns_analytics_logger = analytics_logger is None
        self.analytics_logger = analytics_logger or MemoryAnalyticsLogger(
            self.db_path,
            background_write=background_write,
        )
        self._vector_enabled = bool(
            qdrant_adapter is not None
            and getattr(qdrant_adapter, "enabled", True)
            and env_flag("MEMORY_VECTOR_ENABLED", default=False)
        )
        self.min_trust_score = float(min_trust_score)
        if self.qdrant_adapter is not None:
            self.qdrant_adapter.embedding_adapter = self.embedding_adapter
        self._executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="healbite-memory-qdrant")
            if background_write and self.qdrant_adapter is not None
            else None
        )
        self._fts_enabled = False
        self._initialize_schema()

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
        if self._owns_analytics_logger:
            self.analytics_logger.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            try:
                conn.executescript(_FTS_SQL)
                self._fts_enabled = True
            except sqlite3.OperationalError as exc:
                if "fts5" in str(exc).lower():
                    logger.warning(
                        "FTS5 is unavailable for %s; falling back to LIKE search",
                        self.db_path,
                    )
                    self._fts_enabled = False
                else:
                    raise
        self.analytics_logger.ensure_schema()

    def upsert_fact(
        self,
        *,
        user_id: int,
        entity: str,
        key: str,
        value: str,
        source: str = "unknown",
        trust_score: float = 0.5,
    ) -> int:
        user_id = require_memory_user_id(user_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT id FROM {_FACTS_TABLE} WHERE user_id = ? AND entity = ? AND key = ?",
                (user_id, entity, key),
            ).fetchone()
            if row is None:
                cursor = conn.execute(
                    f"""
                    INSERT INTO {_FACTS_TABLE}(user_id, entity, key, value, source, trust_score)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, entity, key, value, source, trust_score),
                )
                sqlite_id = int(cursor.lastrowid)
            else:
                sqlite_id = int(row["id"])
                conn.execute(
                    f"""
                    UPDATE {_FACTS_TABLE}
                    SET value = ?, source = ?, trust_score = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ?
                    """,
                    (value, source, trust_score, sqlite_id, user_id),
                )
        fact = self.get_fact(sqlite_id=sqlite_id, user_id=user_id)
        if fact is not None:
            self._schedule_qdrant_sync(fact)
        return sqlite_id

    def delete_fact(self, *, sqlite_id: int, user_id: int) -> None:
        user_id = require_memory_user_id(user_id)
        with self._connect() as conn:
            conn.execute(
                f"DELETE FROM {_FACTS_TABLE} WHERE id = ? AND user_id = ?",
                (sqlite_id, user_id),
            )

    def get_fact(self, *, sqlite_id: int, user_id: int) -> dict[str, Any] | None:
        user_id = require_memory_user_id(user_id)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {_FACTS_TABLE} WHERE id = ? AND user_id = ?",
                (sqlite_id, user_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def iter_facts(self, *, user_id: int | None = None) -> Iterator[dict[str, Any]]:
        query = f"SELECT * FROM {_FACTS_TABLE}"
        params: tuple[Any, ...] = ()
        if user_id is not None:
            normalized_user_id = require_memory_user_id(user_id)
            query += " WHERE user_id = ?"
            params = (normalized_user_id,)
        query += " ORDER BY id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        for row in rows:
            yield dict(row)

    def rebuild_qdrant_index(self, *, user_id: int | None = None) -> int:
        started_at = time.perf_counter()
        synced = 0
        for fact in self.iter_facts(user_id=user_id):
            if self._push_fact_to_qdrant(fact):
                synced += 1
        self.analytics_logger.log_rebuild(
            user_id=require_memory_user_id(user_id) if user_id is not None else None,
            source="qdrant",
            found_count=synced,
            processing_time_ms=(time.perf_counter() - started_at) * 1000.0,
        )
        return synced

    def search_relevant_facts(
        self,
        *,
        user_id: int,
        query: str,
        limit: int = 5,
        min_trust_score: float | None = None,
    ) -> list[dict[str, Any]]:
        user_id = require_memory_user_id(user_id)
        trust_threshold = self.min_trust_score if min_trust_score is None else float(min_trust_score)
        started_at = time.perf_counter()
        analytics_source = "like"
        results: list[dict[str, Any]] = []

        if self._vector_enabled and self.qdrant_adapter is not None:
            qdrant_hits = self.qdrant_adapter.search(
                query_text=query,
                user_id=user_id,
                limit=limit,
            )
            hydrated = self._hydrate_qdrant_hits(
                user_id=user_id,
                hits=qdrant_hits,
                min_trust_score=trust_threshold,
            )
            if hydrated:
                results = hydrated[:limit]
                analytics_source = "qdrant"

        if not results:
            fts_results = self._search_sqlite_fts(
                user_id=user_id,
                query=query,
                limit=limit,
                min_trust_score=trust_threshold,
            )
            if fts_results:
                results = fts_results
                analytics_source = "fts5"

        if not results:
            results = self._search_sqlite_like(
                user_id=user_id,
                query=query,
                limit=limit,
                min_trust_score=trust_threshold,
            )
            analytics_source = "like"

        self.analytics_logger.log_search(
            user_id=user_id,
            source=analytics_source,
            found_count=len(results),
            processing_time_ms=(time.perf_counter() - started_at) * 1000.0,
        )
        return results

    def _schedule_qdrant_sync(self, fact: dict[str, Any]) -> None:
        if not self._vector_enabled or self.qdrant_adapter is None:
            return
        if self._executor is None:
            self._push_fact_to_qdrant(fact)
            return
        self._executor.submit(self._push_fact_to_qdrant, fact)

    def _push_fact_to_qdrant(self, fact: dict[str, Any]) -> bool:
        if self.qdrant_adapter is None:
            return False
        return self.qdrant_adapter.upsert_fact(
            sqlite_id=int(fact["id"]),
            user_id=int(fact["user_id"]),
            text=self._fact_text(fact),
            payload={
                "sqlite_id": int(fact["id"]),
                "user_id": int(fact["user_id"]),
                "entity": fact["entity"],
                "key": fact["key"],
                "value": fact["value"],
                "source": fact.get("source"),
                "trust_score": fact.get("trust_score"),
                "updated_at": fact.get("updated_at"),
            },
        )

    @staticmethod
    def _fact_text(fact: dict[str, Any]) -> str:
        return "\n".join(
            [
                str(fact.get("entity", "")),
                str(fact.get("key", "")),
                str(fact.get("value", "")),
            ]
        )

    def _hydrate_qdrant_hits(
        self,
        *,
        user_id: int,
        hits: list[QdrantMemoryHit],
        min_trust_score: float,
    ) -> list[dict[str, Any]]:
        if not hits:
            return []
        sqlite_ids = [hit.sqlite_id for hit in hits if hit.sqlite_id is not None]
        if not sqlite_ids:
            return []

        placeholders = ", ".join("?" for _ in sqlite_ids)
        query = f"SELECT * FROM {_FACTS_TABLE} WHERE user_id = ? AND id IN ({placeholders})"
        with self._connect() as conn:
            rows = conn.execute(query, (user_id, *sqlite_ids)).fetchall()
        facts_by_id = {int(row["id"]): dict(row) for row in rows}

        hydrated: list[dict[str, Any]] = []
        for hit in hits:
            if hit.sqlite_id is None:
                continue
            if hit.payload.get("user_id") != user_id:
                continue
            fact = facts_by_id.get(hit.sqlite_id)
            if fact is None:
                continue
            if float(fact.get("trust_score") or 0.0) < min_trust_score:
                continue
            fact = dict(fact)
            fact["retrieval_source"] = "qdrant"
            fact["semantic_score"] = hit.score
            hydrated.append(fact)
        return hydrated

    def _search_sqlite_fts(
        self,
        *,
        user_id: int,
        query: str,
        limit: int,
        min_trust_score: float,
    ) -> list[dict[str, Any]]:
        if not self._fts_enabled:
            return []
        fts_query = self._sanitize_fts5_query(query)
        if not fts_query:
            return []
        sql = f"""
            SELECT
                f.id,
                f.user_id,
                f.entity,
                f.key,
                f.value,
                f.source,
                f.trust_score,
                f.created_at,
                f.updated_at,
                bm25({_FTS_TABLE}) AS rank
            FROM {_FTS_TABLE}
            JOIN {_FACTS_TABLE} AS f ON f.id = {_FTS_TABLE}.rowid
            WHERE {_FTS_TABLE} MATCH ?
              AND f.user_id = ?
              AND f.trust_score >= ?
            ORDER BY rank, f.trust_score DESC, f.updated_at DESC
            LIMIT ?
        """
        try:
            with self._connect() as conn:
                rows = conn.execute(sql, (fts_query, user_id, min_trust_score, limit)).fetchall()
        except sqlite3.OperationalError as exc:
            logger.warning("FTS search failed; falling back to LIKE search: %s", exc)
            self._fts_enabled = False
            return []
        results = [dict(row) for row in rows]
        for row in results:
            row["retrieval_source"] = "sqlite_fts"
        return results

    def _search_sqlite_like(
        self,
        *,
        user_id: int,
        query: str,
        limit: int,
        min_trust_score: float,
    ) -> list[dict[str, Any]]:
        normalized = (query or "").strip()
        if not normalized:
            return []
        pattern = f"%{normalized}%"
        sql = f"""
            SELECT *
            FROM {_FACTS_TABLE}
            WHERE user_id = ?
              AND trust_score >= ?
              AND (
                  entity LIKE ? COLLATE NOCASE
                  OR key LIKE ? COLLATE NOCASE
                  OR value LIKE ? COLLATE NOCASE
              )
            ORDER BY trust_score DESC, updated_at DESC, id DESC
            LIMIT ?
        """
        with self._connect() as conn:
            rows = conn.execute(sql, (user_id, min_trust_score, pattern, pattern, pattern, limit)).fetchall()
        results = [dict(row) for row in rows]
        for row in results:
            row["retrieval_source"] = "sqlite_like"
        return results

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        tokens = _FTS_TOKEN_RE.findall((query or "").lower())
        return " AND ".join(f'"{token}"' for token in tokens[:8])
