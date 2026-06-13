from __future__ import annotations

import atexit
import logging
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ANALYTICS_TABLE = "memory_analytics_logs"

_ANALYTICS_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {ANALYTICS_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    found_count INTEGER NOT NULL DEFAULT 0,
    processing_time_ms REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_{ANALYTICS_TABLE}_event_type_created_at
    ON {ANALYTICS_TABLE}(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_{ANALYTICS_TABLE}_user_id_created_at
    ON {ANALYTICS_TABLE}(user_id, created_at);
"""

_DEFAULT_ANALYTICS_DB = Path("/home/hermes/healbite.db")
_GLOBAL_ANALYTICS_LOCK = threading.Lock()
_GLOBAL_ANALYTICS_LOGGER: MemoryAnalyticsLogger | None = None


def resolve_analytics_db_path(db_path: str | Path | None = None) -> Path | None:
    if db_path is not None:
        return Path(db_path)
    for env_name in ("MEMORY_ANALYTICS_DB_PATH", "HEALBITE_DB_PATH"):
        value = os.getenv(env_name, "").strip()
        if value:
            return Path(value)
    if _DEFAULT_ANALYTICS_DB.exists():
        return _DEFAULT_ANALYTICS_DB
    return None


class MemoryAnalyticsLogger:
    """Best-effort SQLite analytics sink for Memory OS observability."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        background_write: bool = True,
        enabled: bool = True,
    ) -> None:
        self.db_path = resolve_analytics_db_path(db_path)
        self.enabled = bool(enabled and self.db_path is not None)
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-analytics")
            if self.enabled and background_write
            else None
        )

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)

    def _connect(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise RuntimeError("analytics database path is not configured")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ensure_schema(self) -> None:
        if not self.enabled or self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready or not self.enabled:
                return
            assert self.db_path is not None
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.executescript(_ANALYTICS_SCHEMA_SQL)
            self._schema_ready = True

    def log_event(
        self,
        *,
        user_id: int | None,
        event_type: str,
        source: str,
        found_count: int,
        processing_time_ms: float,
    ) -> None:
        if not self.enabled:
            return
        payload = {
            "user_id": int(user_id) if user_id is not None else None,
            "event_type": str(event_type).strip() or "unknown",
            "source": str(source).strip() or "unknown",
            "found_count": max(int(found_count), 0),
            "processing_time_ms": max(float(processing_time_ms), 0.0),
        }
        if self._executor is None:
            self._safe_write_event(payload)
            return
        self._executor.submit(self._safe_write_event, payload)

    def log_search(
        self,
        *,
        user_id: int,
        source: str,
        found_count: int,
        processing_time_ms: float,
    ) -> None:
        self.log_event(
            user_id=user_id,
            event_type="search",
            source=source,
            found_count=found_count,
            processing_time_ms=processing_time_ms,
        )

    def log_injection(
        self,
        *,
        user_id: int | None,
        source: str,
        found_count: int,
        processing_time_ms: float,
    ) -> None:
        self.log_event(
            user_id=user_id,
            event_type="injection",
            source=source,
            found_count=found_count,
            processing_time_ms=processing_time_ms,
        )

    def log_rebuild(
        self,
        *,
        user_id: int | None,
        source: str,
        found_count: int,
        processing_time_ms: float,
    ) -> None:
        self.log_event(
            user_id=user_id,
            event_type="rebuild",
            source=source,
            found_count=found_count,
            processing_time_ms=processing_time_ms,
        )

    def _safe_write_event(self, payload: dict[str, Any]) -> None:
        try:
            self.ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO {ANALYTICS_TABLE}(
                        user_id,
                        event_type,
                        source,
                        found_count,
                        processing_time_ms
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        payload["user_id"],
                        payload["event_type"],
                        payload["source"],
                        payload["found_count"],
                        payload["processing_time_ms"],
                    ),
                )
        except Exception as exc:
            logger.debug("Memory analytics write skipped: %s", exc)


def get_default_memory_analytics_logger() -> MemoryAnalyticsLogger:
    global _GLOBAL_ANALYTICS_LOGGER
    with _GLOBAL_ANALYTICS_LOCK:
        if _GLOBAL_ANALYTICS_LOGGER is None:
            _GLOBAL_ANALYTICS_LOGGER = MemoryAnalyticsLogger()
            atexit.register(_GLOBAL_ANALYTICS_LOGGER.close)
        return _GLOBAL_ANALYTICS_LOGGER


def _time_clause(hours: float | int | None) -> tuple[str, list[Any]]:
    if hours is None:
        return "", []
    numeric_hours = float(hours)
    if numeric_hours <= 0:
        raise ValueError("hours must be positive")
    threshold = datetime.now(timezone.utc) - timedelta(hours=numeric_hours)
    return " AND created_at >= ?", [threshold.strftime("%Y-%m-%d %H:%M:%S")]


def compute_memory_analytics_summary(
    db_path: str | Path | None = None,
    *,
    user_id: int | None = None,
    hours: float | int | None = None,
) -> dict[str, float | int]:
    resolved = resolve_analytics_db_path(db_path)
    if resolved is None:
        return {
            "total_searches": 0,
            "qdrant_hits": 0,
            "sqlite_fallbacks": 0,
            "qdrant_hit_rate": 0.0,
            "sqlite_fallback_rate": 0.0,
            "avg_search_latency_ms": 0.0,
            "avg_facts_injected": 0.0,
        }

    conn = sqlite3.connect(resolved)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_ANALYTICS_SCHEMA_SQL)
        params: list[Any] = []
        where_parts = ["event_type = 'search'"]
        if user_id is not None:
            where_parts.append("user_id = ?")
            params.append(int(user_id))
        time_clause, time_params = _time_clause(hours)
        if time_clause:
            where_parts.append(time_clause.removeprefix(" AND "))
            params.extend(time_params)
        search_where = " AND ".join(where_parts)

        injection_params: list[Any] = []
        injection_parts = ["event_type = 'injection'"]
        if user_id is not None:
            injection_parts.append("user_id = ?")
            injection_params.append(int(user_id))
        if time_clause:
            injection_parts.append(time_clause.removeprefix(" AND "))
            injection_params.extend(time_params)
        injection_where = " AND ".join(injection_parts)

        search_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_searches,
                SUM(CASE WHEN source = 'qdrant' AND found_count > 0 THEN 1 ELSE 0 END) AS qdrant_hits,
                SUM(CASE WHEN source IN ('fts5', 'like') THEN 1 ELSE 0 END) AS sqlite_fallbacks,
                AVG(processing_time_ms) AS avg_search_latency_ms
            FROM {ANALYTICS_TABLE}
            WHERE {search_where}
            """,
            params,
        ).fetchone()

        injection_row = conn.execute(
            f"""
            SELECT AVG(found_count) AS avg_facts_injected
            FROM {ANALYTICS_TABLE}
            WHERE {injection_where}
            """,
            injection_params,
        ).fetchone()
    finally:
        conn.close()

    total_searches = int(search_row["total_searches"] or 0)
    qdrant_hits = int(search_row["qdrant_hits"] or 0)
    sqlite_fallbacks = int(search_row["sqlite_fallbacks"] or 0)
    avg_search_latency_ms = float(search_row["avg_search_latency_ms"] or 0.0)
    avg_facts_injected = float(injection_row["avg_facts_injected"] or 0.0)
    qdrant_hit_rate = (qdrant_hits / total_searches * 100.0) if total_searches else 0.0
    sqlite_fallback_rate = (sqlite_fallbacks / total_searches * 100.0) if total_searches else 0.0

    return {
        "total_searches": total_searches,
        "qdrant_hits": qdrant_hits,
        "sqlite_fallbacks": sqlite_fallbacks,
        "qdrant_hit_rate": qdrant_hit_rate,
        "sqlite_fallback_rate": sqlite_fallback_rate,
        "avg_search_latency_ms": avg_search_latency_ms,
        "avg_facts_injected": avg_facts_injected,
    }


def format_memory_analytics_report(summary: dict[str, float | int]) -> str:
    return "\n".join(
        [
            "Memory Analytics Report",
            "=======================",
            f"Total Memory Searches: {int(summary['total_searches'])}",
            f"Qdrant Hit Rate (%): {float(summary['qdrant_hit_rate']):.2f}",
            f"SQLite Fallback Rate (%): {float(summary['sqlite_fallback_rate']):.2f}",
            f"Average Search Latency (ms): {float(summary['avg_search_latency_ms']):.2f}",
            f"Average Facts Injected per LLM Call: {float(summary['avg_facts_injected']):.2f}",
        ]
    )