"""Offline, persistent-vector counterexamples across SQLite history boundaries."""

from __future__ import annotations

import ast
import copy
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import pytest

from gateway.memory.identity import (
    IDENTITY_TABLE,
    MIGRATION_SHA256,
    canonical_uuid,
    legacy_fact_uuid,
    memory_point_id,
    migrate_identity_schema,
    stored_epoch,
    validate_identity_transition,
)
from gateway.memory.schema import (
    FACTS_TABLE,
    OUTBOX_TABLE,
    migrate_memory_convergence_schema,
)
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter, QdrantMemoryHit
from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.memory.orphan_classifier import classify_historical_points
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge
from scripts.healbite_schema_migrate import (
    _migrate_borrowed_connection,
    migration_registry_manifest,
)
from scripts.hermes_memory_identity_authority import plan_memory_epoch

EPOCH_A = "10000000-0000-4000-8000-000000000001"
EPOCH_B = "10000000-0000-4000-8000-000000000002"
OWNER = 101


def legacy(conn, *, value="identical", with_deleted=False):
    migrate_memory_convergence_schema(conn, now=0)
    conn.execute(
        f"INSERT INTO {FACTS_TABLE}(id,user_id,entity,key,value,created_at,updated_at) "
        "VALUES(14,?,'profile','goal',?,'2000-01-01','2000-01-01')",
        (OWNER, value),
    )
    if with_deleted:
        conn.execute(
            f"INSERT INTO {OUTBOX_TABLE}(user_id,fact_id,operation,fact_revision,created_at,updated_at) "
            "VALUES(?,13,'DELETE',2,0,0)",
            (OWNER,),
        )
    conn.commit()


def migrate(conn, epoch):
    return _migrate_borrowed_connection(
        conn,
        selected=("memory_convergence", "memory_convergence_v2"),
        legacy_epoch_uuid=epoch,
    )


def identity(conn):
    return conn.execute(f"SELECT fact_uuid FROM {FACTS_TABLE} WHERE id=14").fetchone()[
        0
    ]


class PersistentQdrant:
    def __init__(self):
        self.points = {}
        self.collections = {"existing"}
        self.fail_delete = False

    def get_collection(self, name):
        if name not in self.collections:
            raise LookupError("missing")
        return {}

    def create_collection(self, *, collection_name, **kwargs):
        if collection_name in self.collections:
            raise ValueError("exists")
        self.collections.add(collection_name)
        return True

    def upsert(self, *, points, wait, **kwargs):
        assert wait is True
        for point in points:
            self.points[point["id"]] = copy.deepcopy(point)
        return {"status": "completed"}

    def delete(self, *, points_selector, wait, **kwargs):
        assert wait is True
        if self.fail_delete:
            raise TimeoutError("synthetic failure")
        for point_id in points_selector:
            self.points.pop(point_id, None)
        return {"status": "completed"}


def adapter(client, collection="existing"):
    return QdrantMemoryAdapter(
        enabled=True,
        collection_name=collection,
        client_factory=lambda: client,
        embedding_adapter=EmbeddingAdapter(
            embed_fn=lambda _: [0.1, 0.2], vector_size=2
        ),
        vector_size=2,
    )


def bridge(path, client, monkeypatch):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    return HealBiteMemoryBridge(
        path, qdrant_adapter=adapter(client), background_write=False
    )


@pytest.mark.parametrize("other_value", ["identical", "different"])
def test_independent_legacy_histories_with_id14_and_same_timestamp_are_distinct(
    other_value,
):
    with sqlite3.connect(":memory:") as a, sqlite3.connect(":memory:") as b:
        legacy(a)
        legacy(b, value=other_value)
        if other_value == "identical":
            assert tuple(a.iterdump()) == tuple(
                b.iterdump()
            )  # All persisted columns identical.
        migrate(a, EPOCH_A)
        migrate(b, EPOCH_B)
        assert identity(a) != identity(b)
        assert memory_point_id(OWNER, identity(a)) != memory_point_id(
            OWNER, identity(b)
        )
        assert identity(a) == legacy_fact_uuid(EPOCH_A, OWNER, 14)


def test_three_rehearsals_retry_and_target_share_one_authority(tmp_path):
    source_path = tmp_path / "source.db"
    with sqlite3.connect(source_path) as source:
        legacy(source, with_deleted=True)
        epoch = plan_memory_epoch(source_path)
        assert epoch is not None and uuid.UUID(epoch).version == 4
        snapshots = []
        for i in range(4):
            with sqlite3.connect(tmp_path / f"copy{i}.db") as staged:
                source.backup(staged)
                migrate(staged, epoch)
                snapshot = tuple(staged.iterdump())
                assert migrate(staged, epoch)[1] is False
                assert tuple(staged.iterdump()) == snapshot
                validate_identity_transition(source, staged, epoch=epoch)
                assert staged.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                assert staged.execute("PRAGMA foreign_key_check").fetchall() == []
                snapshots.append(snapshot)
        assert all(snapshot == snapshots[0] for snapshot in snapshots)
        assert (
            plan_memory_epoch(source_path) != epoch
        )  # Independent new authority, not retry.


@pytest.mark.parametrize("bad_epoch", [EPOCH_B, None])
def test_mismatch_precedes_hooks_and_all_mutation(bad_epoch):
    with sqlite3.connect(":memory:") as conn:
        legacy(conn)
        migrate(conn, EPOCH_A)
        before = tuple(conn.iterdump()), conn.total_changes
        hooks = []
        with pytest.raises(sqlite3.DatabaseError, match="MIGRATION_EPOCH_MISMATCH"):
            _migrate_borrowed_connection(
                conn,
                selected=("memory_convergence_v2",),
                legacy_epoch_uuid=bad_epoch,
                transaction_hook=lambda _: hooks.append("called"),
            )
        assert hooks == []
        assert (tuple(conn.iterdump()), conn.total_changes) == before


def test_missing_epoch_on_legacy_rejects_without_any_writes():
    with sqlite3.connect(":memory:") as conn:
        legacy(conn)
        before = tuple(conn.iterdump()), conn.total_changes
        with pytest.raises(
            sqlite3.DatabaseError, match="LEGACY_EPOCH_AUTHORITY_REQUIRED"
        ):
            migrate(conn, None)
        assert (tuple(conn.iterdump()), conn.total_changes) == before


def test_post_migration_restore_keeps_epoch_and_uuid(tmp_path):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as conn:
        legacy(conn)
        migrate(conn, EPOCH_A)
        before = tuple(conn.iterdump())
        assert plan_memory_epoch(path) == EPOCH_A
        with sqlite3.connect(tmp_path / "restored.db") as restored:
            conn.backup(restored)
            assert migrate(restored, EPOCH_A)[1] is False
            assert tuple(restored.iterdump()) == before


@pytest.mark.parametrize(
    "bad", [None, "", "bad", "1" * 32, EPOCH_A.upper().replace("10000000", "ABCDEF00")]
)
def test_invalid_fact_uuid_sql_and_adapter_fail_closed(bad, tmp_path, monkeypatch):
    client = PersistentQdrant()
    b = bridge(tmp_path / "db", client, monkeypatch)
    with sqlite3.connect(b.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="INVALID_MEMORY_UUID"):
            conn.execute(
                f"INSERT INTO {FACTS_TABLE}(user_id,entity,key,value,fact_uuid) VALUES(101,'p','k','v',?)",
                (bad,),
            )
    with pytest.raises(ValueError, match="INVALID_MEMORY_UUID"):
        b.qdrant_adapter.upsert_fact(
            sqlite_id=14, fact_uuid=bad, user_id=OWNER, text="v", payload={}, wait=True
        )
    assert client.points == {}
    b.close()


def test_new_uuid4_is_immutable_on_updates_and_sql(tmp_path, monkeypatch):
    b = bridge(tmp_path / "db", PersistentQdrant(), monkeypatch)
    i = b.upsert_fact(user_id=OWNER, entity="p", key="k", value="v1")
    first = b.get_fact(sqlite_id=i, user_id=OWNER)
    assert uuid.UUID(first["fact_uuid"]).version == 4
    assert b.upsert_fact(user_id=OWNER, entity="p", key="k", value="v2") == i
    second = b.get_fact(sqlite_id=i, user_id=OWNER)
    assert second["fact_uuid"] == first["fact_uuid"]
    assert second["vector_revision"] == 2
    with sqlite3.connect(b.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="IMMUTABLE_MEMORY_UUID"):
            conn.execute(
                f"UPDATE {FACTS_TABLE} SET fact_uuid=? WHERE id=?", (EPOCH_B, i)
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {FACTS_TABLE}(user_id,entity,key,value,fact_uuid) VALUES(202,'p','k','v',?)",
                (first["fact_uuid"],),
            )
    b.close()


def test_real_adapter_persistent_qdrant_sqlite_restore_reuses_id14_without_overwrite(
    tmp_path, monkeypatch
):
    client = PersistentQdrant()
    path = tmp_path / "db"
    b = bridge(path, client, monkeypatch)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO sqlite_sequence(name,seq) VALUES (?,13)", (FACTS_TABLE,)
        )
        with sqlite3.connect(tmp_path / "backup") as backup:
            conn.commit()
            conn.backup(backup)
    i = b.upsert_fact(user_id=OWNER, entity="p", key="k", value="old history")
    assert i == 14
    old = b.get_fact(sqlite_id=i, user_id=OWNER)
    old_points = copy.deepcopy(client.points)
    b.close()
    with (
        sqlite3.connect(tmp_path / "backup") as backup,
        sqlite3.connect(path) as restored,
    ):
        backup.backup(restored)
    b = bridge(path, client, monkeypatch)
    assert b.upsert_fact(user_id=OWNER, entity="p", key="k", value="new history") == 14
    new = b.get_fact(sqlite_id=14, user_id=OWNER)
    assert old["fact_uuid"] != new["fact_uuid"]
    assert len(client.points) == 2
    assert all(client.points[k] == v for k, v in old_points.items())
    hit = QdrantMemoryHit(
        14, {"user_id": OWNER, "vector_revision": 1, "fact_uuid": old["fact_uuid"]}
    )
    assert b._hydrate_qdrant_hits(user_id=OWNER, hits=[hit], min_trust_score=0) == []
    b.close()


def test_delete_after_row_gone_retries_immutable_identity_and_rebuild_is_exact(
    tmp_path, monkeypatch
):
    client = PersistentQdrant()
    b = bridge(tmp_path / "db", client, monkeypatch)
    b._convergence.clock = lambda: 100.0
    i = b.upsert_fact(user_id=OWNER, entity="p", key="k", value="v")
    fact = b.get_fact(sqlite_id=i, user_id=OWNER)
    points = copy.deepcopy(client.points)
    assert b.rebuild_qdrant_index() == 1
    assert points == client.points  # Includes UUID and revision payload.
    client.fail_delete = True
    b.delete_fact(sqlite_id=i, user_id=OWNER)
    assert b.get_fact(sqlite_id=i, user_id=OWNER) is None
    with sqlite3.connect(b.db_path) as conn:
        row = conn.execute(
            f"SELECT fact_uuid,operation,state FROM {OUTBOX_TABLE}"
        ).fetchone()
        assert row == (fact["fact_uuid"], "DELETE", "RETRY")
    client.fail_delete = False
    b._convergence.clock = lambda: 1000.0
    assert b.process_vector_sync_batch().status == "CONVERGED"
    assert client.points == {}
    b.close()


@pytest.mark.parametrize(
    "change", ["missing_uuid", "wrong_uuid", "wrong_owner", "wrong_revision"]
)
def test_hydration_rejects_identity_and_existing_boundary_violations(
    change, tmp_path, monkeypatch
):
    b = bridge(tmp_path / "db", PersistentQdrant(), monkeypatch)
    i = b.upsert_fact(user_id=OWNER, entity="p", key="k", value="v")
    fact = b.get_fact(sqlite_id=i, user_id=OWNER)
    payload = {"user_id": OWNER, "vector_revision": 1, "fact_uuid": fact["fact_uuid"]}
    hit = QdrantMemoryHit(i, payload)
    assert (
        len(b._hydrate_qdrant_hits(user_id=OWNER, hits=[hit], min_trust_score=0)) == 1
    )
    if change == "missing_uuid":
        del payload["fact_uuid"]
    else:
        field, value = {
            "wrong_uuid": ("fact_uuid", EPOCH_B),
            "wrong_owner": ("user_id", 202),
            "wrong_revision": ("vector_revision", 2),
        }[change]
        payload[field] = value
    assert b._hydrate_qdrant_hits(user_id=OWNER, hits=[hit], min_trust_score=0) == []
    assert b._hydrate_qdrant_hits(user_id=202, hits=[hit], min_trust_score=0) == []
    b.close()


def test_outbox_collision_cannot_silently_discard_different_uuid(tmp_path, monkeypatch):
    b = bridge(tmp_path / "db", PersistentQdrant(), monkeypatch)
    with sqlite3.connect(b.db_path) as conn:
        for _ in range(2):
            b._convergence.enqueue_delete(
                conn, user_id=OWNER, fact_id=14, fact_uuid=EPOCH_A, fact_revision=2
            )
        assert conn.execute(f"SELECT COUNT(*) FROM {OUTBOX_TABLE}").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="OUTBOX_IDENTITY_CONFLICT"):
            b._convergence.enqueue_delete(
                conn, user_id=OWNER, fact_id=14, fact_uuid=EPOCH_B, fact_revision=2
            )
    b.close()


def test_legacy_delete_identity_backfilled_without_live_row():
    with sqlite3.connect(":memory:") as conn:
        legacy(conn, with_deleted=True)
        migrate(conn, EPOCH_A)
        row = conn.execute(
            f"SELECT fact_uuid FROM {OUTBOX_TABLE} WHERE fact_id=13"
        ).fetchone()
        assert row[0] == legacy_fact_uuid(EPOCH_A, OWNER, 13)


def test_orphan_uuid_mismatch_and_legacy_payload_have_no_delete_authority():
    facts = [{"id": 14, "user_id": OWNER, "vector_revision": 1, "fact_uuid": EPOCH_B}]
    points: list[dict[str, Any]] = [
        {
            "id": memory_point_id(OWNER, EPOCH_A),
            "payload": {
                "sqlite_id": 14,
                "user_id": OWNER,
                "vector_revision": 1,
                "fact_uuid": EPOCH_A,
            },
        }
    ]
    report = classify_historical_points(canonical_facts=facts, points=points)
    assert report.counts["UUID_MISMATCH"] == 1
    assert report.deletion_authorized is False
    del points[0]["payload"]["fact_uuid"]
    assert (
        classify_historical_points(canonical_facts=facts, points=points).counts[
            "MALFORMED_PAYLOAD"
        ]
        == 1
    )


def test_rebuild_tool_leaves_sqlite_byte_identical(tmp_path, monkeypatch):
    import sys
    from scripts import rebuild_qdrant_memory_index as rebuild

    client = PersistentQdrant()
    path = tmp_path / "db"
    b = bridge(path, client, monkeypatch)
    b.upsert_fact(user_id=OWNER, entity="p", key="k", value="v")
    b.close()
    before = path.read_bytes()
    monkeypatch.setattr(
        rebuild, "QdrantMemoryAdapter", lambda **_: adapter(client, "fresh")
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rebuild",
            "--db-path",
            str(path),
            "--fresh-collection",
            "--collection",
            "fresh",
        ],
    )
    assert rebuild.main() == 0
    assert path.read_bytes() == before
    assert rebuild.main() == 1  # No reuse on retry after create-only success.
    assert path.read_bytes() == before


def test_clean_collection_recovery_never_reuses_existing_name():
    client = PersistentQdrant()
    assert adapter(client).create_fresh_collection() is False
    assert adapter(client, "fresh").create_fresh_collection() is True
    assert adapter(client, "fresh").create_fresh_collection() is False
    assert client.collections == {"existing", "fresh"}


def test_migration_v1_checksum_unchanged_new_v2_registry_entry():
    registry = migration_registry_manifest()
    assert registry[-1]["component"] == "memory_convergence_v2"
    assert registry[-1]["migration_sha256"] == MIGRATION_SHA256
    # Exact historic v1 registry list still has its pre-v2 content digest.
    import hashlib
    import json

    assert (
        hashlib.sha256(
            json.dumps(registry[:-1], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        == "5deb406918e3283c301e0ae7cdfe4275faa0ac6140f19dde10614dc2ba902dba"
    )


def test_no_epoch_generation_in_migrator_or_staged_execution():
    root = Path(__file__).resolve().parents[3]
    migrator = ast.parse(
        (root / "scripts/healbite_schema_migrate.py").read_text(encoding="utf-8")
    )
    assert not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"uuid4", "uuid1"}
        for n in ast.walk(migrator)
    )


def test_v2_failure_rolls_back_ddl_epoch_backfill_and_outbox():
    with sqlite3.connect(":memory:") as conn:
        legacy(conn, with_deleted=True)
        before = tuple(conn.iterdump())

        def reject(component, _conn):
            if component == "memory_convergence_v2":
                raise RuntimeError("synthetic verification rejection")

        with pytest.raises(RuntimeError, match="synthetic verification rejection"):
            _migrate_borrowed_connection(
                conn,
                selected=("memory_convergence_v2",),
                legacy_epoch_uuid=EPOCH_A,
                component_hook=reject,
            )
        assert tuple(conn.iterdump()) == before
        assert stored_epoch(conn) == (False, None)


def test_staged_validation_preserves_outbox_sequence_and_meta_counters():
    with sqlite3.connect(":memory:") as source, sqlite3.connect(":memory:") as target:
        legacy(source)
        source.execute(
            f"INSERT INTO {OUTBOX_TABLE}(id,user_id,fact_id,operation,fact_revision,created_at,updated_at) VALUES(90,101,14,'UPSERT',1,0,0)"
        )
        source.execute(f"DELETE FROM {OUTBOX_TABLE}")
        source.execute(
            "UPDATE memory_os_vector_sync_meta SET processed_count=4,succeeded_count=4"
        )
        source.commit()
        source.backup(target)
        migrate(target, EPOCH_A)
        assert target.execute(f"SELECT id FROM {OUTBOX_TABLE}").fetchone()[0] == 91
        assert target.execute(
            "SELECT processed_count,succeeded_count FROM memory_os_vector_sync_meta"
        ).fetchone() == (4, 4)
        validate_identity_transition(source, target, epoch=EPOCH_A)


def test_uuid_native_empty_database_preserved_by_planning(tmp_path):
    path = tmp_path / "native.db"
    with sqlite3.connect(path) as conn:
        migrate_identity_schema(conn, legacy_epoch_uuid=None)
        assert stored_epoch(conn) == (True, None)
    assert plan_memory_epoch(path) is None
    assert canonical_uuid(EPOCH_A) == EPOCH_A
