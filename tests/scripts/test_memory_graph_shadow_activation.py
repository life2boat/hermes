import json
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch
import subprocess
import sys

from ai_engineering.memory_graph_activation_readiness import (
    check_activation_readiness,
    evaluate_shadow_health,
    GraphSchemaClassification,
    MemoryGraphActivationError,
    PreflightVerdict,
)
from gateway.memory.schema import MemorySchemaClassification
from gateway.memory.graph_store import (
    classify_memory_graph_store_schema,
    migrate_memory_graph_store_schema,
)
from scripts.hermes_staged_schema_migrate import (
    _sha256,
    OrchestratorError,
    _no_symlink_chain,
)

def mock_run_target_migration(contract, staging_dir: Path):
    db_path = staging_dir / "database.sqlite"
    with sqlite3.connect(db_path) as conn:
        migrate_memory_graph_store_schema(conn)

@pytest.fixture
def empty_db(tmp_path):
    db = tmp_path / "test.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE memory_os_facts (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO memory_os_facts (v) VALUES ('authoritative')")
    return db

@pytest.fixture
def staged_args(tmp_path, empty_db):
    class Args:
        db_path = str(empty_db)
        backup_dir = str(tmp_path / "backup")
        staging_root = str(tmp_path / "staging")
        target_image_id = "test-image"

    Path(Args.backup_dir).mkdir(parents=True)
    Path(Args.staging_root).mkdir(parents=True)
    return Args()

def test_mig_01_absent_to_current(empty_db, staged_args):
    with patch("scripts.hermes_staged_schema_migrate._run_target_migration", side_effect=mock_run_target_migration):
        original_sha = _sha256(empty_db)

        with sqlite3.connect(empty_db) as conn:
            authoritative_before = conn.execute("SELECT * FROM memory_os_facts").fetchall()
            assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.ABSENT

        staging_copy = Path(staged_args.staging_root) / "staging.sqlite"
        staging_copy.write_bytes(empty_db.read_bytes())

        with sqlite3.connect(staging_copy) as staging_conn:
            migrate_memory_graph_store_schema(staging_conn)
            assert classify_memory_graph_store_schema(staging_conn) == MemorySchemaClassification.CURRENT
            authoritative_after = staging_conn.execute("SELECT * FROM memory_os_facts").fetchall()

        assert authoritative_before == authoritative_after
        assert _sha256(empty_db) == original_sha

def test_mig_04_incompatible_blocks(empty_db, staged_args):
    with sqlite3.connect(empty_db) as conn:
        conn.execute("CREATE TABLE memory_graph_schema_migrations (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO memory_graph_schema_migrations (id) VALUES (999)")
        assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.INCOMPATIBLE

    original_sha = _sha256(empty_db)
    staging_copy = Path(staged_args.staging_root) / "staging.sqlite"
    staging_copy.write_bytes(empty_db.read_bytes())

    with sqlite3.connect(staging_copy) as staging_conn:
        with pytest.raises(Exception):
            migrate_memory_graph_store_schema(staging_conn)

    assert _sha256(empty_db) == original_sha

def test_preflight_blocks_invalid_sha():
    with pytest.raises(MemoryGraphActivationError, match="INVALID_SHA_FORMAT"):
        check_activation_readiness(
            subject_main_sha="invalid",
            expected_subject_main_sha="invalid",
            candidate_image_revision="rev1",
            expected_candidate_image_revision="rev1",
            db_path_safe=True,
            db_integrity="ok",
            foreign_key_violations=0,
            graph_schema_classification="CURRENT",
            backup_required=True,
            backup_valid=True,
            rollback_proven=True,
            shadow_mode_available=True,
            serve_mode_available=False,
            graph_context_served_to_users=False,
            production_activation_authorized=True,
        )

def test_preflight_blocks_mismatch():
    result = check_activation_readiness(
        subject_main_sha="a"*40,
        expected_subject_main_sha="b"*40,
        candidate_image_revision="rev1",
        expected_candidate_image_revision="rev2",
        db_path_safe=True,
        db_integrity="ok",
        foreign_key_violations=0,
        graph_schema_classification="CURRENT",
        backup_required=True,
        backup_valid=True,
        rollback_proven=True,
        shadow_mode_available=True,
        serve_mode_available=False,
        graph_context_served_to_users=False,
        production_activation_authorized=True,
    )
    assert result.verdict == "BLOCKED"
    assert "CANONICAL_SHA_MISMATCH" in result.reason_codes
    assert "IMAGE_REVISION_MISMATCH" in result.reason_codes

@pytest.fixture
def base_health_kwargs():
    return dict(
        image_revision="rev1",
        canonical_main_sha="a"*40,
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
    )

def test_canary_evaluator_pass(base_health_kwargs):
    receipt = evaluate_shadow_health(**base_health_kwargs)
    assert receipt.verdict == "PASS"

def test_canary_evaluator_fail_matrix(base_health_kwargs):
    def check_fail(updates):
        kwargs = base_health_kwargs.copy()
        kwargs.update(updates)
        assert evaluate_shadow_health(**kwargs).verdict == "FAIL"

    check_fail({"runtime_mode": "serve"})
    check_fail({"runtime_status": "STOPPED"})
    check_fail({"integrity_block_count": 1})
    check_fail({"cross_user_leakage": 1})
    check_fail({"excluded_fact_leakage": 1})
    check_fail({"unexpected_authoritative_db_mutation": 1})
    check_fail({"worker_crash_loop": True})
    check_fail({"queue_bounded": False})
    check_fail({"source_churn_exhaustion_bounded": False})
    check_fail({"baseline_memory_path_healthy": False})
    check_fail({"gateway_healthy": False})
    check_fail({"restart_count_stable": False})
    check_fail({"sqlite_integrity": "corrupted"})
    check_fail({"foreign_key_check": 1})
    check_fail({"graph_serving_disabled_proof": False})

def test_privacy_01_sentinel_absent(base_health_kwargs):
    sentinel = "PR8_MEMORY_GRAPH_ACTIVATION_SECRET_SENTINEL_161803"
    base_health_kwargs["canonical_main_sha"] = "invalid-sha-len"
    try:
        evaluate_shadow_health(**base_health_kwargs)
    except MemoryGraphActivationError as exc:
        assert sentinel not in str(exc)
        assert sentinel not in exc.code

    cmd = [
        sys.executable, "scripts/check_memory_graph_shadow_readiness.py",
        "--subject-main-sha", "bad",
        "--expected-subject-main-sha", "bad",
        "--candidate-image-revision", "rev",
        "--expected-candidate-image-revision", "rev",
        "--db-path-safe", "invalid-bool",
        "--db-integrity", "ok",
        "--foreign-key-violations", "0",
        "--graph-schema-classification", "CURRENT",
        "--backup-required", "true",
        "--backup-valid", "true",
        "--rollback-proven", "true",
        "--shadow-mode-available", "true",
        "--serve-mode-available", "false",
        "--graph-context-served-to-users", "false",
        "--production-activation-authorized", "true"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert sentinel not in result.stderr
    assert sentinel not in result.stdout

def test_rollback_rehearsal(empty_db, tmp_path):
    backup_db = tmp_path / "backup.sqlite"
    backup_db.write_bytes(empty_db.read_bytes())

    with sqlite3.connect(empty_db) as conn:
        migrate_memory_graph_store_schema(conn)
        conn.execute("CREATE TABLE fake_corruption (id INTEGER)")

    assert _sha256(empty_db) != _sha256(backup_db)

    empty_db.write_bytes(backup_db.read_bytes())

    with sqlite3.connect(empty_db) as conn:
        assert classify_memory_graph_store_schema(conn) == MemorySchemaClassification.ABSENT
        rows = conn.execute("SELECT * FROM memory_os_facts").fetchall()
        assert rows == [(1, 'authoritative')]

def test_path_safety_check(tmp_path):
    db_file = tmp_path / "db.sqlite"
    db_file.touch()

    sym_file = tmp_path / "symlink.sqlite"
    try:
        sym_file.symlink_to(db_file)
    except OSError:
        pytest.skip("Symlinks not supported on this Windows environment")

    with pytest.raises(OrchestratorError, match="SYMLINK_PATH_REFUSED"):
        _no_symlink_chain(sym_file)
