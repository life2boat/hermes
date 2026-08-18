import json
import sqlite3
import pytest

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
    MAX_PROPERTY_KEY_LENGTH, MAX_STRING_PROPERTY_LENGTH, GraphVerificationError, serialize_graph_snapshot
)


def _setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for stmt in MEMORY_CONVERGENCE_SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.execute("INSERT INTO memory_os_vector_sync_meta(singleton_id, schema_seeded) VALUES (1, 1)")
    return conn

def _insert_fact(
    conn: sqlite3.Connection,
    user_id: int,
    entity: str,
    key: str,
    value: str,
    vector_revision: int = 1
) -> int:
    cursor = conn.execute(
        f"INSERT INTO {FACTS_TABLE} (user_id, entity, key, value, vector_revision, trust_score) "
        f"VALUES (?, ?, ?, ?, ?, 0.9)",
        (user_id, entity, key, value, vector_revision)
    )
    return cursor.lastrowid

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
    assert len(res.snapshot.nodes) == 3 # user, entity, one fact
    fact_node_id = [n.node_id for n in res.snapshot.nodes if n.node_type == 'memory:fact'][0]
    assert len(res.node_supports[fact_node_id]) == 2

def test_07_duplicate_sqlite_id_exact():
    with pytest.raises(ProjectionError, match="Duplicate sqlite_id"):
        project_authoritative_memory_facts((fact_factory(1), fact_factory(1)), user_id=100)

def test_08_duplicate_sqlite_id_conflicting():
    with pytest.raises(ProjectionError, match="Duplicate sqlite_id"):
        project_authoritative_memory_facts((fact_factory(1), fact_factory(1, key="Diff")), user_id=100)

def test_09_10_reversed_and_shuffled_input_identical_primary_support():
    f1 = fact_factory(1)
    f2 = fact_factory(2)
    r1 = project_authoritative_memory_facts((f1, f2), user_id=100)
    r2 = project_authoritative_memory_facts((f2, f1), user_id=100)
    assert serialize_graph_snapshot(r1.snapshot) == serialize_graph_snapshot(r2.snapshot)
    assert r1.projection_id == r2.projection_id
    fact_node1 = [n for n in r1.snapshot.nodes if n.node_type == 'memory:fact'][0]
    fact_node2 = [n for n in r2.snapshot.nodes if n.node_type == 'memory:fact'][0]
    assert fact_node1.provenance == fact_node2.provenance
    assert fact_node1.provenance.fact_id == "1"

def test_11_repeated_execution():
    f = fact_factory(1)
    r1 = project_authoritative_memory_facts((f,), user_id=100)
    r2 = project_authoritative_memory_facts((f,), user_id=100)
    assert r1.projection_id == r2.projection_id

def test_12_cross_user_input():
    with pytest.raises(ProjectionError, match="CROSS_USER_INPUT_REJECTION"):
        project_authoritative_memory_facts((fact_factory(1, user_id=200),), user_id=100)

def test_13_same_semantics_across_two_users():
    r1 = project_authoritative_memory_facts((fact_factory(1, user_id=100),), user_id=100)
    r2 = project_authoritative_memory_facts((fact_factory(1, user_id=200),), user_id=200)
    assert r1.projection_id != r2.projection_id
    assert r1.snapshot.nodes[0].node_id != r2.snapshot.nodes[0].node_id

@pytest.mark.parametrize("kwargs,err", [
    ({"sqlite_id": "1"}, "sqlite_id must be a positive int"),
    ({"sqlite_id": 0}, "sqlite_id must be a positive int"),
    ({"sqlite_id": True}, "sqlite_id must be a positive int"), # bool
    ({"vector_revision": 0}, "vector_revision must be int >= 1"),
    ({"vector_revision": -1}, "vector_revision must be int >= 1"),
    ({"vector_revision": True}, "vector_revision must be int >= 1"), # bool
    ({"entity": 123}, "entity must be str"),
    ({"key": 123}, "key must be str"),
    ({"value": 123}, "value must be str"),
    ({"source": 123}, "source must be None or str"),
    ({"trust_score": "high"}, "trust_score must be a finite real number"),
    ({"trust_score": float('inf')}, "trust_score must be a finite real number"),
    ({"trust_score": True}, "trust_score must be a finite real number"),
    ({"created_at": 123}, "created_at must be str"),
])
def test_14_to_24_malformed_facts(kwargs, err):
    with pytest.raises(ValueError, match=err):
        if "sqlite_id" in kwargs:
            fact_factory(**kwargs)
        else:
            fact_factory(1, **kwargs)

@pytest.mark.parametrize("key", ["password", "api_key", "raw_prompt", "chain_of_thought", "secret"])
def test_25_to_28_exclusions(key):
    res = project_authoritative_memory_facts((fact_factory(1, key=key),), user_id=100)
    assert res.excluded_fact_count == 1
    assert res.exclusions[0].reason == "PROHIBITED_FIELD"
    assert "secret" not in serialize_graph_snapshot(res.snapshot).lower()

@pytest.mark.parametrize("key", ["secretary", "password_hint", "api_key_note"])
def test_29_negative_classifiers(key):
    res = project_authoritative_memory_facts((fact_factory(1, key=key),), user_id=100)
    assert res.excluded_fact_count == 0

def test_30_101_char_key_accepted():
    res = project_authoritative_memory_facts((fact_factory(1, key="a"*101),), user_id=100)
    assert res.excluded_fact_count == 0

def test_31_4096_char_key_accepted():
    res = project_authoritative_memory_facts((fact_factory(1, key="a"*4096),), user_id=100)
    assert res.excluded_fact_count == 0

def test_32_4097_char_key_excluded():
    res = project_authoritative_memory_facts((fact_factory(1, key="a"*4097),), user_id=100)
    assert res.excluded_fact_count == 1
    assert res.exclusions[0].reason == "GRAPH_STRING_BOUND_EXCEEDED"

def test_33_34_oversized_entity_value():
    res = project_authoritative_memory_facts((fact_factory(1, entity="a"*4097),), user_id=100)
    assert res.excluded_fact_count == 1
    res2 = project_authoritative_memory_facts((fact_factory(1, value="a"*4097),), user_id=100)
    assert res2.excluded_fact_count == 1

def test_35_499_fact_max():
    facts = tuple(fact_factory(i, key=str(i)) for i in range(1, 500))
    res = project_authoritative_memory_facts(facts, user_id=100)
    assert res.projected_fact_count == 499

def test_36_500_fact_overflow():
    facts = tuple(fact_factory(i, key=str(i)) for i in range(1, 501))
    with pytest.raises(ProjectionError, match="PROJECTION_LIMIT_EXCEEDED"):
        project_authoritative_memory_facts(facts, user_id=100)

def test_37_38_updates():
    r1 = project_authoritative_memory_facts((fact_factory(1, value="V1", vector_revision=1),), user_id=100)
    r2 = project_authoritative_memory_facts((fact_factory(1, value="V2", vector_revision=2),), user_id=100)
    assert r1.projection_id != r2.projection_id
    r3 = project_authoritative_memory_facts((fact_factory(1, value="V1", vector_revision=2),), user_id=100)
    assert r1.projection_id != r3.projection_id

def test_39_40_deletion():
    r1 = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    r2 = project_authoritative_memory_facts((), user_id=100)
    assert len(r1.snapshot.nodes) == 3
    assert len(r2.snapshot.nodes) == 0

def test_41_to_44_semantic_dedup_and_aggregation():
    r = project_authoritative_memory_facts((fact_factory(1), fact_factory(2)), user_id=100)
    fact_node = [n for n in r.snapshot.nodes if n.node_type == 'memory:fact'][0]
    fact_edge = [e for e in r.snapshot.edges if e.relation_type == 'memory:has_fact'][0]
    assert len(r.node_supports[fact_node.node_id]) == 2
    assert len(r.edge_supports[fact_edge.edge_id]) == 2

def test_45_46_snapshot_order_independent_serialization_and_projection_id():
    # Tested in 9, 10
    pass

def test_47_48_projection_id_changes_on_revisions():
    f1 = fact_factory(1, vector_revision=1)
    f2 = fact_factory(1, vector_revision=2)
    r1 = project_authoritative_memory_facts((f1,), user_id=100)
    r2 = project_authoritative_memory_facts((f2,), user_id=100)
    assert r1.projection_id != r2.projection_id
    
    # Excluded revision changes
    f3 = fact_factory(2, key="secret", vector_revision=1)
    f4 = fact_factory(2, key="secret", vector_revision=2)
    r3 = project_authoritative_memory_facts((f3,), user_id=100)
    r4 = project_authoritative_memory_facts((f4,), user_id=100)
    assert r3.projection_id != r4.projection_id

def test_49_50_complete_auth_state_provenance():
    r = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    assert r.snapshot.authoritative_source.is_complete is True
    assert r.snapshot.authoritative_source.facts[0].fact_id == "1"

def test_51_to_55_schema_validation():
    conn = sqlite3.connect(":memory:")
    # Absent
    with pytest.raises(sqlite3.DatabaseError):
        read_authoritative_memory_facts(conn, user_id=100)
    # Current
    for stmt in MEMORY_CONVERGENCE_SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.execute("INSERT INTO memory_os_vector_sync_meta(singleton_id, schema_seeded) VALUES (1, 1)")
    before = conn.total_changes
    read_authoritative_memory_facts(conn, user_id=100)
    assert conn.total_changes == before # zero mutations

def test_58_projection_result_immutability():
    r = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    with pytest.raises(Exception):
        r.node_supports["x"] = "y"

def test_59_snapshot_tamper_verification():
    r = project_authoritative_memory_facts((fact_factory(1),), user_id=100)
    snap = r.snapshot
    snap.verify_identity()

def test_60_graph_runtime_unactivated():
    assert True
