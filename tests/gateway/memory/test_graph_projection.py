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
    MAX_PROJECTION_FACTS
)
from ai_engineering.graph_contract import (
    MAX_PROPERTY_KEY_LENGTH, MAX_STRING_PROPERTY_LENGTH, GraphVerificationError
)


def _setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    for stmt in MEMORY_CONVERGENCE_SCHEMA_STATEMENTS:
        conn.execute(stmt)
    # Insert meta singleton
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


def test_read_authoritative_memory_facts() -> None:
    conn = _setup_db()

    id1 = _insert_fact(conn, 100, "Alice", "likes", "apples", 1)
    id2 = _insert_fact(conn, 100, "Alice", "hates", "bananas", 2)
    id3 = _insert_fact(conn, 200, "Bob", "likes", "oranges", 1)  # different user

    facts = read_authoritative_memory_facts(conn, user_id=100)
    assert len(facts) == 2

    assert facts[0].sqlite_id == id1
    assert facts[0].user_id == 100
    assert facts[0].entity == "Alice"
    assert facts[0].key == "likes"
    assert facts[0].value == "apples"
    assert facts[0].vector_revision == 1

    assert facts[1].sqlite_id == id2
    assert facts[1].user_id == 100
    assert facts[1].entity == "Alice"
    assert facts[1].key == "hates"
    assert facts[1].value == "bananas"
    assert facts[1].vector_revision == 2


def test_project_authoritative_memory_facts_success() -> None:
    conn = _setup_db()

    id1 = _insert_fact(conn, 100, "Alice", "likes", "apples", 1)
    id2 = _insert_fact(conn, 100, "Alice", "hates", "bananas", 2)

    facts = read_authoritative_memory_facts(conn, user_id=100)

    result = project_authoritative_memory_facts(facts, user_id=100)

    assert result.user_id == 100
    assert result.input_fact_count == 2
    assert result.projected_fact_count == 2
    assert result.excluded_fact_count == 0
    assert not result.exclusions

    snap = result.snapshot
    # Check identity signature
    snap.verify_identity()

    # 1 user node, 1 entity node ("Alice"), 2 fact nodes
    assert len(snap.nodes) == 4

    node_types = [n.node_type for n in snap.nodes]
    assert node_types.count("memory:user") == 1
    assert node_types.count("memory:entity") == 1
    assert node_types.count("memory:fact") == 2

    # Check edges: 1 user->entity, 2 entity->fact
    assert len(snap.edges) == 3
    edge_types = [e.relation_type for e in snap.edges]
    assert edge_types.count("memory:has_entity") == 1
    assert edge_types.count("memory:has_fact") == 2


def test_project_cross_user_rejection() -> None:
    conn = _setup_db()
    _insert_fact(conn, 100, "Alice", "likes", "apples")
    _insert_fact(conn, 200, "Bob", "likes", "oranges")

    # manually read facts across users (simulate bug in read phase)
    rows = conn.execute(f"SELECT id, user_id, entity, key, value, vector_revision, source, trust_score, created_at, updated_at FROM {FACTS_TABLE}").fetchall()
    facts = [
        AuthoritativeMemoryFact(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8], row[9])
        for row in rows
    ]

    with pytest.raises(ProjectionError, match="CROSS_USER_INPUT_REJECTION"):
        project_authoritative_memory_facts(tuple(facts), user_id=100)


def test_project_limit_exceeded() -> None:
    # 500 facts (exceeds 499)
    facts = []
    for i in range(MAX_PROJECTION_FACTS + 1):
        facts.append(
            AuthoritativeMemoryFact(
                sqlite_id=i+1,
                user_id=100,
                entity="Alice",
                key=f"key_{i}",
                value="val",
                vector_revision=1,
                source=None,
                trust_score=0.9,
                created_at="2026",
                updated_at="2026"
            )
        )

    with pytest.raises(ProjectionError, match="PROJECTION_LIMIT_EXCEEDED"):
        project_authoritative_memory_facts(tuple(facts), user_id=100)


def test_privacy_exclusions() -> None:
    conn = _setup_db()
    _insert_fact(conn, 100, "Alice", "password", "12345") # secret rejected
    _insert_fact(conn, 100, "Alice", "x" * 101, "val") # key too long
    _insert_fact(conn, 100, "Alice", "key", "v" * 4097) # value too long
    _insert_fact(conn, 100, "x" * 4097, "key", "val") # entity too long

    facts = read_authoritative_memory_facts(conn, user_id=100)
    result = project_authoritative_memory_facts(facts, user_id=100)

    assert result.input_fact_count == 4
    assert result.projected_fact_count == 0
    assert result.excluded_fact_count == 4
    assert len(result.exclusions) == 4

    reasons = [e.reason for e in result.exclusions]
    assert "PROHIBITED_FIELD" in reasons
    assert reasons.count("GRAPH_STRING_BOUND_EXCEEDED") == 3


def test_projection_id_stability() -> None:
    conn = _setup_db()
    _insert_fact(conn, 100, "Alice", "likes", "apples", 1)
    facts = read_authoritative_memory_facts(conn, user_id=100)

    r1 = project_authoritative_memory_facts(facts, user_id=100)
    r2 = project_authoritative_memory_facts(facts, user_id=100)

    assert r1.projection_id == r2.projection_id
    assert r1.snapshot.snapshot_id == r2.snapshot.snapshot_id
