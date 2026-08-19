import sqlite3
import pytest
from dataclasses import dataclass

from ai_engineering.graph_contract import GraphSnapshot, AuthoritativeSourceSnapshot
from gateway.memory.graph_store import (
    publish_graph_projection,
    load_graph_projection,
    GraphStoreError,
    validate_memory_graph_store_schema,
)
from gateway.memory.graph_projection import (
    read_authoritative_memory_facts,
    project_authoritative_memory_facts,
    GraphProjectionResult,
)
from gateway.memory.graph_convergence import (
    converge_user_graph,
    inspect_graph_convergence,
    GraphConvergenceState,
    GraphConvergenceStatus,
    GraphConvergenceAssessment,
    GraphConvergenceResult,
    GraphConvergenceIntegrityError,
    GraphConvergenceError,
)

def setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    # Memory OS tables needed
    # (they will be created by migrate_memory_convergence_schema)
    # Graph Store tables
    from gateway.memory.graph_store import migrate_memory_graph_store_schema
    from gateway.memory.schema import migrate_memory_convergence_schema
    migrate_memory_convergence_schema(conn, now=0.0)
    migrate_memory_graph_store_schema(conn)
    validate_memory_graph_store_schema(conn)
    return conn

def insert_mock_fact(conn, user_id, sqlite_id, entity, key, value, vector_revision):
    conn.execute(
        "INSERT INTO memory_os_facts (id, user_id, entity, key, value, vector_revision) VALUES (?, ?, ?, ?, ?, ?)",
        (sqlite_id, user_id, entity, key, value, vector_revision)
    )

def test_contract_validation():
    conn = setup_db()
    with pytest.raises(ValueError):
        converge_user_graph(conn, user_id=True)
    with pytest.raises(ValueError):
        converge_user_graph(conn, user_id="1")
    with pytest.raises(ValueError):
        converge_user_graph(conn, user_id=1, max_attempts=True)
    with pytest.raises(ValueError):
        converge_user_graph(conn, user_id=1, max_attempts=0)
    with pytest.raises(ValueError):
        converge_user_graph(conn, user_id=1, max_attempts=6)
    with pytest.raises(ValueError):
        inspect_graph_convergence(conn, user_id=True)

def test_inspect_missing():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    assessment = inspect_graph_convergence(conn, user_id=1)
    assert assessment.state == GraphConvergenceState.MISSING
    assert assessment.matched_auth_facts_count == 1

def test_inspect_current_and_stale():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    converge_user_graph(conn, user_id=1)
    
    assessment = inspect_graph_convergence(conn, user_id=1)
    assert assessment.state == GraphConvergenceState.CURRENT
    
    insert_mock_fact(conn, 1, 2, "e2", "k2", "v2", 1)
    assessment2 = inspect_graph_convergence(conn, user_id=1)
    assert assessment2.state == GraphConvergenceState.STALE

def test_corruption_hard_fail():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    converge_user_graph(conn, user_id=1)
    # Corrupt the JSON
    conn.execute("UPDATE memory_graph_user_state SET canonical_snapshot_json = '{bad json}'")
    with pytest.raises(GraphConvergenceIntegrityError):
        inspect_graph_convergence(conn, user_id=1)
    with pytest.raises(GraphConvergenceIntegrityError):
        converge_user_graph(conn, user_id=1)

def test_incomplete_source():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    converge_user_graph(conn, user_id=1)
    
    # Tamper the persisted json to make it incomplete
    json_str = conn.execute("SELECT canonical_snapshot_json FROM memory_graph_user_state WHERE user_id = 1").fetchone()[0]
    json_str = json_str.replace('"is_complete":true', '"is_complete":false')
    conn.execute("UPDATE memory_graph_user_state SET canonical_snapshot_json = ?", (json_str,))
    
    with pytest.raises(GraphConvergenceIntegrityError):
        inspect_graph_convergence(conn, user_id=1)

@pytest.mark.parametrize("sqlite_ids", [
    list(range(1, 13)), # 1..12
    [2, 10, 11] # sparse
])
def test_ids_combinations(sqlite_ids):
    conn = setup_db()
    for sid in sqlite_ids:
        insert_mock_fact(conn, 1, sid, f"e{sid}", "k", "v", 1)
    res = converge_user_graph(conn, user_id=1)
    assert res.status == GraphConvergenceStatus.REBUILT_MISSING
    assert res.final_state == GraphConvergenceState.CURRENT
    
    # zero-write second convergence
    initial_changes = conn.total_changes
    res2 = converge_user_graph(conn, user_id=1)
    assert res2.status == GraphConvergenceStatus.NOOP_CURRENT
    assert res2.publish_count == 0
    assert conn.total_changes == initial_changes

def test_stale_rebuild():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    converge_user_graph(conn, user_id=1)
    
    insert_mock_fact(conn, 1, 2, "e2", "k2", "v2", 1)
    res = converge_user_graph(conn, user_id=1)
    assert res.status == GraphConvergenceStatus.REBUILT_STALE
    assert res.initial_state == GraphConvergenceState.STALE
    assert res.final_state == GraphConvergenceState.CURRENT

def test_empty_scope():
    conn = setup_db()
    res = converge_user_graph(conn, user_id=1)
    assert res.status == GraphConvergenceStatus.REBUILT_MISSING
    assessment = inspect_graph_convergence(conn, user_id=1)
    assert assessment.state == GraphConvergenceState.CURRENT

def test_all_excluded_scope():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "api_key", "v", 1)
    res = converge_user_graph(conn, user_id=1)
    assert res.status == GraphConvergenceStatus.REBUILT_MISSING
    assessment = inspect_graph_convergence(conn, user_id=1)
    assert assessment.state == GraphConvergenceState.CURRENT

def test_pre_publish_race_writes(monkeypatch):
    # Mock project_authoritative_memory_facts to simulate a write DURING projection
    # so that source_b != source_a, causing a retry.
    import gateway.memory.graph_convergence
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    
    original_project = gateway.memory.graph_convergence.project_authoritative_memory_facts
    race_injected = False
    
    def fake_project(facts, user_id):
        nonlocal race_injected
        if not race_injected:
            insert_mock_fact(conn, user_id, 2, "e", "k", "v", 1)
            race_injected = True
        return original_project(facts, user_id=user_id)
        
    monkeypatch.setattr(gateway.memory.graph_convergence, "project_authoritative_memory_facts", fake_project)
    
    res = converge_user_graph(conn, user_id=1)
    assert res.status == GraphConvergenceStatus.REBUILT_MISSING
    assert res.attempts == 2
    assert res.publish_count == 1
    assert race_injected

def test_source_change_during_projection_publishes_zero(monkeypatch):
    import gateway.memory.graph_convergence
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    original_project = gateway.memory.graph_convergence.project_authoritative_memory_facts
    
    def fake_project(facts, user_id):
        insert_mock_fact(conn, user_id, 2, "e", "k", "v", 1)
        return original_project(facts, user_id=user_id)
        
    monkeypatch.setattr(gateway.memory.graph_convergence, "project_authoritative_memory_facts", fake_project)
    
    res = converge_user_graph(conn, user_id=1, max_attempts=1)
    assert res.status == GraphConvergenceStatus.SOURCE_CHURN_RETRY_EXHAUSTED
    assert res.attempts == 1
    assert res.publish_count == 0

def test_post_publish_race(monkeypatch):
    # Simulate a write after publish but before load
    import gateway.memory.graph_convergence
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    
    original_publish = gateway.memory.graph_convergence.publish_graph_projection
    race_injected = False
    
    def fake_publish(c, candidate):
        original_publish(c, candidate)
        nonlocal race_injected
        if not race_injected:
            insert_mock_fact(conn, 1, 2, "e", "k", "v", 1)
            race_injected = True
            
    monkeypatch.setattr(gateway.memory.graph_convergence, "publish_graph_projection", fake_publish)
    
    res = converge_user_graph(conn, user_id=1)
    assert res.status == GraphConvergenceStatus.REBUILT_MISSING
    assert res.attempts == 2
    assert res.publish_count == 2
    assert race_injected

def test_repeated_post_publish_race(monkeypatch):
    import gateway.memory.graph_convergence
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    
    original_publish = gateway.memory.graph_convergence.publish_graph_projection
    call_count = 0
    def fake_publish(c, candidate):
        original_publish(c, candidate)
        nonlocal call_count
        call_count += 1
        insert_mock_fact(conn, 1, 100 + call_count, "e", "k", "v", 1)
            
    monkeypatch.setattr(gateway.memory.graph_convergence, "publish_graph_projection", fake_publish)
    
    res = converge_user_graph(conn, user_id=1, max_attempts=3)
    assert res.status == GraphConvergenceStatus.SOURCE_CHURN_RETRY_EXHAUSTED
    assert res.attempts == 3
    assert res.publish_count == 3
    assert res.final_state == GraphConvergenceState.STALE

def test_max_attempts_limits(monkeypatch):
    # Test max_attempts=1 and max_attempts=5
    import gateway.memory.graph_convergence
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    
    def fake_project(facts, user_id):
        insert_mock_fact(conn, user_id, facts[-1].sqlite_id + 1, "e", "k", "v", 1)
        return gateway.memory.graph_projection.project_authoritative_memory_facts(facts, user_id=user_id)
        
    monkeypatch.setattr(gateway.memory.graph_convergence, "project_authoritative_memory_facts", fake_project)
    
    res1 = converge_user_graph(conn, user_id=1, max_attempts=1)
    assert res1.attempts == 1
    
    res5 = converge_user_graph(conn, user_id=1, max_attempts=5)
    assert res5.attempts == 5

def test_caller_transaction_rollback():
    conn = setup_db()
    conn.commit()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    converge_user_graph(conn, user_id=1)
    conn.rollback()
    
    assessment = inspect_graph_convergence(conn, user_id=1)
    assert assessment.state == GraphConvergenceState.MISSING
    assert assessment.matched_auth_facts_count == 0

def test_cross_user_isolation():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    insert_mock_fact(conn, 2, 2, "e", "k", "v", 1)
    converge_user_graph(conn, user_id=1)
    converge_user_graph(conn, user_id=2)
    
    # Snapshot user2
    u2_proj1 = load_graph_projection(conn, user_id=2)
    
    # Change user2
    insert_mock_fact(conn, 2, 3, "e", "k", "v", 1)
    
    # user1 must remain CURRENT
    assert inspect_graph_convergence(conn, user_id=1).state == GraphConvergenceState.CURRENT
    
    # user2 must be STALE
    assert inspect_graph_convergence(conn, user_id=2).state == GraphConvergenceState.STALE
    
    # Snapshot unchanged
    u2_proj2 = load_graph_projection(conn, user_id=2)
    assert u2_proj1 == u2_proj2

def test_authoritative_immutability():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    conn.execute("INSERT INTO memory_os_vector_sync_outbox (user_id, fact_id, operation, fact_revision, state, next_attempt_at, created_at, updated_at) VALUES (1, 1, 'UPSERT', 1, 'PENDING', 0, 0, 0)")
    
    def count(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        
    facts_count = count("memory_os_facts")
    outbox_count = count("memory_os_vector_sync_outbox")
    meta_count = count("memory_os_vector_sync_meta")
    
    converge_user_graph(conn, user_id=1)
    
    assert count("memory_os_facts") == facts_count
    assert count("memory_os_vector_sync_outbox") == outbox_count
    assert count("memory_os_vector_sync_meta") == meta_count

def test_secret_sanitization():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "api_key", "PR52_SECRET_SENTINEL_314159", 1)
    res = converge_user_graph(conn, user_id=1)
    
    # Ensure secret is NOT in result repr
    assert "PR52_SECRET_SENTINEL_314159" not in repr(res)
    assert "PR52_SECRET_SENTINEL_314159" not in str(res)
    
    # Check projection exclusions
    proj = load_graph_projection(conn, user_id=1)
    assert len(proj.exclusions) == 1

def test_read_path_no_auto_rebuild():
    from gateway.memory.graph_query import read_graph_context, GraphFactQuery, GraphContextStatus
    conn = setup_db()
    
    # MISSING graph -> read_graph_context -> MISSING_GRAPH, no writes
    q = GraphFactQuery()
    changes1 = conn.total_changes
    res1 = read_graph_context(conn, user_id=1, query=q)
    assert res1.status == GraphContextStatus.MISSING_GRAPH
    assert conn.total_changes == changes1
    
    # STALE graph -> read_graph_context -> STALE_GRAPH, no writes
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    converge_user_graph(conn, user_id=1)
    insert_mock_fact(conn, 1, 2, "e", "k", "v", 1)
    changes2 = conn.total_changes
    res2 = read_graph_context(conn, user_id=1, query=q)
    assert res2.status == GraphContextStatus.STALE_GRAPH
    assert conn.total_changes == changes2

def test_deterministic_result_ids():
    conn = setup_db()
    insert_mock_fact(conn, 1, 1, "e", "k", "v", 1)
    res1 = converge_user_graph(conn, user_id=1)
    
    # Drop projection and re-converge with identical state
    conn.execute("DELETE FROM memory_graph_user_state")
    
    res2 = converge_user_graph(conn, user_id=1)
    assert res1.projection_id == res2.projection_id
    assert res1.snapshot_id == res2.snapshot_id
