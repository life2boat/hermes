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
FAILURE_MATRIX = Path(__file__).with_name("staged_migration_failure_matrix.py")
CRASH_MATRIX = Path(__file__).with_name("staged_migration_crash_matrix.py")


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


def _fully_migrated_source(root: Path) -> Path:
    source = _source(root)
    result = healbite_schema_migrate.run_migration(
        db_path=str(source),
        staged_copy=True,
    )
    assert result.exit_code == 0
    assert result.path_mode == "STAGED_COPY"
    return source


def test_target_schema_fingerprint_matches_canonical_migration(
    tmp_path: Path,
) -> None:
    source = _fully_migrated_source(tmp_path)
    contract = staged._target_schema_contract()

    assert staged._target_schema_fingerprint(source) == contract.fingerprint
    assert contract.version == f"healbite-schema-{contract.fingerprint[:16]}"


def test_target_schema_fingerprint_rejects_missing_required_object(
    tmp_path: Path,
) -> None:
    source = _fully_migrated_source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "DROP TABLE household_shopping_item_contributions"
        )

    with pytest.raises(
        staged.OrchestratorError,
        match="TARGET_SCHEMA_CONTRACT_MISMATCH",
    ):
        staged._target_schema_fingerprint(source)


def test_target_schema_fingerprint_rejects_incompatible_object_definition(
    tmp_path: Path,
) -> None:
    source = _fully_migrated_source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "DROP INDEX "
            "idx_weekly_menu_ingredients_entry_position_unique"
        )
        connection.execute(
            "CREATE UNIQUE INDEX "
            "idx_weekly_menu_ingredients_entry_position_unique "
            "ON household_weekly_menu_entry_ingredients "
            "(position, menu_entry_id)"
        )

    with pytest.raises(
        staged.OrchestratorError,
        match="TARGET_SCHEMA_CONTRACT_MISMATCH",
    ):
        staged._target_schema_fingerprint(source)


def _minimal_idempotent_migration(_contract: staged.Contract, staging_dir: Path) -> None:
    with sqlite3.connect(staging_dir / "database.sqlite") as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS target_schema_marker (value TEXT)")


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
    source = Path(staged.__file__).read_text(encoding="utf-8")
    assert "HERMES_STAGED_FAILURE" not in source
    assert "HERMES_STAGED_CRASH" not in source


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
    assert payload["publish_state"] == "FINAL_VERIFIED"
    assert payload["atomic_primitive"] == "renameat2_RENAME_EXCHANGE"
    assert payload["source_sqlite_lease_acquired"] is True
    assert payload["staging_sqlite_lease_acquired"] is True
    assert payload["source_lease_held_through_final_verification"] is True
    assert payload["staging_lease_held_through_final_verification"] is True
    assert payload["leases_held_through_final_verification"] is True
    assert payload["poll_only_quiescence_used"] is False
    assert payload["source_fd_identity_pinned"] is True
    assert payload["staging_fd_identity_pinned"] is True
    assert payload["target_parent_fd_pinned"] is True
    assert payload["migration_runs"] == 3
    assert staged._sha256(source) != original_hash
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["STATE"] == "VERIFIED"
    assert len(backups) == 1
    assert staged._sha256(backups[0]) == original_hash
    displaced = list((tmp_path / "staging").glob("staging-*/database.sqlite"))
    assert len(displaced) == 1
    assert staged._sha256(displaced[0]) == original_hash
    assert staged._sqlite_validation(source) == ("ok", 0)
    assert stat.S_IMODE(source.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "phase",
    (
        "backup_creation",
        "backup_file_fsync",
        "backup_directory_fsync",
        "staging_creation",
        "staging_file_fsync",
        "staging_directory_fsync",
        "integrity_validation",
        "foreign_key_validation",
        "previous_image_startup",
        "pre_publish_cleanup",
    ),
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
    assert payload["publish_state"] == "BEFORE_EXCHANGE"
    assert payload["cleanup_failed"] is False
    assert staged._sha256(source) == original_hash
    assert staged._sqlite_validation(source) == ("ok", 0)
    assert list((tmp_path / "staging").glob("staging-*")) == []
    assert len(list((tmp_path / "backups").glob("backup-*.sqlite"))) <= 1
    assert len(list((tmp_path / "backups").glob("manifest-*.json"))) == 1


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
        _failure_callback=_failure_at("target_parent_fsync"),
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "PUBLISH_UNCERTAIN"
    assert payload["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert payload["publish_state"] == "EXCHANGE_VERIFIED_NOT_FSYNCED"
    assert payload["target_may_have_changed"] is True
    assert payload["automatic_retry_allowed"] is False
    assert payload["manual_recovery_required"] is True
    assert staged._sha256(source) != original_hash
    assert staged._sqlite_validation(source) == ("ok", 0)
    assert len(list((tmp_path / "staging").glob("staging-*"))) == 1


def _external_sqlite_attempt(path: Path, mode: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); "
        "\ntry:\n"
        " c.execute('BEGIN IMMEDIATE' if sys.argv[2]=='writer' else 'BEGIN'); "
        " c.execute('SELECT COUNT(*) FROM sqlite_master').fetchone(); sys.exit(0)\n"
        "except sqlite3.OperationalError as e:\n"
        " code=getattr(e,'sqlite_errorcode',None); "
        " sys.exit(75 if isinstance(code,int) and (code & 255) in "
        "(sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) else 76)\n"
        "finally:\n c.close()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code, str(path), mode],
        text=True,
        capture_output=True,
        check=False,
    )


def test_sqlite_leases_refuse_late_readers_and_writers_at_eight_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)
    expected_boundaries = (
        "source_lease_acquired",
        "backup_complete",
        "staging_copy_complete",
        "migration_complete",
        "validation_complete",
        "previous_startup_complete",
        "staging_lease_acquired",
        "final_verification",
    )
    observed: list[str] = []
    reader_refused = 0
    writer_refused = 0

    def assert_locked(name: str, path: Path) -> None:
        nonlocal reader_refused, writer_refused
        observed.append(name)
        reader = _external_sqlite_attempt(path, "reader")
        writer = _external_sqlite_attempt(path, "writer")
        assert reader.returncode == 75, (name, reader.returncode, reader.stderr)
        assert writer.returncode == 75, (name, writer.returncode, writer.stderr)
        reader_refused += 1
        writer_refused += 1

    assert staged.execute_synthetic(
        args,
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
        _lifecycle_callback=assert_locked,
    ) == 0
    capsys.readouterr()
    assert tuple(observed) == expected_boundaries
    assert reader_refused == 8
    assert writer_refused == 8


def test_staging_change_after_compatibility_probe_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    original_hash = staged._sha256(source)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)

    def mutate_staging(name: str, _path: Path) -> None:
        if name != "previous_startup_complete":
            return
        staging_db = next((tmp_path / "staging").glob("staging-*/database.sqlite"))
        with sqlite3.connect(staging_db) as connection:
            connection.execute("CREATE TABLE injected_after_validation (value TEXT)")

    assert staged.execute_synthetic(
        args,
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
        _lifecycle_callback=mutate_staging,
    ) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "STAGING_VALIDATION_IDENTITY_CHANGED"
    assert payload["publish_state"] == "BEFORE_EXCHANGE"
    assert staged._sha256(source) == original_hash
    assert list((tmp_path / "staging").glob("staging-*")) == []


@pytest.mark.parametrize("batch", range(10))
def test_target_substitution_is_reversed_one_hundred_times(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    batch: int,
) -> None:
    _prepare_runtime_identity(monkeypatch)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)
    monkeypatch.setattr(staged, "_expected_schema_names", lambda: {"target_schema_marker"})
    target_schema = staged._target_schema_contract()
    monkeypatch.setattr(
        staged,
        "_target_schema_fingerprint",
        lambda _path: target_schema.fingerprint,
    )
    denied = 0
    for offset in range(10):
        repeat = batch * 10 + offset
        root = _private_directory(tmp_path / f"run-{repeat:03d}")
        source = _source(root)
        args = _args(root, source)
        replacement = source.with_name("replacement.sqlite")
        with sqlite3.connect(replacement) as conn:
            conn.execute("CREATE TABLE replacement_marker (value TEXT NOT NULL)")
            conn.execute("INSERT INTO replacement_marker VALUES ('synthetic')")
        os.chmod(replacement, 0o600)
        replacement_hash = staged._sha256(replacement)

        def substitute_target() -> None:
            os.replace(replacement, source)

        assert staged.execute_synthetic(
            args,
            _migration_runner=_minimal_idempotent_migration,
            _compatibility_probe=lambda *_args, **_kwargs: None,
            _before_exchange_callback=substitute_target,
        ) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["error_type"] == "CONTRACT_DRIFT"
        assert payload["publish_state"] == "EXCHANGE_REVERSED"
        assert payload["automatic_retry_allowed"] is False
        assert payload["manual_recovery_required"] is True
        assert staged._sha256(source) == replacement_hash
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as conn:
            assert conn.execute("SELECT value FROM replacement_marker").fetchone() == ("synthetic",)
        denied += 1
    assert denied == 10


def test_cleanup_failure_does_not_mask_primary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    original_hash = staged._sha256(source)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)

    def inject(phase: str, publish_state: str) -> None:
        if phase == "pre_publish_cleanup":
            raise staged.OrchestratorError("PRIMARY_FAILURE", publish_state=publish_state)
        if phase == "staging_cleanup":
            raise staged.OrchestratorError("INJECTED_STAGING_CLEANUP_FAILURE", publish_state=publish_state)

    assert staged.execute_synthetic(
        args,
        _failure_callback=inject,
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_type"] == "PRIMARY_FAILURE"
    assert payload["cleanup_failed"] is True
    assert payload["cleanup_error_type"] == "INJECTED_STAGING_CLEANUP_FAILURE"
    assert staged._sha256(source) == original_hash
    assert len(list((tmp_path / "staging").glob("staging-*"))) == 1


def test_scoped_body_cleanup_preserves_primary_and_attempts_all_steps() -> None:
    attempted: list[str] = []
    primary = staged.OrchestratorError("PRIMARY_BODY_FAILURE")

    def body() -> None:
        raise primary

    def fail(name: str) -> None:
        attempted.append(name)
        raise OSError(f"private-{name}")

    with pytest.raises(staged._PrimaryAndCleanupError) as captured:
        staged._run_body_with_owned_cleanup(
            body,
            (
                "FIRST_SCOPED_RESOURCE",
                "SCOPED_RESOURCE_RELEASE",
                "FIRST_SCOPED_CLOSE_FAILED",
                lambda: fail("first"),
            ),
            (
                "SECOND_SCOPED_RESOURCE",
                "SCOPED_RESOURCE_RELEASE",
                "SECOND_SCOPED_CLOSE_FAILED",
                lambda: fail("second"),
            ),
        )

    assert captured.value.primary is primary
    assert attempted == ["first", "second"]
    assert [record.error_code for record in captured.value.records] == [
        "FIRST_SCOPED_CLOSE_FAILED",
        "SECOND_SCOPED_CLOSE_FAILED",
    ]
    assert all(
        record.error_type == "OSError"
        for record in captured.value.records
    )
    assert "private-first" not in str(captured.value)
    assert "private-second" not in str(captured.value)


def test_scoped_body_success_reports_cleanup_failure() -> None:
    attempted: list[str] = []

    def fail_cleanup() -> None:
        attempted.append("close")
        raise OSError("private-cleanup-body")

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._run_body_with_owned_cleanup(
            lambda: "completed",
            (
                "SCOPED_RESOURCE",
                "SCOPED_RESOURCE_RELEASE",
                "SCOPED_RESOURCE_CLOSE_FAILED",
                fail_cleanup,
            ),
        )

    assert attempted == ["close"]
    assert len(captured.value.records) == 1
    assert captured.value.records[0].error_code == "SCOPED_RESOURCE_CLOSE_FAILED"
    assert captured.value.records[0].error_type == "OSError"
    assert "private-cleanup-body" not in str(captured.value)


def test_tracked_failure_matrix_contract_smoke(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(FAILURE_MATRIX),
            "--scratch-root",
            str(tmp_path),
            "--repeats",
            "1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["FAILURE_MATRIX_PHASES"] == 18
    assert payload["REPEATS_PER_PHASE"] == 1
    assert payload["FAILURE_MATRIX_REPEAT_RUNS"] == "18/18"
    assert payload["FALSE_COMMIT_REPORTED"] is False
    assert payload["FALSE_ROLLBACK_REPORTED"] is False
    assert payload["BACKUP_AVAILABLE_WHEN_REQUIRED"] is True
    assert payload["PUBLIC_FAILURE_HOOK_EXPOSED"] is False
    assert payload["PUBLIC_CRASH_HOOK_EXPOSED"] is False


def test_tracked_crash_matrix_recovers_only_pre_publish_staging(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(CRASH_MATRIX),
            "matrix",
            "--scratch-root",
            str(tmp_path),
            "--repeats",
            "1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["CRASH_PHASES"] == 10
    assert payload["CRASH_MATRIX_REPEAT_RUNS"] == "10/10"
    assert payload["PRE_PUBLISH_CRASH_STAGING_REMAINS"] == 0
    assert payload["POST_PUBLISH_UNCERTAIN_STAGING_AUTODELETED"] == 0
    assert payload["CORRUPT_TARGETS"] == 0
    assert payload["PARTIAL_SCHEMA_VISIBLE"] is False


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



INTERNAL_DUAL_ERROR_CASES = (
    (
        "pre_primary_source_lease",
        "pre_publish_cleanup",
        ("SOURCE_SQLITE_LEASE",),
    ),
    (
        "post_primary_source_lease",
        "final_verification",
        ("SOURCE_SQLITE_LEASE",),
    ),
    (
        "post_primary_multiple_leases",
        "final_verification",
        ("STAGING_SQLITE_LEASE", "SOURCE_SQLITE_LEASE"),
    ),
    (
        "success_source_lease",
        None,
        ("SOURCE_SQLITE_LEASE",),
    ),
    (
        "success_staging_lease",
        None,
        ("STAGING_SQLITE_LEASE",),
    ),
    (
        "success_staging_pin",
        None,
        ("STAGING_PINNED_DATABASE",),
    ),
    (
        "success_multiple_close_failures",
        None,
        (
            "STAGING_SQLITE_LEASE",
            "STAGING_PINNED_DATABASE",
            "SOURCE_SQLITE_LEASE",
            "SOURCE_PINNED_DATABASE",
        ),
    ),
    (
        "pre_primary_final_source_pin",
        "pre_publish_cleanup",
        ("SOURCE_PINNED_DATABASE",),
    ),
)


def _install_internal_close_failures(
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    failing_resources: tuple[str, ...],
    attempts: list[str],
) -> None:
    original_lease_close = staged.SQLiteLease.close
    original_pin_close = staged.PinnedDatabase.close

    def close_lease(self: staged.SQLiteLease) -> None:
        resource = f"{self.label}_SQLITE_LEASE"
        attempts.append(resource)
        original_lease_close(self)
        if resource in failing_resources:
            raise OSError("sensitive-cleanup-body")

    def close_pin(self: staged.PinnedDatabase) -> None:
        resource = (
            "SOURCE_PINNED_DATABASE"
            if self.path == source
            else "STAGING_PINNED_DATABASE"
        )
        attempts.append(resource)
        original_pin_close(self)
        if resource in failing_resources:
            raise OSError("sensitive-cleanup-body")

    monkeypatch.setattr(staged.SQLiteLease, "close", close_lease)
    monkeypatch.setattr(staged.PinnedDatabase, "close", close_pin)


@pytest.mark.parametrize(
    ("case", "primary_phase", "failing_resources"),
    INTERNAL_DUAL_ERROR_CASES,
)
def test_internal_dual_error_cleanup_lifecycle(
    case: str,
    primary_phase: str | None,
    failing_resources: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    args = _args(tmp_path, source)
    before_hash = staged._sha256(source)
    attempts: list[str] = []
    _install_internal_close_failures(
        monkeypatch,
        source,
        failing_resources,
        attempts,
    )
    monkeypatch.setattr(
        staged,
        "_inspect_image",
        lambda *_args, **_kwargs: REVISION,
    )

    primary_code = (
        "PRIMARY_PRE_EXCHANGE_FAILURE"
        if primary_phase == "pre_publish_cleanup"
        else "PRIMARY_POST_EXCHANGE_FAILURE"
    )

    def inject(phase: str, publish_state: str) -> None:
        if phase == primary_phase:
            raise staged.OrchestratorError(
                primary_code,
                publish_state=publish_state,
            )

    assert staged.execute_synthetic(
        args,
        _failure_callback=inject,
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 1, case
    payload = json.loads(capsys.readouterr().out)

    expected_attempts = [
        "STAGING_SQLITE_LEASE",
        "STAGING_PINNED_DATABASE",
        "SOURCE_SQLITE_LEASE",
        "SOURCE_PINNED_DATABASE",
    ]
    assert attempts == expected_attempts
    expected_codes = [
        f"{resource}_CLOSE_FAILED"
        for resource in expected_attempts
        if resource in failing_resources
    ]
    assert payload["cleanup_exception_count"] == len(failing_resources)
    assert payload["cleanup_failure_codes"] == expected_codes
    assert len(payload["cleanup_failures"]) == len(failing_resources)
    assert {
        failure["resource_kind"]
        for failure in payload["cleanup_failures"]
    } == set(failing_resources)
    assert payload["cleanup_exception_recorded"] is True
    assert payload["automatic_retry_allowed"] is False
    assert payload["manual_recovery_required"] is True
    assert payload["durable_evidence_updated"] is False
    assert payload["false_rollback_reported"] is False

    post_exchange = primary_phase != "pre_publish_cleanup"
    expected_primary = (
        primary_code if primary_phase is not None else "SUCCESS"
    )
    assert payload["primary_exit_classification"] == expected_primary
    assert payload["primary_exception_present"] is (
        primary_phase is not None
    )
    assert payload["primary_exception_preserved"] is (
        primary_phase is not None
    )
    assert payload["exit_classification"] == (
        "PUBLISH_UNCERTAIN"
        if post_exchange
        else primary_code
    )
    assert payload["target_may_have_changed"] is post_exchange
    assert (staged._sha256(source) != before_hash) is post_exchange
    assert len(list((tmp_path / "backups").glob("backup-*.sqlite"))) == 1
    assert len(list((tmp_path / "backups").glob("manifest-*.json"))) == 1
    assert len(list((tmp_path / "staging").glob("staging-*"))) == (
        1 if post_exchange else 0
    )
    serialized = json.dumps(payload, ensure_ascii=True)
    assert "sensitive-cleanup-body" not in serialized


@pytest.mark.parametrize("label", ("SOURCE", "STAGING"))
def test_sqlite_lease_close_aggregates_real_internal_boundaries(
    label: str,
) -> None:
    attempts: list[str] = []

    class FailingConnection:
        def rollback(self) -> None:
            attempts.append("rollback")
            raise OSError("sensitive-rollback")

        def close(self) -> None:
            attempts.append("connection_close")
            raise OSError("sensitive-connection-close")

    lease = staged.SQLiteLease(
        connection=FailingConnection(),  # type: ignore[arg-type]
        label=label,
    )
    with pytest.raises(staged._CleanupAggregateError) as captured:
        lease.close()

    assert attempts == ["rollback", "connection_close"]
    assert [record.error_code for record in captured.value.records] == [
        f"{label}_SQLITE_ROLLBACK_FAILED",
        f"{label}_SQLITE_CONNECTION_CLOSE_FAILED",
    ]
    assert "sensitive" not in str(captured.value)


def test_pinned_database_close_aggregates_file_and_parent_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[int] = []

    def fail_close(fd: int) -> None:
        attempts.append(fd)
        raise OSError("sensitive-fd-close")

    monkeypatch.setattr(staged.os, "close", fail_close)
    identity = staged.SourceIdentity(
        device=1,
        inode=2,
        uid=3,
        gid=4,
        mode=0o600,
        size=5,
        sha256="0" * 64,
    )
    pinned = staged.PinnedDatabase(
        path=tmp_path / "database.sqlite",
        parent_fd=11,
        file_fd=12,
        identity=identity,
    )
    with pytest.raises(staged._CleanupAggregateError) as captured:
        pinned.close()

    assert attempts == [12, 11]
    assert [record.error_code for record in captured.value.records] == [
        "PINNED_DATABASE_FILE_CLOSE_FAILED",
        "PINNED_DATABASE_PARENT_CLOSE_FAILED",
    ]
    assert "sensitive" not in str(captured.value)


def test_prepared_execution_close_attempts_every_owned_resource(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    class FailingResource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            attempts.append(self.name)
            raise OSError(f"sensitive-{self.name}")

    def fail_fd_close(_fd: int) -> None:
        attempts.append("manifest_parent")
        raise OSError("sensitive-manifest-parent")

    monkeypatch.setattr(staged.os, "close", fail_fd_close)
    identity = staged.SourceIdentity(
        device=1,
        inode=2,
        uid=3,
        gid=4,
        mode=0o600,
        size=5,
        sha256="0" * 64,
    )
    contract = staged.Contract(
        source_db=tmp_path / "source.sqlite",
        backup_dir=tmp_path / "backups",
        staging_root=tmp_path / "staging",
        target_image_id=IMAGE_ID,
        previous_image_id=PREVIOUS_IMAGE_ID,
        expected_source_revision=REVISION,
        synthetic_root=None,
    )
    target = staged._target_schema_contract()
    authorization = staged._ProductionAuthorization(
        operation_id="1" * 32,
        plan_sha256="2" * 64,
        source_identity=identity,
        image_revision=REVISION,
        target_schema_version=target.version,
        target_schema_fingerprint=target.fingerprint,
        _seal=staged._PRODUCTION_AUTHORIZATION_SEAL,
    )
    prepared = staged._PreparedProductionExecution(
        contract=contract,
        source_identity=identity,
        source_pin=FailingResource("source_pin"),  # type: ignore[arg-type]
        source_lease=FailingResource("source_lease"),  # type: ignore[arg-type]
        backup_parent_fd=13,
        authorization=authorization,
    )
    with pytest.raises(staged._CleanupAggregateError) as captured:
        prepared.close()

    assert attempts == ["source_lease", "source_pin", "manifest_parent"]
    assert [record.error_code for record in captured.value.records] == [
        "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
        "SOURCE_PINNED_DATABASE_CLOSE_FAILED",
        "MANIFEST_PARENT_CLOSE_FAILED",
    ]
    assert prepared.source_lease is None
    assert prepared.source_pin is None
    assert prepared.backup_parent_fd is None


def test_preparation_failure_uses_structured_cleanup_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_runtime_identity(monkeypatch)
    source = _source(tmp_path)
    backup_dir = _private_directory(tmp_path / "backups")
    staging_root = _private_directory(tmp_path / "staging")
    contract = staged.Contract(
        source_db=source,
        backup_dir=backup_dir,
        staging_root=staging_root,
        target_image_id=IMAGE_ID,
        previous_image_id=PREVIOUS_IMAGE_ID,
        expected_source_revision=REVISION,
        synthetic_root=None,
    )
    identity = staged._source_identity(
        source,
        require_private_parent=True,
    )
    target = staged._target_schema_contract()
    authorization = staged._ProductionAuthorization(
        operation_id="1" * 32,
        plan_sha256="2" * 64,
        source_identity=identity,
        image_revision=REVISION,
        target_schema_version=target.version,
        target_schema_fingerprint=target.fingerprint,
        _seal=staged._PRODUCTION_AUTHORIZATION_SEAL,
    )
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    backup_parent_fd = os.open(backup_dir, flags)
    duplicated_fd: int | None = None
    real_dup = os.dup
    real_close = os.close

    def track_dup(fd: int) -> int:
        nonlocal duplicated_fd
        duplicated_fd = real_dup(fd)
        return duplicated_fd

    def close_then_fail(fd: int) -> None:
        real_close(fd)
        if fd == duplicated_fd:
            raise OSError("sensitive-manifest-close")

    monkeypatch.setattr(staged, "_contract", lambda *_args, **_kwargs: contract)
    monkeypatch.setattr(
        staged,
        "_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            staged.OrchestratorError("PRIMARY_PREPARATION_FAILURE")
        ),
    )
    monkeypatch.setattr(staged.os, "dup", track_dup)
    monkeypatch.setattr(staged.os, "close", close_then_fail)
    try:
        with pytest.raises(staged._StagedCleanupTransport) as captured:
            staged._prepare_authorized_production_execution(
                argparse.Namespace(),
                authorization=authorization,
                expected_source_identity=identity,
                backup_parent_fd=backup_parent_fd,
            )
    finally:
        real_close(backup_parent_fd)

    payload = staged._merge_cleanup_transport(captured.value)
    assert payload["exit_classification"] == "PRIMARY_PREPARATION_FAILURE"
    assert payload["primary_exit_classification"] == (
        "PRIMARY_PREPARATION_FAILURE"
    )
    assert payload["primary_exception_preserved"] is True
    assert payload["cleanup_exception_count"] == 1
    assert payload["cleanup_failure_codes"] == [
        "MANIFEST_PARENT_CLOSE_FAILED"
    ]
    assert payload["target_may_have_changed"] is False
    assert payload["automatic_retry_allowed"] is False
    assert payload["manual_recovery_required"] is True
    assert payload["durable_evidence_updated"] is False
    assert "sensitive" not in json.dumps(payload, ensure_ascii=True)



def test_pinned_database_acquisition_preserves_primary_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    attempts: list[int] = []
    real_close = os.close

    def close_then_fail(fd: int) -> None:
        attempts.append(fd)
        real_close(fd)
        raise OSError("sensitive-acquisition-close")

    monkeypatch.setattr(
        staged,
        "_identity_from_fd",
        lambda _fd: (_ for _ in ()).throw(
            staged.OrchestratorError("PRIMARY_PIN_IDENTITY_FAILURE")
        ),
    )
    monkeypatch.setattr(staged.os, "close", close_then_fail)
    with pytest.raises(staged._PrimaryAndCleanupError) as captured:
        staged._open_pinned_database(source)

    assert isinstance(captured.value.primary, staged.OrchestratorError)
    assert captured.value.primary.code == "PRIMARY_PIN_IDENTITY_FAILURE"
    assert len(attempts) == 2
    assert [record.error_code for record in captured.value.records] == [
        "PINNED_DATABASE_FILE_CLOSE_FAILED",
        "PINNED_DATABASE_PARENT_CLOSE_FAILED",
    ]
    assert "sensitive" not in str(captured.value)


def test_sqlite_lease_acquisition_preserves_primary_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    class FakeResult:
        def fetchone(self) -> tuple[str]:
            return ("delete",)

    class FailingConnection:
        def execute(self, _statement: str) -> FakeResult:
            return FakeResult()

        def rollback(self) -> None:
            attempts.append("rollback")
            raise OSError("sensitive-acquisition-rollback")

        def close(self) -> None:
            attempts.append("connection_close")
            raise OSError("sensitive-acquisition-close")

    identity = staged.SourceIdentity(
        device=1,
        inode=2,
        uid=3,
        gid=4,
        mode=0o600,
        size=5,
        sha256="0" * 64,
    )
    pin = staged.PinnedDatabase(
        path=tmp_path / "database.sqlite",
        parent_fd=11,
        file_fd=12,
        identity=identity,
    )
    monkeypatch.setattr(staged, "_sidecars", lambda _path: [])
    monkeypatch.setattr(
        staged.sqlite3,
        "connect",
        lambda *_args, **_kwargs: FailingConnection(),
    )
    monkeypatch.setattr(
        staged,
        "_path_matches_pin",
        lambda _pin: (_ for _ in ()).throw(
            staged.OrchestratorError("PRIMARY_LEASE_IDENTITY_FAILURE")
        ),
    )

    with pytest.raises(staged._PrimaryAndCleanupError) as captured:
        staged._acquire_sqlite_lease(pin, label="SOURCE")

    assert isinstance(captured.value.primary, staged.OrchestratorError)
    assert captured.value.primary.code == "PRIMARY_LEASE_IDENTITY_FAILURE"
    assert attempts == ["rollback", "connection_close"]
    assert [record.error_code for record in captured.value.records] == [
        "SOURCE_SQLITE_ROLLBACK_FAILED",
        "SOURCE_SQLITE_CONNECTION_CLOSE_FAILED",
    ]
    assert "sensitive" not in str(captured.value)


def test_staging_cleanup_preserves_primary_and_both_descriptor_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare_runtime_identity(monkeypatch)
    staging_root = _private_directory(tmp_path / "staging")
    operation_path = _private_directory(staging_root / "staging-operation")
    source = _source(tmp_path)
    root_metadata = staging_root.stat()
    operation_metadata = operation_path.stat()
    source_metadata = source.stat()
    manifest = staged.DurableManifest(
        path=tmp_path / "manifest.json",
        payload={
            "PUBLISH_STATE": "BEFORE_EXCHANGE",
            "STAGING_ROOT_DEVICE": root_metadata.st_dev,
            "STAGING_ROOT_INODE": root_metadata.st_ino,
            "STAGING_DIRECTORY_PATH": str(operation_path),
            "STAGING_DIRECTORY_DEVICE": operation_metadata.st_dev,
            "STAGING_DIRECTORY_INODE": operation_metadata.st_ino,
            "TARGET_PATH": str(source),
            "SOURCE_DEVICE": source_metadata.st_dev,
            "SOURCE_INODE": source_metadata.st_ino,
        },
    )
    attempts: list[int] = []
    real_close = os.close

    def close_then_fail(fd: int) -> None:
        attempts.append(fd)
        real_close(fd)
        raise OSError("sensitive-staging-directory-close")

    def fail_cleanup(_phase: str, publish_state: str) -> None:
        raise staged.OrchestratorError(
            "PRIMARY_STAGING_CLEANUP_FAILURE",
            publish_state=publish_state,
        )

    monkeypatch.setattr(staged.os, "close", close_then_fail)
    with pytest.raises(staged._PrimaryAndCleanupError) as captured:
        staged._cleanup_operation_staging(
            manifest,
            staging_root,
            failure_callback=fail_cleanup,
        )

    assert isinstance(captured.value.primary, staged.OrchestratorError)
    assert captured.value.primary.code == "PRIMARY_STAGING_CLEANUP_FAILURE"
    assert len(attempts) == 2
    assert [record.error_code for record in captured.value.records] == [
        "STAGING_OPERATION_DIRECTORY_CLOSE_FAILED",
        "STAGING_ROOT_DIRECTORY_CLOSE_FAILED",
    ]
    assert operation_path.exists()
    assert "sensitive" not in str(captured.value)
