from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

from gateway.memory.settings import env_string, env_int
from gateway.memory.graph_convergence import (
    GraphConvergenceIntegrityError,
    converge_user_graph,
)
from gateway.memory.graph_query import (
    GraphFactQuery,
    GraphStructuralMatch,
    GraphReadIntegrityError,
    read_graph_context,
)
from gateway.memory.graph_store import (
    GraphStoreError,
    validate_memory_graph_store_schema,
)
from gateway.memory.graph_projection import (
    GraphVerificationError,
    ProjectionError,
)

logger = logging.getLogger(__name__)


class GraphRuntimeMode(Enum):
    DISABLED = "disabled"
    SHADOW = "shadow"


class GraphRuntimeStatus(Enum):
    DISABLED = "DISABLED"
    NOT_STARTED = "NOT_STARTED"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    BLOCKED = "BLOCKED"


class GraphRuntimeReasonCode(Enum):
    DATABASE_NOT_AVAILABLE = "DATABASE_NOT_AVAILABLE"
    GRAPH_SCHEMA_NOT_CURRENT = "GRAPH_SCHEMA_NOT_CURRENT"
    UNSUPPORTED_GRAPH_RUNTIME_MODE = "UNSUPPORTED_GRAPH_RUNTIME_MODE"
    GRAPH_INTEGRITY_FAILURE = "GRAPH_INTEGRITY_FAILURE"
    CONVERGENCE_INTEGRITY_FAILURE = "CONVERGENCE_INTEGRITY_FAILURE"
    SOURCE_CHURN_RETRY_EXHAUSTED = "SOURCE_CHURN_RETRY_EXHAUSTED"
    QUEUE_CAPACITY_REACHED = "QUEUE_CAPACITY_REACHED"
    WORKER_EXCEPTION = "WORKER_EXCEPTION"
    OK = "OK"


@dataclass(frozen=True, slots=True)
class GraphRuntimeContextResult:
    status: str
    user_id: int
    query: GraphFactQuery
    matches: tuple[GraphStructuralMatch, ...]
    matched_count: int
    convergence_scheduled: bool
    safe_reason_code: str | None


def resolve_graph_context(
    conn: sqlite3.Connection,
    *,
    runtime: MemoryGraphRuntime,
    user_id: int,
    query: GraphFactQuery,
) -> GraphRuntimeContextResult:
    if runtime.mode == GraphRuntimeMode.DISABLED or runtime.status != GraphRuntimeStatus.RUNNING:
        return GraphRuntimeContextResult(
            status="DISABLED",
            user_id=user_id,
            query=query,
            matches=(),
            matched_count=0,
            convergence_scheduled=False,
            safe_reason_code=None,
        )

    try:
        res = read_graph_context(conn, user_id=user_id, query=query)
    except (GraphReadIntegrityError, GraphStoreError, GraphVerificationError, ProjectionError):
        return GraphRuntimeContextResult(
            status="BLOCKED_INTEGRITY",
            user_id=user_id,
            query=query,
            matches=(),
            matched_count=0,
            convergence_scheduled=False,
            safe_reason_code="GRAPH_INTEGRITY_FAILURE",
        )

    if res.status.name == "READY":
        return GraphRuntimeContextResult(
            status="READY",
            user_id=user_id,
            query=query,
            matches=res.matches,
            matched_count=res.matched_count,
            convergence_scheduled=False,
            safe_reason_code=None,
        )

    # For MISSING_GRAPH and STALE_GRAPH
    scheduled = runtime.schedule_convergence(user_id)
    return GraphRuntimeContextResult(
        status="NO_GRAPH_CONTEXT",
        user_id=user_id,
        query=query,
        matches=(),
        matched_count=0,
        convergence_scheduled=scheduled,
        safe_reason_code="QUEUE_CAPACITY_REACHED" if not scheduled else None,
    )


class MemoryGraphRuntime:
    def __init__(
        self,
        *,
        mode: GraphRuntimeMode,
        db_path: Path | str | None,
        queue_capacity: int = 128,
        max_attempts: int = 3,
    ) -> None:
        self.mode = mode
        self.db_path = None if db_path is None else Path(db_path)
        self.queue_capacity = queue_capacity
        self.max_attempts = max_attempts
        
        self.status = GraphRuntimeStatus.DISABLED if mode == GraphRuntimeMode.DISABLED else GraphRuntimeStatus.NOT_STARTED
        
        self._queue: deque[int] = deque()
        self._pending_users: set[int] = set()
        self._queue_lock = threading.Lock()
        
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._condition = asyncio.Condition()

        self._counters = {
            "pending_users": 0,
            "processed_users": 0,
            "current_noop_count": 0,
            "rebuilt_missing_count": 0,
            "rebuilt_stale_count": 0,
            "churn_exhausted_count": 0,
            "integrity_block_count": 0,
            "queue_rejected_count": 0,
        }

    @classmethod
    def from_environment(cls, db_path: Path | str | None) -> "MemoryGraphRuntime":
        raw_mode = env_string("MEMORY_GRAPH_MODE", "disabled").lower()
        if raw_mode == "disabled":
            mode = GraphRuntimeMode.DISABLED
        elif raw_mode == "shadow":
            mode = GraphRuntimeMode.SHADOW
        else:
            # We will start in NOT_STARTED but immediately go to BLOCKED in start()
            mode = GraphRuntimeMode.SHADOW # Temp fallback, will be blocked
        
        queue_capacity = env_int("MEMORY_GRAPH_QUEUE_CAPACITY", 128, min_val=1, max_val=1024)
        max_attempts = env_int("MEMORY_GRAPH_CONVERGENCE_MAX_ATTEMPTS", 3, min_val=1, max_val=5)
        
        runtime = cls(
            mode=mode,
            db_path=db_path,
            queue_capacity=queue_capacity,
            max_attempts=max_attempts,
        )
        if raw_mode not in ("disabled", "shadow"):
            runtime._unsupported_mode = True
        return runtime

    def schedule_convergence(self, user_id: int) -> bool:
        with self._queue_lock:
            if user_id in self._pending_users:
                return True
            if len(self._queue) >= self.queue_capacity:
                self._counters["queue_rejected_count"] += 1
                return False
            self._queue.append(user_id)
            self._pending_users.add(user_id)
            self._counters["pending_users"] = len(self._queue)
        
        if self._task:
            try:
                asyncio.get_running_loop().call_soon_threadsafe(
                    self._notify_worker
                )
            except RuntimeError:
                pass
        return True

    def _notify_worker(self) -> None:
        async def _notify():
            async with self._condition:
                self._condition.notify()
        try:
            asyncio.create_task(_notify())
        except RuntimeError:
            pass

    async def start(self) -> bool:
        if self.mode == GraphRuntimeMode.DISABLED:
            self._publish()
            return False
            
        if getattr(self, "_unsupported_mode", False):
            self.status = GraphRuntimeStatus.BLOCKED
            self._publish(GraphRuntimeReasonCode.UNSUPPORTED_GRAPH_RUNTIME_MODE)
            return False

        if self._task is not None:
            return True

        if self.db_path is None or not self.db_path.is_file():
            self.status = GraphRuntimeStatus.BLOCKED
            self._publish(GraphRuntimeReasonCode.DATABASE_NOT_AVAILABLE)
            return False

        # Validate schema
        try:
            with sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True) as conn:
                validate_memory_graph_store_schema(conn)
        except Exception:
            self.status = GraphRuntimeStatus.BLOCKED
            self._publish(GraphRuntimeReasonCode.GRAPH_SCHEMA_NOT_CURRENT)
            return False

        self.status = GraphRuntimeStatus.RUNNING
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="memory-graph-convergence")
        self._publish()
        return True

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._stop_event.set()
        
        async with self._condition:
            self._condition.notify_all()
            
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._publish()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            user_id = None
            with self._queue_lock:
                if self._queue:
                    user_id = self._queue.popleft()
                    self._pending_users.remove(user_id)
                    self._counters["pending_users"] = len(self._queue)
                    
            if user_id is None:
                async with self._condition:
                    try:
                        await asyncio.wait_for(self._condition.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                continue
                
            await asyncio.to_thread(self._converge_user, user_id)
            self._counters["processed_users"] += 1
            self._publish()

    def _converge_user(self, user_id: int) -> None:
        assert self.db_path is not None
        try:
            with sqlite3.connect(self.db_path, isolation_level=None) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    res = converge_user_graph(conn, user_id=user_id, max_attempts=self.max_attempts)
                    conn.execute("COMMIT")
                    
                    if res.status.name == "NOOP_CURRENT":
                        self._counters["current_noop_count"] += 1
                    elif res.status.name == "REBUILT_MISSING":
                        self._counters["rebuilt_missing_count"] += 1
                    elif res.status.name == "REBUILT_STALE":
                        self._counters["rebuilt_stale_count"] += 1
                    elif res.status.name == "SOURCE_CHURN_RETRY_EXHAUSTED":
                        self._counters["churn_exhausted_count"] += 1
                        
                except Exception as e:
                    conn.execute("ROLLBACK")
                    raise e
                    
        except GraphConvergenceIntegrityError:
            self._counters["integrity_block_count"] += 1
        except Exception as exc:
            logger.error(f"Worker exception: {exc}")
            self.status = GraphRuntimeStatus.DEGRADED
            self._publish(GraphRuntimeReasonCode.WORKER_EXCEPTION)

    def _publish(self, reason: GraphRuntimeReasonCode | None = None) -> None:
        try:
            from gateway.status import write_runtime_status
            
            snapshot = {
                "status": self.status.value,
                "mode": self.mode.value,
                "alert_status": "OK" if self.status in (GraphRuntimeStatus.DISABLED, GraphRuntimeStatus.RUNNING) else "ALERT",
                **self._counters,
            }
            if reason:
                snapshot["alert_reasons"] = [reason.value]
                
            write_runtime_status(memory_graph=snapshot)
        except Exception:
            pass
