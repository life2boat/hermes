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


import json
import sqlite3
import pytest
from gateway.memory.graph_store import (
    GraphStoreSchemaClassification,
    classify_memory_graph_store_schema,
    migrate_memory_graph_store_schema,
    publish_graph_projection,
    load_graph_projection,
    GraphStoreError,
    _CREATE_META,
    MEMORY_GRAPH_STORE_SCHEMA_VERSION,
)
import gateway.memory.graph_store as gs
from gateway.memory.graph_projection import GraphProjectionResult


import json
import sqlite3
import pytest
from gateway.memory.graph_store import (
    GraphStoreSchemaClassification,
    classify_memory_graph_store_schema,
    migrate_memory_graph_store_schema,
    publish_graph_projection,
    load_graph_projection,
    GraphStoreError,
    _CREATE_META,
    MEMORY_GRAPH_STORE_SCHEMA_VERSION,
)
import gateway.memory.graph_store as gs
from gateway.memory.graph_projection import GraphProjectionResult

import json
import sqlite3
import pytest
from gateway.memory.graph_store import (
    GraphStoreSchemaClassification,
    classify_memory_graph_store_schema,
    migrate_memory_graph_store_schema,
    publish_graph_projection,
    load_graph_projection,
    GraphStoreError,
    _CREATE_META,
    MEMORY_GRAPH_STORE_SCHEMA_VERSION,
)
import gateway.memory.graph_store as gs
from gateway.memory.graph_projection import GraphProjectionResult


# Helper to populate and build projection
def _get_populated_conn_and_proj():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    insert_fact(conn, 1, "Alice", "likes", "apples")
    facts = gs.read_authoritative_memory_facts(conn, user_id=1)
    projection = gs.project_authoritative_memory_facts(facts, user_id=1)
    return conn, projection


def test_classify_partial_schema():
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_META)
    conn.execute(
        "INSERT INTO memory_graph_store_meta (singleton_id, schema_version) VALUES (1, 1)"
    )
    cls = classify_memory_graph_store_schema(conn)
    assert cls == GraphStoreSchemaClassification.INCOMPATIBLE


def test_classify_wrong_columns():
    conn = setup_db()
    migrate_memory_graph_store_schema(conn)
    conn.execute("DROP TABLE memory_graph_store_meta")
    conn.execute(
        "CREATE TABLE memory_graph_store_meta (singleton_id TEXT, schema_version TEXT)"
    )
    cls = classify_memory_graph_store_schema(conn)
    assert cls == GraphStoreSchemaClassification.INCOMPATIBLE


def test_migrate_incompatible_rollback():
    conn = setup_db()
    conn.execute("CREATE TABLE memory_graph_nodes (wrong_col TEXT)")
    with pytest.raises(GraphStoreError):
        migrate_memory_graph_store_schema(conn)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_graph_%'"
    ).fetchall()
    assert len(tables) == 1
    assert tables[0][0] == "memory_graph_nodes"


def test_foreign_keys_off():
    conn, projection = _get_populated_conn_and_proj()
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    with pytest.raises(GraphStoreError, match="foreign_keys=ON is required"):
        publish_graph_projection(conn, projection)

    assert load_graph_projection(conn, 1) is None


@pytest.mark.parametrize(
    "tamper_col",
    [
        "input_fact_count",
        "projected_fact_count",
        "excluded_fact_count",
        "graph_schema_version",
        "projection_version",
    ],
)
def test_count_and_version_tamper(tamper_col):
    conn, projection = _get_populated_conn_and_proj()
    publish_graph_projection(conn, projection)

    conn.execute(
        f"UPDATE memory_graph_user_state SET {tamper_col} = 999 WHERE user_id = 1"
    )

    with pytest.raises(GraphStoreError):
        load_graph_projection(conn, 1)


def test_noncanonical_json():
    conn, projection = _get_populated_conn_and_proj()
    publish_graph_projection(conn, projection)

    cur = conn.cursor()
    cur.execute(
        "SELECT canonical_snapshot_json FROM memory_graph_user_state WHERE user_id = 1"
    )
    js = cur.fetchone()[0]

    obj = json.loads(js)
    noncanonical_js = json.dumps(obj, indent=2)

    cur.execute(
        "UPDATE memory_graph_user_state SET canonical_snapshot_json = ? WHERE user_id = 1",
        (noncanonical_js,),
    )
    with pytest.raises(GraphStoreError, match="Noncanonical JSON stored"):
        load_graph_projection(conn, 1)


@pytest.mark.parametrize(
    "hook_point",
    [
        "after_delete",
        "after_user_state",
        "after_nodes",
        "after_edges",
        "after_node_supports",
        "after_edge_supports",
        "after_exclusions",
        "before_release",
    ],
)
def test_failure_injection_rollback(hook_point):
    conn, projection = _get_populated_conn_and_proj()
    publish_graph_projection(conn, projection)

    loaded = load_graph_projection(conn, 1)
    assert loaded is not None

    gs._FAILURE_INJECTION_HOOK = hook_point
    try:
        with pytest.raises(Exception, match=hook_point):
            publish_graph_projection(conn, projection)
    finally:
        gs._FAILURE_INJECTION_HOOK = None

    loaded2 = load_graph_projection(conn, 1)
    assert loaded2 is not None
    assert loaded2.projection_id == loaded.projection_id


def test_extra_node_rejection():
    conn, projection = _get_populated_conn_and_proj()
    publish_graph_projection(conn, projection)

    conn.execute(
        "INSERT INTO memory_graph_nodes (user_id, node_id, node_type, properties_json, primary_provenance_fact_id, primary_provenance_revision) VALUES (1, 'fake_node', 'T', '{}', '1', 1)"
    )

    with pytest.raises(GraphStoreError, match="Node count mismatch"):
        load_graph_projection(conn, 1)


def test_extra_edge_rejection():
    conn, projection = _get_populated_conn_and_proj()
    publish_graph_projection(conn, projection)

    real_node = list(projection.node_supports.keys())[0]
    conn.execute(
        "INSERT INTO memory_graph_edges (user_id, edge_id, source_node_id, target_node_id, relation_type, properties_json, primary_provenance_fact_id, primary_provenance_revision) VALUES (1, 'fake_edge', ?, ?, 'R', '{}', '1', 1)",
        (real_node, real_node),
    )

    with pytest.raises(GraphStoreError, match="Edge count mismatch"):
        load_graph_projection(conn, 1)


def test_extra_support_rejection():
    conn, projection = _get_populated_conn_and_proj()
    publish_graph_projection(conn, projection)

    real_node = list(projection.node_supports.keys())[0]
    conn.execute(
        "INSERT INTO memory_graph_node_supports (user_id, node_id, fact_id, revision) VALUES (1, ?, '999', 1)",
        (real_node,),
    )

    with pytest.raises(
        GraphStoreError, match="Validation of reconstructed projection failed"
    ):
        load_graph_projection(conn, 1)


def test_savepoint_cleanup():
    conn, projection = _get_populated_conn_and_proj()
    gs._FAILURE_INJECTION_HOOK = "before_release"
    try:
        with pytest.raises(Exception):
            publish_graph_projection(conn, projection)
    finally:
        gs._FAILURE_INJECTION_HOOK = None

    conn.execute("SAVEPOINT publish_graph")
    conn.execute("RELEASE SAVEPOINT publish_graph")


def test_outer_transaction_preservation():
    conn, projection = _get_populated_conn_and_proj()
    conn.commit()  # End implicit transaction
    conn.isolation_level = None  # Autocommit mode, we manage transactions
    conn.execute("CREATE TABLE dummy (id INT)")

    conn.execute("BEGIN TRANSACTION")
    conn.execute("INSERT INTO dummy VALUES (1)")

    gs._FAILURE_INJECTION_HOOK = "after_delete"
    try:
        with pytest.raises(Exception):
            publish_graph_projection(conn, projection)
    finally:
        gs._FAILURE_INJECTION_HOOK = None

    conn.execute("COMMIT")

    count = conn.execute("SELECT COUNT(*) FROM dummy").fetchone()[0]
    assert count == 1


def test_schema_classification_adversarial():
    from gateway.memory.graph_store import (
        _CREATE_META,
        _CREATE_USER_STATE,
        _CREATE_NODES,
        _CREATE_EDGES,
        _CREATE_NODE_SUPPORTS,
        _CREATE_EDGE_SUPPORTS,
        _CREATE_EXCLUSIONS,
        classify_memory_graph_store_schema,
        MEMORY_GRAPH_STORE_SCHEMA_VERSION,
        migrate_memory_graph_store_schema,
        GraphStoreError,
    )

    # helper
    def build_schema(override_stmts=None):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        stmts = {
            "meta": _CREATE_META,
            "user_state": _CREATE_USER_STATE,
            "nodes": _CREATE_NODES,
            "edges": _CREATE_EDGES,
            "node_supports": _CREATE_NODE_SUPPORTS,
            "edge_supports": _CREATE_EDGE_SUPPORTS,
            "exclusions": _CREATE_EXCLUSIONS,
        }
        if override_stmts:
            stmts.update(override_stmts)
        for s in stmts.values():
            if s:
                conn.execute(s)
        conn.execute(
            "INSERT INTO memory_graph_store_meta VALUES (1, ?)",
            (MEMORY_GRAPH_STORE_SCHEMA_VERSION,),
        )
        return conn

    # correct schema
    conn = build_schema()
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.CURRENT
    )
    conn.close()

    # missing table
    conn = build_schema({"edges": None})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # wrong column type
    wrong_type = _CREATE_NODES.replace(
        "node_type TEXT NOT NULL", "node_type INTEGER NOT NULL"
    )
    conn = build_schema({"nodes": wrong_type})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # wrong NOT NULL
    wrong_notnull = _CREATE_NODES.replace("node_type TEXT NOT NULL", "node_type TEXT")
    conn = build_schema({"nodes": wrong_notnull})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # wrong PK
    wrong_pk = _CREATE_NODES.replace(
        "PRIMARY KEY (user_id, node_id)", "PRIMARY KEY (user_id)"
    )
    conn = build_schema({"nodes": wrong_pk})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # wrong composite PK order
    wrong_pk_order = _CREATE_NODES.replace(
        "PRIMARY KEY (user_id, node_id)", "PRIMARY KEY (node_id, user_id)"
    )
    conn = build_schema({"nodes": wrong_pk_order})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # missing FK
    missing_fk = _CREATE_EDGES.replace(
        "FOREIGN KEY (user_id) REFERENCES memory_graph_user_state(user_id) ON DELETE CASCADE,",
        "",
    )
    conn = build_schema({"edges": missing_fk})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # wrong FK target
    wrong_fk_target = _CREATE_EDGES.replace(
        "REFERENCES memory_graph_user_state(user_id)",
        "REFERENCES memory_graph_nodes(user_id)",
    )
    conn = build_schema({"edges": wrong_fk_target})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # wrong ON DELETE behavior
    wrong_on_delete = _CREATE_EDGES.replace("ON DELETE CASCADE", "ON DELETE RESTRICT")
    conn = build_schema({"edges": wrong_on_delete})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # meta table without singleton CHECK
    wrong_meta = _CREATE_META.replace("CHECK(singleton_id = 1)", "")
    conn = build_schema({"meta": wrong_meta})
    assert (
        classify_memory_graph_store_schema(conn)
        == GraphStoreSchemaClassification.INCOMPATIBLE
    )
    conn.close()

    # existing malformed partial schema -> migration fails closed
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memory_graph_nodes (user_id INTEGER)")

    with pytest.raises(GraphStoreError) as excinfo:
        migrate_memory_graph_store_schema(conn)
    assert "Cannot migrate from INCOMPATIBLE schema" in str(excinfo.value)

    # verify unchanged
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {r[0] for r in cur.fetchall()}
    assert tables == {"memory_graph_nodes"}
    conn.close()


def test_schema_version_independence():
    from gateway.memory.graph_store import (
        publish_graph_projection,
        load_graph_projection,
        GraphStoreError,
    )
    from ai_engineering.graph_contract import (
        GRAPH_SCHEMA_VERSION,
        AuthoritativeSourceSnapshot,
    )
    from gateway.memory.graph_projection import project_authoritative_memory_facts
    from gateway.memory.graph_store import MEMORY_GRAPH_STORE_SCHEMA_VERSION
    import sqlite3
    import pytest

    conn = setup_db()
    migrate_memory_graph_store_schema(conn)

    proj = project_authoritative_memory_facts(tuple([]), user_id=1)

    invalid_proj = project_authoritative_memory_facts(tuple([]), user_id=1)
    object.__setattr__(
        invalid_proj.snapshot, "schema_version", GRAPH_SCHEMA_VERSION + 1
    )

    with pytest.raises(GraphStoreError) as excinfo:
        publish_graph_projection(conn, invalid_proj)
    assert (
        f"Snapshot schema version {GRAPH_SCHEMA_VERSION + 1} != {GRAPH_SCHEMA_VERSION}"
        in str(excinfo.value)
    )

    # 2. Write successful
    publish_graph_projection(conn, proj)

    # 3. Read back -> it should pass
    loaded = load_graph_projection(conn, proj.user_id)
    assert loaded is not None

    # 4. Tamper store version
    conn.execute(
        "UPDATE memory_graph_user_state SET graph_schema_version = ?",
        (GRAPH_SCHEMA_VERSION + 1,),
    )
    with pytest.raises(GraphStoreError) as excinfo:
        load_graph_projection(conn, proj.user_id)
    assert "Tampered graph_schema_version" in str(excinfo.value)


def test_exclusions_through_store():
    from gateway.memory.graph_store import (
        publish_graph_projection,
        load_graph_projection,
    )
    from gateway.memory.graph_projection import (
        project_authoritative_memory_facts,
        verify_graph_projection_result,
        AuthoritativeMemoryFact,
    )

    conn = setup_db()
    migrate_memory_graph_store_schema(conn)

    facts = [
        AuthoritativeMemoryFact(
            1,
            1,
            "e1",
            "password",
            "VERY_SECRET_PR31_SENTINEL_73921",
            1,
            None,
            1.0,
            "",
            "",
        ),
        AuthoritativeMemoryFact(2, 1, "e2", "a" * 5000, "val", 1, None, 1.0, "", ""),
        AuthoritativeMemoryFact(3, 1, "e3", "name", "John", 1, None, 1.0, "", ""),
    ]

    # 1. Project
    projection = project_authoritative_memory_facts(tuple(facts), user_id=1)

    # 2. Verify
    verify_graph_projection_result(projection)

    # 3. Publish
    publish_graph_projection(conn, projection)

    # 4. Load
    loaded = load_graph_projection(conn, 1)
    assert loaded is not None
    assert loaded.excluded_fact_count == 2
    assert len(loaded.exclusions) == 2

    reasons = {ex.reason for ex in loaded.exclusions}
    assert "PROHIBITED_FIELD" in reasons
    assert "GRAPH_STRING_BOUND_EXCEEDED" in reasons

    # 5. Check no leakage in DB
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memory_graph_%'"
    )
    tables = [row[0] for row in cur.fetchall()]

    for table in tables:
        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        for row in rows:
            row_str = str(row)
            assert "VERY_SECRET_PR31_SENTINEL_73921" not in row_str
