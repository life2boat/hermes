#!/usr/bin/env python3
"""Test-only worker for abrupt staged-migration transaction termination."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import healbite_schema_migrate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_durable_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    encoded = json.dumps(payload, sort_keys=True).encode("ascii")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main() -> int:
    parser = argparse.ArgumentParser(description="Test-only active SQLite transaction crash worker")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--mode", choices=("migrate", "crash-active"), required=True)
    parser.add_argument("--evidence-path", required=True)
    args = parser.parse_args()
    database = Path(args.db_path).resolve(strict=True)
    evidence = Path(args.evidence_path).resolve(strict=False)
    target_sha_before = _sha256(database)
    metadata = database.stat()
    identity = healbite_schema_migrate.ProcessIdentity(
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        groups=frozenset({metadata.st_gid}),
    )

    def terminate_in_active_transaction(conn: sqlite3.Connection) -> None:
        journal_exists = Path(f"{database}-journal").exists()
        write_lock_held = False
        competitor = sqlite3.connect(database, timeout=0)
        try:
            competitor.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            write_lock_held = isinstance(code, int) and (code & 0xFF) in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }
        finally:
            competitor.close()
        payload = {
            "BEGIN_IMMEDIATE_CONFIRMED": bool(conn.in_transaction),
            "COMMIT_NOT_COMPLETED": True,
            "CRASH_HOOK_REACHED": True,
            "CRASH_HOOK_REQUESTED": True,
            "JOURNAL_EXISTS_OR_WRITE_LOCK_HELD": journal_exists or write_lock_held,
            "MIGRATION_COMMIT_STATE": "NOT_COMPLETED",
            "SQLITE_TRANSACTION_ACTIVE": bool(conn.in_transaction),
            "STAGING_JOURNAL_STATE": "PRESENT" if journal_exists else "ABSENT_WITH_WRITE_LOCK_HELD",
            "TARGET_SHA_BEFORE": target_sha_before,
            "WORKER_PID": os.getpid(),
        }
        _write_durable_json(evidence, payload)
        os._exit(137)

    result = healbite_schema_migrate.run_migration(
        db_path=str(database),
        staged_copy=True,
        _transaction_hook=terminate_in_active_transaction if args.mode == "crash-active" else None,
        _identity=identity,
    )
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
