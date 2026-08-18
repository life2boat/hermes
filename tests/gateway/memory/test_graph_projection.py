import json
import sqlite3
import pytest
import sys

from gateway.memory.schema import FACTS_TABLE, MEMORY_CONVERGENCE_SCHEMA_STATEMENTS
from gateway.memory.graph_projection import (
    AuthoritativeMemoryFact,
    GraphProjectionResult,
    read_authoritative_memory_facts,
    project_authoritative_memory_facts,
    ProjectionError,
    MAX_PROJECTION_FACTS,
    ProjectionExclusion
)
from ai_engineering.graph_contract import (
    GraphNode, GraphEdge, GraphProvenance, GraphSnapshot, AuthoritativeSourceSnapshot, AuthoritativeFactState,
    MAX_PROPERTY_KEY_LENGTH, MAX_STRING_PROPERTY_LENGTH, GraphVerificationError, serialize_graph_snapshot,
    classify_provenance
)

def _setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for stmt in MEMORY_CONVERGENCE_SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.execute("INSERT INTO memory_os_vector_sync_meta(singleton_id, schema_seeded) VALUES (1, 1)")
    return conn

def fact_factory(sqlite_id, user_id=100, entity="E", key="K", value="V", vector_revision=1, source=None, trust_score=0.9, created_at="2026", updated_at="2026"):
    return AuthoritativeMemoryFact(sqlite_id, user_id, entity, key, value, vector_revision, source, trust_score, created_at, updated_at)

def test_01_empty_scope():
    assert project_authoritative_memory_facts(tuple(), user_id=100).input_fact_count == 0

def test_02_one_fact():
    res = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    assert res.projected_fact_count == 1
    assert len(res.snapshot.nodes) == 3 # user, entity, fact

def test_03_multiple_facts():
    f1 = fact_factory(1, key="K1")
    f2 = fact_factory(2, key="K2")
    res = project_authoritative_memory_facts((f1, f2), user_id=100)
    assert res.projected_fact_count == 2
    assert len(res.snapshot.nodes) == 4

def test_04_multiple_entities():
    res = project_authoritative_memory_facts((fact_factory(1, entity="E1"), fact_factory(2, entity="E2")), user_id=100)
    assert len(res.snapshot.nodes) == 5

def test_05_multiple_keys():
    res = project_authoritative_memory_facts((fact_factory(1, key="K1"), fact_factory(2, key="K2")), user_id=100)
    assert len(res.snapshot.nodes) == 4

def test_06_duplicate_semantic_fact_aggregates_supports():
    res = project_authoritative_memory_facts((fact_factory(1), fact_factory(2)), user_id=100)
    assert res.projected_fact_count == 2
    assert len(res.snapshot.nodes) == 3
    fact_node_id = [n.node_id for n in res.snapshot.nodes if n.node_type == 'memory:fact'][0]
    assert len(res.node_supports[fact_node_id]) == 2

def test_07_duplicate_sqlite_id_exact():
    with pytest.raises(ProjectionError, match="Duplicate sqlite_id"):
        project_authoritative_memory_facts((fact_factory(1), fact_factory(1)), user_id=100)

def test_08_duplicate_sqlite_id_conflicting():
    with pytest.raises(ProjectionError, match="Duplicate sqlite_id"):
        project_authoritative_memory_facts((fact_factory(1), fact_factory(1, key="Diff")), user_id=100)

def test_09_reversed_input_identical_primary_support():
    f1 = fact_factory(1)
    f2 = fact_factory(2)
    r1 = project_authoritative_memory_facts((f1, f2), user_id=100)
    r2 = project_authoritative_memory_facts((f2, f1), user_id=100)
    assert serialize_graph_snapshot(r1.snapshot) == serialize_graph_snapshot(r2.snapshot)
    assert r1.projection_id == r2.projection_id

def test_10_repeated_execution():
    f = fact_factory(1)
    r1 = project_authoritative_memory_facts((f,), user_id=100)
    r2 = project_authoritative_memory_facts((f,), user_id=100)
    assert r1.projection_id == r2.projection_id

def test_11_cross_user_input():
    with pytest.raises(ProjectionError, match="CROSS_USER_INPUT_REJECTION"):
        project_authoritative_memory_facts((fact_factory(1, user_id=200),), user_id=100)

def test_12_same_semantics_across_two_users():
    r1 = project_authoritative_memory_facts((fact_factory(1, user_id=100),), user_id=100)
    r2 = project_authoritative_memory_facts((fact_factory(1, user_id=200),), user_id=200)
    assert r1.projection_id != r2.projection_id
    assert r1.snapshot.nodes[0].node_id != r2.snapshot.nodes[0].node_id

@pytest.mark.parametrize("kwargs,err", [
    ({"sqlite_id": "1"}, "sqlite_id must be a positive int"),
    ({"sqlite_id": 0}, "sqlite_id must be a positive int"),
    ({"sqlite_id": True}, "sqlite_id must be a positive int"),
    ({"vector_revision": 0}, "vector_revision must be int >= 1"),
    ({"vector_revision": -1}, "vector_revision must be int >= 1"),
    ({"vector_revision": True}, "vector_revision must be int >= 1"),
    ({"entity": 123}, "entity must be str"),
    ({"key": 123}, "key must be str"),
    ({"value": 123}, "value must be str"),
    ({"source": 123}, "source must be None or str"),
    ({"trust_score": "high"}, "trust_score must be a finite real number"),
    ({"trust_score": float('inf')}, "trust_score must be a finite real number"),
    ({"trust_score": True}, "trust_score must be a finite real number"),
    ({"created_at": 123}, "created_at must be str"),
    ({"updated_at": 123}, "updated_at must be str"),
    ({"updated_at": True}, "updated_at must be str"),
])
def test_13_to_28_malformed_facts(kwargs, err):
    with pytest.raises(ValueError, match=err):
        if "sqlite_id" in kwargs:
            fact_factory(**kwargs)
        else:
            fact_factory(1, **kwargs)

@pytest.mark.parametrize("key", ["password", "api_key", "raw_prompt", "chain_of_thought", "secret"])
def test_29_to_33_exclusions(key):
    res = project_authoritative_memory_facts((fact_factory(1, key=key),), user_id=100)
    assert res.excluded_fact_count == 1
    assert res.exclusions[0].reason == "PROHIBITED_FIELD"
    assert "secret" not in serialize_graph_snapshot(res.snapshot).lower()

@pytest.mark.parametrize("key", ["secretary", "password_hint", "api_key_note"])
def test_34_to_36_negative_classifiers(key):
    res = project_authoritative_memory_facts((fact_factory(1, key=key),), user_id=100)
    assert res.excluded_fact_count == 0

def test_37_101_char_key_accepted():
    res = project_authoritative_memory_facts((fact_factory(1, key="a"*101),), user_id=100)
    assert res.excluded_fact_count == 0

def test_38_4096_char_key_accepted():
    res = project_authoritative_memory_facts((fact_factory(1, key="a"*4096),), user_id=100)
    assert res.excluded_fact_count == 0

def test_39_4097_char_key_excluded():
    res = project_authoritative_memory_facts((fact_factory(1, key="a"*4097),), user_id=100)
    assert res.excluded_fact_count == 1
    assert res.exclusions[0].reason == "GRAPH_STRING_BOUND_EXCEEDED"

def test_40_oversized_entity():
    res = project_authoritative_memory_facts((fact_factory(1, entity="a"*4097),), user_id=100)
    assert res.excluded_fact_count == 1

def test_41_oversized_value():
    res2 = project_authoritative_memory_facts((fact_factory(1, value="a"*4097),), user_id=100)
    assert res2.excluded_fact_count == 1

def test_42_499_fact_max():
    facts = tuple(fact_factory(i, entity=f"E{i}", key=f"K{i}") for i in range(1, 500))
    res = project_authoritative_memory_facts(facts, user_id=100)
    assert res.projected_fact_count == 499
    assert len(res.snapshot.nodes) == 999
    assert len(res.snapshot.edges) == 998

def test_43_500_fact_overflow():
    facts = tuple(fact_factory(i, key=str(i)) for i in range(1, 501))
    with pytest.raises(ProjectionError, match="PROJECTION_LIMIT_EXCEEDED"):
        project_authoritative_memory_facts(facts, user_id=100)

def test_44_updates_change_projection_id():
    r1 = project_authoritative_memory_facts((fact_factory(1, value="V1", vector_revision=1),), user_id=100)
    r2 = project_authoritative_memory_facts((fact_factory(1, value="V2", vector_revision=2),), user_id=100)
    assert r1.projection_id != r2.projection_id

def test_45_same_value_different_revision_changes_projection_id():
    r1 = project_authoritative_memory_facts((fact_factory(1, value="V1", vector_revision=1),), user_id=100)
    r3 = project_authoritative_memory_facts((fact_factory(1, value="V1", vector_revision=2),), user_id=100)
    assert r1.projection_id != r3.projection_id

def test_46_deletion():
    r1 = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    r2 = project_authoritative_memory_facts((), user_id=100)
    assert len(r1.snapshot.nodes) == 3
    assert len(r2.snapshot.nodes) == 0

def test_47_semantic_dedup_and_aggregation():
    r = project_authoritative_memory_facts((fact_factory(1), fact_factory(2)), user_id=100)
    fact_node = [n for n in r.snapshot.nodes if n.node_type == 'memory:fact'][0]
    fact_edge = [e for e in r.snapshot.edges if e.relation_type == 'memory:has_fact'][0]
    assert len(r.node_supports[fact_node.node_id]) == 2
    assert len(r.edge_supports[fact_edge.edge_id]) == 2

def test_48_projection_id_changes_on_revisions_excluded():
    f3 = fact_factory(2, key="secret", vector_revision=1)
    f4 = fact_factory(2, key="secret", vector_revision=2)
    r3 = project_authoritative_memory_facts((f3,), user_id=100)
    r4 = project_authoritative_memory_facts((f4,), user_id=100)
    assert r3.projection_id != r4.projection_id

def test_49_complete_auth_state_provenance():
    r = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    assert r.snapshot.authoritative_source.is_complete is True
    assert r.snapshot.authoritative_source.facts[0].fact_id == "1"

def test_50_schema_validation_absent():
    conn = sqlite3.connect(":memory:")
    with pytest.raises(sqlite3.DatabaseError):
        read_authoritative_memory_facts(conn, user_id=100)

def test_51_schema_validation_current():
    conn = _setup_db()
    before = conn.total_changes
    read_authoritative_memory_facts(conn, user_id=100)
    assert conn.total_changes == before

def test_52_schema_validation_incompatible():
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {FACTS_TABLE} (user_id INTEGER, invalid_col TEXT)")
    conn.execute("CREATE TABLE memory_os_vector_sync_meta(singleton_id INTEGER, schema_seeded INTEGER)")
    conn.execute("INSERT INTO memory_os_vector_sync_meta(singleton_id, schema_seeded) VALUES (1, 1)")
    with pytest.raises(sqlite3.DatabaseError): # from validate_memory_convergence_schema
        read_authoritative_memory_facts(conn, user_id=100)

def test_53_projection_result_immutability():
    r = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    with pytest.raises(Exception):
        r.node_supports["x"] = "y"

def test_54_snapshot_tamper_verification():
    r = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    snap = r.snapshot
    # Change auth source fact revision but keep snapshot_id
    tampered_auth = AuthoritativeSourceSnapshot((AuthoritativeFactState("1", 99, "ACTIVE"),), True)
    tampered_snap = GraphSnapshot(snap.schema_version, snap.snapshot_id, snap.nodes, snap.edges, tampered_auth)
    with pytest.raises(GraphVerificationError):
        tampered_snap.verify_identity()

def test_55_provenance_classification_current():
    r = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    snap = r.snapshot
    prov = snap.nodes[0].provenance
    assert classify_provenance(prov, snap.authoritative_source) == "CURRENT"

def test_56_privacy_secret_sentinel_leakage():
    sentinel = "VERY_SECRET_SENTINEL_VALUE_123"
    r = project_authoritative_memory_facts((fact_factory(1, key="secret", value=sentinel),), user_id=100)
    res = r.snapshot
    ser = serialize_graph_snapshot(res)
    assert sentinel not in ser
    assert sentinel not in str(r.exclusions)
    for n in res.nodes:
        assert sentinel not in str(n)
    for e in res.edges:
        assert sentinel not in str(e)

def test_57_static_independence():
    import ast
    with open("gateway/memory/graph_projection.py", "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names: imports.append(n.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module: imports.append(node.module)
    for bad in ["qdrant", "telegram", "requests", "aiohttp"]:
        assert not any(bad in imp for imp in imports)

def test_58_runtime_activation_static_check():
    import ast
    with open("gateway/memory/runtime.py", "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    assert "gateway.memory.graph_projection" not in imports

def test_59_snapshot_node_order_canonical():
    f1 = fact_factory(1, entity="E1", key="K1")
    f2 = fact_factory(2, entity="E2", key="K2")
    r1 = project_authoritative_memory_facts((f1, f2), user_id=100)
    r2 = project_authoritative_memory_facts((f2, f1), user_id=100)
    assert r1.snapshot.nodes == r2.snapshot.nodes

def test_60_snapshot_edge_order_canonical():
    f1 = fact_factory(1, entity="E1", key="K1")
    f2 = fact_factory(2, entity="E2", key="K2")
    r1 = project_authoritative_memory_facts((f1, f2), user_id=100)
    r2 = project_authoritative_memory_facts((f2, f1), user_id=100)
    assert r1.snapshot.edges == r2.snapshot.edges

def test_61_snapshot_source_order_canonical():
    f1 = fact_factory(1)
    f2 = fact_factory(2)
    r1 = project_authoritative_memory_facts((f1, f2), user_id=100)
    r2 = project_authoritative_memory_facts((f2, f1), user_id=100)
    assert r1.snapshot.authoritative_source.facts == r2.snapshot.authoritative_source.facts
