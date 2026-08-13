from __future__ import annotations

from unittest.mock import MagicMock

from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge


def test_failed_client_initialization_recovers_on_due_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    clock = [100.0]
    client = MagicMock()
    client.get_collection.return_value = {}
    attempts = {"count": 0}

    def factory():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectionError("synthetic outage")
        return client

    embedding = EmbeddingAdapter(embed_fn=lambda _text: [0.1, 0.2], vector_size=2)
    adapter = QdrantMemoryAdapter(
        collection_name="test",
        vector_size=2,
        embedding_adapter=embedding,
        client_factory=factory,
        enabled=True,
    )
    bridge = HealBiteMemoryBridge(
        tmp_path / "memory.sqlite",
        qdrant_adapter=adapter,
        embedding_adapter=embedding,
        background_write=False,
    )
    bridge._convergence.clock = lambda: clock[0]

    bridge.upsert_fact(
        user_id=101,
        entity="profile",
        key="goal",
        value="synthetic",
    )
    assert bridge.get_vector_sync_status().status == "DEGRADED"

    assert bridge.process_vector_sync_batch().processed == 0
    assert attempts["count"] == 1

    clock[0] += 3.0
    result = bridge.process_vector_sync_batch()
    assert result.status == "CONVERGED"
    assert attempts["count"] == 2
    assert client.upsert.call_args.kwargs["wait"] is True
    bridge.close()


def test_retry_limit_becomes_durably_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_VECTOR_ENABLED", "true")
    clock = [200.0]
    client = MagicMock()
    client.get_collection.return_value = {}
    client.upsert.side_effect = ConnectionError("synthetic outage")
    embedding = EmbeddingAdapter(embed_fn=lambda _text: [0.1, 0.2], vector_size=2)
    adapter = QdrantMemoryAdapter(
        collection_name="test",
        vector_size=2,
        embedding_adapter=embedding,
        client_factory=lambda: client,
        enabled=True,
    )
    bridge = HealBiteMemoryBridge(
        tmp_path / "memory.sqlite",
        qdrant_adapter=adapter,
        embedding_adapter=embedding,
        background_write=False,
    )
    bridge._convergence.clock = lambda: clock[0]
    bridge._convergence.max_attempts = 2

    bridge.upsert_fact(
        user_id=101,
        entity="profile",
        key="goal",
        value="synthetic",
    )
    clock[0] += 3.0
    bridge.process_vector_sync_batch()

    status = bridge.get_vector_sync_status()
    assert status.status == "BLOCKED"
    assert status.blocked_count == 1
    assert bridge.process_vector_sync_batch().processed == 0
    bridge.close()
