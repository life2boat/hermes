"""Post-merge adversarial evidence for Memory fact UUID/epoch safety.

All persistence and derived-vector clients in this module are temporary test
fixtures.  No production runtime or Qdrant service is contacted.
"""

from __future__ import annotations

import copy
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

import gateway.memory.identity as identity_module
from gateway.memory.convergence import MemoryVectorConvergence
from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.memory.identity import (
    IDENTITY_TABLE,
    classify_identity_schema,
    legacy_fact_uuid,
    memory_point_id,
    migrate_identity_schema,
    stored_epoch,
    validate_identity_schema,
)
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter, QdrantMemoryHit
from gateway.memory.schema import (
    FACTS_TABLE,
    OUTBOX_TABLE,
    MemorySchemaClassification,
    classify_memory_convergence_schema,
    migrate_memory_convergence_schema,
)
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge
from scripts.healbite_schema_migrate import _migrate_borrowed_connection


EPOCH_A = "20000000-0000-4000-8000-000000000001"
EPOCH_B = "20000000-0000-4000-8000-000000000002"
FACT_UUID_A = "30000000-0000-4000-8000-000000000001"
FACT_UUID_B = "30000000-0000-4000-8000-000000000002"
SHARED_FACT_UUID = "30000000-0000-4000-8000-000000000003"
OWNER_A = 1101
OWNER_B = 2202


class _Completed:
    status = "completed"


class RecordingQdrant:
    """Persistent in-memory derived store behind the real adapter boundary."""

    def __init__(self) -> None:
        self.points: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def get_collection(self, _name: str) -> dict[str, Any]:
        return {}

    def upsert(self, *, points: list[dict[str, Any]], wait: bool, **_: Any) -> _Completed:
        assert wait is True
        ids = tuple(str(point["id"]) for point in points)
        self.calls.append(("UPSERT", ids))
        for point in points:
            self.points[str(point["id"])] = copy.deepcopy(point)
        return _Completed()

    def delete(
        self, *, points_selector: list[str], wait: bool, **_: Any
    ) -> _Completed:
        assert wait is True
        ids = tuple(str(point_id) for point_id in points_selector)
        self.calls.append(("DELETE", ids))
        for point_id in ids:
            self.points.pop(point_id, None)
        return _Completed()


def _adapter(client: RecordingQdrant) -> QdrantMemoryAdapter:
    return QdrantMemoryAdapter(
        enabled=True,
        collection_name="test-memory",
        client_factory=lambda: client,
        embedding_adapter=EmbeddingAdapter(
            embed_fn=lambda _: [0.25, 0.75], vector_size=2
        ),
        vector_size=2,
    )


def _convergence(path: Path, adapter: QdrantMemoryAdapter) -> MemoryVectorConvergence:
    return MemoryVectorConvergence(
        path,
        qdrant_adapter=adapter,
        vector_enabled=True,
        fact_text=lambda fact: f"{fact['entity']}\n{fact['key']}\n{fact['value']}",
        clock=lambda: 100.0,
    )


def _create_legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        migrate_memory_convergence_schema(conn, now=0.0)
        conn.executemany(
            f"""
            INSERT INTO {FACTS_TABLE}(
                id, user_id, entity, key, value, vector_revision,
                source, trust_score, created_at, updated_at
            ) VALUES (?, ?, 'profile', 'goal', ?, 1, 'test', 1.0, '2000', '2000')
            """,
            ((14, OWNER_A, "first"), (15, OWNER_A, "second")),
        )
        conn.execute(
            f"""
            INSERT INTO {OUTBOX_TABLE}(
                user_id, fact_id, operation, fact_revision, created_at, updated_at
            ) VALUES (?, 13, 'DELETE', 2, 0, 0)
            """,
            (OWNER_A,),
        )
        conn.commit()


def _create_uuid_native_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        migrate_identity_schema(conn, legacy_epoch_uuid=None, now=0.0)
        conn.commit()


def _insert_current_fact(
    path: Path,
    *,
    fact_id: int,
    fact_uuid: str,
    user_id: int = OWNER_A,
) -> tuple[Any, ...]:
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"""
            INSERT INTO {FACTS_TABLE}(
                id, user_id, entity, key, value, vector_revision,
                source, trust_score, created_at, updated_at, fact_uuid
            ) VALUES (?, ?, 'profile', 'goal', 'current-B', 1,
                      'test', 1.0, '2000', '2000', ?)
            """,
            (fact_id, user_id, fact_uuid),
        )
        conn.commit()
        return tuple(
            conn.execute(f"SELECT * FROM {FACTS_TABLE} WHERE id=?", (fact_id,)).fetchone()
        )


def _seed_point(
    adapter: QdrantMemoryAdapter,
    *,
    fact_id: int,
    fact_uuid: str,
    user_id: int = OWNER_A,
    value: str,
) -> None:
    assert adapter.upsert_fact(
        sqlite_id=fact_id,
        fact_uuid=fact_uuid,
        user_id=user_id,
        text=value,
        payload={"vector_revision": 1, "value": value},
        wait=True,
    )


def _fact_snapshot(path: Path, fact_id: int) -> tuple[Any, ...]:
    with sqlite3.connect(path) as conn:
        row = conn.execute(f"SELECT * FROM {FACTS_TABLE} WHERE id=?", (fact_id,)).fetchone()
        assert row is not None
        return tuple(row)


@pytest.mark.parametrize("preferred_epoch", (EPOCH_A, EPOCH_B))
def test_two_concurrent_legacy_epoch_authorities_exactly_one_wins_without_partial_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    preferred_epoch: str,
) -> None:
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)
    first_validation = threading.Barrier(2)
    preferred_holds_lock = threading.Event()
    local = threading.local()
    real_validate_epoch = identity_module.validate_epoch

    def synchronized_validate_epoch(conn: sqlite3.Connection, epoch: str | None) -> None:
        calls = int(getattr(local, "calls", 0)) + 1
        local.calls = calls
        real_validate_epoch(conn, epoch)
        if calls == 1:
            first_validation.wait(timeout=10)
            if epoch != preferred_epoch:
                assert preferred_holds_lock.wait(timeout=10)
        elif epoch == preferred_epoch:
            # The second validation occurs only after BEGIN IMMEDIATE acquired
            # SQLite's write lock.  Release the competing stale preflight now.
            preferred_holds_lock.set()

    monkeypatch.setattr(identity_module, "validate_epoch", synchronized_validate_epoch)
    outcomes: list[tuple[str, str, int, str | None]] = []
    outcomes_lock = threading.Lock()

    def migrate_with(epoch: str) -> None:
        conn = sqlite3.connect(path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            _migrate_borrowed_connection(
                conn,
                selected=("memory_convergence_v2",),
                legacy_epoch_uuid=epoch,
            )
        except Exception as exc:  # outcome is the evidence under test
            result = ("ERROR", epoch, conn.total_changes, str(exc))
        else:
            result = ("SUCCESS", epoch, conn.total_changes, None)
        finally:
            conn.close()
        with outcomes_lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=migrate_with, args=(epoch,), daemon=True)
        for epoch in (EPOCH_A, EPOCH_B)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    successes = [item for item in outcomes if item[0] == "SUCCESS"]
    errors = [item for item in outcomes if item[0] == "ERROR"]

    with sqlite3.connect(path) as conn:
        assert classify_identity_schema(conn) is MemorySchemaClassification.CURRENT
        validate_identity_schema(conn)
        metadata_exists, winning_epoch = stored_epoch(conn)
        assert metadata_exists is True
        assert winning_epoch in (EPOCH_A, EPOCH_B)
        for fact_id, user_id, fact_uuid in conn.execute(
            f"SELECT id, user_id, fact_uuid FROM {FACTS_TABLE} ORDER BY id"
        ):
            assert winning_epoch is not None
            assert fact_uuid == legacy_fact_uuid(winning_epoch, user_id, fact_id)
        for fact_id, user_id, fact_uuid in conn.execute(
            f"SELECT fact_id, user_id, fact_uuid FROM {OUTBOX_TABLE} ORDER BY id"
        ):
            assert winning_epoch is not None
            assert fact_uuid == legacy_fact_uuid(winning_epoch, user_id, fact_id)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    assert len(successes) == 1
    assert len(errors) == 1
    assert successes[0][1] == preferred_epoch
    assert successes[0][2] > 0
    assert errors[0][1] != preferred_epoch
    assert errors[0][2] == 0
    assert errors[0][3] is not None
    assert "MIGRATION_EPOCH_MISMATCH" in errors[0][3]


def test_two_concurrent_same_epoch_authorities_are_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)
    first_validation = threading.Barrier(2)
    local = threading.local()
    real_validate_epoch = identity_module.validate_epoch

    def synchronized_validate_epoch(conn: sqlite3.Connection, epoch: str | None) -> None:
        calls = int(getattr(local, "calls", 0)) + 1
        local.calls = calls
        real_validate_epoch(conn, epoch)
        if calls == 1:
            first_validation.wait(timeout=10)

    monkeypatch.setattr(identity_module, "validate_epoch", synchronized_validate_epoch)
    outcomes: list[tuple[str, int, str | None]] = []
    outcomes_lock = threading.Lock()

    def migrate_same_epoch() -> None:
        conn = sqlite3.connect(path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            _migrate_borrowed_connection(
                conn,
                selected=("memory_convergence_v2",),
                legacy_epoch_uuid=EPOCH_A,
            )
        except Exception as exc:  # outcome is the evidence under test
            result = ("ERROR", conn.total_changes, str(exc))
        else:
            result = ("SUCCESS", conn.total_changes, None)
        finally:
            conn.close()
        with outcomes_lock:
            outcomes.append(result)

    threads = [
        threading.Thread(target=migrate_same_epoch, daemon=True) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    assert [status for status, _, _ in outcomes] == ["SUCCESS", "SUCCESS"]
    assert sorted(changes for _, changes, _ in outcomes)[0] == 0
    with sqlite3.connect(path) as conn:
        validate_identity_schema(conn)
        assert stored_epoch(conn) == (True, EPOCH_A)
        for fact_id, user_id, fact_uuid in conn.execute(
            f"SELECT id, user_id, fact_uuid FROM {FACTS_TABLE} ORDER BY id"
        ):
            assert fact_uuid == legacy_fact_uuid(EPOCH_A, user_id, fact_id)


def test_current_schema_rejects_different_epoch_without_database_delta(
    tmp_path: Path,
) -> None:
    path = tmp_path / "current.db"
    _create_legacy_database(path)
    with sqlite3.connect(path) as conn:
        _migrate_borrowed_connection(
            conn,
            selected=("memory_convergence_v2",),
            legacy_epoch_uuid=EPOCH_A,
        )
        before = tuple(conn.iterdump()), conn.total_changes
        with pytest.raises(sqlite3.DatabaseError, match="MIGRATION_EPOCH_MISMATCH"):
            _migrate_borrowed_connection(
                conn,
                selected=("memory_convergence_v2",),
                legacy_epoch_uuid=EPOCH_B,
            )
        assert (tuple(conn.iterdump()), conn.total_changes) == before


def test_current_schema_same_epoch_is_idempotent_without_identity_churn(
    tmp_path: Path,
) -> None:
    path = tmp_path / "current.db"
    _create_legacy_database(path)
    with sqlite3.connect(path) as conn:
        _migrate_borrowed_connection(
            conn,
            selected=("memory_convergence_v2",),
            legacy_epoch_uuid=EPOCH_A,
        )
        before = tuple(conn.iterdump()), conn.total_changes
        phases, changed = _migrate_borrowed_connection(
            conn,
            selected=("memory_convergence_v2",),
            legacy_epoch_uuid=EPOCH_A,
        )
        assert changed is False
        assert phases[0].changed is False
        assert (tuple(conn.iterdump()), conn.total_changes) == before


def test_stale_upsert_after_sqlite_id_reuse_deletes_only_old_uuid_point(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.db"
    _create_uuid_native_database(path)
    fact_id = 14
    current_before = _insert_current_fact(path, fact_id=fact_id, fact_uuid=FACT_UUID_B)
    client = RecordingQdrant()
    adapter = _adapter(client)
    convergence = _convergence(path, adapter)
    _seed_point(adapter, fact_id=fact_id, fact_uuid=FACT_UUID_A, value="stale-A")
    _seed_point(adapter, fact_id=fact_id, fact_uuid=FACT_UUID_B, value="current-B")
    point_a = memory_point_id(OWNER_A, FACT_UUID_A)
    point_b = memory_point_id(OWNER_A, FACT_UUID_B)
    point_b_before = copy.deepcopy(client.points[point_b])
    client.calls.clear()
    with sqlite3.connect(path) as conn:
        convergence.enqueue_upsert(
            conn,
            user_id=OWNER_A,
            fact_id=fact_id,
            fact_uuid=FACT_UUID_A,
            fact_revision=1,
        )
        conn.commit()

    result = convergence.process_batch(batch_size=1)

    assert result.superseded == 1
    assert client.calls == [("DELETE", (point_a,))]
    assert point_a not in client.points
    assert client.points[point_b] == point_b_before
    assert _fact_snapshot(path, fact_id) == current_before
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            f"SELECT operation, fact_uuid, fact_revision FROM {OUTBOX_TABLE}"
        ).fetchall() == [("DELETE", FACT_UUID_A, 2)]


def test_stale_delete_after_sqlite_id_reuse_is_old_uuid_only_and_repeatable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.db"
    _create_uuid_native_database(path)
    fact_id = 14
    current_before = _insert_current_fact(path, fact_id=fact_id, fact_uuid=FACT_UUID_B)
    client = RecordingQdrant()
    adapter = _adapter(client)
    convergence = _convergence(path, adapter)
    _seed_point(adapter, fact_id=fact_id, fact_uuid=FACT_UUID_A, value="stale-A")
    _seed_point(adapter, fact_id=fact_id, fact_uuid=FACT_UUID_B, value="current-B")
    point_a = memory_point_id(OWNER_A, FACT_UUID_A)
    point_b = memory_point_id(OWNER_A, FACT_UUID_B)
    client.calls.clear()
    with sqlite3.connect(path) as conn:
        convergence.enqueue_delete(
            conn,
            user_id=OWNER_A,
            fact_id=fact_id,
            fact_uuid=FACT_UUID_A,
            fact_revision=1,
        )
        conn.commit()

    first = convergence.process_batch(batch_size=1)
    assert first.superseded == 1
    assert client.calls == [("DELETE", (point_a,))]
    assert point_a not in client.points and point_b in client.points
    assert _fact_snapshot(path, fact_id) == current_before

    # Apply the canonical B correction, then replay another stale A delete.
    assert convergence.process_batch(batch_size=1).succeeded == 1
    with sqlite3.connect(path) as conn:
        convergence.enqueue_delete(
            conn,
            user_id=OWNER_A,
            fact_id=fact_id,
            fact_uuid=FACT_UUID_A,
            fact_revision=2,
        )
        conn.commit()
    replay = convergence.process_batch(batch_size=1)
    assert replay.superseded == 1
    assert _fact_snapshot(path, fact_id) == current_before
    assert point_a not in client.points and point_b in client.points
    delete_targets = [ids for operation, ids in client.calls if operation == "DELETE"]
    assert delete_targets == [(point_a,), (point_a,)]


def test_identical_fact_uuid_isolated_by_user_in_point_identity_and_hydration(
    tmp_path: Path,
) -> None:
    point_a = memory_point_id(OWNER_A, SHARED_FACT_UUID)
    point_b = memory_point_id(OWNER_B, SHARED_FACT_UUID)
    assert point_a != point_b
    client = RecordingQdrant()
    adapter = _adapter(client)
    _seed_point(
        adapter,
        fact_id=14,
        fact_uuid=SHARED_FACT_UUID,
        user_id=OWNER_A,
        value="owner-A",
    )
    _seed_point(
        adapter,
        fact_id=14,
        fact_uuid=SHARED_FACT_UUID,
        user_id=OWNER_B,
        value="owner-B",
    )
    assert set(client.points) == {point_a, point_b}

    paths = {OWNER_A: tmp_path / "owner-a.db", OWNER_B: tmp_path / "owner-b.db"}
    for owner, path in paths.items():
        _create_uuid_native_database(path)
        _insert_current_fact(
            path, fact_id=14, fact_uuid=SHARED_FACT_UUID, user_id=owner
        )

    for requested, foreign in ((OWNER_A, OWNER_B), (OWNER_B, OWNER_A)):
        bridge = object.__new__(HealBiteMemoryBridge)
        bridge.db_path = paths[requested]
        own_hit = QdrantMemoryHit(
            sqlite_id=14,
            payload={
                "user_id": requested,
                "fact_uuid": SHARED_FACT_UUID,
                "vector_revision": 1,
            },
        )
        foreign_hit = QdrantMemoryHit(
            sqlite_id=14,
            payload={
                "user_id": foreign,
                "fact_uuid": SHARED_FACT_UUID,
                "vector_revision": 1,
            },
        )
        assert len(
            bridge._hydrate_qdrant_hits(
                user_id=requested, hits=[own_hit], min_trust_score=0
            )
        ) == 1
        assert (
            bridge._hydrate_qdrant_hits(
                user_id=requested, hits=[foreign_hit], min_trust_score=0
            )
            == []
        )


class FaultingConnection(sqlite3.Connection):
    """Inject process-like failures while retaining real SQLite transactions."""

    fault_mode: str | None = None
    fact_backfills: int = 0
    metadata_inserted: bool = False

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        normalized = " ".join(sql.split())
        if (
            self.fault_mode == "first_identity_ddl"
            and normalized.startswith(f"ALTER TABLE {OUTBOX_TABLE} ADD COLUMN fact_uuid")
        ):
            raise RuntimeError("fault after first identity DDL")
        if normalized.startswith(f"UPDATE {FACTS_TABLE} SET fact_uuid"):
            self.fact_backfills += 1
            if self.fault_mode == "uuid_backfill" and self.fact_backfills == 2:
                raise RuntimeError("fault during UUID backfill")
        cursor = super().execute(sql, parameters)
        if normalized.startswith(f"INSERT INTO {IDENTITY_TABLE} VALUES"):
            self.metadata_inserted = True
            return cursor
        if (
            self.fault_mode == "after_metadata"
            and self.metadata_inserted
            and normalized.startswith("CREATE")
        ):
            raise RuntimeError("fault after identity metadata insert")
        return cursor


def _durable_identity_state(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        identity_state = classify_identity_schema(conn)
        facts_columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({FACTS_TABLE})")
        }
        outbox_columns = {
            row[1] for row in conn.execute(f"PRAGMA table_info({OUTBOX_TABLE})")
        }
        if identity_state is MemorySchemaClassification.CURRENT:
            validate_identity_schema(conn)
            return "V2_COMPLETE"
        metadata_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (IDENTITY_TABLE,),
        ).fetchone()
        if (
            identity_state is MemorySchemaClassification.ABSENT
            and classify_memory_convergence_schema(conn)
            is MemorySchemaClassification.CURRENT
            and "fact_uuid" not in facts_columns
            and "fact_uuid" not in outbox_columns
            and metadata_exists is None
        ):
            return "LEGACY_COMPLETE"
        return "MIXED_OR_PARTIAL"


@pytest.mark.parametrize(
    ("fault_mode", "transaction_hook"),
    (
        (None, "after_begin"),
        ("first_identity_ddl", None),
        ("uuid_backfill", None),
        ("after_metadata", None),
    ),
    ids=(
        "after-BEGIN",
        "after-first-identity-DDL",
        "during-UUID-backfill",
        "after-metadata-before-completion",
    ),
)
def test_migration_failure_reopen_is_never_partial(
    tmp_path: Path, fault_mode: str | None, transaction_hook: str | None
) -> None:
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)
    conn = sqlite3.connect(path, factory=FaultingConnection)
    conn.fault_mode = fault_mode

    def fail_after_begin(_conn: sqlite3.Connection) -> None:
        raise RuntimeError("fault after BEGIN")

    try:
        with pytest.raises(RuntimeError, match="fault"):
            _migrate_borrowed_connection(
                conn,
                selected=("memory_convergence_v2",),
                legacy_epoch_uuid=EPOCH_A,
                transaction_hook=fail_after_begin if transaction_hook else None,
            )
    finally:
        conn.close()

    assert _durable_identity_state(path) == "LEGACY_COMPLETE"
    with sqlite3.connect(path) as reopened:
        assert reopened.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert reopened.execute("PRAGMA foreign_key_check").fetchall() == []
