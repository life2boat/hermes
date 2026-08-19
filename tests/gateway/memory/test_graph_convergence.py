import sqlite3
import pytest
import time

from gateway.memory.graph_convergence import (
    converge_user_graph,
    GraphConvergenceStatus,
    GraphConvergenceError,
)
from gateway.memory.graph_store import (
    classify_memory_graph_store_schema,
    validate_memory_graph_store_schema,
    _CREATE_META,
    _CREATE_USER_STATE,
    _CREATE_EDGES,
    _CREATE_NODE_SUPPORTS,
    _CREATE_EDGE_SUPPORTS,
    _CREATE_EXCLUSIONS,
    MEMORY_GRAPH_STORE_SCHEMA_VERSION,
    load_graph_projection,
    GraphStoreSchemaClassification,
)
from gateway.memory.schema import migrate_memory_convergence_schema


def insert_mock_fact(conn, user_id, sqlite_id, entity, key, value, revision):
    conn.execute(
        """
        INSERT INTO memory_os_facts (
            id, user_id,
            vector_revision,
            entity, key, value
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (sqlite_id, user_id, revision, entity, key, value),
    )


def setup_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate_memory_convergence_schema(conn, now=time.time())

    # Setup graph store tables
    conn.execute(_CREATE_META)
    conn.execute(_CREATE_USER_STATE)
    conn.execute("""
    CREATE TABLE memory_graph_nodes (
        user_id INTEGER NOT NULL,
        node_id TEXT NOT NULL,
        node_type TEXT NOT NULL,
        properties_json TEXT NOT NULL,
        primary_provenance_fact_id TEXT NOT NULL,
        primary_provenance_revision INTEGER NOT NULL,
        PRIMARY KEY (user_id, node_id),
        FOREIGN KEY (user_id) REFERENCES memory_graph_user_state(user_id) ON DELETE CASCADE
    )""")
    conn.execute(_CREATE_EDGES)
    conn.execute(_CREATE_NODE_SUPPORTS)
    conn.execute(_CREATE_EDGE_SUPPORTS)
    conn.execute(_CREATE_EXCLUSIONS)
    conn.execute(
        "INSERT INTO memory_graph_store_meta VALUES (1, ?)",
        (MEMORY_GRAPH_STORE_SCHEMA_VERSION,),
    )

    return conn


def test_missing_rebuild():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "k1", "v1", 1)

    res = converge_user_graph(conn, 1)
    assert res.status == GraphConvergenceStatus.MISSING_REBUILD
    assert res.matched_auth_facts_count == 1

    # verify graph exists now
    proj = load_graph_projection(conn, 1)
    assert proj is not None
    assert len(proj.snapshot.nodes) > 0


def test_current_noop():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "refresh_token", "v1", 1)

    converge_user_graph(conn, 1)  # First time is MISSING_REBUILD

    total_changes = conn.total_changes
    res = converge_user_graph(conn, 1)  # Second time is CURRENT_NOOP
    assert res.status == GraphConvergenceStatus.CURRENT_NOOP

    # second convergence writes 0
    assert conn.total_changes == total_changes


def test_stale_rebuild():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "refresh_token", "v1", 1)

    converge_user_graph(conn, 1)

    # add fact
    insert_mock_fact(conn, 1, 2, "e2", "k2", "v2", 1)

    res = converge_user_graph(conn, 1)
    assert res.status == GraphConvergenceStatus.STALE_REBUILD
    assert res.matched_auth_facts_count == 2

    proj = load_graph_projection(conn, 1)
    assert len(proj.snapshot.authoritative_source.facts) == 2


def test_empty_authoritative_scope():
    conn = setup_db()

    res = converge_user_graph(conn, 1)
    assert res.status == GraphConvergenceStatus.MISSING_REBUILD
    assert res.matched_auth_facts_count == 0

    proj = load_graph_projection(conn, 1)
    assert len(proj.snapshot.nodes) == 0


def test_all_excluded_scope():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "refresh_token", "v1", 1)

    res = converge_user_graph(conn, 1)
    assert res.status == GraphConvergenceStatus.MISSING_REBUILD

    proj = load_graph_projection(conn, 1)
    assert len(proj.snapshot.nodes) == 0

    # Check NOOP next
    res2 = converge_user_graph(conn, 1)
    assert res2.status == GraphConvergenceStatus.CURRENT_NOOP


def test_corruption_hard_fail():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "refresh_token", "v1", 1)
    converge_user_graph(conn, 1)

    # Corrupt graph store
    conn.execute(
        "UPDATE memory_graph_user_state SET canonical_snapshot_json = 'INVALID JSON'"
    )

    with pytest.raises(GraphConvergenceError) as exc_info:
        converge_user_graph(conn, 1)

    assert "Corrupted persisted graph:" in str(exc_info.value)


def test_cross_user_isolation():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "refresh_token", "v1", 1)
    insert_mock_fact(conn, 2, 2, "e2", "k2", "v2", 1)

    res1 = converge_user_graph(conn, 1)
    assert res1.matched_auth_facts_count == 1

    # user 2 is still missing
    res2 = converge_user_graph(conn, 2)
    assert res2.status == GraphConvergenceStatus.MISSING_REBUILD
    assert res2.matched_auth_facts_count == 1


def test_caller_transaction_ownership_preserved():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "refresh_token", "v1", 1)

    # Start a transaction manually
    conn.execute("SAVEPOINT my_savepoint")
    converge_user_graph(conn, 1)
    # Rollback
    conn.execute("ROLLBACK TO my_savepoint")

    # The graph should not be there because we rolled back caller transaction
    proj = load_graph_projection(conn, 1)
    assert proj is None


def test_source_churn_exhaustion(monkeypatch):
    import gateway.memory.graph_convergence

    call_count = 0
    original_read = gateway.memory.graph_convergence.read_authoritative_memory_facts

    def fake_read(conn, user_id):
        nonlocal call_count
        call_count += 1
        insert_mock_fact(conn, user_id, 100 + call_count, f"e{call_count}", "k", "v", 1)
        return original_read(conn, user_id=user_id)

    monkeypatch.setattr(
        gateway.memory.graph_convergence, "read_authoritative_memory_facts", fake_read
    )

    conn = setup_db()

    with pytest.raises(GraphConvergenceError) as exc_info:
        converge_user_graph(conn, 1)

    assert "SOURCE_CHURN_EXHAUSTION" in str(exc_info.value)


def test_authoritative_memory_rows_unchanged():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e1", "refresh_token", "v1", 1)

    # snapshot authoritative facts
    before = conn.execute("SELECT * FROM memory_os_facts").fetchall()

    converge_user_graph(conn, 1)

    after = conn.execute("SELECT * FROM memory_os_facts").fetchall()
    assert before == after
