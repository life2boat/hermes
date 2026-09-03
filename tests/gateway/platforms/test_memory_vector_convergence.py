from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

import pytest

from gateway.memory.convergence import OUTBOX_TABLE
from gateway.memory.qdrant_adapter import QdrantMemoryHit
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge


@dataclass
class FakeQdrant:
    enabled: bool = True
    fail_upserts: set[int] = field(default_factory=set)
    fail_deletes: set[int] = field(default_factory=set)
    points: dict[tuple[int, int], dict] = field(default_factory=dict)
    upsert_calls: list[dict] = field(default_factory=list)
    delete_calls: list[dict] = field(default_factory=list)

    def upsert_fact(self, **kwargs):
        assert kwargs["wait"] is True
        self.upsert_calls.append(dict(kwargs))
        if kwargs["sqlite_id"] in self.fail_upserts:
            return False
        self.points[(kwargs["user_id"], kwargs["sqlite_id"])] = dict(kwargs["payload"])
        return True

    def delete_fact(self, **kwargs):
        assert kwargs["wait"] is True
        self.delete_calls.append(dict(kwargs))
        if kwargs["sqlite_id"] in self.fail_deletes:
            return False
        self.points.pop((kwargs["user_id"], kwargs["sqlite_id"]), None)
        return True

    def search(self, **_kwargs):
        return []


def _bridge(tmp_path, monkeypatch, qdrant=None, *, enabled=True, name="memory.sqlite"):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true" if enabled else "false")
    return HealBiteMemoryBridge(
        tmp_path / name,
        qdrant_adapter=qdrant,
        background_write=False,
    )


def _fact(bridge, *, user_id=101, value="alpha"):
    return bridge.upsert_fact(
        user_id=user_id,
        entity="profile",
        key="goal",
        value=value,
        source="test",
        trust_score=0.9,
    )


def _outbox_rows(db_path):
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {OUTBOX_TABLE} ORDER BY id")]


def test_atomic_enqueue_failure_rolls_back_canonical_mutation(tmp_path, monkeypatch):
    bridge = _bridge(tmp_path, monkeypatch, FakeQdrant())

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("fault injection before commit")

    monkeypatch.setattr(bridge._convergence, "enqueue_upsert", fail_enqueue)
    with pytest.raises(RuntimeError, match="fault injection"):
        _fact(bridge)

    assert list(bridge.iter_facts()) == []
    assert _outbox_rows(bridge.db_path) == []
    bridge.close()


def test_restart_recovers_commit_before_qdrant_call(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    fact_id = _fact(disabled)
    assert disabled.get_vector_sync_status().status == "PENDING"
    disabled.close()

    fake = FakeQdrant()
    restarted = _bridge(tmp_path, monkeypatch, fake)
    result = restarted.process_vector_sync_batch()

    assert result.status == "CONVERGED"
    assert fake.points[(101, fact_id)]["vector_revision"] == 1
    restarted.close()


def test_qdrant_outage_keeps_durable_retryable_upsert(tmp_path, monkeypatch):
    now = [100.0]
    fake = FakeQdrant(fail_upserts={1})
    bridge = _bridge(tmp_path, monkeypatch, fake)
    bridge._convergence.clock = lambda: now[0]

    fact_id = _fact(bridge)
    status = bridge.get_vector_sync_status()
    assert fact_id == 1
    assert status.status == "DEGRADED"
    assert status.retryable_count == 1
    assert status.last_error_class == "QDRANT_OPERATION_FAILED"
    assert "alpha" not in repr(status.as_dict())
    bridge.close()


def test_delete_is_canonical_and_durable_while_qdrant_is_down(tmp_path, monkeypatch):
    now = [200.0]
    fake = FakeQdrant()
    bridge = _bridge(tmp_path, monkeypatch, fake)
    bridge._convergence.clock = lambda: now[0]
    fact_id = _fact(bridge)
    fake.fail_deletes.add(fact_id)

    bridge.delete_fact(sqlite_id=fact_id, user_id=101)

    assert bridge.get_fact(sqlite_id=fact_id, user_id=101) is None
    assert bridge.get_vector_sync_status().status == "DEGRADED"
    stale = QdrantMemoryHit(
        sqlite_id=fact_id,
        payload={"sqlite_id": fact_id, "user_id": 101},
        score=1.0,
    )
    assert bridge._hydrate_qdrant_hits(user_id=101, hits=[stale], min_trust_score=0) == []
    bridge.close()


def test_success_before_ack_crash_replays_idempotently(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    fact_id = _fact(disabled)
    disabled.close()
    fake = FakeQdrant()
    bridge = _bridge(tmp_path, monkeypatch, fake)
    original_ack = bridge._convergence._ack

    def crash_before_ack(*_args, **_kwargs):
        raise RuntimeError("fault injection after derived mutation")

    monkeypatch.setattr(bridge._convergence, "_ack", crash_before_ack)
    with pytest.raises(RuntimeError, match="after derived mutation"):
        bridge.process_vector_sync_batch()
    assert len(fake.upsert_calls) == 1
    assert len(_outbox_rows(bridge.db_path)) == 1

    monkeypatch.setattr(bridge._convergence, "_ack", original_ack)
    bridge.process_vector_sync_batch()
    assert len(fake.upsert_calls) == 2
    assert list(fake.points) == [(101, fact_id)]
    assert bridge.get_vector_sync_status().status == "CONVERGED"
    bridge.close()


def test_late_old_upsert_cannot_overwrite_newer_revision(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    fact_id = _fact(disabled, value="old")
    _fact(disabled, value="new")
    disabled.close()

    fake = FakeQdrant()
    bridge = _bridge(tmp_path, monkeypatch, fake)
    result = bridge.process_vector_sync_batch()

    assert result.superseded == 1
    assert len(fake.upsert_calls) == 1
    assert fake.points[(101, fact_id)]["value"] == "new"
    assert fake.points[(101, fact_id)]["vector_revision"] == 2
    bridge.close()


def test_late_upsert_after_delete_cannot_resurrect_fact(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    fact_id = _fact(disabled)
    disabled.delete_fact(sqlite_id=fact_id, user_id=101)
    disabled.close()

    fake = FakeQdrant(points={(101, fact_id): {"value": "stale"}})
    bridge = _bridge(tmp_path, monkeypatch, fake)
    bridge.process_vector_sync_batch(batch_size=10)

    assert (101, fact_id) not in fake.points
    assert fake.upsert_calls == []
    assert len(fake.delete_calls) == 2
    bridge.close()


def test_late_delete_cannot_remove_recreated_logical_fact(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    old_id = _fact(disabled, value="old")
    disabled.delete_fact(sqlite_id=old_id, user_id=101)
    new_id = _fact(disabled, value="recreated")
    disabled.close()

    fake = FakeQdrant(points={(101, old_id): {"value": "old"}})
    bridge = _bridge(tmp_path, monkeypatch, fake)
    bridge.process_vector_sync_batch(batch_size=10)

    assert new_id > old_id
    assert (101, old_id) not in fake.points
    assert fake.points[(101, new_id)]["value"] == "recreated"
    bridge.close()


def test_poison_operation_is_blocked_without_qdrant_call(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    _fact(disabled)
    disabled.close()
    with sqlite3.connect(tmp_path / "memory.sqlite") as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"UPDATE {OUTBOX_TABLE} SET operation = 'POISON'")

    fake = FakeQdrant()
    bridge = _bridge(tmp_path, monkeypatch, fake)
    result = bridge.process_vector_sync_batch()

    assert result.blocked == 1
    assert bridge.get_vector_sync_status().status == "BLOCKED"
    assert fake.upsert_calls == fake.delete_calls == []
    bridge.close()


def test_foreign_owner_operation_fails_closed(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    _fact(disabled, user_id=101)
    disabled.close()
    with sqlite3.connect(tmp_path / "memory.sqlite") as conn:
        conn.execute(f"UPDATE {OUTBOX_TABLE} SET user_id = 202")

    fake = FakeQdrant()
    bridge = _bridge(tmp_path, monkeypatch, fake)
    bridge.process_vector_sync_batch()

    status = bridge.get_vector_sync_status()
    assert status.status == "BLOCKED"
    assert status.last_error_class == "OWNER_MISMATCH"
    assert fake.upsert_calls == fake.delete_calls == []
    bridge.close()


def test_partial_batch_failure_isolated_and_observable(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    first = _fact(disabled, user_id=101)
    second = disabled.upsert_fact(
        user_id=202,
        entity="profile",
        key="goal",
        value="beta",
        source="test",
    )
    disabled.close()

    fake = FakeQdrant(fail_upserts={first})
    bridge = _bridge(tmp_path, monkeypatch, fake)
    result = bridge.process_vector_sync_batch(batch_size=10)

    assert result.retried == 1
    assert result.succeeded == 1
    assert (202, second) in fake.points
    assert bridge.get_vector_sync_status().status == "DEGRADED"
    bridge.close()


def test_vector_disabled_backlog_converges_after_reenable(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    fact_id = _fact(disabled)
    assert disabled.process_vector_sync_batch().processed == 0
    assert disabled.get_vector_sync_status().status == "PENDING"
    disabled.close()

    fake = FakeQdrant()
    enabled = _bridge(tmp_path, monkeypatch, fake)
    enabled.process_vector_sync_batch()
    assert (101, fact_id) in fake.points
    assert enabled.get_vector_sync_status().status == "CONVERGED"
    enabled.close()


def test_batch_size_is_bounded_and_queue_selection_is_indexed(tmp_path, monkeypatch):
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    for user_id in range(1, 6):
        _fact(disabled, user_id=user_id)
    disabled.close()

    fake = FakeQdrant()
    bridge = _bridge(tmp_path, monkeypatch, fake)
    result = bridge.process_vector_sync_batch(batch_size=2)
    assert result.processed == 2
    assert result.remaining == 3
    with sqlite3.connect(bridge.db_path) as conn:
        indexes = {row[1] for row in conn.execute(f"PRAGMA index_list({OUTBOX_TABLE})")}
    assert any(name.endswith("_ready") for name in indexes)
    bridge.close()


def test_legacy_schema_migration_seeds_existing_facts_once(tmp_path, monkeypatch):
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
            INSERT INTO memory_os_facts(user_id, entity, key, value)
            VALUES (101, 'profile', 'goal', 'legacy');
            """
        )

    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    from gateway.memory.identity import migrate_identity_schema
    with pytest.raises(sqlite3.DatabaseError, match="LEGACY_EPOCH_AUTHORITY_REQUIRED"):
        HealBiteMemoryBridge(db_path, background_write=False)
    with sqlite3.connect(db_path) as conn:
        migrate_identity_schema(conn, legacy_epoch_uuid="10000000-0000-4000-8000-000000000001")
    first = HealBiteMemoryBridge(db_path, background_write=False)
    assert len(_outbox_rows(db_path)) == 1
    first.close()
    second = HealBiteMemoryBridge(db_path, background_write=False)
    assert len(_outbox_rows(db_path)) == 1
    second.close()

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_os_facts)")}
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert "vector_revision" in columns
    assert integrity == "ok"
    assert fk_violations == []


def test_transient_sqlite_lock_recovered(tmp_path, monkeypatch):
    """Test that a transient SQLITE_BUSY / database is locked error recovers on retry."""
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    _fact(disabled)
    disabled.close()

    fake = FakeQdrant()
    bridge = _bridge(tmp_path, monkeypatch, fake)
    real_connect = bridge._convergence._connect
    attempts = [0]

    def flaky_connect(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(bridge._convergence, "_connect", flaky_connect)
    result = bridge.process_vector_sync_batch()
    assert result.status == "CONVERGED"
    assert attempts[0] > 1
    assert len(fake.upsert_calls) == 1
    bridge.close()


def test_non_lock_sqlite_error_propagates(tmp_path, monkeypatch):
    """Test that non-lock SQLite errors (schema, corruption, etc.) propagate immediately."""
    bridge = _bridge(tmp_path, monkeypatch, FakeQdrant())

    def corrupt_connect(*args, **kwargs):
        raise sqlite3.OperationalError("no such table: memory_vector_outbox")

    monkeypatch.setattr(bridge._convergence, "_connect", corrupt_connect)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        bridge.process_vector_sync_batch()
    bridge.close()


def test_retry_is_bounded(tmp_path, monkeypatch):
    """Test that persistent lock failure does not wait indefinitely and raises OperationalError."""
    bridge = _bridge(tmp_path, monkeypatch, FakeQdrant())
    attempts = [0]

    def locked_connect(*args, **kwargs):
        attempts[0] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(bridge._convergence, "_connect", locked_connect)
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        bridge.process_vector_sync_batch()
    from gateway.memory.convergence import _MAX_LOCK_RETRIES

    assert attempts[0] == _MAX_LOCK_RETRIES
    bridge.close()


def test_time_budget_preserved(tmp_path, monkeypatch):
    """Test that execution stops when time budget is exceeded under real SQLite lock contention.

    Proves that attempt 0 does not wait for the default 1-second busy timeout when a smaller
    time budget is requested.
    """
    disabled = _bridge(tmp_path, monkeypatch, FakeQdrant(enabled=False), enabled=False)
    _fact(disabled)
    disabled.close()

    bridge = _bridge(tmp_path, monkeypatch, FakeQdrant())
    db_path = tmp_path / "memory.sqlite"
    lock_conn = sqlite3.connect(db_path, isolation_level=None)
    lock_conn.execute("BEGIN EXCLUSIVE")
    try:
        start = time.perf_counter()
        try:
            bridge.process_vector_sync_batch(time_budget_seconds=0.05)
        except Exception:
            pass
        duration = time.perf_counter() - start
        assert duration < 0.8
    finally:
        lock_conn.execute("ROLLBACK")
        lock_conn.close()
        bridge.close()
