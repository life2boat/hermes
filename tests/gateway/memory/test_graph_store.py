import sqlite3
import pytest

from ai_engineering.graph_contract import GraphVerificationError
from gateway.memory.graph_projection import (
    GraphProjectionResult,
    verify_graph_projection_result,
    project_authoritative_memory_facts,
    read_authoritative_memory_facts,
)
from gateway.memory.graph_store import (
    classify_memory_graph_store_schema,
    validate_memory_graph_store_schema,
    migrate_memory_graph_store_schema,
    publish_graph_projection,
    load_graph_projection,
    clear_user_graph_projection,
    rebuild_user_graph_store,
    GraphStoreSchemaClassification,
    GraphStoreError,
    MEMORY_GRAPH_STORE_SCHEMA_VERSION,
)

from gateway.memory.schema import migrate_memory_convergence_schema
import time


def setup_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate_memory_convergence_schema(conn, now=time.time())
    return conn


def insert_fact(conn, user_id, entity, key, val, rev=1):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO memory_os_facts (user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?)",
        (user_id, entity, key, val, rev),
    )
    return cur.lastrowid


def test_schema_classification_absent():
    conn = setup_db()
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.ABSENT
    )
    with pytest.raises(GraphStoreError):
        validate_memory_graph_store_schema(conn)


def test_schema_migration_and_current():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.CURRENT
    )
    validate_memory_graph_store_schema(conn)


def test_schema_migration_idempotent():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    migrate_memory_graph_store_schema(conn)
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.CURRENT
    )


def test_schema_incompatible():
    conn = setup_db()
    conn.execute(
        "CREATE TABLE memory_graph_store_meta (singleton_id INTEGER PRIMARY KEY, schema_version INTEGER)"
    )
    conn.execute("INSERT INTO memory_graph_store_meta VALUES (1, 999)")
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    with pytest.raises(GraphStoreError, match="Cannot migrate"):
        migrate_memory_graph_store_schema(conn)


def test_publish_and_load_empty_projection():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)

    # Empty projection
    facts = read_authoritative_memory_facts(conn, user_id=1)
    proj = project_authoritative_memory_facts(facts, user_id=1)

    publish_graph_projection(conn, proj)

    loaded = load_graph_projection(conn, 1)
    assert loaded is not None
    assert loaded.projection_id == proj.projection_id
    assert loaded.input_fact_count == 0
    assert len(loaded.snapshot.nodes) == 0
    assert len(loaded.snapshot.edges) == 0


def test_publish_and_load_populated_projection():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)

    insert_fact(conn, 1, "Alice", "likes", "apples")
    insert_fact(conn, 1, "Alice", "likes", "bananas")
    insert_fact(conn, 2, "Bob", "likes", "oranges")

    rebuild_user_graph_store(conn, 1)

    loaded = load_graph_projection(conn, 1)
    assert loaded is not None
    assert loaded.input_fact_count == 2
    assert loaded.projected_fact_count == 2
    assert len(loaded.snapshot.nodes) == 4  # user, entity, fact, fact
    assert len(loaded.snapshot.edges) == 3  # has_entity, has_fact, has_fact

    # Bob is completely isolated
    loaded_bob = load_graph_projection(conn, 2)
    assert loaded_bob is None


def test_stale_read():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    assert load_graph_projection(conn, 999) is None


def test_replace_atomic():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)

    insert_fact(conn, 1, "Alice", "likes", "apples")
    rebuild_user_graph_store(conn, 1)
    proj1 = load_graph_projection(conn, 1)
    assert proj1 is not None

    # modify DB
    insert_fact(conn, 1, "Alice", "hates", "pears")
    rebuild_user_graph_store(conn, 1)
    proj2 = load_graph_projection(conn, 1)
    assert proj2 is not None

    assert proj1.projection_id != proj2.projection_id
    assert proj2.projected_fact_count == 2


def test_cross_user_isolation():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    insert_fact(conn, 1, "Alice", "x", "y")
    insert_fact(conn, 2, "Bob", "a", "b")

    rebuild_user_graph_store(conn, 1)
    rebuild_user_graph_store(conn, 2)

    conn.execute("DELETE FROM memory_graph_user_state WHERE user_id = 1")

    assert load_graph_projection(conn, 1) is None
    assert load_graph_projection(conn, 2) is not None


def test_clear_derived_user_graph():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    insert_fact(conn, 1, "Alice", "x", "y")
    rebuild_user_graph_store(conn, 1)

    clear_user_graph_projection(conn, 1)
    assert load_graph_projection(conn, 1) is None

    # Authoritative is unchanged
    assert len(read_authoritative_memory_facts(conn, user_id=1)) == 1


def test_transaction_rollback_preserves_old_state():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    insert_fact(conn, 1, "Alice", "x", "y")
    rebuild_user_graph_store(conn, 1)
    proj1 = load_graph_projection(conn, 1)
    assert proj1 is not None

    insert_fact(conn, 1, "Alice", "hates", "pears")
    facts = read_authoritative_memory_facts(conn, user_id=1)
    proj2 = project_authoritative_memory_facts(facts, user_id=1)

    # Tamper with proj2 to cause publish failure
    import dataclasses

    proj2 = dataclasses.replace(proj2, projection_id="tampered")

    with pytest.raises(Exception):
        publish_graph_projection(conn, proj2)

    proj_after = load_graph_projection(conn, 1)
    assert proj_after is not None
    assert proj_after.projection_id == proj1.projection_id


def test_read_back_corruption():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    insert_fact(conn, 1, "Alice", "x", "y")
    rebuild_user_graph_store(conn, 1)

    # delete a node row manually to simulate corruption
    cur = conn.cursor()
    cur.execute("DELETE FROM memory_graph_nodes WHERE node_type = 'memory:entity'")

    with pytest.raises(GraphStoreError, match="Node count mismatch"):
        load_graph_projection(conn, 1)


def test_foreign_key_enforcement():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    insert_fact(conn, 1, "Alice", "x", "y")
    rebuild_user_graph_store(conn, 1)

    cur = conn.cursor()
    with pytest.raises(sqlite3.IntegrityError):
        # Insert edge pointing to non-existent node
        cur.execute(
            """INSERT INTO memory_graph_edges
               (user_id, edge_id, source_node_id, target_node_id, relation_type, properties_json, primary_provenance_fact_id, primary_provenance_revision)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                1,
                "bad_edge",
                "bad_node_1",
                "bad_node_2",
                "memory:has_entity",
                "{}",
                1,
                1,
            ),
        )


@pytest.mark.parametrize(
    "corrupt_json",
    [
        "{}",
        '{"schema_version": 1}',
        '{"snapshot_id": "tampered", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "tampered", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_tampered", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 2, "snapshot_id": "gs_tampered", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        "[]",
        "null",
        '"tampered"',
        "123",
        '{"schema_version": 1, "nodes": []}',
        '{"snapshot_id": "gs_x"}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": null, "edges": []}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": null}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": null}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [null], "edges": []}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [null]}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [{"node_id": "x"}], "edges": []}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [{"node_id": "x", "node_type": "t"}], "edges": []}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [{"edge_id": "e"}]}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [{"edge_id": "e", "source_node_id": "a"}]}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [{"edge_id": "e", "source_node_id": "a", "target_node_id": "b"}]}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [null], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [{"fact_id": "f"}], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [{"fact_id": "f", "revision": 1}], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": false}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": "maybe"}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": 1}}',
        '{"schema_version": "1", "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": 123, "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": "[]", "edges": [], "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": "[]", "authoritative_source": {"facts": [], "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": "null"}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": "[]", "is_complete": true}}',
        '{"schema_version": 1, "snapshot_id": "gs_x", "nodes": [], "edges": [], "authoritative_source": {"facts": [], "is_complete": null}}',
    ],
)
def test_read_corruption_json_parameterized(corrupt_json):
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    insert_fact(conn, 1, "Alice", "likes", "apples")
    rebuild_user_graph_store(conn, 1)

    # We corrupt it manually then load it, we expect error
    conn.execute(
        "UPDATE memory_graph_user_state SET canonical_snapshot_json = ?",
        (corrupt_json,),
    )
    with pytest.raises(GraphStoreError):
        load_graph_projection(conn, 1)
