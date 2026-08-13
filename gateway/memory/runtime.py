from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from gateway.memory.analytics import resolve_analytics_db_path
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter
from gateway.memory.settings import env_flag
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_SECONDS = 60.0
_MIN_INTERVAL_SECONDS = 5.0
_MAX_INTERVAL_SECONDS = 3600.0
_SHUTDOWN_TIMEOUT_SECONDS = 5.0


def _bounded_interval(raw: str | None) -> float:
    try:
        value = float(raw) if raw is not None else _DEFAULT_INTERVAL_SECONDS
    except (TypeError, ValueError):
        value = _DEFAULT_INTERVAL_SECONDS
    return min(_MAX_INTERVAL_SECONDS, max(_MIN_INTERVAL_SECONDS, value))


class MemoryVectorRuntime:
    """Gateway-owned bounded reconciliation lifecycle for derived vector state."""

    def __init__(
        self,
        *,
        enabled: bool,
        db_path: str | Path | None,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        bridge_factory: Callable[[Path], HealBiteMemoryBridge] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.db_path = None if db_path is None else Path(db_path)
        self.interval_seconds = min(
            _MAX_INTERVAL_SECONDS,
            max(_MIN_INTERVAL_SECONDS, float(interval_seconds)),
        )
        self._bridge_factory = bridge_factory or self._default_bridge_factory
        self._bridge: HealBiteMemoryBridge | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._snapshot: dict[str, Any] = {
            "status": "DISABLED" if not self.enabled else "NOT_STARTED",
            "alert_status": "OK" if not self.enabled else "WATCH",
        }

    @classmethod
    def from_environment(cls) -> "MemoryVectorRuntime":
        return cls(
            enabled=env_flag("MEMORY_VECTOR_ENABLED", default=False),
            db_path=resolve_analytics_db_path(),
            interval_seconds=_bounded_interval(
                os.getenv("MEMORY_VECTOR_RECONCILE_INTERVAL_SECONDS")
            ),
        )

    @staticmethod
    def _default_bridge_factory(db_path: Path) -> HealBiteMemoryBridge:
        adapter = QdrantMemoryAdapter(enabled=True)
        return HealBiteMemoryBridge(
            db_path,
            qdrant_adapter=adapter,
            background_write=False,
            ensure_schema_on_init=False,
        )

    @property
    def snapshot(self) -> dict[str, Any]:
        return dict(self._snapshot)

    async def start(self) -> bool:
        if not self.enabled:
            self._publish({"status": "DISABLED", "alert_status": "OK"})
            return False
        if self._task is not None:
            return True
        if self.db_path is None or not self.db_path.is_file():
            self._publish(
                {
                    "status": "BLOCKED",
                    "alert_status": "ALERT",
                    "alert_reasons": ["DATABASE_NOT_AVAILABLE"],
                }
            )
            logger.warning(
                "[MemoryVectorRuntime] startup blocked: canonical database is unavailable"
            )
            return False
        try:
            self._bridge = self._bridge_factory(self.db_path)
        except Exception as exc:
            self._publish(
                {
                    "status": "BLOCKED",
                    "alert_status": "ALERT",
                    "alert_reasons": ["INITIALIZATION_FAILED"],
                    "last_error_class": exc.__class__.__name__,
                }
            )
            logger.warning(
                "[MemoryVectorRuntime] initialization failed: error_type=%s",
                exc.__class__.__name__,
            )
            return False
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run(), name="memory-vector-reconciliation"
        )
        return True

    async def _run(self) -> None:
        assert self._bridge is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.to_thread(
                    self._bridge.process_vector_sync_batch,
                    batch_size=25,
                    time_budget_seconds=2.0,
                )
                self._publish(self._bridge.get_vector_sync_status().as_dict())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._publish(
                    {
                        "status": "DEGRADED",
                        "alert_status": "ALERT",
                        "alert_reasons": ["RECONCILIATION_EXCEPTION"],
                        "last_error_class": exc.__class__.__name__,
                    }
                )
                logger.warning(
                    "[MemoryVectorRuntime] reconciliation failed: error_type=%s",
                    exc.__class__.__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_seconds
                )
            except asyncio.TimeoutError:
                continue

    def _publish(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = dict(snapshot)
        try:
            from gateway.status import write_runtime_status

            write_runtime_status(memory_vector=self._snapshot)
        except Exception:
            logger.debug("Memory vector runtime status publication failed", exc_info=True)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._stop_event.set()
        timed_out = False
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                timed_out = True
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                self._publish(
                    {
                        "status": "PENDING",
                        "alert_status": "WATCH",
                        "alert_reasons": ["SHUTDOWN_DEFERRED_TO_DURABLE_RECOVERY"],
                    }
                )
        if timed_out:
            # asyncio.to_thread cannot stop an already-running call. Keep the
            # bridge alive rather than closing resources underneath it; the
            # durable outbox is recovered on the next startup.
            return
        if self._bridge is not None:
            await asyncio.to_thread(self._bridge.close)
            self._bridge = None
