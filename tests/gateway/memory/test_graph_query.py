import sqlite3
import time
import ast
import os
from dataclasses import FrozenInstanceError, replace
from types import MappingProxyType
from typing import Any, cast

import pytest

from ai_engineering.graph_contract import (
    AuthoritativeSourceSnapshot,
    GraphEdge,
    GraphNode,
    GraphProvenance,
    GraphSnapshot,
)
from gateway.memory.schema import migrate_memory_convergence_schema
from gateway.memory.graph_store import (
    migrate_memory_graph_store_schema,
    publish_graph_projection,
    rebuild_user_graph_store,
)
from gateway.memory.graph_projection import (
    AuthoritativeMemoryFact,
    read_authoritative_memory_facts,
    project_authoritative_memory_facts,
)
from gateway.memory.graph_query import (
    GraphContextStatus,
    GraphFactQuery,
    GraphReadIntegrityError,
    GraphStructuralMatch,
    hydrate_graph_matches,
    query_graph_projection,
    read_graph_context,
)


def setup_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate_memory_convergence_schema(conn, now=time.time())
    migrate_memory_graph_store_schema(conn)
    return conn


def insert_fact(conn, user_id, entity, key, val, rev=1):
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO memory_os_facts (user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?)",
        (user_id, entity, key, val, rev),
    )
    return cur.lastrowid


def update_fact(conn, fact_id, entity, key, val, rev):
    conn.execute(
        "UPDATE memory_os_facts SET entity=?, key=?, value=?, vector_revision=? WHERE id=?",
        (entity, key, val, rev, fact_id),
    )


def delete_fact(conn, fact_id):
    conn.execute("DELETE FROM memory_os_facts WHERE id=?", (fact_id,))


def rebuild_graph(conn, user_id):
    rebuild_user_graph_store(conn, user_id)


def projection_for_facts(*facts):
    projection = project_authoritative_memory_facts(tuple(facts), user_id=1)
    return projection


def projection_from_db(conn, user_id=1):
    facts = read_authoritative_memory_facts(conn, user_id=user_id)
    return facts, project_authoritative_memory_facts(facts, user_id=user_id)


def projection_with_structure(
    projection,
    *,
    nodes=None,
    edges=None,
    node_supports=None,
    edge_supports=None,
    authoritative_source=None,
):
    snapshot = GraphSnapshot.create(
        list(projection.snapshot.nodes if nodes is None else nodes),
        list(projection.snapshot.edges if edges is None else edges),
        projection.snapshot.authoritative_source
        if authoritative_source is None
        else authoritative_source,
    )
    return replace(
        projection,
        snapshot=snapshot,
        node_supports=MappingProxyType(
            dict(projection.node_supports if node_supports is None else node_supports)
        ),
        edge_supports=MappingProxyType(
            dict(projection.edge_supports if edge_supports is None else edge_supports)
        ),
    )


def structural_fixture():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    facts, projection = projection_from_db(conn)
    matches = query_graph_projection(projection, GraphFactQuery())
    assert len(matches) == 1
    return facts, projection, matches[0]


READ_ONLY_TABLES = (
    "memory_os_facts",
    "memory_os_vector_sync_outbox",
    "memory_os_vector_sync_meta",
    "memory_graph_store_meta",
    "memory_graph_user_state",
    "memory_graph_nodes",
    "memory_graph_edges",
    "memory_graph_node_supports",
    "memory_graph_edge_supports",
    "memory_graph_exclusions",
)


def db_snapshot(conn):
    return {
        table: tuple(conn.execute(f"SELECT * FROM {table} ORDER BY rowid"))
        for table in READ_ONLY_TABLES
    }


# --- QUERY CONTRACT ---


def test_query_contract_invalid_entity_type():
    with pytest.raises(ValueError):
        GraphFactQuery(entity=cast(Any, 123), key=None)


def test_query_contract_defaults_to_all_facts():
    assert GraphFactQuery() == GraphFactQuery(entity=None, key=None, limit=50)


def test_query_contract_invalid_key_type():
    with pytest.raises(ValueError):
        GraphFactQuery(entity=None, key=cast(Any, []))


def test_query_contract_limit_bool_rejected():
    with pytest.raises(ValueError):
        GraphFactQuery(entity=None, key=None, limit=True)


def test_query_contract_limit_zero_rejected():
    with pytest.raises(ValueError):
        GraphFactQuery(entity=None, key=None, limit=0)


def test_query_contract_limit_max_rejected():
    with pytest.raises(ValueError):
        GraphFactQuery(entity=None, key=None, limit=101)


def test_query_contract_immutable():
    q = GraphFactQuery(entity="Alice", key="age")
    with pytest.raises(FrozenInstanceError):
        setattr(q, "entity", "Bob")


# --- PURE QUERY & STRUCTURAL SEMANTICS ---


def test_pure_query_all_facts():
    conn = setup_db()
    insert_fact(conn, 1, "Alice", "age", "30")
    insert_fact(conn, 1, "Alice", "city", "NY")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.READY
    assert res.matched_count == 2
    assert len(res.matches) == 2


def test_pure_query_exact_entity():
    conn = setup_db()
    insert_fact(conn, 1, "Alice", "age", "30")
    insert_fact(conn, 1, "Bob", "age", "40")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity="Alice", key=None)
    )
    assert res.matched_count == 1
    assert res.matches[0].entity == "Alice"


def test_pure_query_exact_entity_no_match():
    conn = setup_db()
    insert_fact(conn, 1, "Alice", "age", "30")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity="Charlie", key=None)
    )
    assert res.matched_count == 0


def test_pure_query_exact_key():
    conn = setup_db()
    insert_fact(conn, 1, "Alice", "age", "30")
    insert_fact(conn, 1, "Alice", "city", "NY")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key="city")
    )
    assert res.matched_count == 1
    assert res.matches[0].key == "city"


def test_pure_query_entity_and_key():
    conn = setup_db()
    insert_fact(conn, 1, "Alice", "age", "30")
    insert_fact(conn, 1, "Alice", "city", "NY")
    insert_fact(conn, 1, "Bob", "age", "40")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity="Alice", key="age")
    )
    assert res.matched_count == 1
    assert res.matches[0].entity == "Alice"
    assert res.matches[0].key == "age"


def test_pure_query_case_sensitive():
    conn = setup_db()
    insert_fact(conn, 1, "alice", "age", "30")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity="Alice", key=None)
    )
    assert res.matched_count == 0


def test_pure_query_whitespace_preserved():
    conn = setup_db()
    insert_fact(conn, 1, "Alice ", " age ", " 30 ")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity="Alice ", key=" age ")
    )
    assert res.matched_count == 1
    assert res.matches[0].value == " 30 "


def test_pure_query_unicode_preserved():
    conn = setup_db()
    insert_fact(conn, 1, "Ёжик", "Ключ", "Значение🚀")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity="Ёжик", key=None)
    )
    assert res.matched_count == 1
    assert res.matches[0].value == "Значение🚀"


def test_pure_query_deterministic_ordering():
    conn = setup_db()
    insert_fact(conn, 1, "C", "k", "v1")
    insert_fact(conn, 1, "A", "k", "v2")
    insert_fact(conn, 1, "B", "k", "v3")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.matches[0].entity == "A"
    assert res.matches[1].entity == "B"
    assert res.matches[2].entity == "C"


def test_pure_query_limit_applied_after_ordering():
    conn = setup_db()
    insert_fact(conn, 1, "C", "k", "v1")
    insert_fact(conn, 1, "A", "k", "v2")
    insert_fact(conn, 1, "B", "k", "v3")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None, limit=2)
    )
    assert res.matched_count == 2
    assert res.matches[0].entity == "A"
    assert res.matches[1].entity == "B"


def test_structural_semantics_only_fact_returned():
    conn = setup_db()
    insert_fact(conn, 1, "Alice", "age", "30")
    rebuild_graph(conn, 1)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    for m in res.matches:
        # Match maps to memory:fact node
        assert m.entity is not None


def test_structural_semantics_wrong_relation_rejected():
    conn = setup_db()
    insert_fact(conn, 1, "Alice", "age", "30")
    rebuild_graph(conn, 1)

    conn.execute(
        "UPDATE memory_graph_edges SET relation_type='memory:wrong_relation' WHERE relation_type='memory:has_fact'"
    )

    with pytest.raises(GraphReadIntegrityError):
        # The schema mismatch or missing supports will trigger integrity error during loading or hydration
        read_graph_context(conn, user_id=1, query=GraphFactQuery(entity=None, key=None))


# --- READ STATUS & FRESHNESS ---


def test_read_status_missing_graph():
    conn = setup_db()
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.MISSING_GRAPH


def test_read_status_fresh_empty():
    conn = setup_db()
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.READY
    assert res.matched_count == 0


def test_read_status_fresh_populated():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.READY
    assert res.matched_count == 1


def test_freshness_ids_one_through_twelve_ready():
    conn = setup_db()
    for index in range(1, 13):
        assert insert_fact(conn, 1, "A", f"k{index}", f"v{index}") == index
    rebuild_graph(conn, 1)

    res = read_graph_context(conn, user_id=1, query=GraphFactQuery())

    assert res.status == GraphContextStatus.READY
    assert res.matched_count == 12


def test_freshness_crossing_nine_ten_ready():
    conn = setup_db()
    for index in range(10):
        insert_fact(conn, 1, "A", f"key-{index}", f"value-{index}")
    rebuild_graph(conn, 1)

    res = read_graph_context(conn, user_id=1, query=GraphFactQuery())

    assert res.status == GraphContextStatus.READY
    assert {row.sqlite_id for match in res.matches for row in match.supports} == set(
        range(1, 11)
    )


def test_freshness_sparse_multi_digit_ids_ready():
    conn = setup_db()
    insert_fact(conn, 2, "other", "k1", "v1")
    wanted_ids = [insert_fact(conn, 1, "A", "k2", "v2")]
    for index in range(3, 10):
        insert_fact(conn, 2, "other", f"k{index}", f"v{index}")
    wanted_ids.extend(
        [
            insert_fact(conn, 1, "A", "k10", "v10"),
            insert_fact(conn, 1, "A", "k11", "v11"),
        ]
    )
    assert wanted_ids == [2, 10, 11]
    rebuild_graph(conn, 1)

    res = read_graph_context(conn, user_id=1, query=GraphFactQuery())

    assert res.status == GraphContextStatus.READY
    assert sorted(
        row.sqlite_id for match in res.matches for row in match.supports
    ) == wanted_ids


def test_freshness_caller_tuple_order_independent():
    conn = setup_db()
    for index in range(1, 13):
        insert_fact(conn, 1, "A", f"k{index}", f"v{index}")
    facts = read_authoritative_memory_facts(conn, user_id=1)
    projection = project_authoritative_memory_facts(tuple(reversed(facts)), user_id=1)
    publish_graph_projection(conn, projection)

    res = read_graph_context(conn, user_id=1, query=GraphFactQuery())

    assert res.status == GraphContextStatus.READY
    assert res.matched_count == 12


def test_freshness_incomplete_persisted_source_is_integrity_error(monkeypatch):
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    facts, projection = projection_from_db(conn)
    incomplete = AuthoritativeSourceSnapshot(
        projection.snapshot.authoritative_source.facts, is_complete=False
    )
    projection = projection_with_structure(
        projection, authoritative_source=incomplete
    )
    monkeypatch.setattr(
        "gateway.memory.graph_query.read_authoritative_memory_facts",
        lambda _conn, *, user_id: facts,
    )
    monkeypatch.setattr(
        "gateway.memory.graph_query.load_graph_projection",
        lambda _conn, *, user_id: projection,
    )

    with pytest.raises(GraphReadIntegrityError, match="AUTHORITATIVE_SOURCE_INCOMPLETE"):
        read_graph_context(conn, user_id=1, query=GraphFactQuery())


def test_read_status_no_match_fresh():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity="X", key=None)
    )
    assert res.status == GraphContextStatus.READY
    assert res.matched_count == 0


def test_freshness_new_authoritative_fact_stale():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    insert_fact(conn, 1, "X", "Y", "Z")
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.STALE_GRAPH
    assert res.matched_count == 0


def test_freshness_deleted_authoritative_fact_stale():
    conn = setup_db()
    f1 = insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    delete_fact(conn, f1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.STALE_GRAPH


def test_freshness_revision_increment_stale():
    conn = setup_db()
    f1 = insert_fact(conn, 1, "A", "B", "C", rev=1)
    rebuild_graph(conn, 1)
    update_fact(conn, f1, "A", "B", "C", rev=2)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.STALE_GRAPH


def test_freshness_semantic_update_stale():
    conn = setup_db()
    f1 = insert_fact(conn, 1, "A", "B", "C", rev=1)
    rebuild_graph(conn, 1)
    update_fact(conn, f1, "A", "B", "D", rev=2)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.STALE_GRAPH


def test_same_revision_semantic_tampering_fails_hydration_closed():
    conn = setup_db()
    fact_id = insert_fact(conn, 1, "A", "B", "C", rev=1)
    rebuild_graph(conn, 1)
    update_fact(conn, fact_id, "A", "B", "tampered", rev=1)

    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_VALUE_MISMATCH"):
        read_graph_context(conn, user_id=1, query=GraphFactQuery())


def test_stale_result_is_always_payload_empty():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    insert_fact(conn, 1, "X", "Y", "Z")
    result = read_graph_context(conn, user_id=1, query=GraphFactQuery())
    assert result.status == GraphContextStatus.STALE_GRAPH
    assert result.matches == () and result.matched_count == 0


def test_freshness_no_auto_rebuild():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    insert_fact(conn, 1, "X", "Y", "Z")

    # Run query, expect STALE
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.status == GraphContextStatus.STALE_GRAPH

    # Verify graph wasn't automatically rebuilt (should still be STALE)
    res2 = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res2.status == GraphContextStatus.STALE_GRAPH


# --- HYDRATION ---


def test_hydration_one_support():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert len(res.matches[0].supports) == 1


def test_hydration_multiple_supports_dedup():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.matched_count == 1
    assert len(res.matches[0].supports) == 2


def test_hydration_missing_support_row_fail():
    _, _, match = structural_fixture()
    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_ROW_MISSING"):
        hydrate_graph_matches((), (match,), user_id=1)


def test_privacy_prohibited_field_never_returned():
    conn = setup_db()
    insert_fact(conn, 1, "A", "secret", "value123")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.matched_count == 0


def test_privacy_oversized_sentinel_never_returned():
    conn = setup_db()
    insert_fact(conn, 1, "A", "key", "X" * 4097)
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.matched_count == 0


def test_privacy_exclusion_reason_no_trigger():
    conn = setup_db()
    insert_fact(conn, 1, "A", "secret", "value123")
    rebuild_graph(conn, 1)
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key="secret")
    )
    assert res.matched_count == 0


def test_privacy_excluded_sentinel_never_enters_context():
    sentinel = "PR41_SECRET_CONTEXT_SENTINEL_918273"
    conn = setup_db()
    insert_fact(conn, 1, "A", "secret", sentinel)
    insert_fact(conn, 1, "A", "safe", "visible")
    rebuild_graph(conn, 1)

    result = read_graph_context(conn, user_id=1, query=GraphFactQuery())

    assert result.matched_count == 1
    assert sentinel not in repr(result)


# --- USER ISOLATION ---


def test_user_isolation_cannot_read_other_projection():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    insert_fact(conn, 2, "X", "Y", "Z")
    rebuild_graph(conn, 1)
    rebuild_graph(conn, 2)

    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res.matched_count == 1
    assert res.matches[0].entity == "A"


def test_user_isolation_identical_semantics():
    conn = setup_db()
    insert_fact(conn, 1, "User", "Name", "Common")
    insert_fact(conn, 2, "User", "Name", "Common")
    rebuild_graph(conn, 1)
    rebuild_graph(conn, 2)

    res1 = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    assert res1.matched_count == 1
    assert len(res1.matches[0].supports) == 1
    assert res1.matches[0].supports[0].user_id == 1


def test_user_id_bool_rejected():
    conn = setup_db()
    with pytest.raises(ValueError, match="user_id"):
        read_graph_context(conn, user_id=True, query=GraphFactQuery())


def test_user_isolation_cross_user_support_fails_closed():
    facts, _, match = structural_fixture()
    cross_user = replace(facts[0], user_id=2)
    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_USER_MISMATCH"):
        hydrate_graph_matches((cross_user,), (match,), user_id=1)


# --- DB SAFETY ---


def test_db_safety_total_changes_unchanged():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)

    before_changes = conn.total_changes
    res = read_graph_context(
        conn, user_id=1, query=GraphFactQuery(entity=None, key=None)
    )
    after_changes = conn.total_changes

    assert res.matched_count == 1
    assert before_changes == after_changes


# --- INTEGRITY ---


def test_integrity_corrupted_snapshot():
    conn = setup_db()
    insert_fact(conn, 1, "A", "B", "C")
    rebuild_graph(conn, 1)

    conn.execute(
        "UPDATE memory_graph_user_state SET canonical_snapshot_json = 'INVALID_JSON'"
    )

    with pytest.raises(GraphReadIntegrityError):
        read_graph_context(conn, user_id=1, query=GraphFactQuery(entity=None, key=None))


# --- STATIC ---


def test_static_no_forbidden_imports():
    query_py_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "gateway",
        "memory",
        "graph_query.py",
    )
    with open(query_py_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                assert "qdrant" not in name.name
                assert "telegram" not in name.name
                assert "provider" not in name.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert "qdrant" not in node.module
                assert "telegram" not in node.module
                assert "provider" not in node.module


def test_structural_semantics_disconnected_fact_rejected():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    _, projection = projection_from_db(conn)
    kept_edges = tuple(
        edge
        for edge in projection.snapshot.edges
        if edge.relation_type != "memory:has_fact"
    )
    kept_ids = {edge.edge_id for edge in kept_edges}
    corrupted = projection_with_structure(
        projection,
        edges=kept_edges,
        edge_supports={
            key: value
            for key, value in projection.edge_supports.items()
            if key in kept_ids
        },
    )

    with pytest.raises(GraphReadIntegrityError):
        query_graph_projection(corrupted, GraphFactQuery())


def test_structural_semantics_fact_linked_to_wrong_entity():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    insert_fact(conn, 1, "B", "other", "value")
    _, projection = projection_from_db(conn)
    fact = next(
        node
        for node in projection.snapshot.nodes
        if node.node_type == "memory:fact" and node.properties["entity"] == "A"
    )
    wrong_entity = next(
        node
        for node in projection.snapshot.nodes
        if node.node_type == "memory:entity" and node.properties["entity"] == "B"
    )
    original = next(
        edge
        for edge in projection.snapshot.edges
        if edge.relation_type == "memory:has_fact"
        and edge.target_node_id == fact.node_id
    )
    wrong_edge = GraphEdge.create(
        wrong_entity.node_id,
        fact.node_id,
        "memory:has_fact",
        {},
        original.provenance,
    )
    edges = tuple(
        wrong_edge if edge.edge_id == original.edge_id else edge
        for edge in projection.snapshot.edges
    )
    edge_supports = dict(projection.edge_supports)
    edge_supports[wrong_edge.edge_id] = edge_supports.pop(original.edge_id)
    corrupted = projection_with_structure(
        projection, edges=edges, edge_supports=edge_supports
    )

    with pytest.raises(GraphReadIntegrityError):
        query_graph_projection(corrupted, GraphFactQuery())


def test_structural_semantics_fact_with_two_entity_parents_rejected():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    insert_fact(conn, 1, "B", "other", "value")
    _, projection = projection_from_db(conn)
    fact = next(
        node
        for node in projection.snapshot.nodes
        if node.node_type == "memory:fact" and node.properties["entity"] == "A"
    )
    wrong_entity = next(
        node
        for node in projection.snapshot.nodes
        if node.node_type == "memory:entity" and node.properties["entity"] == "B"
    )
    primary_edge = next(
        edge
        for edge in projection.snapshot.edges
        if edge.relation_type == "memory:has_fact"
        and edge.target_node_id == fact.node_id
    )
    extra_edge = GraphEdge.create(
        wrong_entity.node_id,
        fact.node_id,
        "memory:has_fact",
        {},
        primary_edge.provenance,
    )
    corrupted = projection_with_structure(
        projection,
        edges=(*projection.snapshot.edges, extra_edge),
        edge_supports={
            **dict(projection.edge_supports),
            extra_edge.edge_id: projection.edge_supports[primary_edge.edge_id],
        },
    )

    with pytest.raises(GraphReadIntegrityError, match="FACT_PARENT_CARDINALITY_INVALID"):
        query_graph_projection(corrupted, GraphFactQuery())


def test_structural_semantics_entity_disconnected_from_user_rejected():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    _, projection = projection_from_db(conn)
    kept_edges = tuple(
        edge
        for edge in projection.snapshot.edges
        if edge.relation_type != "memory:has_entity"
    )
    kept_ids = {edge.edge_id for edge in kept_edges}
    corrupted = projection_with_structure(
        projection,
        edges=kept_edges,
        edge_supports={
            key: value
            for key, value in projection.edge_supports.items()
            if key in kept_ids
        },
    )

    with pytest.raises(GraphReadIntegrityError, match="ENTITY_REACHABILITY_INVALID"):
        query_graph_projection(corrupted, GraphFactQuery())


@pytest.mark.parametrize(
    ("node_type", "properties"),
    [
        ("memory:user", {"user_id": 2}),
        ("memory:entity", {"user_id": 2, "entity": "A"}),
        ("memory:fact", {"user_id": 2, "entity": "A", "key": "k", "value": "v"}),
        ("memory:fact", {"user_id": 1, "entity": "A", "key": "k"}),
        ("memory:fact", {"user_id": 1, "entity": "A", "key": 7, "value": "v"}),
    ],
)
def test_structural_semantics_invalid_node_properties_fail_closed(
    node_type, properties
):
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    _, projection = projection_from_db(conn)
    target = next(node for node in projection.snapshot.nodes if node.node_type == node_type)
    malformed = replace(target, properties=MappingProxyType(properties))
    snapshot = replace(
        projection.snapshot,
        nodes=tuple(
            malformed if node.node_id == target.node_id else node
            for node in projection.snapshot.nodes
        ),
    )

    with pytest.raises(GraphReadIntegrityError):
        query_graph_projection(replace(projection, snapshot=snapshot), GraphFactQuery())


def test_pure_layer_order_independent_of_node_edge_input_order():
    conn = setup_db()
    insert_fact(conn, 1, "B", "k", "2")
    insert_fact(conn, 1, "A", "k", "1")
    _, projection = projection_from_db(conn)
    reversed_projection = replace(
        projection,
        snapshot=replace(
            projection.snapshot,
            nodes=tuple(reversed(projection.snapshot.nodes)),
            edges=tuple(reversed(projection.snapshot.edges)),
        ),
        node_supports=MappingProxyType(
            dict(reversed(list(projection.node_supports.items())))
        ),
        edge_supports=MappingProxyType(
            dict(reversed(list(projection.edge_supports.items())))
        ),
    )

    assert query_graph_projection(projection, GraphFactQuery()) == (
        query_graph_projection(reversed_projection, GraphFactQuery())
    )


def test_hydration_wrong_revision_fail():
    facts, _, match = structural_fixture()
    provenance = GraphProvenance(
        "sqlite_memory_os_facts", match.node_provenance.fact_id, 2
    )
    corrupted = replace(match, node_provenance=provenance, supports=(provenance,))
    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_REVISION_MISMATCH"):
        hydrate_graph_matches(facts, (corrupted,), user_id=1)


def test_hydration_wrong_user_fail():
    facts, _, match = structural_fixture()
    wrong_user = replace(facts[0], user_id=2)
    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_USER_MISMATCH"):
        hydrate_graph_matches((wrong_user,), (match,), user_id=1)


def test_hydration_entity_mismatch_fail():
    facts, _, match = structural_fixture()
    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_ENTITY_MISMATCH"):
        hydrate_graph_matches((replace(facts[0], entity="B"),), (match,), user_id=1)


def test_hydration_key_mismatch_fail():
    facts, _, match = structural_fixture()
    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_KEY_MISMATCH"):
        hydrate_graph_matches((replace(facts[0], key="other"),), (match,), user_id=1)


def test_hydration_value_mismatch_fail():
    facts, _, match = structural_fixture()
    with pytest.raises(GraphReadIntegrityError, match="SUPPORT_VALUE_MISMATCH"):
        hydrate_graph_matches((replace(facts[0], value="other"),), (match,), user_id=1)


def test_hydration_unknown_source_system_fail():
    facts, _, match = structural_fixture()
    provenance = object.__new__(GraphProvenance)
    object.__setattr__(provenance, "source_system", "unknown")
    object.__setattr__(provenance, "fact_id", match.node_provenance.fact_id)
    object.__setattr__(provenance, "revision", match.node_provenance.revision)
    corrupted = replace(match, node_provenance=provenance, supports=(provenance,))
    with pytest.raises(GraphReadIntegrityError, match="UNKNOWN_SOURCE_SYSTEM"):
        hydrate_graph_matches(facts, (corrupted,), user_id=1)


def test_hydration_noncanonical_provenance_fact_id_fail():
    facts, _, match = structural_fixture()
    provenance = GraphProvenance("sqlite_memory_os_facts", "01", 1)
    corrupted = replace(match, node_provenance=provenance, supports=(provenance,))
    with pytest.raises(GraphReadIntegrityError, match="NONCANONICAL"):
        hydrate_graph_matches(facts, (corrupted,), user_id=1)


def test_hydration_duplicate_support_evidence_deduplicated():
    facts, _, match = structural_fixture()
    duplicated = replace(match, supports=(match.supports[0], match.supports[0]))
    result = hydrate_graph_matches(facts, (duplicated,), user_id=1)
    assert len(result) == 1
    assert len(result[0].supports) == 1


def test_hydration_multi_support_canonical_order():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    insert_fact(conn, 1, "A", "k", "v")
    facts, projection = projection_from_db(conn)
    match = query_graph_projection(projection, GraphFactQuery())[0]
    reversed_match = replace(match, supports=tuple(reversed(match.supports)))
    result = hydrate_graph_matches(tuple(reversed(facts)), (reversed_match,), user_id=1)
    assert [row.sqlite_id for row in result[0].supports] == [1, 2]


def test_db_safety_graph_tables_unchanged():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    rebuild_graph(conn, 1)
    before = db_snapshot(conn)
    changes = conn.total_changes
    result = read_graph_context(conn, user_id=1, query=GraphFactQuery())
    assert result.status == GraphContextStatus.READY
    assert db_snapshot(conn) == before
    assert conn.total_changes == changes


def test_db_safety_authoritative_facts_unchanged():
    conn = setup_db()
    insert_fact(conn, 1, "A", "k", "v")
    rebuild_graph(conn, 1)
    insert_fact(conn, 1, "B", "k", "v")
    before = db_snapshot(conn)
    changes = conn.total_changes
    result = read_graph_context(conn, user_id=1, query=GraphFactQuery())
    assert result.status == GraphContextStatus.STALE_GRAPH
    assert db_snapshot(conn) == before
    assert conn.total_changes == changes


@pytest.mark.parametrize("mode", ["READY", "STALE", "MISSING"])
def test_db_safety_all_tables_unchanged_for_every_read_status(mode):
    conn = setup_db()
    if mode != "MISSING":
        insert_fact(conn, 1, "A", "k", "v")
        rebuild_graph(conn, 1)
    if mode == "STALE":
        insert_fact(conn, 1, "B", "k", "v")
    before = db_snapshot(conn)
    changes = conn.total_changes
    result = read_graph_context(conn, user_id=1, query=GraphFactQuery())
    expected_status = {
        "READY": "READY", "STALE": "STALE_GRAPH", "MISSING": "MISSING_GRAPH"
    }[mode]
    assert result.status.name == expected_status
    assert db_snapshot(conn) == before
    assert conn.total_changes == changes


def test_test_file_has_no_placeholder_test_body():
    tree = ast.parse(open(__file__, encoding="utf-8").read())
    placeholders = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        and (not node.body or all(isinstance(item, ast.Pass) for item in node.body))
    ]
    assert placeholders == []


def test_static_read_path_has_no_mutation_surface():
    source = open(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "gateway", "memory", "graph_query.py"
        ),
        encoding="utf-8",
    ).read()
    tree = ast.parse(source)
    forbidden_calls = {
        "migrate_memory_graph_store_schema",
        "publish_graph_projection",
        "rebuild_user_graph_store",
        "clear_user_graph_projection",
    }
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint(forbidden_calls)
    for keyword in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ", "ALTER "):
        assert keyword not in source.upper()
