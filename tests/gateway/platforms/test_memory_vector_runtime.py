from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from gateway.memory.runtime import MemoryVectorRuntime


@dataclass
class _Status:
    status: str = "CONVERGED"

    def as_dict(self):
        return {
            "status": self.status,
            "alert_status": "OK" if self.status == "CONVERGED" else "WATCH",
        }


class _Bridge:
    def __init__(self):
        self.calls = 0
        self.closed = False
        self.available = False

    def process_vector_sync_batch(self, **_kwargs):
        self.calls += 1
        return None

    def get_vector_sync_status(self):
        return _Status("CONVERGED" if self.available else "DEGRADED")

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_disabled_runtime_never_opens_database_or_bridge(tmp_path):
    built = []
    runtime = MemoryVectorRuntime(
        enabled=False,
        db_path=tmp_path / "absent.sqlite",
        bridge_factory=lambda path: built.append(path),
    )
    assert await runtime.start() is False
    assert built == []
    assert runtime.snapshot["status"] == "DISABLED"
    await runtime.stop()


@pytest.mark.asyncio
async def test_enabled_runtime_fails_closed_when_database_is_missing(tmp_path):
    runtime = MemoryVectorRuntime(enabled=True, db_path=tmp_path / "absent.sqlite")
    assert await runtime.start() is False
    assert runtime.snapshot["status"] == "BLOCKED"
    assert runtime.snapshot["alert_reasons"] == ["DATABASE_NOT_AVAILABLE"]


@pytest.mark.asyncio
async def test_startup_tick_then_periodic_recovery_and_bounded_shutdown(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    db_path.touch()
    bridge = _Bridge()
    runtime = MemoryVectorRuntime(
        enabled=True,
        db_path=db_path,
        interval_seconds=5,
        bridge_factory=lambda _path: bridge,
    )
    assert await runtime.start() is True
    for _ in range(200):
        if runtime.snapshot["status"] == "DEGRADED":
            break
        await asyncio.sleep(0.001)
    assert bridge.calls == 1
    assert runtime.snapshot["status"] == "DEGRADED"

    bridge.available = True
    runtime._stop_event.set()
    await runtime._task
    runtime._task = None
    runtime._stop_event.clear()
    runtime.interval_seconds = 0.001
    assert await runtime.start() is True
    for _ in range(200):
        if bridge.calls >= 2:
            break
        await asyncio.sleep(0.001)
    assert bridge.calls >= 2
    assert runtime.snapshot["status"] == "CONVERGED"
    await runtime.stop()
    assert bridge.closed is True


@pytest.mark.asyncio
async def test_start_is_single_owner_per_runtime(tmp_path):
    db_path = tmp_path / "memory.sqlite"
    db_path.touch()
    bridge = _Bridge()
    runtime = MemoryVectorRuntime(
        enabled=True, db_path=db_path, bridge_factory=lambda _path: bridge
    )
    assert await runtime.start() is True
    first_task = runtime._task
    assert await runtime.start() is True
    assert runtime._task is first_task
    await runtime.stop()
