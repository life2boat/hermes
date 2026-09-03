from __future__ import annotations

import sqlite3

from gateway.memory import convergence
from gateway.memory.convergence import OUTBOX_TABLE
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge


class RaisingAdapter:
    enabled = True

    def __init__(self):
        self.calls = []

    def upsert_fact(self, **kwargs):
        self.calls.append(kwargs["sqlite_id"])
        if kwargs["sqlite_id"] == 1:
            raise ConnectionError("synthetic adapter failure")
        return True

    def delete_fact(self, **_kwargs):
        return True

    def search(self, **_kwargs):
        return []


def _seed(tmp_path, monkeypatch, count=2):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    bridge = HealBiteMemoryBridge(tmp_path / "memory.sqlite", background_write=False)
    for user_id in range(1, count + 1):
        bridge.upsert_fact(
            user_id=user_id,
            entity="profile",
            key="goal",
            value=f"synthetic-{user_id}",
        )
    bridge.close()


def test_adapter_exception_does_not_abort_later_batch_operations(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    adapter = RaisingAdapter()
    bridge = HealBiteMemoryBridge(
        tmp_path / "memory.sqlite",
        qdrant_adapter=adapter,
        background_write=False,
    )

    result = bridge.process_vector_sync_batch(batch_size=10)

    assert result.retried == 1
    assert result.succeeded == 1
    assert adapter.calls == [1, 2]
    bridge.close()


def test_worker_honors_injected_time_budget_without_sleep(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, count=3)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    adapter = RaisingAdapter()
    bridge = HealBiteMemoryBridge(
        tmp_path / "memory.sqlite",
        qdrant_adapter=adapter,
        background_write=False,
    )
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 1.0] + [1.0] * 10)
    monkeypatch.setattr(convergence.time, "perf_counter", lambda: next(ticks))

    result = bridge.process_vector_sync_batch(batch_size=100, time_budget_seconds=0.5)

    assert result.processed == 1
    assert result.remaining == 3
    bridge.close()


def test_non_positive_owner_in_poisoned_outbox_is_blocked(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, count=1)
    with sqlite3.connect(tmp_path / "memory.sqlite") as conn:
        conn.execute(f"UPDATE {OUTBOX_TABLE} SET user_id = 0")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    adapter = RaisingAdapter()
    bridge = HealBiteMemoryBridge(
        tmp_path / "memory.sqlite",
        qdrant_adapter=adapter,
        background_write=False,
    )

    bridge.process_vector_sync_batch()

    assert bridge.get_vector_sync_status().status == "BLOCKED"
    assert adapter.calls == []
    bridge.close()
