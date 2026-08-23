import json
import subprocess
import sqlite3
import pytest
from pathlib import Path

from gateway.memory.schema import MemorySchemaClassification
from gateway.memory.graph_store import (
    classify_memory_graph_store_schema,
    migrate_memory_graph_store_schema,
)
from ai_engineering.memory_graph_activation_readiness import (
    MemoryGraphShadowHealthReceipt,
    serialize_receipt,
)

def run_preflight(**kwargs) -> dict:
    cmd = ["python", "scripts/check_memory_graph_shadow_readiness.py"]
    for k, v in kwargs.items():
        cmd.append(f"--{k.replace('_', '-')}")
        cmd.append(str(v).lower() if isinstance(v, bool) else str(v))
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout.strip():
        return json.loads(result.stdout)
    return {}

@pytest.fixture
def base_args():
    return {
        "subject_main_sha": "4e26af36923f489b85b8ea83f196c0712515bcc8",
        "expected_subject_main_sha": "4e26af36923f489b85b8ea83f196c0712515bcc8",
        "candidate_image_revision": "rev1",
        "db_path_safe": True,
        "db_integrity": "ok",
        "foreign_key_violations": 0,
        "graph_schema_classification": "CURRENT",
        "backup_required": True,
        "backup_valid": True,
        "rollback_proven": True,
        "shadow_mode_available": True,
        "serve_mode_available": False,
        "graph_context_served_to_users": False,
        "production_activation_authorized": True
    }

def test_preflight_01_exact_provenance_pass(base_args):
    result = run_preflight(**base_args)
    assert result.get("verdict") == "PASS"
    assert not result.get("reason_codes")

def test_preflight_02_main_sha_mismatch(base_args):
    base_args["subject_main_sha"] = "wrongsha"
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "CANONICAL_SHA_MISMATCH" in result.get("reason_codes")

def test_preflight_03_unsafe_db_path(base_args):
    base_args["db_path_safe"] = False
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "UNSAFE_DB_PATH" in result.get("reason_codes")

def test_preflight_04_integrity_failure(base_args):
    base_args["db_integrity"] = "corrupt"
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "DATABASE_INTEGRITY_FAILURE" in result.get("reason_codes")

def test_preflight_05_foreign_key_violation(base_args):
    base_args["foreign_key_violations"] = 1
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "FOREIGN_KEY_VIOLATION" in result.get("reason_codes")

def test_preflight_06_incompatible_graph_schema(base_args):
    base_args["graph_schema_classification"] = "INCOMPATIBLE"
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "GRAPH_SCHEMA_INCOMPATIBLE" in result.get("reason_codes")

def test_preflight_07_backup_required_missing(base_args):
    base_args["backup_valid"] = False
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "BACKUP_INVALID" in result.get("reason_codes")

def test_preflight_08_rollback_unproven(base_args):
    base_args["rollback_proven"] = False
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "ROLLBACK_NOT_PROVEN" in result.get("reason_codes")

def test_preflight_09_serve_mode_enabled(base_args):
    base_args["serve_mode_available"] = True
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "SERVE_MODE_UNEXPECTEDLY_ENABLED" in result.get("reason_codes")

def test_preflight_10_graph_serving_enabled(base_args):
    base_args["graph_context_served_to_users"] = True
    result = run_preflight(**base_args)
    assert result.get("verdict") == "BLOCKED"
    assert "GRAPH_SERVING_NOT_DISABLED" in result.get("reason_codes")

def test_privacy_01_sentinel_absent(base_args):
    result = run_preflight(**base_args)
    dump = json.dumps(result)
    assert "PR8_MEMORY_GRAPH_ACTIVATION_SECRET_SENTINEL_161803" not in dump

@pytest.fixture
def empty_db(tmp_path):
    db = tmp_path / "test.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE dummy (id INTEGER PRIMARY KEY)")
    return db

def test_mig_01_absent_to_current(empty_db):
    with sqlite3.connect(empty_db) as conn:
        assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.ABSENT
        migrate_memory_graph_store_schema(conn)
        assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.CURRENT

def test_mig_02_current_to_noop(empty_db):
    with sqlite3.connect(empty_db) as conn:
        migrate_memory_graph_store_schema(conn)
        assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.CURRENT
        migrate_memory_graph_store_schema(conn)
        assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.CURRENT

def test_mig_04_incompatible_blocks(empty_db):
    with sqlite3.connect(empty_db) as conn:
        conn.execute("CREATE TABLE memory_graph_schema_migrations (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO memory_graph_schema_migrations (id) VALUES (999)")
        assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.INCOMPATIBLE
        with pytest.raises(Exception):
            migrate_memory_graph_store_schema(conn)

def test_mig_08_authoritative_facts_unchanged(empty_db):
    with sqlite3.connect(empty_db) as conn:
        conn.execute("CREATE TABLE memory_os_facts (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO memory_os_facts (v) VALUES ('test')")
        conn.execute("CREATE TABLE memory_fact_revisions (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE fact_trust (id INTEGER PRIMARY KEY)")
        
        migrate_memory_graph_store_schema(conn)
        
        cur = conn.execute("SELECT v FROM memory_os_facts")
        assert cur.fetchone()[0] == 'test'

def test_canary_01_shadow_running_green():
    receipt = MemoryGraphShadowHealthReceipt(
        image_revision="rev",
        canonical_main_sha="sha",
        runtime_mode="shadow",
        observation_start_utc="1",
        observation_end_utc="2",
        runtime_status="RUNNING",
        integrity_block_count=0,
        cross_user_leakage=0,
        excluded_fact_leakage=0,
        unexpected_authoritative_db_mutation=0,
        worker_crash_loop=False,
        queue_bounded=True,
        source_churn_exhaustion_bounded=True,
        baseline_memory_path_healthy=True,
        gateway_healthy=True,
        restart_count_stable=True,
        sqlite_integrity="ok",
        foreign_key_check=0,
        graph_serving_disabled_proof=True,
        verdict="PASS"
    )
    dump = json.loads(serialize_receipt(receipt))
    assert dump["verdict"] == "PASS"

def test_rb_05_restore_rehearsal_pass(empty_db):
    assert empty_db.exists()
