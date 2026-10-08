"""Comprehensive adversarial test suite for post-PR399 audit remediation:
H06-ext: Lease-safe failure and recovery cleanup with strict fenced ownership
H10-ext: Unified SQLite schema compatibility validation (CHECK, defaults, types, partial indexes, triggers)
H11-ext: Versioned canonical rollback HMAC binding (executed_at, producer, rehearsal_type, status)
C02-ext: Git candidate isolation across nested .gitattributes and safe diff inspection
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    ClaimRecord,
    DispatcherConfig,
    LinearTask,
    TaskState,
)
from ai_engineering.linear_dispatcher.dispatcher import AutonomousDispatcher
from ai_engineering.linear_dispatcher.execution_ledger import ExecutionLedger, ExecutionState
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.state_machine import TaskStateMachine
from ai_engineering.linear_dispatcher.task_executor import SandboxedBrokerTaskExecutor
from ai_engineering.linear_dispatcher.validator import LocalValidator
from ai_engineering.linear_dispatcher.worktree_service import (
    WorktreeIsolationError,
    get_clean_git_env,
    get_effective_filter_opts,
)
from hermes_cli.backup import (
    extract_check_constraints,
    validate_sqlite_schema_compatibility,
)
from scripts.generate_offline_evidence import (
    compute_rollback_rehearsal_payload,
    get_real_rollback_evidence,
)


# ==============================================================================
# 1. H06-ext: Lease-Safe Failure and Success Cleanup
# ==============================================================================

def test_h06_ext_executor_failure_with_valid_lease_cleans_up(tmp_path: Path):
    """When execution fails with a valid lease, cleanup callback executes and lease is released."""
    persistence_file = tmp_path / "leases.json"
    lm = LeaseManager(persistence_path=persistence_file)
    task_id = "TASK-FAIL-01"
    worker_id = "worker-1"

    ok, claim, _ = lm.claim(task_id, worker_id, lease_duration_sec=60)
    assert ok is True
    assert claim is not None

    cleaned = False

    def callback():
        nonlocal cleaned
        cleaned = True

    fenced_ok = lm.fenced_cleanup(task_id, worker_id, claim.claim_token, callback)
    assert fenced_ok is True
    assert cleaned is True
    # Lease should now be released
    assert task_id not in lm._leases


def test_h06_ext_executor_failure_with_expired_lease_preserves_worktree(tmp_path: Path):
    """When execution fails after lease expiry, fenced cleanup blocks and leaves worktree intact."""
    persistence_file = tmp_path / "leases.json"
    lm = LeaseManager(persistence_path=persistence_file)
    task_id = "TASK-FAIL-EXPIRED"
    worker_id = "worker-1"

    ok, claim, _ = lm.claim(task_id, worker_id, lease_duration_sec=10)
    assert ok is True
    assert claim is not None

    # Force expiry by advancing time
    now_past = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=20)
    now_past_iso = now_past.isoformat()

    cleaned = False

    def callback():
        nonlocal cleaned
        cleaned = True

    fenced_ok = lm.fenced_cleanup(task_id, worker_id, claim.claim_token, callback, now_iso=now_past_iso)
    assert fenced_ok is False
    assert cleaned is False


def test_h06_ext_foreign_claim_theft_prevents_destructive_cleanup(tmp_path: Path):
    """If another worker steals the lease, the original worker's cleanup is strictly rejected."""
    persistence_file = tmp_path / "leases.json"
    lm = LeaseManager(persistence_path=persistence_file)
    task_id = "TASK-THEFT-01"
    worker1 = "worker-1"
    worker2 = "worker-2"

    ok, claim1, _ = lm.claim(task_id, worker1, lease_duration_sec=60)
    assert ok is True
    assert claim1 is not None

    # Force steal by worker2 in lease manager
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    claim2_record = ClaimRecord(
        task_id=task_id,
        claim_owner=worker2,
        claim_token="stolen-token-999",
        claimed_at=now_iso,
        lease_expires_at=(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)).isoformat(),
    )
    lm._leases[task_id] = claim2_record

    cleaned = False

    def callback():
        nonlocal cleaned
        cleaned = True

    # worker1 attempts cleanup with original token
    fenced_ok = lm.fenced_cleanup(task_id, worker1, claim1.claim_token, callback)
    assert fenced_ok is False
    assert cleaned is False

    # worker2's lease remains intact
    assert task_id in lm._leases
    assert lm._leases[task_id].claim_owner == worker2


def test_h06_ext_claim_token_replacement_prevents_cleanup(tmp_path: Path):
    """If the token changes even for the same worker, cleanup is rejected."""
    persistence_file = tmp_path / "leases.json"
    lm = LeaseManager(persistence_path=persistence_file)
    task_id = "TASK-TOKEN-01"
    worker_id = "worker-1"

    ok, claim, _ = lm.claim(task_id, worker_id, lease_duration_sec=60)
    assert ok is True
    assert claim is not None

    cleaned = False

    def callback():
        nonlocal cleaned
        cleaned = True

    fenced_ok = lm.fenced_cleanup(task_id, worker_id, "different-token", callback)
    assert fenced_ok is False
    assert cleaned is False


def test_h06_ext_dispatcher_fenced_cleanup_preserves_worktree_on_stolen_lease(tmp_path: Path):
    """_perform_fenced_cleanup preserves worktree directory if lease is stolen."""
    dispatcher = AutonomousDispatcher.__new__(AutonomousDispatcher)
    dispatcher._config = DispatcherConfig(worker_id="worker-primary")
    dispatcher._canonical_main_sha = "main-sha-001"
    dispatcher._canonical_root = tmp_path / "canonical"
    dispatcher._worktree_base_dir = tmp_path / "worktrees"
    dispatcher._worktree_service = MagicMock()
    dispatcher._worktree_service.canonical_root = tmp_path / "canonical"

    persistence_file = tmp_path / "leases.json"
    lm = LeaseManager(persistence_path=persistence_file)
    dispatcher._lease_manager = lm
    ledger_mock = MagicMock()
    dispatcher._ledger = ledger_mock

    task_id = "TASK-FENCE-01"
    worker_id = "worker-primary"
    ok, claim, _ = lm.claim(task_id, worker_id, lease_duration_sec=60)
    assert ok is True
    assert claim is not None

    # Worktree directory
    wt = tmp_path / "worktrees" / "test-wt"
    wt.mkdir(parents=True)
    (wt / "dirty.txt").write_text("mutation", encoding="utf-8")

    # Another worker steals the lease
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stolen_record = ClaimRecord(
        task_id=task_id,
        claim_owner="worker-thief",
        claim_token="token-stolen-456",
        claimed_at=now_iso,
        lease_expires_at=(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=60)).isoformat(),
    )
    lm._leases[task_id] = stolen_record

    # Perform fenced cleanup with original token
    cleaned = dispatcher._perform_fenced_cleanup(
        task_id=task_id,
        worker_id=worker_id,
        claim_token=claim.claim_token,
        wt_path=wt,
        branch_name="agent/test-branch",
        clear_ledger=True,
    )

    assert cleaned is False
    # Worktree must NOT be deleted!
    assert wt.exists()
    # Ledger must NOT be cleared!
    ledger_mock.clear_task_evidence.assert_not_called()


# ==============================================================================
# 2. H10-ext: Unified SQLite Schema Compatibility Validation
# ==============================================================================

def test_h10_ext_dropped_check_constraint_rejected():
    """validate_sqlite_schema_compatibility rejects a candidate where a CHECK constraint is dropped."""
    conn_live = sqlite3.connect(":memory:")
    conn_live.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance REAL CHECK(balance >= 0));")
    conn_live.execute("PRAGMA user_version = 1;")

    conn_cand = sqlite3.connect(":memory:")
    conn_cand.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, balance REAL);")
    conn_cand.execute("PRAGMA user_version = 1;")

    ok, err = validate_sqlite_schema_compatibility(conn_live, conn_cand)
    assert ok is False
    assert "CHECK constraint mismatch" in str(err)


def test_h10_ext_altered_default_value_rejected():
    """validate_sqlite_schema_compatibility rejects candidate with altered column default value."""
    conn_live = sqlite3.connect(":memory:")
    conn_live.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1);")
    conn_live.execute("PRAGMA user_version = 1;")

    conn_cand = sqlite3.connect(":memory:")
    conn_cand.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, enabled INTEGER DEFAULT 0);")
    conn_cand.execute("PRAGMA user_version = 1;")

    ok, err = validate_sqlite_schema_compatibility(conn_live, conn_cand)
    assert ok is False
    assert "column definition mismatch" in str(err)


def test_h10_ext_changed_column_type_rejected():
    """validate_sqlite_schema_compatibility rejects candidate with changed declared type."""
    conn_live = sqlite3.connect(":memory:")
    conn_live.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, price REAL);")
    conn_live.execute("PRAGMA user_version = 1;")

    conn_cand = sqlite3.connect(":memory:")
    conn_cand.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, price TEXT);")
    conn_cand.execute("PRAGMA user_version = 1;")

    ok, err = validate_sqlite_schema_compatibility(conn_live, conn_cand)
    assert ok is False
    assert "column definition mismatch" in str(err)


def test_h10_ext_partial_index_modification_rejected():
    """validate_sqlite_schema_compatibility rejects candidate with altered partial index WHERE predicate."""
    conn_live = sqlite3.connect(":memory:")
    conn_live.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, status TEXT, val INTEGER);")
    conn_live.execute("CREATE INDEX idx_active ON records(val) WHERE status = 'active';")
    conn_live.execute("PRAGMA user_version = 1;")

    conn_cand = sqlite3.connect(":memory:")
    conn_cand.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, status TEXT, val INTEGER);")
    conn_cand.execute("CREATE INDEX idx_active ON records(val) WHERE status = 'pending';")
    conn_cand.execute("PRAGMA user_version = 1;")

    ok, err = validate_sqlite_schema_compatibility(conn_live, conn_cand)
    assert ok is False
    assert "index or unique constraint mismatch" in str(err)


def test_h10_ext_trigger_definition_mismatch_rejected():
    """validate_sqlite_schema_compatibility rejects candidate missing a trigger or with altered trigger SQL."""
    conn_live = sqlite3.connect(":memory:")
    conn_live.execute("CREATE TABLE audits (id INTEGER PRIMARY KEY, val TEXT);")
    conn_live.execute("CREATE TRIGGER tr_audit AFTER INSERT ON audits BEGIN SELECT 1; END;")
    conn_live.execute("PRAGMA user_version = 1;")

    conn_cand = sqlite3.connect(":memory:")
    conn_cand.execute("CREATE TABLE audits (id INTEGER PRIMARY KEY, val TEXT);")
    conn_cand.execute("PRAGMA user_version = 1;")

    ok, err = validate_sqlite_schema_compatibility(conn_live, conn_cand)
    assert ok is False
    assert "trigger definition mismatch" in str(err)


def test_h10_ext_user_version_mismatch_rejected():
    """validate_sqlite_schema_compatibility rejects candidate with mismatched user_version."""
    conn_live = sqlite3.connect(":memory:")
    conn_live.execute("CREATE TABLE t (id INTEGER);")
    conn_live.execute("PRAGMA user_version = 2;")

    conn_cand = sqlite3.connect(":memory:")
    conn_cand.execute("CREATE TABLE t (id INTEGER);")
    conn_cand.execute("PRAGMA user_version = 1;")

    ok, err = validate_sqlite_schema_compatibility(conn_live, conn_cand)
    assert ok is False
    assert "user_version mismatch" in str(err)


# ==============================================================================
# 3. H11-ext: Versioned Authenticated Rollback Evidence
# ==============================================================================

def test_h11_ext_timestamp_refresh_attempt_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Attempting to refresh executed_at without re-signing must fail HMAC verification with BLOCKED."""
    key = "secret-key-123"
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", key)

    target_sha = "target123"
    digest = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    revision = target_sha
    receipt_id = "receipt-rollback-tamper"
    producer = "healbite-rehearsal-engine"
    rehearsal_type = "docker-compose-revert"

    health_file = tmp_path / "rollback-health.json"
    health_payload = {
        "status": "PASS",
        "target_sha": target_sha,
        "rollback_image_digest": digest,
        "rollback_revision": revision,
    }
    h_bytes = json.dumps(health_payload).encode("utf-8")
    health_file.write_bytes(h_bytes)
    h_hash = hashlib.sha256(h_bytes).hexdigest()

    # Original timestamp and signature
    orig_time = "2026-10-08T00:00:00+00:00"
    payload = compute_rollback_rehearsal_payload(
        receipt_id, target_sha, digest, revision, h_hash, producer, orig_time, rehearsal_type, "PASS"
    )
    sig = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    # Attacker tries to update executed_at to current time while keeping old signature
    refreshed_time = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    rehearsal_payload = {
        "status": "PASS",
        "receipt_id": receipt_id,
        "producer": producer,
        "executed_at": refreshed_time,  # TAMPERED
        "rehearsal_type": rehearsal_type,
        "rollback_image_digest": digest,
        "rollback_revision": revision,
        "target_sha": target_sha,
        "health_check_status": "PASS",
        "health_evidence_path": str(health_file),
        "health_evidence_sha256": h_hash,
        "rollback_procedure_proven": True,
        "execution_provenance": {
            "runtime_identity": "healbite-production",
            "isolation_level": "docker",
            "signature": sig,  # Signature was for orig_time!
        },
    }
    rehearsal_file.write_text(json.dumps(rehearsal_payload), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    with patch("subprocess.check_output") as mock_dock:
        mock_dock.return_value = json.dumps([{
            "ImageManifestDescriptor": {"digest": digest},
            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
        }]).encode("utf-8")

        res = get_real_rollback_evidence(target_sha, refreshed_time)
        assert res.get("status") == "BLOCKED"


def test_h11_ext_legacy_unversioned_payload_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Legacy signature omitting executed_at or version prefix must be rejected."""
    key = "secret-key-123"
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", key)

    target_sha = "target123"
    digest = "sha256:2222222222222222222222222222222222222222222222222222222222222222"
    revision = target_sha
    receipt_id = "receipt-rollback-legacy"
    producer = "healbite-rehearsal-engine"
    rehearsal_type = "docker-compose-revert"

    health_file = tmp_path / "rollback-health.json"
    health_payload = {
        "status": "PASS",
        "target_sha": target_sha,
        "rollback_image_digest": digest,
        "rollback_revision": revision,
    }
    h_bytes = json.dumps(health_payload).encode("utf-8")
    health_file.write_bytes(h_bytes)
    h_hash = hashlib.sha256(h_bytes).hexdigest()

    # Legacy unversioned payload omitting executed_at
    legacy_payload = f"{receipt_id}:{target_sha}:{digest}:{revision}:{h_hash}:{producer}".encode("utf-8")
    legacy_sig = hmac.new(key.encode("utf-8"), legacy_payload, hashlib.sha256).hexdigest()

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    rehearsal_payload = {
        "status": "PASS",
        "receipt_id": receipt_id,
        "producer": producer,
        "executed_at": now_iso,
        "rehearsal_type": rehearsal_type,
        "rollback_image_digest": digest,
        "rollback_revision": revision,
        "target_sha": target_sha,
        "health_check_status": "PASS",
        "health_evidence_path": str(health_file),
        "health_evidence_sha256": h_hash,
        "rollback_procedure_proven": True,
        "execution_provenance": {
            "runtime_identity": "healbite-production",
            "isolation_level": "docker",
            "signature": legacy_sig,
        },
    }
    rehearsal_file.write_text(json.dumps(rehearsal_payload), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    with patch("subprocess.check_output") as mock_dock:
        mock_dock.return_value = json.dumps([{
            "ImageManifestDescriptor": {"digest": digest},
            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
        }]).encode("utf-8")

        res = get_real_rollback_evidence(target_sha, now_iso)
        assert res.get("status") == "BLOCKED"


# ==============================================================================
# 4. C02-ext: Git Candidate Isolation Across Nested .gitattributes
# ==============================================================================

def test_c02_ext_git_config_query_error_fails_closed(tmp_path: Path):
    """When git config query returns unexpected exit code (not 0 or 1), fail closed with WorktreeIsolationError."""
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=128, stderr="fatal: corrupted repository config")
        with pytest.raises(WorktreeIsolationError) as exc_info:
            get_effective_filter_opts(fake_repo)
        assert "git config query failed with exit code 128" in str(exc_info.value)


def test_c02_ext_nested_gitattributes_discovered_and_neutralized(tmp_path: Path):
    """Filter and diff drivers in nested subdirectories are discovered and neutralized."""
    repo_dir = tmp_path / "nested_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)

    # Subdirectory with .gitattributes
    sub_dir = repo_dir / "src" / "deep" / "nested"
    sub_dir.mkdir(parents=True)
    (sub_dir / ".gitattributes").write_text(
        "*.dat filter=adversarial_sub_filter diff=adversarial_sub_diff\n", encoding="utf-8"
    )

    # Root .gitattributes
    (repo_dir / ".gitattributes").write_text(
        "*.bin filter=root_filter\n", encoding="utf-8"
    )

    opts = get_effective_filter_opts(repo_dir)

    # Ensure both root and nested filters are neutralized
    opts_str = " ".join(opts)
    assert "-c filter.adversarial_sub_filter.clean=" in opts_str
    assert "-c filter.adversarial_sub_filter.smudge=" in opts_str
    assert "-c filter.adversarial_sub_filter.process=" in opts_str
    assert "-c filter.adversarial_sub_filter.required=false" in opts_str
    assert "-c diff.adversarial_sub_diff.command=" in opts_str
    assert "-c diff.adversarial_sub_diff.textconv=" in opts_str
    assert "-c filter.root_filter.clean=" in opts_str


def test_c02_ext_local_validator_runs_with_no_textconv_and_driver_neutralization(tmp_path: Path):
    """LocalValidator.run_diff_check executes git diff with --no-textconv, --no-ext-diff, and neutralized filters."""
    repo_dir = tmp_path / "repo_val"
    repo_dir.mkdir()
    subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)

    # Add custom filter and diff in .gitattributes
    (repo_dir / ".gitattributes").write_text("*.txt filter=evil diff=evil\n", encoding="utf-8")
    (repo_dir / "file.txt").write_text("hello\n", encoding="utf-8")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        ok, msg = LocalValidator.run_diff_check(repo_dir)
        assert ok is True
        assert msg == "PASS"

        # Verify that subprocess was called with expected arguments
        called_cmds = [call.args[0] for call in mock_run.call_args_list if isinstance(call.args[0], list)]
        # Filter for git diff calls
        diff_calls = [cmd for cmd in called_cmds if "diff" in cmd]
        assert len(diff_calls) >= 2  # unstaged and staged
        for cmd in diff_calls:
            assert "--no-textconv" in cmd
            assert "--no-ext-diff" in cmd
            assert "filter.evil.clean=" in " ".join(cmd)
            assert "diff.evil.command=" in " ".join(cmd)
