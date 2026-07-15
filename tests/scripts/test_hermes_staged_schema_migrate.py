from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import Callable
import pytest

from scripts import healbite_schema_migrate
from scripts import hermes_staged_schema_migrate as staged


REVISION = "1" * 40
IMAGE_ID = "sha256:" + "2" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "3" * 64


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _source(root: Path) -> Path:
    parent = _private_directory(root / "source")
    path = parent / "database.sqlite"
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        conn.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    os.chmod(path, 0o600)
    return path


def _args(root: Path, source: Path, **overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "source_db": str(source),
        "backup_dir": str(_private_directory(root / "backups")),
        "staging_root": str(_private_directory(root / "staging")),
        "target_image_id": IMAGE_ID,
        "previous_image_id": PREVIOUS_IMAGE_ID,
        "expected_source_revision": REVISION,
        "synthetic_root": str(root),
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _prepare_runtime_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    uid_getter = getattr(os, "geteuid", None)
    gid_getter = getattr(os, "getegid", None)
    assert callable(uid_getter) and callable(gid_getter)
    monkeypatch.setattr(staged, "RUNTIME_UID", int(uid_getter()))
    monkeypatch.setattr(staged, "RUNTIME_GID", int(gid_getter()))


def _host_migration(_contract: staged.Contract, staging_dir: Path) -> None:
    result = healbite_schema_migrate.run_migration(
        db_path=str(staging_dir / "database.sqlite"),
        staged_copy=True,
    )
    assert result.exit_code == 0
    assert result.path_mode == "STAGED_COPY"


def _failure_at(selected: str) -> Callable[[str, str], None]:
    def callback(phase: str, publish_state: str) -> None:
        if phase == selected:
            raise staged.OrchestratorError(f"INJECTED_{phase.upper()}_FAILURE", publish_state=publish_state)

    return callback


def test_public_orchestrator_has_no_production_execute_mode() -> None:
    parser = staged.build_parser()
    help_text = parser.format_help()

    assert "execute-synthetic" in help_text
    assert "execute-production" not in help_text


def test_public_parsers_do_not_expose_fault_injection() -> None:
    migration_help = healbite_schema_migrate.build_parser().format_help()
    staged_parser = staged.build_parser()
    subparsers = staged_parser._subparsers._group_actions[0]
    synthetic_help = subparsers.choices["execute-synthetic"].format_help()

    assert "--test-crash-after" not in migration_help
    assert "--test-crash-after" not in synthetic_help
    assert "--test-fail-phase" not in synthetic_help


def test_transaction_crash_worker_reaches_live_transaction_without_commit(tmp_path: Path) -> None:
    source = _source(tmp_path)
    evidence_path = tmp_path / "transaction-evidence.json"
    before = staged._sha256(source)
    worker = Path(__file__).with_name("staged_migration_crash_worker.py")

    result = subprocess.run(
        [
            sys.executable,
            str(worker),
            "--mode",
            "crash-active",
            "--db-path",
            str(source),
            "--evidence-path",
            str(evidence_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert evidence_path.exists(), (result.returncode, result.stderr)
    evidence = json.loads(evidence_path.read_text(encoding="ascii"))
    assert result.returncode == 137
    assert result.stdout == ""
    assert result.stderr == ""
    assert evidence["CRASH_HOOK_REQUESTED"] is True
    assert evidence["CRASH_HOOK_REACHED"] is True
    assert evidence["BEGIN_IMMEDIATE_CONFIRMED"] is True
    assert evidence["SQLITE_TRANSACTION_ACTIVE"] is True
    assert evidence["JOURNAL_EXISTS_OR_WRITE_LOCK_HELD"] is True
    assert evidence["COMMIT_NOT_COMPLETED"] is True
    assert evidence["MIGRATION_COMMIT_STATE"] == "NOT_COMPLETED"
    assert staged._sha256(source) == before
    assert staged._sqlite_validation(source) == ("ok", 0)


def test_plan_is_read_only_and_reports_quiescence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    before = (staged._sha256(source), source.stat().st_ino, tuple(tmp_path.rglob("*")))
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)

    assert staged.plan(args) == 0

    after = (staged._sha256(source), source.stat().st_ino, tuple(tmp_path.rglob("*")))
    payload = json.loads(capsys.readouterr().out)
    assert after == before
    assert payload["plan_read_only"] is True
    assert payload["production_execution_enabled"] is False


def test_quiescence_refuses_active_reader(tmp_path: Path) -> None:
    source = _source(tmp_path)
    reader = sqlite3.connect(source)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM legacy_rows").fetchall()
    try:
        with pytest.raises(staged.OrchestratorError, match="SOURCE_NOT_QUIESCENT"):
            staged._check_quiescent(source)
    finally:
        reader.rollback()
        reader.close()


def test_quiescence_refuses_active_writer(tmp_path: Path) -> None:
    source = _source(tmp_path)
    writer = sqlite3.connect(source)
    writer.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(staged.OrchestratorError, match="SOURCE_NOT_QUIESCENT"):
            staged._check_quiescent(source)
    finally:
        writer.rollback()
        writer.close()


def test_quiescence_refuses_existing_sidecar(tmp_path: Path) -> None:
    source = _source(tmp_path)
    sidecar = Path(f"{source}-journal")
    sidecar.touch()

    with pytest.raises(staged.OrchestratorError, match="SQLITE_SIDECAR_PRESENT"):
        staged._check_quiescent(source)


def test_manifest_rejects_unknown_and_non_monotonic_states(tmp_path: Path) -> None:
    manifest = staged.DurableManifest(tmp_path / "manifest.json", {"STATE": "PLANNED"})
    manifest.transition("BACKED_UP")

    with pytest.raises(staged.OrchestratorError, match="UNKNOWN_MANIFEST_STATE"):
        manifest.transition("NOT_A_STATE")
    with pytest.raises(staged.OrchestratorError, match="NON_MONOTONIC_MANIFEST_STATE"):
        manifest.transition("PLANNED")


def test_execute_synthetic_publishes_only_validated_staging_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    original_hash = staged._sha256(source)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)
    assert staged.execute_synthetic(
        args,
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    manifests = list((tmp_path / "backups").glob("manifest-*.json"))
    backups = list((tmp_path / "backups").glob("backup-*.sqlite"))
    assert payload["publish_state"] == "VERIFIED"
    assert payload["migration_runs"] == 3
    assert staged._sha256(source) != original_hash
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["STATE"] == "VERIFIED"
    assert len(backups) == 1
    assert staged._sha256(backups[0]) == original_hash
    assert staged._sqlite_validation(source) == ("ok", 0)
    assert stat.S_IMODE(source.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "phase",
    ("household", "weekly", "shopping", "integrity", "previous_compatibility", "atomic_publish"),
)
def test_pre_publish_failures_leave_target_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    original_hash = staged._sha256(source)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)
    assert staged.execute_synthetic(
        args,
        _failure_callback=_failure_at(phase),
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["publish_state"] == "NOT_PUBLISHED"
    assert staged._sha256(source) == original_hash
    assert staged._sqlite_validation(source) == ("ok", 0)


def test_post_publish_fsync_failure_is_unknown_and_not_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    original_hash = staged._sha256(source)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)
    assert staged.execute_synthetic(
        args,
        _failure_callback=_failure_at("target_dir_fsync"),
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["publish_state"] == "UNKNOWN"
    assert payload["automatic_retry_allowed"] is False
    assert payload["manual_recovery_required"] is True
    assert staged._sha256(source) != original_hash
    assert staged._sqlite_validation(source) == ("ok", 0)


def test_cross_filesystem_publish_is_refused_when_second_filesystem_available(tmp_path: Path) -> None:
    other_root = Path("/dev/shm")
    if not other_root.is_dir() or other_root.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("no second writable filesystem")
    source = _source(tmp_path)
    backup_dir = _private_directory(tmp_path / "backups")
    staging_root = other_root / f"healbite-staged-test-{os.getpid()}"
    staging_root.mkdir(mode=0o700)
    try:
        contract = staged.Contract(
            source_db=source,
            backup_dir=backup_dir,
            staging_root=staging_root,
            target_image_id=IMAGE_ID,
            previous_image_id=PREVIOUS_IMAGE_ID,
            expected_source_revision=REVISION,
            synthetic_root=None,
        )
        with pytest.raises(staged.OrchestratorError, match="CROSS_FILESYSTEM_PUBLISH_REFUSED"):
            staged._preflight(contract, synthetic=False, inspect_images=False)
    finally:
        staging_root.rmdir()
