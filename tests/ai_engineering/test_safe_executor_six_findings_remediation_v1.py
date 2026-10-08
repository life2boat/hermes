"""Regression tests for the final six high findings remediation:
C01: Exact Cordis tool-disable identities
C02: Git candidate isolation and dotted driver neutralization
H05: Recovery tuple handling and get_pr_for_branch contract
H06: Final lease fencing before normal worktree cleanup
H10: Complete SQLite schema compatibility validation
H11: Authenticated rollback evidence requiring HERMES_PROVENANCE_KEY
Executor Readiness: Sandboxed broker default selection and credential isolation
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
from ai_engineering.linear_dispatcher.github_service import PRCreationResult
from ai_engineering.linear_dispatcher.execution_ledger import ExecutionState
from ai_engineering.linear_dispatcher.dispatcher import AutonomousDispatcher
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.state_machine import TaskStateMachine
from ai_engineering.linear_dispatcher.task_executor import (
    DshModelGateway,
    LinuxCodexTaskExecutor,
    SandboxedBrokerTaskExecutor,
)
from ai_engineering.linear_dispatcher.worker import DispatcherWorker
from ai_engineering.linear_dispatcher.worktree_service import (
    SAFE_GIT_OPTS,
    WorktreeService,
    get_clean_git_env,
    get_effective_filter_opts,
)


# ==============================================================================
# 1. C01: Exact Cordis Tool-Disable Identities
# ==============================================================================

VALID_CORDIS_PATCH_YAML = """- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    model: deepseek-v4-pro
- id: tool-fs
  disabled: true
- id: tool-fs-search
  disabled: true
- id: tool-pwsh
  disabled: true
- id: tool-bash
  disabled: true
- id: tool-jobs
  disabled: true
- id: web
  disabled: true
- id: subagent
  disabled: true
- id: tool-skill
  disabled: true
- id: tool-todo
  disabled: true
- id: tool-goal
  disabled: true
"""


def test_c01_valid_cordis_patch_passes(tmp_path: Path):
    """C01: Exact canonical tool IDs and names pass overlay verification."""
    patch_file = tmp_path / "valid.patch.yml"
    patch_file.write_text(VALID_CORDIS_PATCH_YAML, encoding="utf-8")
    gw = DshModelGateway(patch_path=patch_file)
    assert gw._verify_no_tools_patch() is True


def test_c01_miscased_tool_id_rejected_fail_closed(tmp_path: Path):
    """C01: Mis-cased TOOL-FS entry cannot pass validation without lowercase coercion."""
    patch_file = tmp_path / "miscased.patch.yml"
    miscased_content = VALID_CORDIS_PATCH_YAML.replace("id: tool-fs", "id: TOOL-FS")
    patch_file.write_text(miscased_content, encoding="utf-8")
    gw = DshModelGateway(patch_path=patch_file)
    assert gw._verify_no_tools_patch() is False


def test_c01_miscased_web_or_subagent_rejected(tmp_path: Path):
    """C01: Mis-cased WEB or SubAgent entries are rejected."""
    patch_file = tmp_path / "miscased_web.patch.yml"
    miscased_content = VALID_CORDIS_PATCH_YAML.replace("id: web", "id: WEB")
    patch_file.write_text(miscased_content, encoding="utf-8")
    gw = DshModelGateway(patch_path=patch_file)
    assert gw._verify_no_tools_patch() is False


def test_c01_unknown_tool_id_rejected(tmp_path: Path):
    """C01: Unknown or misspelled tool ID fails closed."""
    patch_file = tmp_path / "unknown_tool.patch.yml"
    content = VALID_CORDIS_PATCH_YAML + "- id: tool-unknown\n  disabled: true\n"
    patch_file.write_text(content, encoding="utf-8")
    gw = DshModelGateway(patch_path=patch_file)
    assert gw._verify_no_tools_patch() is False


def test_c01_duplicate_tool_id_rejected(tmp_path: Path):
    """C01: Duplicate tool ID entries are ambiguous and rejected."""
    patch_file = tmp_path / "dup_tool.patch.yml"
    content = VALID_CORDIS_PATCH_YAML + "- id: tool-fs\n  disabled: true\n"
    patch_file.write_text(content, encoding="utf-8")
    gw = DshModelGateway(patch_path=patch_file)
    assert gw._verify_no_tools_patch() is False


def test_c01_enabled_tool_fails_closed(tmp_path: Path):
    """C01: An enabled tool entry immediately fails closed."""
    patch_file = tmp_path / "enabled_tool.patch.yml"
    content = VALID_CORDIS_PATCH_YAML.replace("id: tool-bash\n  disabled: true", "id: tool-bash\n  disabled: false")
    patch_file.write_text(content, encoding="utf-8")
    gw = DshModelGateway(patch_path=patch_file)
    assert gw._verify_no_tools_patch() is False


def test_c01_forward_deepseek_api_key_in_isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """C01: Isolated DSH environment forwards DEEPSEEK_API_KEY when present."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-dsh-key")
    gw = DshModelGateway()
    env = gw._get_isolated_env(tmp_path)
    assert env.get("DEEPSEEK_API_KEY") == "sk-secret-dsh-key"
    assert env.get("CORDIS_DISABLE_PLUGINS") == "1"


# ==============================================================================
# 2. C02: Git Candidate Isolation & Dotted Driver Neutralization
# ==============================================================================

def test_c02_dotted_filter_and_diff_driver_discovery(tmp_path: Path):
    """C02: Discover and neutralize filter/diff drivers with dots in names."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)

    # Configure dotted filter and diff drivers in git config
    subprocess.run(["git", "config", "filter.audit.driver.clean", "echo exploit"], cwd=repo, check=True)
    subprocess.run(["git", "config", "filter.audit.driver.smudge", "cat"], cwd=repo, check=True)
    subprocess.run(["git", "config", "diff.custom.driver.command", "sh exploit.sh"], cwd=repo, check=True)

    # Configure .gitattributes with dotted driver names
    (repo / ".gitattributes").write_text("*.dat filter=audit.driver diff=custom.driver\n", encoding="utf-8")

    opts = get_effective_filter_opts(repo)
    opts_str = " ".join(opts)

    assert "filter.audit.driver.clean=" in opts_str
    assert "filter.audit.driver.smudge=" in opts_str
    assert "diff.custom.driver.command=" in opts_str
    assert "diff.custom.driver.textconv=" in opts_str


def test_c02_clean_git_env_sanitizes_all_git_control_vars():
    """C02: get_clean_git_env sanitizes all GIT_* variables and keeps only trusted allowlist."""
    dirty_env = {
        "GIT_DIR": "/evil/.git",
        "GIT_WORK_TREE": "/evil/wt",
        "GIT_INDEX_FILE": "/evil/index",
        "GIT_EXEC_PATH": "/evil/bin",
        "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=true'",
        "PAGER": "less",
        "EDITOR": "nano",
        "VISUAL": "vim",
        "UNTRUSTED_VAR": "malicious_payload",
        "PATH": "C:\\Windows\\System32" if os.name == "nt" else "/usr/bin",
        "USER": "testuser",
    }
    clean = get_clean_git_env(dirty_env)

    # Control variables stripped
    assert "GIT_DIR" not in clean
    assert "GIT_WORK_TREE" not in clean
    assert "GIT_INDEX_FILE" not in clean
    assert "GIT_EXEC_PATH" not in clean
    assert "GIT_CONFIG_PARAMETERS" not in clean
    assert "PAGER" not in clean
    assert "EDITOR" not in clean
    assert "VISUAL" not in clean
    assert "UNTRUSTED_VAR" not in clean

    # Trusted variables retained
    assert "PATH" in clean
    # Safe Git overrides injected
    assert clean.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert clean.get("GIT_CONFIG_GLOBAL") == os.devnull
    assert clean.get("GIT_SSH_COMMAND") == "false"


def test_c02_real_adversarial_dotted_driver_neutralization(tmp_path: Path):
    """C02: Real runtime adversarial test ensuring dotted driver cannot execute code."""
    sentinel = tmp_path / "sentinel_c02_exploit.txt"
    if sentinel.exists():
        sentinel.unlink()

    repo = tmp_path / "untrusted_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)

    exploit_cmd = f"python3 -c \"import pathlib; pathlib.Path(r'{sentinel}').touch()\""
    subprocess.run(["git", "config", "filter.audit.driver.clean", exploit_cmd], cwd=repo, check=True)
    subprocess.run(["git", "config", "filter.audit.driver.required", "true"], cwd=repo, check=True)
    (repo / ".gitattributes").write_text("*.txt filter=audit.driver\n", encoding="utf-8")

    # Target file
    (repo / "payload.txt").write_text("untrusted content", encoding="utf-8")

    # Run git add with neutralization opts and clean env
    dynamic_opts = get_effective_filter_opts(repo)
    cmd = ["git"] + SAFE_GIT_OPTS + dynamic_opts + ["-C", str(repo), "status"]
    clean_env = get_clean_git_env()

    res = subprocess.run(cmd, env=clean_env, capture_output=True, text=True)
    assert res.returncode == 0

    # Sentinel must NOT exist
    assert not sentinel.exists(), "Adversarial dotted filter driver executed on host!"


# ==============================================================================
# 3. H05: Recovery Tuple Handling
# ==============================================================================

def test_h05_recovery_unpacks_get_pr_for_branch_tuple_cleanly():
    """H05: Recovery cleanly unpacks get_pr_for_branch tuple without AttributeError."""
    dispatcher = AutonomousDispatcher.__new__(AutonomousDispatcher)
    dispatcher._github_service = MagicMock()
    dispatcher._worktree_service = MagicMock()
    dispatcher._worktree_service.canonical_root = Path("/tmp")
    dispatcher._canonical_root = Path("/tmp")
    mock_run_git = MagicMock()
    mock_run_git.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="head-sha-001 refs/heads/agent/her-100-test\n", stderr=""
    )
    dispatcher._run_git = mock_run_git
    dispatcher._linear_client = MagicMock()
    dispatcher._lease_manager = MagicMock()
    dispatcher._lease_manager.verify_lease.return_value = True
    dispatcher._ledger = MagicMock()
    dispatcher._config = DispatcherConfig()
    dispatcher._canonical_main_sha = "main-sha-001"

    # Mock get_pr_for_branch returning tuple (bool, PRCreationResult)
    pr_result = PRCreationResult(
        pr_number=42,
        pr_url="https://github.com/life2boat/hermes/pull/42",
        base_sha="main-sha-001",
        head_sha="head-sha-001",
        is_draft=True,
    )
    dispatcher._github_service.get_pr_for_branch.return_value = (True, pr_result)

    recovery_state = ExecutionState(
        task_id="HER-100",
        state=TaskState.BRANCH_PUSHED.value,
        claim_owner="hermes-autonomous-dispatcher-v1",
        claim_token="claim-tok-1",
        worktree_path="/tmp/wt",
        branch="agent/her-100-test",
        head_sha="head-sha-001",
        pr_number=None,
        pr_url=None,
        base_sha="main-sha-001",
        execution_id="exec-1",
    )

    # Mock _wait_for_ci and _writeback_service
    dispatcher._wait_for_ci = MagicMock(return_value=(False, None, "CI_TIMEOUT"))

    res = dispatcher.recover_task(recovery_state)
    assert res is not None
    # No AttributeError was raised! PR number was reconciled
    assert recovery_state.pr_number == 42
    assert recovery_state.pr_url == "https://github.com/life2boat/hermes/pull/42"


def test_h05_recovery_rejects_mismatched_pr_head_sha():
    """H05: Recovery rejects PR with head SHA mismatch and fails closed."""
    dispatcher = AutonomousDispatcher.__new__(AutonomousDispatcher)
    dispatcher._github_service = MagicMock()
    dispatcher._worktree_service = MagicMock()
    dispatcher._worktree_service.canonical_root = Path("/tmp")
    dispatcher._canonical_root = Path("/tmp")
    dispatcher._linear_client = MagicMock()
    dispatcher._lease_manager = MagicMock()
    dispatcher._lease_manager.verify_lease.return_value = True
    dispatcher._ledger = MagicMock()
    dispatcher._config = DispatcherConfig()
    dispatcher._canonical_main_sha = "main-sha-001"

    # PR with WRONG head SHA
    pr_result = PRCreationResult(
        pr_number=42,
        pr_url="https://github.com/life2boat/hermes/pull/42",
        base_sha="main-sha-001",
        head_sha="wrong-sha-999",
        is_draft=True,
    )
    dispatcher._github_service.get_pr_for_branch.return_value = (True, pr_result)

    recovery_state = ExecutionState(
        task_id="HER-101",
        state=TaskState.BRANCH_PUSHED.value,
        claim_owner="hermes-autonomous-dispatcher-v1",
        claim_token="claim-tok-1",
        worktree_path="/tmp/wt",
        branch="agent/her-101-test",
        head_sha="head-sha-001",
        pr_number=None,
        pr_url=None,
        base_sha="main-sha-001",
        execution_id="exec-1",
    )

    # Mock ls-remote so remote check passes
    mock_run_git = MagicMock()
    mock_run_git.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="head-sha-001 refs/heads/agent/her-101-test\n", stderr=""
    )
    dispatcher._run_git = mock_run_git

    res = dispatcher.recover_task(recovery_state)
    assert res is not None
    assert res.final_state == TaskState.BLOCKED
    assert res.block_reason == BlockReasonCode.RECOVERY_BLOCKED.value


# ==============================================================================
# 4. H06: Final Lease Fencing Before Normal Worktree Cleanup
# ==============================================================================

def test_h06_final_lease_loss_before_normal_cleanup_blocks_and_preserves_worktree():
    """H06: Losing lease immediately after writeback halts normal cleanup and blocks."""
    dispatcher = AutonomousDispatcher.__new__(AutonomousDispatcher)
    dispatcher._config = DispatcherConfig(worker_id="worker-primary")
    dispatcher._canonical_main_sha = "main-sha-001"
    dispatcher._canonical_root = Path("/tmp")
    dispatcher._worktree_base_dir = Path("/tmp")
    dispatcher._worktree_service = MagicMock()
    dispatcher._worktree_service.canonical_root = Path("/tmp")
    dispatcher._lease_manager = MagicMock()
    dispatcher._ledger = MagicMock()
    dispatcher._github_service = MagicMock()
    dispatcher._writeback_service = MagicMock()
    dispatcher._linear_client = MagicMock()

    task = LinearTask(
        id="HER-200",
        uuid="uuid-200",
        title="Test Task",
        description="desc",
        state="Todo",
        priority=1,
        assignee="u1",
        labels=("agent:auto", "agent:shadow"),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url="https://linear.app/issue/HER-200",
    )
    claim_record = ClaimRecord(
        task_id="HER-200",
        claim_owner="worker-primary",
        claim_token="token-abc",
        claimed_at="2026-10-08T00:00:00Z",
        lease_expires_at="2026-10-08T01:00:00Z",
    )
    pr_result = PRCreationResult(
        pr_number=55,
        pr_url="https://github.com/life2boat/hermes/pull/55",
        base_sha="main-sha-001",
        head_sha="head-sha-001",
        is_draft=True,
    )

    # Setup normal pipeline pass until final lease check
    dispatcher._worktree_service.create_worktree.return_value = (Path("/tmp/wt"), "agent/her-200-test", None)
    dispatcher._worktree_service.commit_changes.return_value = ("head-sha-001", None)
    dispatcher._github_service.create_draft_pr.return_value = (True, pr_result, None)
    dispatcher._writeback_service.writeback_and_verify.return_value = (True, None)

    ci_pass_result = MagicMock()
    ci_pass_result.overall_status = "PASS"
    ci_pass_result.head_sha = "head-sha-001"
    ci_pass_result.runs = [MagicMock()]
    dispatcher._wait_for_ci = MagicMock(return_value=(True, ci_pass_result, None))

    mock_executor = MagicMock()
    mock_executor.health.return_value = True
    mock_executor.execution_ready.return_value = True
    mock_executor.execute.return_value = MagicMock(
        status="SUCCESS",
        execution_id="exec-200",
        error_reason=None,
    )
    dispatcher._task_executor = mock_executor
    dispatcher._get_git_changes = MagicMock(return_value=["feature.py"])
    dispatcher._resolve_remote_main_sha = MagicMock(return_value="main-sha-001")

    def fake_git_run(args, cwd, **kwargs):
        if "diff" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="feature.py\n", stderr="")
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="head-sha-001\n", stderr="")
        if "remote" in args and "get-url" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="https://github.com/life2boat/hermes.git\n", stderr="")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    dispatcher._safe_git_run = fake_git_run

    # Simulate lease verification: passes early checks (1..5), but FAILS at final check before cleanup (6)!
    calls = []
    def mock_verify_lease(task_id, owner, token, now_iso=None):
        calls.append(len(calls))
        if len(calls) >= 6:
            return False
        return True

    dispatcher._lease_manager.claim.return_value = (True, claim_record, None)
    dispatcher._lease_manager.is_held_by_foreign_worker.return_value = False
    dispatcher._lease_manager.verify_lease.side_effect = mock_verify_lease

    from ai_engineering.linear_dispatcher.validator import LocalValidator
    with patch.object(LocalValidator, "run_validation", return_value=(True, {"status": "PASS"})):
        result = dispatcher.dispatch_one_task([task])

    # Must be BLOCKED with LOST_LEASE
    assert result.final_state == TaskState.BLOCKED
    assert result.block_reason == BlockReasonCode.LOST_LEASE.value

    # Worktree must NOT be removed!
    dispatcher._worktree_service.remove_worktree.assert_not_called()
    # Ledger must NOT be cleared!
    dispatcher._ledger.clear.assert_not_called()


# ==============================================================================
# 5. H10: Complete SQLite Schema Compatibility Validation
# ==============================================================================

def test_h10_sqlite_schema_type_mismatch_rejected(tmp_path: Path):
    """H10: Database restore rejects candidate with mismatched column declared types."""
    from hermes_cli.backup import restore_quick_snapshot

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()

    # Existing live database: name is TEXT
    live_db = hermes_home / "state.db"
    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);")
    conn.execute("INSERT INTO users VALUES (1, 'Alice');")
    conn.execute("PRAGMA user_version = 1;")
    conn.commit()
    conn.close()

    # Snapshot directory
    snap_dir = hermes_home / "snapshots" / "quick" / "snap_type_mismatch"
    snap_dir.mkdir(parents=True)
    (snap_dir / "manifest.json").write_text(json.dumps({"files": {"state.db": {}}}), encoding="utf-8")

    # Candidate DB in snapshot: name is BLOB (type mismatch!)
    cand_db = snap_dir / "state.db"
    c_conn = sqlite3.connect(cand_db)
    c_conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name BLOB NOT NULL);")
    c_conn.execute("INSERT INTO users VALUES (1, X'1234');")
    c_conn.execute("PRAGMA user_version = 1;")
    c_conn.commit()
    c_conn.close()

    restore_quick_snapshot("snap_type_mismatch", hermes_home=hermes_home)

    # Live DB table must retain original TEXT data and type!
    verify_conn = sqlite3.connect(live_db)
    row = verify_conn.execute("SELECT name FROM users WHERE id = 1").fetchone()
    cols = verify_conn.execute("PRAGMA table_info('users')").fetchall()
    verify_conn.close()
    assert row[0] == "Alice"
    name_col_type = next(c[2] for c in cols if c[1] == "name")
    assert name_col_type.upper() == "TEXT"


def test_h10_sqlite_schema_notnull_mismatch_rejected(tmp_path: Path):
    """H10: Database restore rejects candidate with mismatched NOT NULL constraint."""
    from hermes_cli.backup import restore_quick_snapshot

    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()

    # Existing live database: name is NOT NULL
    live_db = hermes_home / "state.db"
    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL);")
    conn.execute("INSERT INTO users VALUES (1, 'Bob');")
    conn.execute("PRAGMA user_version = 1;")
    conn.commit()
    conn.close()

    # Snapshot directory
    snap_dir = hermes_home / "snapshots" / "quick" / "snap_notnull_mismatch"
    snap_dir.mkdir(parents=True)
    (snap_dir / "manifest.json").write_text(json.dumps({"files": {"state.db": {}}}), encoding="utf-8")

    # Candidate DB in snapshot: name is NULLable (constraint mismatch!)
    cand_db = snap_dir / "state.db"
    c_conn = sqlite3.connect(cand_db)
    c_conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT);")
    c_conn.execute("INSERT INTO users VALUES (1, NULL);")
    c_conn.execute("PRAGMA user_version = 1;")
    c_conn.commit()
    c_conn.close()

    restore_quick_snapshot("snap_notnull_mismatch", hermes_home=hermes_home)

    # Live DB table must retain original NOT NULL constraint and data!
    verify_conn = sqlite3.connect(live_db)
    row = verify_conn.execute("SELECT name FROM users WHERE id = 1").fetchone()
    cols = verify_conn.execute("PRAGMA table_info('users')").fetchall()
    verify_conn.close()
    assert row[0] == "Bob"
    name_col_notnull = next(c[3] for c in cols if c[1] == "name")
    assert name_col_notnull == 1


# ==============================================================================
# 6. H11: Authenticated Rollback Evidence
# ==============================================================================

def test_h11_missing_provenance_key_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """H11: Missing HERMES_PROVENANCE_KEY must fail closed with STATUS=BLOCKED."""
    from scripts.generate_offline_evidence import get_real_rollback_evidence

    monkeypatch.delenv("HERMES_PROVENANCE_KEY", raising=False)

    target_sha = "abc123sha"
    digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    revision = target_sha

    # Create dummy health file
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

    # Create rehearsal receipt with arbitrary 64-char hex string signature
    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    fake_sig = "a" * 64
    rehearsal_payload = {
        "status": "PASS",
        "receipt_id": "receipt-rollback-001",
        "producer": "healbite-rehearsal-engine",
        "executed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "rehearsal_type": "docker-compose-revert",
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
            "signature": fake_sig,
        },
    }
    rehearsal_file.write_text(json.dumps(rehearsal_payload), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))

    res = get_real_rollback_evidence(target_sha, datetime.datetime.now(datetime.timezone.utc).isoformat())
    assert res.get("status") == "BLOCKED"


def test_h11_valid_provenance_key_and_signature_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """H11: Cryptographically signed rehearsal with trusted key passes."""
    from scripts.generate_offline_evidence import get_real_rollback_evidence

    key = "super-secret-provenance-key"
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", key)

    target_sha = "abc123sha"
    digest = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    revision = target_sha
    receipt_id = "receipt-rollback-001"

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

    payload = f"{receipt_id}:{target_sha}:{digest}:{revision}:{h_hash}".encode("utf-8")
    valid_sig = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rehearsal_payload = {
        "status": "PASS",
        "receipt_id": receipt_id,
        "producer": "healbite-rehearsal-engine",
        "executed_at": now_iso,
        "rehearsal_type": "docker-compose-revert",
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
            "signature": valid_sig,
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
        assert res.get("status") == "PASS"


# ==============================================================================
# 7. Executor Readiness & Default Selection
# ==============================================================================

def test_executor_readiness_default_is_sandboxed_broker(monkeypatch: pytest.MonkeyPatch):
    """DispatcherWorker selects SandboxedBrokerTaskExecutor by default without codex fallback."""
    monkeypatch.delenv("DISPATCHER_EXECUTOR_BACKEND", raising=False)
    monkeypatch.setenv("DSH_PATCH_PATH", "dummy.patch.yml")

    worker = DispatcherWorker()
    executor = worker._get_executor()
    assert isinstance(executor, SandboxedBrokerTaskExecutor)


def test_executor_readiness_legacy_codex_remains_fail_closed(monkeypatch: pytest.MonkeyPatch):
    """Legacy LinuxCodexTaskExecutor remains fail-closed."""
    monkeypatch.setenv("DISPATCHER_EXECUTOR_BACKEND", "codex")
    worker = DispatcherWorker()
    executor = worker._get_executor()
    assert isinstance(executor, LinuxCodexTaskExecutor)
    # Fail-closed: execution_ready() is strictly False without unsupported override
    assert executor.execution_ready() is False


def test_single_file_fallback_ast_rejects_empty_or_all_expr(tmp_path: Path):
    """SandboxedBrokerTaskExecutor rejects fallback AST if body is empty or consists purely of expressions."""
    executor = SandboxedBrokerTaskExecutor()
    (tmp_path / "module.py").write_text("def run(): pass\n", encoding="utf-8")

    # Empty payload
    res, err = executor._parse_mutation_payload("```python\n\n```", tmp_path)
    assert res == []
    assert err == "SINGLE_FILE_FALLBACK_EMPTY_BODY_REJECTED"

    # Multi-statement expressions (bare literals/dictionaries)
    all_expr_code = '```python\n"just a string"\n{"key": "value"}\n[1, 2, 3]\n```'
    res, err = executor._parse_mutation_payload(all_expr_code, tmp_path)
    assert res == []
    assert err == "SINGLE_FILE_FALLBACK_LITERAL_EXPRESSION_REJECTED"

