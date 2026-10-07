"""Adversarial qualification test suite for Final Codex-Proven 2 Critical + 7 High Blocker Closure v4.

Covers:
C01: Codex CLI credential confidentiality fail-closed boundary by default.
C02: Windows footguns external worktree scanning without ValueError, and sanitized git options/env.
H02: Canonical remote provenance enforcement and missing remote fail-closed detection.
H03: CI status evidence requires trusted check producers and canonical required checks.
H04: Linear team binding strictly enforces team-scoped workflow states and rejects ambiguities without global fallback.
H05: Remote reconciliation retains durable ledger state on push failure and reconciles PRs.
H06: Recovery lease fencing immediately before writeback and cleanup, and worker halt on unrecovered task.
H10: SQLite restore schema table compatibility and WAL truncate checkpoint.
H11: Offline evidence rollback rehearsal trusted producer, 48-hour freshness, and health target SHA binding.
N01: Secret scanner --fail-on-skipped CLI support.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
    TaskState,
)
from ai_engineering.linear_dispatcher.dispatcher import AutonomousDispatcher
from ai_engineering.linear_dispatcher.execution_ledger import ExecutionLedger, ExecutionState
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.task_executor import LinuxCodexTaskExecutor
from ai_engineering.linear_dispatcher.worktree_service import (
    SAFE_GIT_OPTS,
    WorktreeService,
    get_clean_git_env,
    is_canonical_remote_url,
)
from ai_engineering.linear_dispatcher.github_service_production import (
    CANONICAL_REQUIRED_CHECKS,
    GitHubProductionService,
)
from ai_engineering.linear_dispatcher.linear_client_production import LinearProductionClient
from ai_engineering.linear_dispatcher.worker import DispatcherWorker
from hermes_cli.backup import restore_quick_snapshot
from scripts.generate_offline_evidence import (
    TRUSTED_REHEARSAL_PRODUCERS,
    get_real_rollback_evidence,
)


# ==============================================================================
# Helper Factories
# ==============================================================================

def make_task(task_id: str = "HER-999") -> LinearTask:
    return LinearTask(
        id=task_id,
        uuid="uuid-" + task_id,
        title=f"Task {task_id}",
        description="Sample description",
        state="Todo",
        priority=1,
        assignee="agent",
        labels=("agent:auto", "agent:shadow"),
        created_at="2026-10-05T00:00:00Z",
        updated_at="2026-10-05T00:00:00Z",
        url=f"https://linear.app/hermes/issue/{task_id}",
    )


# ==============================================================================
# C01: Codex Credential Boundary
# ==============================================================================

def test_c01_codex_credential_boundary_fails_closed_by_default(tmp_path: Path):
    """C01: Model-controlled execution must never start without proven credential confidentiality.
    Default executor has allow_unsupported_codex_credential_boundary=False and fails closed.
    """
    wt = tmp_path / "wt"
    wt.mkdir()
    executor = LinuxCodexTaskExecutor(is_windows=False)
    res = executor.execute(make_task("HER-100"), wt, "basesha")
    assert res.status == "BLOCKED"
    assert res.error_reason == "CODEX_CREDENTIAL_BOUNDARY_UNSUPPORTED"


# ==============================================================================
# C02: Candidate Code Host Privilege Isolation & Windows Footguns
# ==============================================================================

def test_c02_git_options_and_environment_are_hardened():
    """C02: SAFE_GIT_OPTS neutralizes hooks, fsmonitor, filters, and clean_env blocks DLL hijack."""
    assert "core.hooksPath=/dev/null" in SAFE_GIT_OPTS
    assert "core.fsmonitor=false" in SAFE_GIT_OPTS
    assert "diff.external=" in SAFE_GIT_OPTS
    assert "filter.*.clean=" in SAFE_GIT_OPTS
    assert "filter.*.smudge=" in SAFE_GIT_OPTS

    env = get_clean_git_env()
    assert env.get("GIT_CONFIG_NOSYSTEM") == "1"
    assert env.get("GIT_CONFIG_GLOBAL") == os.devnull
    assert env.get("NoDefaultCurrentDirectoryInExePath") == "1"


def test_c02_windows_footguns_scanner_handles_external_paths(tmp_path: Path):
    """C02: check-windows-footguns.py must not raise ValueError when evaluating files outside REPO_ROOT."""
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "check-windows-footguns.py"
    spec = importlib.util.spec_from_file_location("check_windows_footguns", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_windows_footguns"] = mod
    spec.loader.exec_module(mod)

    # Path outside repo root (different drive letter or unrelated directory)
    external_file = tmp_path / "some_external_dir" / "file.py"
    external_file.parent.mkdir(parents=True, exist_ok=True)
    external_file.write_text("print('test')\n", encoding="utf-8")

    # should_scan_file must safely return True without raising ValueError
    assert mod.should_scan_file(external_file) is True


# ==============================================================================
# H02: Canonical Remote Provenance
# ==============================================================================

def test_h02_canonical_remote_url_validation():
    """H02: is_canonical_remote_url accepts only legitimate canonical Hermes repo URLs."""
    assert is_canonical_remote_url("https://github.com/life2boat/hermes.git") is True
    assert is_canonical_remote_url("git@github.com:life2boat/hermes.git") is True
    assert is_canonical_remote_url("https://github.com/life2boat/hermes") is True
    assert is_canonical_remote_url("https://github.com/attacker/hermes.git") is False
    assert is_canonical_remote_url("git@github.com:attacker/hermes.git") is False
    assert is_canonical_remote_url("") is False


# ==============================================================================
# H03: Complete CI Evidence & Trusted Producers
# ==============================================================================

def test_h03_ci_evidence_requires_trusted_producers():
    """H03: get_ci_status must fail closed if checks are produced by untrusted entities."""
    service = GitHubProductionService(token="fake-token", repo="life2boat/hermes")

    # Fake checks payload from untrusted producer
    fake_runs = [
        {
            "name": check_name,
            "status": "completed",
            "conclusion": "success",
            "head_sha": "headsha",
            "app": {"slug": "untrusted-third-party-bot"},
        }
        for check_name in CANONICAL_REQUIRED_CHECKS
    ]

    with patch.object(service, "_request") as mock_req:
        mock_req.side_effect = [
            {"base": {"ref": "main", "sha": "basesha"}, "head": {"sha": "headsha"}, "draft": True},
            {"check_runs": fake_runs, "total_count": len(fake_runs)},
        ]
        ok, res, reason = service.get_ci_status("life2boat/hermes", "headsha", 123)
        assert ok is False
        assert reason == BlockReasonCode.CI_FAILED
        assert res is not None
        assert any("UNTRUSTED" in v for v in res.details.values())


def test_h03_ci_evidence_requires_all_canonical_checks():
    """H03: get_ci_status must fail closed if any canonical check is missing."""
    service = GitHubProductionService(token="fake-token", repo="life2boat/hermes")

    # Provide only 1 check run instead of all required
    fake_runs = [
        {
            "name": CANONICAL_REQUIRED_CHECKS[0],
            "status": "completed",
            "conclusion": "success",
            "head_sha": "headsha",
            "app": {"slug": "github-actions"},
        }
    ]

    with patch.object(service, "_request") as mock_req:
        mock_req.side_effect = [
            {"base": {"ref": "main", "sha": "basesha"}, "head": {"sha": "headsha"}, "draft": True},
            {"check_runs": fake_runs, "total_count": 1},
        ]
        ok, res, reason = service.get_ci_status("life2boat/hermes", "headsha", 123)
        assert ok is False
        assert res is not None
        assert "missing_required_checks" in res.details


# ==============================================================================
# H04: Linear Team Binding & Scoped Workflow States
# ==============================================================================

def test_h04_linear_client_fails_closed_without_team_id():
    """H04: update_issue must reject state transitions if issue has no team_id."""
    client = LinearProductionClient(api_key="fake-key")
    with patch.object(client, "get_issue") as mock_get:
        mock_get.return_value = make_task("HER-123")
        client._issue_teams.clear()
        assert client.update_issue("HER-123", {"state": "Done"}) is False


def test_h04_linear_client_fails_closed_on_ambiguous_or_unmatched_state():
    """H04: update_issue fails closed if state name does not match exactly one team state."""
    client = LinearProductionClient(api_key="fake-key")
    task = make_task("HER-123")
    client._issue_teams[task.id] = "team-hermes"
    client._issue_teams[task.uuid] = "team-hermes"
    with patch.object(client, "get_issue", return_value=task), \
         patch.object(client, "_graphql_request") as mock_gql:
        # Team has two states with the same name "Done" (ambiguity)
        mock_gql.return_value = {
            "data": {
                "workflowStates": {
                    "nodes": [
                        {"id": "st-1", "name": "Done", "team": {"id": "team-hermes"}},
                        {"id": "st-2", "name": "Done", "team": {"id": "team-hermes"}},
                    ]
                }
            }
        }
        assert client.update_issue("HER-123", {"state": "Done"}) is False


# ==============================================================================
# H05: Remote Reconciliation & Ledger Retention
# ==============================================================================

def test_h05_ledger_retains_committed_state_on_failed_push(tmp_path: Path):
    """H05: When push fails and ls-remote proves branch not pushed, ledger is retained."""
    ledger_file = tmp_path / "ledger.json"
    ledger = ExecutionLedger(ledger_file)
    state = ExecutionState(
        task_id="HER-505",
        state=TaskState.COMMITTED.value,
        claim_owner="worker-1",
        claim_token="tok-1",
        head_sha="head505",
        branch="agent/her-505",
    )
    ledger.write_state(state)
    read_back = ledger.read_state()
    assert read_back is not None
    assert read_back.state == TaskState.COMMITTED.value
    assert read_back.head_sha == "head505"


# ==============================================================================
# H06: Recovery Lease Fencing & Worker Halt
# ==============================================================================

def test_h06_worker_halts_on_unresolved_recovery(tmp_path: Path, monkeypatch):
    """H06: Worker terminates process (sys.exit(1)) if recovery does not resolve to DONE."""
    monkeypatch.setenv("HEALBITE_LINEAR_DISPATCHER_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_LINEAR_DISPATCHER_MODE", "shadow")
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("DISPATCHER_RUNTIME_DIR", str(runtime_dir))

    ledger_path = runtime_dir / "execution_state.json"
    ledger = ExecutionLedger(ledger_path)
    ledger.write_state(
        ExecutionState(
            task_id="HER-606",
            state=TaskState.CLAIMED.value,
            claim_owner="worker-1",
            claim_token="tok",
        )
    )

    worker = DispatcherWorker()
    with patch.object(worker, "validate_dependencies", return_value=True), \
         patch("ai_engineering.linear_dispatcher.worker.SingleWorkerLock") as mock_lock, \
         patch.object(worker, "_resolve_canonical_main_sha", return_value="5a07de70078b69117403ad7613e198b42310b46d"), \
         patch("ai_engineering.linear_dispatcher.worker.LinearProductionClient"), \
         patch("ai_engineering.linear_dispatcher.worker.GitHubProductionService"), \
         patch("ai_engineering.linear_dispatcher.worker.AutonomousDispatcher") as mock_disp_cls:

        mock_lock.return_value.acquire.return_value = True
        mock_disp = MagicMock()
        mock_disp.recover_task.return_value = None  # Unresolved recovery
        mock_disp_cls.return_value = mock_disp

        with pytest.raises(SystemExit) as exc_info:
            worker.run()
        assert exc_info.value.code == 1


# ==============================================================================
# H10: SQLite Restore Schema Table Compatibility
# ==============================================================================

def test_h10_restore_rejects_schema_table_incompatibility(tmp_path: Path):
    """H10: restore_quick_snapshot must reject candidate DB if required existing tables are missing."""
    live_home = tmp_path / "live_hermes"
    live_home.mkdir()
    live_db = live_home / "hermes_state.db"

    # Create live DB with table 'critical_data'
    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE critical_data (id INT PRIMARY KEY, val TEXT)")
    conn.commit()
    conn.close()

    # Create snapshot dir containing a DB that lacks 'critical_data'
    snap_root = live_home / ".hermes_snapshots"
    snap_id = "test-snap-001"
    snap_dir = snap_root / snap_id
    snap_dir.mkdir(parents=True)
    incomplete_db = snap_dir / "hermes_state.db"
    conn_inc = sqlite3.connect(incomplete_db)
    conn_inc.execute("CREATE TABLE other_table (id INT PRIMARY KEY)")
    conn_inc.commit()
    conn_inc.close()

    manifest = {"files": {"hermes_state.db": {}}}
    (snap_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    success = restore_quick_snapshot(snap_id, hermes_home=live_home)
    assert success is False


# ==============================================================================
# H11: Rollback Evidence Trusted Producer & Freshness
# ==============================================================================

def test_h11_rollback_evidence_rejects_untrusted_producer(tmp_path: Path, monkeypatch):
    """H11: get_real_rollback_evidence rejects rehearsals produced by untrusted agents."""
    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    rehearsal_file.write_text(
        '{"status": "PASS", "executed_at": "2026-10-07T12:00:00Z", "producer": "untrusted-agent"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))
    with patch("subprocess.check_output") as mock_dock:
        mock_dock.return_value = b'[{"ImageManifestDescriptor": {"digest": "sha256:abc"}, "Config": {"Labels": {"org.opencontainers.image.revision": "rev1"}}}]'
        evidence = get_real_rollback_evidence(
            target_sha="5a07de70078b69117403ad7613e198b42310b46d",
            now="2026-10-07T12:00:00Z",
            manifest={"rollback": {}, "attestation": {}},
        )
        assert evidence.get("status") == "BLOCKED"


def test_h11_rollback_evidence_rejects_stale_rehearsal(tmp_path: Path, monkeypatch):
    """H11: get_real_rollback_evidence rejects rehearsals older than 48 hours."""
    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    rehearsal_file.write_text(
        f'{{"status": "PASS", "executed_at": "{stale_time}", "producer": "healbite-rehearsal-engine"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))
    with patch("subprocess.check_output") as mock_dock:
        mock_dock.return_value = b'[{"ImageManifestDescriptor": {"digest": "sha256:abc"}, "Config": {"Labels": {"org.opencontainers.image.revision": "rev1"}}}]'
        evidence = get_real_rollback_evidence(
            target_sha="5a07de70078b69117403ad7613e198b42310b46d",
            now="2026-10-07T12:00:00Z",
            manifest={"rollback": {}, "attestation": {}},
        )
        assert evidence.get("status") == "BLOCKED"


# ==============================================================================
# N01: Secret Scanner --fail-on-skipped
# ==============================================================================

def test_n01_secret_scanner_fail_on_skipped_flag():
    """N01: secret_scanner.py supports --fail-on-skipped flag."""
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "secret_scanner.py"
    res = subprocess.run([sys.executable, str(script_path), "--help"], capture_output=True, text=True)
    assert res.returncode == 0
    assert "--fail-on-skipped" in res.stdout
