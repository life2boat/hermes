from __future__ import annotations

import sqlite3
import threading

import pytest

from gateway.memory.convergence import META_TABLE, OUTBOX_TABLE
from gateway.memory.identity import migrate_identity_schema
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge


class BarrierAdapter:
    enabled = True

    def __init__(self):
        self.barrier = threading.Barrier(2)
        self.calls = 0
        self.points = {}

    def upsert_fact(self, **kwargs):
        self.calls += 1
        self.barrier.wait(timeout=2)
        self.points[(kwargs["user_id"], kwargs["sqlite_id"])] = kwargs["payload"]
        return True

    def delete_fact(self, **kwargs):
        self.calls += 1
        self.barrier.wait(timeout=2)
        self.points.pop((kwargs["user_id"], kwargs["sqlite_id"]), None)
        return True

    def search(self, **_kwargs):
        return []


class MixedBarrierAdapter(BarrierAdapter):
    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    def upsert_fact(self, **kwargs):
        with self._lock:
            call = self.calls
            self.calls += 1
        self.barrier.wait(timeout=2)
        if call == 0:
            return False
        self.points[(kwargs["user_id"], kwargs["sqlite_id"])] = kwargs["payload"]
        return True


def _seed_disabled(db_path, monkeypatch, *, count=1):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    bridge = HealBiteMemoryBridge(db_path, background_write=False)
    for user_id in range(1, count + 1):
        bridge.upsert_fact(
            user_id=user_id,
            entity="profile",
            key="goal",
            value="synthetic",
        )
    bridge.close()


def test_two_workers_delivering_same_operation_converge_safely(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _seed_disabled(db_path, monkeypatch)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    adapter = BarrierAdapter()
    first = HealBiteMemoryBridge(db_path, qdrant_adapter=adapter, background_write=False)
    second = HealBiteMemoryBridge(db_path, qdrant_adapter=adapter, background_write=False)
    errors = []

    def run(bridge):
        try:
            bridge.process_vector_sync_batch()
        except Exception as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(bridge,)) for bridge in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert all(not thread.is_alive() for thread in threads)
    assert adapter.calls == 2
    assert first.get_vector_sync_status().status == "CONVERGED"
    assert list(adapter.points) == [(1, 1)]
    first.close()
    second.close()


def test_operational_health_distinguishes_watch_alert_and_blocked(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _seed_disabled(db_path, monkeypatch)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    bridge = HealBiteMemoryBridge(db_path, qdrant_adapter=BarrierAdapter(), background_write=False)
    bridge._convergence.clock = lambda: 100.0
    bridge._convergence.alert_age_seconds = 600.0
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"UPDATE {OUTBOX_TABLE} SET created_at = 99")
    status = bridge.get_vector_sync_status()
    assert (status.status, status.alert_status, status.alert_reasons) == (
        "PENDING",
        "WATCH",
        (),
    )
    bridge._convergence.clock = lambda: 1000.0
    status = bridge.get_vector_sync_status()
    assert status.alert_status == "ALERT"
    assert status.alert_reasons == ("STALE_BACKLOG",)
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"UPDATE {OUTBOX_TABLE} SET state = 'BLOCKED'")
    status = bridge.get_vector_sync_status()
    assert status.status == "BLOCKED"
    assert "BLOCKED_WORK" in status.alert_reasons
    assert "synthetic" not in repr(status.as_dict())
    bridge.close()


def test_one_worker_retry_cannot_overwrite_other_worker_success(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _seed_disabled(db_path, monkeypatch)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    adapter = MixedBarrierAdapter()
    bridges = [
        HealBiteMemoryBridge(db_path, qdrant_adapter=adapter, background_write=False)
        for _ in range(2)
    ]
    threads = [
        threading.Thread(target=bridge.process_vector_sync_batch) for bridge in bridges
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert all(not thread.is_alive() for thread in threads)
    assert bridges[0].get_vector_sync_status().status == "CONVERGED"
    assert list(adapter.points) == [(1, 1)]
    for bridge in bridges:
        bridge.close()


def test_transient_sqlite_busy_does_not_erase_backlog(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _seed_disabled(db_path, monkeypatch)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    bridge = HealBiteMemoryBridge(db_path, qdrant_adapter=BarrierAdapter(), background_write=False)
    lock = sqlite3.connect(db_path, timeout=0.01)
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            bridge.process_vector_sync_batch()
    finally:
        lock.rollback()
        lock.close()
    assert bridge.get_vector_sync_status().pending_count == 1
    bridge.close()


def test_reconciler_liveness_alert_is_machine_readable(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _seed_disabled(db_path, monkeypatch)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")

    class SuccessAdapter:
        enabled = True

        def upsert_fact(self, **_kwargs):
            return True

        delete_fact = upsert_fact

        def search(self, **_kwargs):
            return []

    bridge = HealBiteMemoryBridge(db_path, qdrant_adapter=SuccessAdapter(), background_write=False)
    bridge._convergence.alert_age_seconds = 600
    bridge._convergence.clock = lambda: 100
    bridge.process_vector_sync_batch()
    bridge._convergence.clock = lambda: 701
    status = bridge.get_vector_sync_status()
    assert status.status == "CONVERGED"
    assert status.alert_status == "ALERT"
    assert status.alert_reasons == ("RECONCILER_STALE",)
    bridge.close()


def test_last_reconciliation_timestamp_is_additive_and_updated(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    _seed_disabled(db_path, monkeypatch)
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")

    class SuccessAdapter:
        enabled = True

        def upsert_fact(self, **_kwargs):
            return True

        delete_fact = upsert_fact

        def search(self, **_kwargs):
            return []

    bridge = HealBiteMemoryBridge(db_path, qdrant_adapter=SuccessAdapter(), background_write=False)
    bridge._convergence.clock = lambda: 123.0
    bridge.process_vector_sync_batch()
    status = bridge.get_vector_sync_status()
    assert status.last_reconciliation_at == 123.0
    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({META_TABLE})")}
    assert "last_reconciliation_at" in columns
    bridge.close()


def test_representative_legacy_migration_is_idempotent_and_private(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE memory_os_facts (
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
            """
        )
        conn.executemany(
            "INSERT INTO memory_os_facts(user_id, entity, key, value) VALUES (?, 'profile', 'goal', ?)",
            [(user_id, "private-fact-content") for user_id in range(1, 501)],
        )
        migrate_identity_schema(conn, legacy_epoch_uuid="00000000-0000-4000-8000-000000000000")
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    first = HealBiteMemoryBridge(db_path, background_write=False)
    first.close()
    second = HealBiteMemoryBridge(db_path, background_write=False)
    second.close()
    with sqlite3.connect(db_path) as conn:
        seeded = conn.execute(f"SELECT COUNT(*) FROM {OUTBOX_TABLE}").fetchone()[0]
        duplicate = conn.execute(
            f"""SELECT COUNT(*) FROM (
                SELECT user_id, fact_id, fact_revision, operation, COUNT(*) c
                FROM {OUTBOX_TABLE} GROUP BY 1,2,3,4 HAVING c > 1
            )"""
        ).fetchone()[0]
        outbox_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({OUTBOX_TABLE})")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert seeded == 500
    assert duplicate == 0
    assert "value" not in outbox_columns
    assert integrity == "ok"
    assert fk == []
