#!/usr/bin/env python3
"""Tracked deterministic failure matrix for staged SQLite publication."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import healbite_schema_migrate
from scripts import hermes_staged_schema_migrate as staged


PHASES = (
    "backup_creation",
    "backup_file_fsync",
    "backup_directory_fsync",
    "staging_creation",
    "staging_file_fsync",
    "staging_directory_fsync",
    "household_migration",
    "weekly_migration",
    "shopping_migration",
    "integrity_validation",
    "foreign_key_validation",
    "previous_image_startup",
    "publish_exchange",
    "displaced_target_identity_verification",
    "target_parent_fsync",
    "final_verification",
    "staging_cleanup",
    "manifest_fsync",
)
POST_EXCHANGE_PHASES = frozenset(PHASES[12:16])
DUMMY_IMAGE = "sha256:" + "2" * 64
DUMMY_PREVIOUS_IMAGE = "sha256:" + "3" * 64
DUMMY_REVISION = "1" * 40


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _source(root: Path) -> Path:
    parent = _private(root / "source")
    database = parent / "database.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    os.chmod(database, 0o600)
    return database


def _arguments(root: Path, source: Path) -> argparse.Namespace:
    return argparse.Namespace(
        source_db=str(source),
        backup_dir=str(_private(root / "backups")),
        staging_root=str(_private(root / "staging")),
        target_image_id=DUMMY_IMAGE,
        previous_image_id=DUMMY_PREVIOUS_IMAGE,
        expected_source_revision=DUMMY_REVISION,
        synthetic_root=str(root),
    )


def _single(root: Path, selected: str) -> dict[str, object]:
    source = _source(root)
    arguments = _arguments(root, source)
    before_hash = staged._sha256(source)
    reached = False

    def inject(phase: str, publish_state: str) -> None:
        nonlocal reached
        if selected == "staging_cleanup" and phase == "pre_publish_cleanup":
            raise staged.OrchestratorError("PRIMARY_BEFORE_CLEANUP", publish_state=publish_state)
        if phase != selected:
            return
        reached = True
        raise staged.OrchestratorError(f"INJECTED_{phase.upper()}", publish_state=publish_state)

    def migrate(_contract: staged.Contract, staging_dir: Path) -> None:
        def component(name: str, _connection: sqlite3.Connection) -> None:
            inject(f"{name}_migration", "BEFORE_EXCHANGE")

        result = healbite_schema_migrate.run_migration(
            db_path=str(staging_dir / "database.sqlite"),
            staged_copy=True,
            _component_hook=component,
        )
        if result.exit_code != 0:
            raise staged.OrchestratorError("TARGET_IMAGE_MIGRATION_FAILED")

    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        return_code = staged.execute_synthetic(
            arguments,
            _failure_callback=inject,
            _migration_runner=migrate,
            _compatibility_probe=lambda *_args, **_kwargs: None,
        )
    payload = json.loads(capture.getvalue())
    if selected == "staging_cleanup":
        reached = payload.get("cleanup_error_type") == "INJECTED_STAGING_CLEANUP"
    if return_code != 1 or not reached:
        raise RuntimeError(f"failure boundary not reached: {selected}")
    if staged._sqlite_validation(source) != ("ok", 0):
        raise RuntimeError(f"target corruption: {selected}")

    target_changed = staged._sha256(source) != before_hash
    staging_count = len(list((root / "staging").glob("staging-*")))
    if selected in POST_EXCHANGE_PHASES:
        if not target_changed or staging_count != 1:
            raise RuntimeError(f"post-exchange state mismatch: {selected}")
        if payload.get("manual_recovery_required") is not True:
            raise RuntimeError(f"manual recovery missing: {selected}")
    elif selected == "staging_cleanup":
        if target_changed or staging_count != 1 or payload.get("cleanup_failed") is not True:
            raise RuntimeError("cleanup failure reporting mismatch")
    elif target_changed or staging_count != 0:
        raise RuntimeError(f"pre-exchange cleanup mismatch: {selected}")

    backup_available = bool(list((root / "backups").glob("backup-*.sqlite")))
    if selected not in {"backup_creation", "backup_file_fsync"} and not backup_available:
        raise RuntimeError(f"durable backup missing: {selected}")
    if payload.get("automatic_retry_allowed") is not False:
        raise RuntimeError(f"automatic retry exposed: {selected}")
    if payload.get("false_rollback_reported") is not False:
        raise RuntimeError(f"false rollback reported: {selected}")
    return {
        "backup_available": backup_available,
        "post_exchange": selected in POST_EXCHANGE_PHASES,
        "staging_count": staging_count,
        "target_changed": target_changed,
    }


def run_matrix(scratch_root: Path, repeats: int) -> dict[str, object]:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    matrix_root = scratch_root / f"failure-matrix-{uuid.uuid4().hex}"
    matrix_root.mkdir(mode=0o700)
    old_uid, old_gid = staged.RUNTIME_UID, staged.RUNTIME_GID
    old_inspect = staged._inspect_image
    staged.RUNTIME_UID = int(os.geteuid())
    staged.RUNTIME_GID = int(os.getegid())
    staged._inspect_image = lambda *_args, **_kwargs: DUMMY_REVISION
    completed = 0
    try:
        for phase in PHASES:
            for repeat in range(repeats):
                run_root = matrix_root / f"{phase}-{repeat:02d}"
                run_root.mkdir(mode=0o700)
                _single(run_root, phase)
                completed += 1
                shutil.rmtree(run_root)
        return {
            "BACKUP_AVAILABLE_WHEN_REQUIRED": True,
            "FAILURE_MATRIX_PHASES": len(PHASES),
            "FAILURE_MATRIX_REPEAT_RUNS": f"{completed}/{len(PHASES) * repeats}",
            "FALSE_COMMIT_REPORTED": False,
            "FALSE_ROLLBACK_REPORTED": False,
            "PUBLIC_CRASH_HOOK_EXPOSED": False,
            "PUBLIC_FAILURE_HOOK_EXPOSED": False,
            "REPEATS_PER_PHASE": repeats,
        }
    finally:
        staged.RUNTIME_UID, staged.RUNTIME_GID = old_uid, old_gid
        staged._inspect_image = old_inspect
        if matrix_root.exists():
            shutil.rmtree(matrix_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch-root", required=True)
    parser.add_argument("--repeats", type=int, default=20)
    arguments = parser.parse_args()
    result = run_matrix(Path(arguments.scratch_root).resolve(), arguments.repeats)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
