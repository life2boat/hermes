from __future__ import annotations

from unittest.mock import MagicMock

from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter


def _adapter(client):
    return QdrantMemoryAdapter(
        collection_name="test_memory",
        vector_size=3,
        embedding_adapter=EmbeddingAdapter(
            embed_fn=lambda _text: [0.1, 0.2, 0.3],
            vector_size=3,
        ),
        client_factory=lambda: client,
        enabled=True,
    )


def test_reconciliation_upsert_uses_strong_wait_acknowledgement():
    client = MagicMock()
    client.get_collection.return_value = {}
    adapter = _adapter(client)

    assert adapter.upsert_fact(
        sqlite_id=7,
        user_id=11,
        text="synthetic",
        payload={"vector_revision": 3},
        wait=True,
    )

    assert client.upsert.call_args.kwargs["wait"] is True


def test_delete_derives_scoped_point_id_and_uses_wait_acknowledgement():
    client = MagicMock()
    client.get_collection.return_value = {}
    adapter = _adapter(client)

    assert adapter.delete_fact(sqlite_id=7, user_id=11, wait=True)

    kwargs = client.delete.call_args.kwargs
    assert kwargs["wait"] is True
    assert kwargs["points_selector"] == [adapter.point_id(sqlite_id=7, user_id=11)]
    assert kwargs["points_selector"] != [adapter.point_id(sqlite_id=7, user_id=12)]


def test_delete_failure_is_fail_closed_and_redacted(caplog):
    private_detail = "PRIVATE_FAILURE_DETAIL_MUST_NOT_APPEAR"
    client = MagicMock()
    client.get_collection.return_value = {}
    client.delete.side_effect = RuntimeError(private_detail)
    adapter = _adapter(client)

    assert not adapter.delete_fact(sqlite_id=7, user_id=11, wait=True)
    assert "RuntimeError" in caplog.text
    assert private_detail not in caplog.text
