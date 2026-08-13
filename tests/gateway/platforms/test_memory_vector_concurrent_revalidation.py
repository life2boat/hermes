from __future__ import annotations

from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge


class CallbackQdrant:
    enabled = True

    def __init__(self, callback):
        self.callback = callback
        self.callback_used = False
        self.points = {}
        self.revisions = []

    def upsert_fact(self, **kwargs):
        revision = kwargs["payload"]["vector_revision"]
        self.revisions.append(revision)
        if not self.callback_used:
            self.callback_used = True
            self.callback()
        self.points[(kwargs["user_id"], kwargs["sqlite_id"])] = dict(kwargs["payload"])
        return True

    def delete_fact(self, **kwargs):
        self.points.pop((kwargs["user_id"], kwargs["sqlite_id"]), None)
        return True

    def search(self, **_kwargs):
        return []


def _write(bridge, value):
    return bridge.upsert_fact(
        user_id=101,
        entity="profile",
        key="goal",
        value=value,
        source="test",
    )


def test_update_during_qdrant_call_leaves_canonical_correction(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"

    def concurrent_update():
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
        writer = HealBiteMemoryBridge(db_path, background_write=False)
        _write(writer, "new")
        writer.close()

    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    fake = CallbackQdrant(concurrent_update)
    bridge = HealBiteMemoryBridge(
        db_path,
        qdrant_adapter=fake,
        background_write=False,
    )

    fact_id = _write(bridge, "old")
    assert bridge.get_vector_sync_status().status == "PENDING"
    bridge.process_vector_sync_batch()

    assert fake.revisions == [1, 2]
    assert fake.points[(101, fact_id)]["value"] == "new"
    assert bridge.get_vector_sync_status().status == "CONVERGED"
    bridge.close()


def test_delete_during_qdrant_call_leaves_canonical_delete_correction(tmp_path, monkeypatch):
    db_path = tmp_path / "memory.sqlite"
    fact_id_holder = {}

    def concurrent_delete():
        monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
        writer = HealBiteMemoryBridge(db_path, background_write=False)
        writer.delete_fact(sqlite_id=fact_id_holder["id"], user_id=101)
        writer.close()

    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    fake = CallbackQdrant(concurrent_delete)
    bridge = HealBiteMemoryBridge(
        db_path,
        qdrant_adapter=fake,
        background_write=False,
    )

    # Insert without auto-processing so the callback knows the fact identity.
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "false")
    bridge._vector_enabled = False
    bridge._convergence.vector_enabled = False
    fact_id_holder["id"] = _write(bridge, "old")
    bridge._vector_enabled = True
    bridge._convergence.vector_enabled = True
    bridge.process_vector_sync_batch()
    assert bridge.get_vector_sync_status().status == "PENDING"
    bridge.process_vector_sync_batch()

    assert (101, fact_id_holder["id"]) not in fake.points
    assert bridge.get_vector_sync_status().status == "CONVERGED"
    bridge.close()
