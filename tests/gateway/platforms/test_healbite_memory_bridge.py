from __future__ import annotations

import contextlib
import importlib.util
import io
import sqlite3
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

from gateway.memory.analytics import MemoryAnalyticsLogger, compute_memory_analytics_summary
from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter, QdrantMemoryHit
from gateway.platforms.healbite_memory_bridge import HealBiteMemoryBridge, build_memory_session_id

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "rebuild_qdrant_memory_index.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("rebuild_qdrant_memory_index", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
rebuild_qdrant_memory_index = importlib.util.module_from_spec(SCRIPT_SPEC)
SCRIPT_SPEC.loader.exec_module(rebuild_qdrant_memory_index)


def _set_vector_env(enabled: bool) -> None:
    os.environ["MEMORY_VECTOR_ENABLED"] = "true" if enabled else "false"


def _build_bridge(
    tmp_path,
    mock_client: MagicMock | None = None,
    *,
    client_factory=None,
    min_trust_score: float = 0.0,
    enabled: bool = True,
) -> HealBiteMemoryBridge:
    _set_vector_env(enabled)
    embedding = EmbeddingAdapter(embed_fn=lambda _text: [0.25, 0.5, 0.75], vector_size=3)
    qdrant = QdrantMemoryAdapter(
        collection_name="test_memory",
        timeout=0.2,
        vector_size=3,
        embedding_adapter=embedding,
        client_factory=client_factory or (lambda: mock_client),
        enabled=enabled,
    )
    return HealBiteMemoryBridge(
        tmp_path / "memory.sqlite",
        qdrant_adapter=qdrant,
        embedding_adapter=embedding,
        background_write=False,
        min_trust_score=min_trust_score,
    )


def test_search_falls_back_to_sqlite_when_qdrant_times_out(tmp_path, caplog):
    mock_client = MagicMock()
    mock_client.search.side_effect = TimeoutError("qdrant timed out")
    bridge = _build_bridge(tmp_path, mock_client)
    bridge._fts_enabled = False

    bridge.upsert_fact(
        user_id=101,
        entity="profile",
        key="goal",
        value="Reduce sodium intake",
        source="user",
        trust_score=0.9,
    )
    bridge.upsert_fact(
        user_id=202,
        entity="profile",
        key="goal",
        value="Increase protein intake",
        source="user",
        trust_score=0.9,
    )

    results = bridge.search_relevant_facts(user_id=101, query="sodium", limit=5)

    assert len(results) == 1
    assert results[0]["user_id"] == 101
    assert results[0]["value"] == "Reduce sodium intake"
    assert results[0]["retrieval_source"] == "sqlite_like"
    assert "Traceback" not in caplog.text
    bridge.close()


def test_qdrant_search_always_filters_by_user_id(tmp_path):
    mock_client = MagicMock()
    mock_client.search.return_value = []
    bridge = _build_bridge(tmp_path, mock_client)

    bridge.search_relevant_facts(user_id=248875361, query="vitamin d", limit=3)

    kwargs = mock_client.search.call_args.kwargs
    assert kwargs["query_filter"] == {
        "must": [{"key": "user_id", "match": {"value": 248875361}}]
    }
    bridge.close()


def test_dual_write_payload_contains_sqlite_id_and_user_id(tmp_path):
    mock_client = MagicMock()
    mock_client.search.return_value = []
    bridge = _build_bridge(tmp_path, mock_client)

    sqlite_id = bridge.upsert_fact(
        user_id=5179574383,
        entity="report",
        key="breakfast",
        value="Greek yogurt and berries",
        source="food_log",
        trust_score=0.8,
    )

    kwargs = mock_client.upsert.call_args.kwargs
    point = kwargs["points"][0]

    assert sqlite_id == point["payload"]["sqlite_id"]
    assert point["payload"]["user_id"] == 5179574383
    assert point["payload"]["value"] == "Greek yogurt and berries"
    bridge.close()


def test_search_falls_back_to_sqlite_when_qdrant_client_init_fails(tmp_path, caplog):
    def broken_factory():
        raise ConnectionError("qdrant offline")

    bridge = _build_bridge(tmp_path, client_factory=broken_factory)
    bridge._fts_enabled = False
    bridge.upsert_fact(
        user_id=1,
        entity="profile",
        key="focus",
        value="fiber",
        source="user",
        trust_score=0.9,
    )

    results = bridge.search_relevant_facts(user_id=1, query="fiber", limit=3)

    assert [item["value"] for item in results] == ["fiber"]
    assert results[0]["retrieval_source"] == "sqlite_like"
    assert "Traceback" not in caplog.text
    bridge.close()


def test_search_falls_back_to_sqlite_when_qdrant_search_raises_connection_error(tmp_path, caplog):
    mock_client = MagicMock()
    mock_client.search.side_effect = ConnectionError("socket closed")
    bridge = _build_bridge(tmp_path, mock_client)
    bridge._fts_enabled = False
    bridge.upsert_fact(
        user_id=9,
        entity="meal",
        key="note",
        value="avocado toast",
        source="user",
        trust_score=0.7,
    )

    results = bridge.search_relevant_facts(user_id=9, query="avocado", limit=2)

    assert len(results) == 1
    assert results[0]["retrieval_source"] == "sqlite_like"
    assert "Traceback" not in caplog.text
    bridge.close()


def test_search_falls_back_to_sqlite_when_qdrant_search_raises_unexpected_exception(tmp_path, caplog):
    mock_client = MagicMock()
    mock_client.search.side_effect = RuntimeError("adapter exploded")
    bridge = _build_bridge(tmp_path, mock_client)
    bridge._fts_enabled = False
    bridge.upsert_fact(
        user_id=10,
        entity="profile",
        key="supplement",
        value="magnesium glycinate",
        source="user",
        trust_score=0.85,
    )

    results = bridge.search_relevant_facts(user_id=10, query="magnesium", limit=2)

    assert len(results) == 1
    assert results[0]["retrieval_source"] == "sqlite_like"
    assert "Traceback" not in caplog.text
    bridge.close()


def test_qdrant_results_are_validated_against_sqlite(tmp_path):
    mock_client = MagicMock()
    bridge = _build_bridge(tmp_path, mock_client)
    sqlite_id = bridge.upsert_fact(
        user_id=33,
        entity="profile",
        key="goal",
        value="lower sugar",
        source="user",
        trust_score=0.95,
    )
    mock_client.search.return_value = [
        QdrantMemoryHit(
            sqlite_id=sqlite_id,
            payload={"sqlite_id": sqlite_id, "user_id": 33},
            score=0.99,
        )
    ]

    results = bridge.search_relevant_facts(user_id=33, query="sugar", limit=1)

    assert len(results) == 1
    assert results[0]["id"] == sqlite_id
    assert results[0]["value"] == "lower sugar"
    assert results[0]["retrieval_source"] == "qdrant"
    bridge.close()


def test_corrupted_payload_without_sqlite_id_is_dropped(tmp_path):
    mock_client = MagicMock()
    bridge = _build_bridge(tmp_path, mock_client)
    bridge._fts_enabled = False
    bridge.upsert_fact(
        user_id=44,
        entity="profile",
        key="goal",
        value="sleep more",
        source="user",
        trust_score=0.8,
    )
    mock_client.search.return_value = [
        QdrantMemoryHit(
            sqlite_id=None,
            payload={"user_id": 44, "value": "forged value"},
            score=0.77,
        )
    ]

    results = bridge.search_relevant_facts(user_id=44, query="forged", limit=3)

    assert results == []
    bridge.close()


def test_stale_qdrant_index_result_is_dropped_when_sqlite_row_is_gone(tmp_path):
    mock_client = MagicMock()
    bridge = _build_bridge(tmp_path, mock_client)
    sqlite_id = bridge.upsert_fact(
        user_id=55,
        entity="profile",
        key="goal",
        value="eat more greens",
        source="user",
        trust_score=0.9,
    )
    bridge.delete_fact(sqlite_id=sqlite_id, user_id=55)
    mock_client.search.return_value = [
        QdrantMemoryHit(
            sqlite_id=sqlite_id,
            payload={"sqlite_id": sqlite_id, "user_id": 55},
            score=0.91,
        )
    ]

    results = bridge.search_relevant_facts(user_id=55, query="greens", limit=3)

    assert results == []
    bridge.close()


def test_qdrant_result_is_dropped_when_trust_score_below_threshold(tmp_path):
    mock_client = MagicMock()
    bridge = _build_bridge(tmp_path, mock_client, min_trust_score=0.8)
    sqlite_id = bridge.upsert_fact(
        user_id=66,
        entity="profile",
        key="advice",
        value="try expensive detox",
        source="llm_guess",
        trust_score=0.2,
    )
    mock_client.search.return_value = [
        QdrantMemoryHit(
            sqlite_id=sqlite_id,
            payload={"sqlite_id": sqlite_id, "user_id": 66},
            score=0.95,
        )
    ]

    results = bridge.search_relevant_facts(user_id=66, query="detox", limit=2)

    assert results == []
    bridge.close()


def test_mismatched_qdrant_user_id_is_hard_blocked(tmp_path):
    mock_client = MagicMock()
    bridge = _build_bridge(tmp_path, mock_client)
    sqlite_id = bridge.upsert_fact(
        user_id=77,
        entity="profile",
        key="plan",
        value="user 77 plan",
        source="user",
        trust_score=0.92,
    )
    mock_client.search.return_value = [
        QdrantMemoryHit(
            sqlite_id=sqlite_id,
            payload={"sqlite_id": sqlite_id, "user_id": 88},
            score=0.88,
        )
    ]

    results = bridge.search_relevant_facts(user_id=77, query="plan", limit=2)

    assert results != []
    assert all(item["retrieval_source"] != "qdrant" for item in results)
    assert all(item["user_id"] == 77 for item in results)
    bridge.close()


def test_rebuild_script_reindexes_all_sqlite_facts_with_expected_payload(tmp_path, monkeypatch):
    source_bridge = HealBiteMemoryBridge(tmp_path / "facts.sqlite", background_write=False)
    source_bridge.upsert_fact(
        user_id=1001,
        entity="profile",
        key="goal",
        value="hydrate more",
        source="user",
        trust_score=0.9,
    )
    source_bridge.upsert_fact(
        user_id=1002,
        entity="profile",
        key="goal",
        value="eat more protein",
        source="user",
        trust_score=0.85,
    )
    source_bridge.close()

    captured_calls: list[dict[str, object]] = []

    class FakeQdrantAdapter:
        def __init__(self, *args, **kwargs):
            self.embedding_adapter = kwargs.get("embedding_adapter")

        def upsert_fact(self, *, sqlite_id, user_id, text, payload):
            captured_calls.append(
                {
                    "sqlite_id": sqlite_id,
                    "user_id": user_id,
                    "text": text,
                    "payload": payload,
                }
            )
            return True

    monkeypatch.setattr(rebuild_qdrant_memory_index, "QdrantMemoryAdapter", FakeQdrantAdapter)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rebuild_qdrant_memory_index.py",
            "--db-path",
            str(tmp_path / "facts.sqlite"),
            "--collection",
            "test_collection",
        ],
    )

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = rebuild_qdrant_memory_index.main()

    assert exit_code == 0
    assert len(captured_calls) == 2
    assert {call["user_id"] for call in captured_calls} == {1001, 1002}
    assert all(call["sqlite_id"] == call["payload"]["sqlite_id"] for call in captured_calls)
    assert all(call["user_id"] == call["payload"]["user_id"] for call in captured_calls)
    assert "Reindexed 2 facts into test_collection" in output.getvalue()


def test_vector_disabled_skips_qdrant_client_initialization(tmp_path):
    call_count = {"count": 0}

    def client_factory():
        call_count["count"] += 1
        return MagicMock()

    _set_vector_env(False)
    bridge = _build_bridge(tmp_path, client_factory=client_factory, enabled=False)
    bridge._fts_enabled = False
    bridge.upsert_fact(
        user_id=200,
        entity="profile",
        key="goal",
        value="walk daily",
        source="user",
        trust_score=0.8,
    )

    results = bridge.search_relevant_facts(user_id=200, query="walk", limit=2)

    assert len(results) == 1
    assert results[0]["retrieval_source"] == "sqlite_like"
    assert call_count["count"] == 0
    bridge.close()


def test_rebuild_script_dry_run_counts_without_qdrant(tmp_path, monkeypatch):
    source_bridge = HealBiteMemoryBridge(tmp_path / "facts_dry.sqlite", background_write=False)
    source_bridge.upsert_fact(
        user_id=5001,
        entity="profile",
        key="goal",
        value="sleep better",
        source="user",
        trust_score=0.9,
    )
    source_bridge.upsert_fact(
        user_id=5002,
        entity="profile",
        key="goal",
        value="drink more water",
        source="user",
        trust_score=0.85,
    )
    source_bridge.close()

    _set_vector_env(False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rebuild_qdrant_memory_index.py",
            "--db-path",
            str(tmp_path / "facts_dry.sqlite"),
            "--dry-run",
        ],
    )

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = rebuild_qdrant_memory_index.main()

    assert exit_code == 0
    assert "Dry run: 2 facts would be reindexed" in output.getvalue()


def test_build_memory_session_id_rejects_missing_user_id():
    try:
        build_memory_session_id(None)
    except ValueError as exc:
        assert "non-null user_id" in str(exc)
    else:
        raise AssertionError("expected ValueError for missing user_id")

def test_search_survives_analytics_db_write_failure(tmp_path):
    mock_client = MagicMock()
    bridge = _build_bridge(tmp_path, mock_client)
    bridge._fts_enabled = False
    bridge.upsert_fact(
        user_id=909,
        entity="profile",
        key="goal",
        value="walk after dinner",
        source="user",
        trust_score=0.8,
    )

    def broken_connect():
        raise sqlite3.OperationalError("database is locked")

    bridge.analytics_logger._connect = broken_connect

    results = bridge.search_relevant_facts(user_id=909, query="walk", limit=2)

    assert len(results) == 1
    assert results[0]["retrieval_source"] == "sqlite_like"
    bridge.close()


def test_search_analytics_rows_stay_scoped_to_user_id(tmp_path):
    mock_client = MagicMock()
    bridge = _build_bridge(tmp_path, mock_client)
    bridge._fts_enabled = False
    bridge.upsert_fact(
        user_id=111,
        entity="profile",
        key="goal",
        value="reduce sodium",
        source="user",
        trust_score=0.9,
    )
    bridge.upsert_fact(
        user_id=222,
        entity="profile",
        key="goal",
        value="increase protein",
        source="user",
        trust_score=0.9,
    )

    bridge.search_relevant_facts(user_id=111, query="sodium", limit=2)
    bridge.search_relevant_facts(user_id=222, query="protein", limit=2)
    bridge.close()

    conn = sqlite3.connect(tmp_path / "memory.sqlite")
    try:
        rows = conn.execute(
            "SELECT user_id, source, found_count FROM memory_analytics_logs WHERE event_type = 'search' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert rows == [(111, "like", 1), (222, "like", 1)]
    assert compute_memory_analytics_summary(tmp_path / "memory.sqlite", user_id=111)["total_searches"] == 1
    assert compute_memory_analytics_summary(tmp_path / "memory.sqlite", user_id=222)["total_searches"] == 1


def test_memory_analytics_logger_swallow_db_errors(tmp_path):
    logger = MemoryAnalyticsLogger(tmp_path / "analytics.sqlite", background_write=False)

    def broken_connect():
        raise sqlite3.OperationalError("disk I/O error")

    logger._connect = broken_connect
    logger.log_search(user_id=1, source="like", found_count=1, processing_time_ms=12.5)
    logger.close()