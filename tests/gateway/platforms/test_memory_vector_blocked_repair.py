from __future__ import annotations

from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge


class RepairableAdapter:
    enabled = True

    def __init__(self):
        self.available = False
        self.calls = 0

    def upsert_fact(self, **_kwargs):
        self.calls += 1
        return self.available

    def delete_fact(self, **_kwargs):
        self.calls += 1
        return self.available

    def search(self, **_kwargs):
        return []


def test_blocked_work_requires_explicit_repair_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    clock = [100.0]
    adapter = RepairableAdapter()
    bridge = HealBiteMemoryBridge(
        tmp_path / "memory.sqlite",
        qdrant_adapter=adapter,
        background_write=False,
    )
    bridge._convergence.clock = lambda: clock[0]
    bridge._convergence.max_attempts = 1
    bridge.upsert_fact(
        user_id=101,
        entity="profile",
        key="goal",
        value="synthetic",
    )
    assert bridge.get_vector_sync_status().status == "BLOCKED"
    assert bridge.process_vector_sync_batch().processed == 0

    adapter.available = True
    result = bridge.process_vector_sync_batch(retry_blocked=True)

    assert result.status == "CONVERGED"
    assert result.succeeded == 1
    assert adapter.calls == 2
    bridge.close()
