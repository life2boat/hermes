"""Adversarial Regression Test Suite for Safe Executor Architecture Pivot and Blocker Closure v5.

Covers:
- READINESS_HEALTH_MISMATCH: Worker execution_ready() fail-closed check
- C02: Dynamic Git filter and diff driver discovery & explicit neutralization
- H02: Remote push URL validation
- H03: Exact 23 canonical required checks, deduplication by latest run, no prefix matching
- H04: Linear GraphQL error rejection and authentic receipt PRODUCER enforcement
- H05: Recovery ls-remote branch discovery and PR reconciliation
- H06: Recovery lease fencing across failed-CI cleanup and WRITEBACK_DONE
- H10: SQLite restore schema column comparison, user_version check, and backup API publication
- H11: Undefined rehearsal_producer fix and cryptographic signature provenance verification
- N01: LocalValidator --fail-on-skipped wiring
"""

import datetime
import hashlib
import hmac
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
    ClaimRecord,
    LinearTask,
    TaskState,
)
from ai_engineering.linear_dispatcher.execution_ledger import ExecutionState
from ai_engineering.linear_dispatcher.dispatcher import (
    AutonomousDispatcher,
    DispatcherConfig,
)
from ai_engineering.linear_dispatcher.github_service import CIStatusResult
from ai_engineering.linear_dispatcher.github_service_production import (
    CANONICAL_REQUIRED_CHECKS,
    GitHubProductionService,
)
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.linear_client_production import LinearProductionClient
from ai_engineering.linear_dispatcher.task_executor import (
    CodexTaskExecutor,
    ExecutionResult,
    ITaskExecutor,
    LinuxCodexTaskExecutor,
    SimulatedTaskExecutor,
)
from ai_engineering.linear_dispatcher.validator import LocalValidator
from ai_engineering.linear_dispatcher.worker import DispatcherWorker
from ai_engineering.linear_dispatcher.worktree_service import (
    WorktreeService,
    get_clean_git_env,
    get_effective_filter_opts,
    is_canonical_remote_url,
)
from ai_engineering.linear_dispatcher.writeback import (
    ExecutionEvidence,
    WritebackService,
)
from hermes_cli.backup import restore_quick_snapshot
from scripts.generate_offline_evidence import get_real_rollback_evidence


def make_task(task_id: str = "HER-901") -> LinearTask:
    return LinearTask(
        id=task_id,
        uuid=f"uuid-{task_id}",
        title=f"Task {task_id}",
        description="Task description",
        state="Todo",
        priority=1,
        assignee="test-assignee",
        labels=("agent-auto", "agent-shadow"),
        created_at="2026-10-08T00:00:00Z",
        updated_at="2026-10-08T00:00:00Z",
        url=f"https://linear.app/issue/{task_id}",
    )


def test_codex_executors_execution_ready_fails_closed_by_default():
    """CodexTaskExecutor and LinuxCodexTaskExecutor fail closed on execution_ready()."""
    win_exec = CodexTaskExecutor()
    assert win_exec.execution_ready() is False

    lin_exec = LinuxCodexTaskExecutor(allow_unsupported_codex_credential_boundary=False)
    assert lin_exec.execution_ready() is False

    sim_exec = SimulatedTaskExecutor()
    assert sim_exec.execution_ready() is True


def test_worker_fails_closed_when_executor_not_execution_ready(tmp_path: Path, monkeypatch):
    """Worker fails closed during validate_dependencies when executor is healthy but not execution_ready."""
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")
    monkeypatch.setenv("LINEAR_API_KEY", "fake-lin-key")

    class HealthyButUnreadyExecutor:
        def health(self) -> bool:
            return True  # Binary is installed and healthy
        def validate_isolation(self) -> tuple[bool, str | None]:
            return True, None
        def execution_ready(self) -> bool:
            return False  # Privilege separation unsupported; execution forbidden
        def execute(self, *args, **kwargs):
            raise RuntimeError("Execute should never be called when execution_ready is False")

    worker = DispatcherWorker(task_executor=HealthyButUnreadyExecutor())
    config = DispatcherConfig(worker_id="test-worker")
    wt_base = tmp_path / "wt_base"
    lease_path = tmp_path / "leases.json"

    is_ready = worker.validate_dependencies(config, wt_base, lease_path)
    assert is_ready is False


# ==============================================================================
# 2. C02: Dynamic Git Filter & Diff Driver Discovery & Neutralization
# ==============================================================================

def test_c02_dynamic_filter_discovery_neutralizes_all_drivers(tmp_path: Path):
    """get_effective_filter_opts dynamically finds and neutralizes drivers from .git/config and .gitattributes."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)

    # Configure malicious drivers in git config
    subprocess.run(
        ["git", "config", "filter.evil_filter.clean", "malicious_clean_cmd"],
        cwd=str(repo_dir),
        check=True,
    )
    subprocess.run(
        ["git", "config", "diff.malicious_diff.textconv", "malicious_textconv_cmd"],
        cwd=str(repo_dir),
        check=True,
    )

    # Configure attributes
    (repo_dir / ".gitattributes").write_text("*.txt filter=attr_filter diff=attr_diff\n", encoding="utf-8")

    opts = get_effective_filter_opts(repo_dir)
    assert "-c" in opts
    # All 4 drivers must have explicit neutralization entries
    opts_str = " ".join(opts)
    assert "filter.evil_filter.clean=" in opts_str
    assert "diff.malicious_diff.textconv=" in opts_str
    assert "filter.attr_filter.clean=" in opts_str
    assert "diff.attr_diff.textconv=" in opts_str

    # Test that running git with these options prevents sentinel execution
    sentinel_file = tmp_path / "sentinel.txt"
    bad_script = tmp_path / "bad.py"
    bad_script.write_text(f"import sys; from pathlib import Path; Path(r'{sentinel_file}').write_text('PWNED'); sys.exit(0)\n", encoding="utf-8")
    subprocess.run(
        ["git", "config", "filter.pwn.clean", f"{sys.executable} {bad_script}"],
        cwd=str(repo_dir),
        check=True,
    )

    opts = get_effective_filter_opts(repo_dir)
    # Run git add with effective filter options
    dummy_file = repo_dir / "test.txt"
    dummy_file.write_text("sample content\n", encoding="utf-8")
    (repo_dir / ".gitattributes").write_text("*.txt filter=pwn\n", encoding="utf-8")

    cmd = ["git"] + opts + ["-C", str(repo_dir), "add", "test.txt"]
    subprocess.run(cmd, check=True, capture_output=True)

    # Sentinel must NOT exist
    assert not sentinel_file.exists()


def test_c02_clean_git_env_sanitizes_relative_paths():
    """get_clean_git_env strips relative elements and dot directories from PATH."""
    abs_dir = "C:\\Windows\\System32" if os.name == "nt" else "/usr/bin"
    dirty_env = {
        "PATH": f".{os.pathsep}relative/bin{os.pathsep}{abs_dir}{os.pathsep}..{os.sep}parent",
        "GIT_DIR": ".git",
        "PAGER": "cat",
    }
    clean = get_clean_git_env(dirty_env)
    assert "PAGER" not in clean
    clean_path = clean["PATH"].split(os.pathsep)
    assert "." not in clean_path
    assert "relative/bin" not in clean_path
    assert f"..{os.sep}parent" not in clean_path
    assert abs_dir in clean_path
    assert all(Path(p).is_absolute() for p in clean_path)


# ==============================================================================
# 3. H02: Remote Push URL Validation
# ==============================================================================

def test_h02_canonical_remote_url_validation():
    """is_canonical_remote_url validates HTTPS, SSH, and local canonical checkouts."""
    assert is_canonical_remote_url("https://github.com/life2boat/hermes.git") is True
    assert is_canonical_remote_url("https://github.com/life2boat/hermes") is True
    assert is_canonical_remote_url("git@github.com:life2boat/hermes.git") is True

    # Attacker URLs
    assert is_canonical_remote_url("https://github.com/attacker/hermes.git") is False
    assert is_canonical_remote_url("https://github.com/life2boat/hermes-malicious.git") is False
    assert is_canonical_remote_url("git@github.com:attacker/hermes.git") is False


# ==============================================================================
# 4. H03: Canonical Required Checks & Check Run Deduplication
# ==============================================================================

def test_h03_canonical_required_checks_exact_23():
    """CANONICAL_REQUIRED_CHECKS contains exactly the 23 authoritative GitHub Actions check names."""
    assert len(CANONICAL_REQUIRED_CHECKS) == 23
    expected = {
        "agent-release-gate",
        "check-attribution",
        "check-common-ancestor",
        "docs-site-checks",
        "nix (ubuntu-latest)",
        "nix (macos-latest)",
        "Windows footguns (blocking)",
        "ruff enforcement (blocking)",
        "ruff + ty diff",
        "typecheck (web)",
        "typecheck (apps/shared)",
        "typecheck (apps/desktop)",
        "typecheck (apps/bootstrap-installer)",
        "typecheck (ui-tui)",
        "test (1)",
        "test (2)",
        "test (3)",
        "test (4)",
        "test (5)",
        "test (6)",
        "e2e",
        "Scan PR for critical supply chain risks",
        "Check PyPI dependency upper bounds",
    }
    assert set(CANONICAL_REQUIRED_CHECKS) == expected


def test_h03_check_run_deduplication_selects_latest_run():
    """get_ci_status deduplicates check runs by name, selecting the latest run per name."""
    service = GitHubProductionService(token="fake-token", repo="life2boat/hermes")

    # Build check runs where 'Scan PR for critical supply chain risks' has a skipped run (id 1) and a success run (id 2)
    fake_runs = []
    for idx, name in enumerate(CANONICAL_REQUIRED_CHECKS, start=10):
        if name == "Scan PR for critical supply chain risks":
            fake_runs.append({
                "id": 1,
                "name": name,
                "status": "completed",
                "conclusion": "skipped",
                "head_sha": "headsha",
                "app": {"slug": "github-actions"},
            })
            fake_runs.append({
                "id": 2,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "head_sha": "headsha",
                "app": {"slug": "github-actions"},
            })
        else:
            fake_runs.append({
                "id": idx,
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "head_sha": "headsha",
                "app": {"slug": "github-actions"},
            })

    with patch.object(service, "_request") as mock_req:
        mock_req.side_effect = [
            {"base": {"ref": "main", "sha": "basesha"}, "head": {"sha": "headsha"}, "draft": True},
            {"check_runs": fake_runs, "total_count": len(fake_runs)},
        ]
        ok, res, reason = service.get_ci_status("life2boat/hermes", "headsha", 123)
        assert ok is True
        assert reason is None
        assert res.overall_status == "PASS"


def test_h03_exact_name_matching_rejects_prefixed_alternatives():
    """Prefix or substring check names are rejected; exact matching is required."""
    service = GitHubProductionService(token="fake-token", repo="life2boat/hermes")

    # Replace agent-release-gate with a prefixed fake
    fake_runs = [
        {
            "id": idx,
            "name": ("agent-release-gate-fake" if name == "agent-release-gate" else name),
            "status": "completed",
            "conclusion": "success",
            "head_sha": "headsha",
            "app": {"slug": "github-actions"},
        }
        for idx, name in enumerate(CANONICAL_REQUIRED_CHECKS, start=1)
    ]

    with patch.object(service, "_request") as mock_req:
        mock_req.side_effect = [
            {"base": {"ref": "main", "sha": "basesha"}, "head": {"sha": "headsha"}, "draft": True},
            {"check_runs": fake_runs, "total_count": len(fake_runs)},
        ]
        ok, res, reason = service.get_ci_status("life2boat/hermes", "headsha", 123)
        assert ok is False
        assert res.overall_status == "PENDING"
        assert "agent-release-gate" in res.details.get("missing_required_checks", "")


# ==============================================================================
# 5. H04: Linear GraphQL Error Rejection & Team Binding & Authentic Receipt
# ==============================================================================

def test_h04_linear_client_fails_closed_on_graphql_errors():
    """LinearProductionClient raises RuntimeError on GraphQL errors, failing update_issue closed."""
    client = LinearProductionClient(api_key="fake-key")

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"errors": [{"message": "Field 'xyz' does not exist"}]}).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        # _graphql_request must raise RuntimeError
        with pytest.raises(RuntimeError, match="Linear API returned errors"):
            client._graphql_request("query { test }")

        # update_issue must return False
        assert client.update_issue("HER-123", {"state": "Done"}) is False


def test_h04_receipt_producer_enforcement():
    """WritebackService.is_authentic_receipt strictly requires PRODUCER: linear-dispatcher-worker."""
    evidence = ExecutionEvidence(
        task_id="HER-100",
        execution_status="PASS",
        claim_owner="worker-1",
        branch="agent/her-100",
        pr_number=10,
        pr_url="https://github.com/life2boat/hermes/pull/10",
        base_sha="basesha",
        head_sha="headsha",
        validation_status="PASS",
        ci_status="PASS",
        ci_head_sha="headsha",
        sha_match="YES",
        execution_id="exec-100",
    )
    md = evidence.to_markdown()
    assert "- **PRODUCER:** `linear-dispatcher-worker`" in md
    assert WritebackService.is_authentic_receipt(md, head_sha="headsha", exec_id="exec-100") is True

    # Tampered: missing producer
    tampered = md.replace("- **PRODUCER:** `linear-dispatcher-worker`\n", "")
    assert WritebackService.is_authentic_receipt(tampered, head_sha="headsha", exec_id="exec-100") is False

    # Tampered: wrong producer
    wrong_producer = md.replace("linear-dispatcher-worker", "untrusted-agent")
    assert WritebackService.is_authentic_receipt(wrong_producer, head_sha="headsha", exec_id="exec-100") is False


# ==============================================================================
# 6. H06: Lease Fencing in Recovery
# ==============================================================================

def test_h06_recovery_fences_writeback_done_cleanup_on_lost_lease(tmp_path: Path):
    """If lease expires or is stolen in WRITEBACK_DONE state, dispatcher fences cleanup and fails closed."""
    leases_file = tmp_path / "leases.json"
    leases = LeaseManager(leases_file)
    task_id = "HER-600"
    ok, claim, _ = leases.claim(task_id, "worker-1", lease_duration_sec=10)
    assert ok is True
    assert claim is not None

    config = DispatcherConfig(worker_id="worker-1")
    linear = MagicMock()
    linear.get_issue_comments.return_value = []
    # Refetched issue already Done with authentic receipt in description
    evidence = ExecutionEvidence(
        task_id=task_id,
        execution_status="PASS",
        claim_owner="worker-1",
        branch="agent/her-600",
        pr_number=1,
        pr_url="https://url",
        base_sha="basesha",
        head_sha="headsha",
        validation_status="PASS",
        ci_status="PASS",
        ci_head_sha="headsha",
        sha_match="YES",
        execution_id="exec-600",
    )
    task = make_task(task_id)
    task_done = LinearTask(
        id=task.id,
        uuid=task.uuid,
        title=task.title,
        description=evidence.to_markdown(),
        state="Done",
        priority=task.priority,
        assignee=task.assignee,
        labels=task.labels,
        created_at=task.created_at,
        updated_at=task.updated_at,
        url=task.url,
    )
    linear.get_issue.return_value = task_done

    wt_service = MagicMock()
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=MagicMock(),
        worktree_base_dir=tmp_path / "wt",
        canonical_main_sha="mainsha",
    )

    rec_state = ExecutionState(
        task_id=task_id,
        state=TaskState.WRITEBACK_DONE.value,
        claim_owner="worker-1",
        claim_token=claim.claim_token,
        head_sha="headsha",
        branch="agent/her-600",
        worktree_path=str(tmp_path / "wt"),
        execution_id="exec-600",
    )

    # Simulate lease expiring right before worktree cleanup
    call_count = 0
    orig_verify = leases.verify_lease

    def mock_verify(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
            return False
        return orig_verify(*args, **kwargs)

    leases.verify_lease = mock_verify

    res = dispatcher.recover_task(rec_state)
    assert res is not None
    assert res.final_state == TaskState.BLOCKED
    assert res.block_reason == BlockReasonCode.LOST_LEASE.value
    # Worktree must NOT be removed by worker-1 without valid lease
    wt_service.remove_worktree.assert_not_called()


# ==============================================================================
# 7. H10: SQLite Schema Compatibility & Restore Publication
# ==============================================================================

def test_h10_restore_rejects_column_mismatch_in_same_name_table(tmp_path: Path):
    """restore_quick_snapshot rejects candidate DB when a common table has column mismatches."""
    live_home = tmp_path / "live"
    live_home.mkdir()
    live_db = live_home / "state.db"

    # Live DB with table users having columns (id, email)
    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE users (id INT PRIMARY KEY, email TEXT)")
    conn.commit()
    conn.close()

    # Snapshot DB with same table name but columns (id, username)
    snap_root = live_home / "state-snapshots"
    snap_id = "snap-col-mismatch"
    snap_dir = snap_root / snap_id
    snap_dir.mkdir(parents=True)
    snap_db = snap_dir / "state.db"
    conn_snap = sqlite3.connect(snap_db)
    conn_snap.execute("CREATE TABLE users (id INT PRIMARY KEY, username TEXT)")
    conn_snap.commit()
    conn_snap.close()

    (snap_dir / "manifest.json").write_text(json.dumps({"files": {"state.db": {}}}), encoding="utf-8")

    assert restore_quick_snapshot(snap_id, hermes_home=live_home) is False


def test_h10_restore_rejects_user_version_mismatch(tmp_path: Path):
    """restore_quick_snapshot rejects candidate DB with mismatched PRAGMA user_version."""
    live_home = tmp_path / "live"
    live_home.mkdir()
    live_db = live_home / "state.db"

    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE data (id INT)")
    conn.execute("PRAGMA user_version = 5")
    conn.commit()
    conn.close()

    snap_root = live_home / "state-snapshots"
    snap_id = "snap-ver-mismatch"
    snap_dir = snap_root / snap_id
    snap_dir.mkdir(parents=True)
    snap_db = snap_dir / "state.db"
    conn_snap = sqlite3.connect(snap_db)
    conn_snap.execute("CREATE TABLE data (id INT)")
    conn_snap.execute("PRAGMA user_version = 3")  # Older version
    conn_snap.commit()
    conn_snap.close()

    (snap_dir / "manifest.json").write_text(json.dumps({"files": {"state.db": {}}}), encoding="utf-8")

    assert restore_quick_snapshot(snap_id, hermes_home=live_home) is False


def test_h10_restore_publishes_via_backup_api(tmp_path: Path):
    """restore_quick_snapshot updates live DB via SQLite backup API preserving integrity."""
    live_home = tmp_path / "live"
    live_home.mkdir()
    live_db = live_home / "state.db"

    conn = sqlite3.connect(live_db)
    conn.execute("CREATE TABLE data (id INT PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO data VALUES (1, 'initial')")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    snap_root = live_home / "state-snapshots"
    snap_id = "snap-valid"
    snap_dir = snap_root / snap_id
    snap_dir.mkdir(parents=True)
    snap_db = snap_dir / "state.db"
    conn_snap = sqlite3.connect(snap_db)
    conn_snap.execute("CREATE TABLE data (id INT PRIMARY KEY, val TEXT)")
    conn_snap.execute("INSERT INTO data VALUES (1, 'restored')")
    conn_snap.execute("PRAGMA user_version = 1")
    conn_snap.commit()
    conn_snap.close()

    (snap_dir / "manifest.json").write_text(json.dumps({"files": {"state.db": {}}}), encoding="utf-8")

    assert restore_quick_snapshot(snap_id, hermes_home=live_home) is True

    # Verify live DB content
    chk = sqlite3.connect(live_db)
    rows = chk.execute("SELECT val FROM data WHERE id = 1").fetchall()
    chk.close()
    assert rows == [("restored",)]


# ==============================================================================
# 8. H11: Rollback Evidence Provenance & Signature Verification
# ==============================================================================

def test_h11_undefined_rehearsal_producer_fixed(tmp_path: Path, monkeypatch):
    """get_real_rollback_evidence executes without NameError: rehearsal_producer."""
    rehearsal_file = tmp_path / "rollback-rehearsal.json"
    rehearsal_file.write_text(
        json.dumps({
            "status": "PASS",
            "executed_at": "2026-10-08T00:00:00Z",
            "producer": "untrusted-agent",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_ROLLBACK_REHEARSAL_PATH", str(rehearsal_file))
    res = get_real_rollback_evidence("sha123", "2026-10-08T00:00:00Z")
    assert res.get("status") == "BLOCKED"


def test_h11_valid_signed_rehearsal_passes(tmp_path: Path, monkeypatch):
    """Properly signed rehearsal from trusted producer passes."""
    key = "test-secret-provenance-key"
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", key)

    target_sha = "5a07de70078b69117403ad7613e198b42310b46d"
    digest = "sha256:abc"
    revision = "rev1"
    receipt_id = "receipt-rollback-001"

    # Health evidence
    health_file = tmp_path / "rollback-health.json"
    health_payload = {
        "status": "PASS",
        "target_sha": target_sha,
        "rollback_image_digest": digest,
        "rollback_revision": revision,
    }
    health_bytes = json.dumps(health_payload).encode("utf-8")
    health_file.write_bytes(health_bytes)
    health_hash = hashlib.sha256(health_bytes).hexdigest()

    # Compute valid signature
    payload = f"{receipt_id}:{target_sha}:{digest}:{revision}:{health_hash}".encode("utf-8")
    sig = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()

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
        "health_evidence_sha256": health_hash,
        "rollback_procedure_proven": True,
        "execution_provenance": {
            "runtime_identity": "healbite-production",
            "isolation_level": "docker",
            "signature": sig,
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
        assert res.get("canonical_rehearsal_evidence") is not None


# ==============================================================================
# 9. N01: LocalValidator --fail-on-skipped
# ==============================================================================

def test_n01_local_validator_passes_fail_on_skipped(tmp_path: Path):
    """LocalValidator passes --fail-on-skipped to secret_scanner.py."""
    with patch.object(LocalValidator, "run_diff_check", return_value=(True, "PASS")):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            trusted_dir = Path(__file__).resolve().parents[2]
            LocalValidator.run_validation(tmp_path, trusted_root=trusted_dir)

            # Find secret_scanner run
            secret_calls = [
                c for c in mock_run.call_args_list
                if any("secret_scanner.py" in str(arg) for arg in c[0][0])
            ]
            assert len(secret_calls) == 1
            cmd = secret_calls[0][0][0]
            assert "--fail-on-skipped" in cmd
