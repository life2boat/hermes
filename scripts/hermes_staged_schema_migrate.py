#!/usr/bin/env python3
"""Plan and exercise durable staged SQLite schema publication.

Only ``execute-synthetic`` can mutate a target. A production execution mode is
deliberately absent until a separate production gate authorizes it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


SHA_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
MANIFEST_STATES = (
    "PLANNED",
    "BACKED_UP",
    "MIGRATED",
    "VALIDATED",
    "PUBLISHED",
    "VERIFIED",
    "FAILED",
)
STATE_RANK = {state: rank for rank, state in enumerate(MANIFEST_STATES)}
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
RUNTIME_UID = 10000
RUNTIME_GID = 10000
CRASH_GATES = (
    "planned",
    "backup_fsynced",
    "staging_copied",
    "active_sqlite_transaction",
    "migration_committed",
    "validated",
    "before_publish",
    "after_publish",
    "before_target_dir_fsync",
    "after_target_dir_fsync",
)


class OrchestratorError(RuntimeError):
    def __init__(self, code: str, *, publish_state: str = "NOT_PUBLISHED") -> None:
        super().__init__(code)
        self.code = code
        self.publish_state = publish_state


@dataclass(frozen=True)
class Contract:
    source_db: Path
    backup_dir: Path
    staging_root: Path
    target_image_id: str
    previous_image_id: str
    expected_source_revision: str
    synthetic_root: Path | None


@dataclass(frozen=True)
class SourceIdentity:
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    size: int
    sha256: str


@dataclass
class DurableManifest:
    path: Path
    payload: dict[str, Any]

    def transition(self, state: str, **updates: Any) -> None:
        if state not in STATE_RANK:
            raise OrchestratorError("UNKNOWN_MANIFEST_STATE")
        previous = str(self.payload.get("STATE", "PLANNED"))
        if state != "FAILED" and STATE_RANK[state] < STATE_RANK.get(previous, -1):
            raise OrchestratorError("NON_MONOTONIC_MANIFEST_STATE")
        self.payload.update(updates)
        self.payload["STATE"] = state
        _write_json_durable(self.path, self.payload)


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _copy_durable(source: Path, destination: Path, *, uid: int, gid: int) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    destination_fd = os.open(destination, flags, 0o600)
    try:
        with source.open("rb") as source_handle, os.fdopen(destination_fd, "wb") as destination_handle:
            shutil.copyfileobj(source_handle, destination_handle, 1024 * 1024)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    except Exception:
        if destination.exists():
            destination.unlink()
        raise
    os.chmod(destination, 0o600)
    os.chown(destination, uid, gid)
    _fsync_file(destination)
    _fsync_directory(destination.parent)


def _absolute_path(value: str, name: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise OrchestratorError(f"{name}_NOT_ABSOLUTE")
    return path


def _no_symlink_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise OrchestratorError("SYMLINK_PATH_REFUSED")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _source_identity(path: Path, *, require_private_parent: bool) -> SourceIdentity:
    _no_symlink_chain(path)
    metadata = path.lstat()
    parent = path.parent.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OrchestratorError("SOURCE_NOT_SINGLE_REGULAR_FILE")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise OrchestratorError("SOURCE_MODE_INVALID")
    if require_private_parent:
        if parent.st_uid != RUNTIME_UID or parent.st_gid != RUNTIME_GID or stat.S_IMODE(parent.st_mode) != 0o700:
            raise OrchestratorError("SYNTHETIC_SOURCE_PARENT_NOT_PRIVATE")
        if metadata.st_uid != RUNTIME_UID or metadata.st_gid != RUNTIME_GID:
            raise OrchestratorError("SYNTHETIC_SOURCE_OWNER_INVALID")
    return SourceIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
        sha256=_sha256(path),
    )


def _sidecars(path: Path) -> list[Path]:
    return [Path(f"{path}{suffix}") for suffix in SIDECAR_SUFFIXES if Path(f"{path}{suffix}").exists()]


def _check_quiescent(path: Path) -> None:
    if _sidecars(path):
        raise OrchestratorError("SQLITE_SIDECAR_PRESENT")
    lock_probe = (
        "import fcntl, os, sys; "
        "fd=os.open(sys.argv[1], os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)); "
        "\ntry:\n fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "except BlockingIOError:\n sys.exit(75)\n"
        "finally:\n os.close(fd)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", lock_probe, str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 75:
        raise OrchestratorError("SOURCE_NOT_QUIESCENT")
    if result.returncode != 0:
        raise OrchestratorError("QUIESCENCE_PROBE_FAILED")
    if _sidecars(path):
        raise OrchestratorError("SQLITE_SIDECAR_PRESENT")


def _sqlite_validation(path: Path) -> tuple[str, int]:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()
    return integrity, foreign_keys


def _database_snapshot(path: Path) -> tuple[tuple[tuple[str, str, str], ...], dict[str, int]]:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        objects = tuple(
            (str(row[0]), str(row[1]), str(row[2] or ""))
            for row in conn.execute(
                "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        counts: dict[str, int] = {}
        for object_type, name, _sql in objects:
            if object_type == "table":
                counts[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    finally:
        conn.close()
    return objects, counts


def _expected_schema_names() -> set[str]:
    from scripts.healbite_schema_migrate import _component_statements

    names: set[str] = set()
    pattern = re.compile(
        r"^create\s+(?:unique\s+)?(?:table|index)\s+(?:if\s+not\s+exists\s+)?([a-zA-Z0-9_]+)",
        re.IGNORECASE,
    )
    for statements in _component_statements().values():
        for statement in statements:
            match = pattern.match(statement.strip())
            if match is None:
                raise OrchestratorError("EXPECTED_SCHEMA_CONTRACT_DRIFT")
            names.add(match.group(1))
    return names


def _inspect_image(image_id: str, expected_revision: str | None = None) -> str:
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise OrchestratorError("IMAGE_ID_INVALID")
    result = subprocess.run(
        ["docker", "image", "inspect", image_id, "--format", '{{ index .Config.Labels "org.opencontainers.image.revision" }}'],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise OrchestratorError("IMAGE_NOT_AVAILABLE")
    revision = result.stdout.strip()
    if expected_revision is not None and revision != expected_revision:
        raise OrchestratorError("IMAGE_REVISION_MISMATCH")
    return revision


def _contract(args: argparse.Namespace, *, synthetic: bool) -> Contract:
    source_db = _absolute_path(args.source_db, "SOURCE_DB")
    backup_dir = _absolute_path(args.backup_dir, "BACKUP_DIR")
    staging_root = _absolute_path(args.staging_root, "STAGING_ROOT")
    synthetic_root = _absolute_path(args.synthetic_root, "SYNTHETIC_ROOT") if synthetic else None
    if not SHA_RE.fullmatch(args.expected_source_revision):
        raise OrchestratorError("EXPECTED_SOURCE_REVISION_INVALID")
    if not IMAGE_ID_RE.fullmatch(args.target_image_id) or not IMAGE_ID_RE.fullmatch(args.previous_image_id):
        raise OrchestratorError("IMAGE_ID_INVALID")
    if synthetic:
        assert synthetic_root is not None
        _no_symlink_chain(synthetic_root)
        if any(not _inside(path, synthetic_root) for path in (source_db, backup_dir, staging_root)):
            raise OrchestratorError("SYNTHETIC_PATH_OUTSIDE_ROOT")
    return Contract(
        source_db=source_db,
        backup_dir=backup_dir,
        staging_root=staging_root,
        target_image_id=args.target_image_id,
        previous_image_id=args.previous_image_id,
        expected_source_revision=args.expected_source_revision,
        synthetic_root=synthetic_root,
    )


def _preflight(contract: Contract, *, synthetic: bool, inspect_images: bool) -> SourceIdentity:
    identity = _source_identity(contract.source_db, require_private_parent=synthetic)
    if not contract.backup_dir.is_dir() or not contract.staging_root.is_dir():
        raise OrchestratorError("OPERATION_DIRECTORY_MISSING")
    _no_symlink_chain(contract.backup_dir)
    _no_symlink_chain(contract.staging_root)
    if synthetic:
        for directory in (contract.backup_dir, contract.staging_root):
            metadata = directory.lstat()
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise OrchestratorError("SYNTHETIC_OPERATION_DIRECTORY_NOT_PRIVATE")
    if contract.source_db.parent.stat().st_dev != contract.staging_root.stat().st_dev:
        raise OrchestratorError("CROSS_FILESYSTEM_PUBLISH_REFUSED")
    _check_quiescent(contract.source_db)
    integrity, foreign_keys = _sqlite_validation(contract.source_db)
    if integrity != "ok" or foreign_keys != 0:
        raise OrchestratorError("SOURCE_DATABASE_INVALID")
    if inspect_images:
        _inspect_image(contract.target_image_id, contract.expected_source_revision)
        _inspect_image(contract.previous_image_id)
    return identity


def _crash(gate: str, selected: str | None) -> None:
    if selected == gate:
        os.kill(os.getpid(), signal.SIGKILL)


def _fail(phase: str, selected: str | None, *, publish_state: str = "NOT_PUBLISHED") -> None:
    if selected == phase:
        raise OrchestratorError(f"INJECTED_{phase.upper()}_FAILURE", publish_state=publish_state)


def _run_target_migration(contract: Contract, staging_dir: Path, *, crash_gate: str | None) -> None:
    container_db = "/migration/database.sqlite"
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{RUNTIME_UID}:{RUNTIME_GID}",
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        "-v",
        f"{staging_dir}:/migration:rw",
        contract.target_image_id,
        "/opt/hermes/scripts/healbite_schema_migrate.py",
        "--db-path",
        container_db,
        "--staged-copy",
        "--json",
    ]
    if crash_gate == "active_sqlite_transaction":
        command.extend(["--test-crash-after", "active_sqlite_transaction"])
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if crash_gate == "active_sqlite_transaction" and result.returncode != 0:
        os.kill(os.getpid(), signal.SIGKILL)
    if result.returncode != 0:
        raise OrchestratorError("TARGET_IMAGE_MIGRATION_FAILED")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OrchestratorError("TARGET_IMAGE_RESULT_INVALID") from exc
    if payload.get("status") != "success" or payload.get("path_mode") != "STAGED_COPY":
        raise OrchestratorError("TARGET_IMAGE_RESULT_CONTRACT_FAILED")


def _run_previous_image_probe(contract: Contract, staging_dir: Path) -> None:
    code = (
        "import sqlite3; "
        "p='file:/migration/database.sqlite?mode=ro'; "
        "c=sqlite3.connect(p,uri=True); c.execute('PRAGMA query_only=ON'); "
        "assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; "
        "assert not c.execute('PRAGMA foreign_key_check').fetchall(); c.close(); "
        "import gateway.healbite_weekly_menus, gateway.healbite_shopping"
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--user",
        f"{RUNTIME_UID}:{RUNTIME_GID}",
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        "-v",
        f"{staging_dir}:/migration:ro",
        "-e",
        "HEALBITE_WEEKLY_MENU_ENABLED=false",
        "-e",
        "HEALBITE_SHOPPING_LIST_ENABLED=false",
        contract.previous_image_id,
        "-c",
        code,
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise OrchestratorError("PREVIOUS_IMAGE_COMPATIBILITY_FAILED")


def plan(args: argparse.Namespace) -> int:
    contract = _contract(args, synthetic=False)
    identity = _preflight(contract, synthetic=False, inspect_images=True)
    _json_print(
        {
            "status": "PASS",
            "mode": "PLAN",
            "plan_read_only": True,
            "production_execution_enabled": False,
            "quiescent": True,
            "same_filesystem": True,
            "source_inode": identity.inode,
            "source_sha256": identity.sha256,
            "target_revision_matches": True,
        }
    )
    return 0


def execute_synthetic(args: argparse.Namespace) -> int:
    contract = _contract(args, synthetic=True)
    source_identity = _preflight(contract, synthetic=True, inspect_images=True)
    operation_id = uuid.uuid4().hex
    backup = contract.backup_dir / f"backup-{operation_id}.sqlite"
    staging_dir = contract.staging_root / f"staging-{operation_id}"
    staging_db = staging_dir / "database.sqlite"
    manifest_path = contract.backup_dir / f"manifest-{operation_id}.json"
    baseline_objects, baseline_counts = _database_snapshot(contract.source_db)
    published = False
    manifest = DurableManifest(
        path=manifest_path,
        payload={
            "OPERATION_ID": operation_id,
            "SOURCE_INODE": source_identity.inode,
            "SOURCE_SHA256": source_identity.sha256,
            "BACKUP_PATH": str(backup),
            "BACKUP_SHA256": None,
            "STAGING_PATH": str(staging_db),
            "STAGING_SHA256": None,
            "TARGET_PATH": str(contract.source_db),
            "STATE": "PLANNED",
        },
    )
    manifest.transition("PLANNED")
    _crash("planned", args.test_crash_after)
    try:
        _copy_durable(
            contract.source_db,
            backup,
            uid=source_identity.uid,
            gid=source_identity.gid,
        )
        backup_sha = _sha256(backup)
        if backup_sha != source_identity.sha256 or _sqlite_validation(backup) != ("ok", 0):
            raise OrchestratorError("BACKUP_VALIDATION_FAILED")
        manifest.transition("BACKED_UP", BACKUP_SHA256=backup_sha)
        _crash("backup_fsynced", args.test_crash_after)

        staging_dir.mkdir(mode=0o700)
        os.chmod(staging_dir, 0o700)
        os.chown(staging_dir, RUNTIME_UID, RUNTIME_GID)
        _fsync_directory(contract.staging_root)
        _copy_durable(
            contract.source_db,
            staging_db,
            uid=RUNTIME_UID,
            gid=RUNTIME_GID,
        )
        if _sha256(staging_db) != backup_sha:
            raise OrchestratorError("STAGING_SOURCE_MISMATCH")
        _crash("staging_copied", args.test_crash_after)

        for run_number in range(1, 4):
            _fail(("household", "weekly", "shopping")[min(run_number - 1, 2)], args.test_fail_phase)
            before = _database_snapshot(staging_db) if run_number > 1 else None
            _run_target_migration(contract, staging_dir, crash_gate=args.test_crash_after)
            after = _database_snapshot(staging_db)
            if before is not None and after != before:
                raise OrchestratorError("MIGRATION_NOT_IDEMPOTENT")
        _crash("migration_committed", args.test_crash_after)
        manifest.transition("MIGRATED", STAGING_SHA256=_sha256(staging_db))

        _fail("integrity", args.test_fail_phase)
        integrity, foreign_keys = _sqlite_validation(staging_db)
        migrated_objects, migrated_counts = _database_snapshot(staging_db)
        if integrity != "ok" or foreign_keys != 0 or _sidecars(staging_db):
            raise OrchestratorError("MIGRATED_DATABASE_INVALID")
        for table, count in baseline_counts.items():
            if migrated_counts.get(table) != count:
                raise OrchestratorError("LEGACY_DATA_MUTATED")
        baseline_names = {name for _kind, name, _sql in baseline_objects}
        migrated_names = {name for _kind, name, _sql in migrated_objects}
        expected_names = _expected_schema_names()
        if not baseline_names.issubset(migrated_names) or not expected_names.issubset(migrated_names):
            raise OrchestratorError("BASELINE_SCHEMA_REMOVED")
        if migrated_names - baseline_names - expected_names:
            raise OrchestratorError("UNKNOWN_SCHEMA_OBJECTS")
        for table in expected_names - baseline_names:
            if table in migrated_counts and migrated_counts[table] != 0:
                raise OrchestratorError("BACKFILL_ROWS_CREATED")
        metadata = staging_db.stat()
        if (
            stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != RUNTIME_UID
            or metadata.st_gid != RUNTIME_GID
            or metadata.st_nlink != 1
        ):
            raise OrchestratorError("MIGRATED_DATABASE_METADATA_INVALID")

        _fail("previous_compatibility", args.test_fail_phase)
        before_previous_probe = _sha256(staging_db)
        _run_previous_image_probe(contract, staging_dir)
        if _sha256(staging_db) != before_previous_probe:
            raise OrchestratorError("PREVIOUS_IMAGE_MUTATED_STAGING")
        manifest.transition("VALIDATED", STAGING_SHA256=before_previous_probe)
        _crash("validated", args.test_crash_after)
        _crash("before_publish", args.test_crash_after)
        _fail("atomic_publish", args.test_fail_phase)

        current_identity = _source_identity(contract.source_db, require_private_parent=True)
        if current_identity != source_identity:
            raise OrchestratorError("SOURCE_IDENTITY_CHANGED")
        if staging_db.stat().st_dev != contract.source_db.parent.stat().st_dev:
            raise OrchestratorError("CROSS_FILESYSTEM_PUBLISH_REFUSED")
        _fsync_file(staging_db)
        os.replace(staging_db, contract.source_db)
        published = True
        manifest.transition("PUBLISHED")
        _crash("after_publish", args.test_crash_after)
        _crash("before_target_dir_fsync", args.test_crash_after)
        _fail("target_dir_fsync", args.test_fail_phase, publish_state="UNKNOWN")
        _fsync_directory(contract.source_db.parent)
        _crash("after_target_dir_fsync", args.test_crash_after)

        if _sqlite_validation(contract.source_db) != ("ok", 0):
            raise OrchestratorError("PUBLISHED_DATABASE_INVALID", publish_state="UNKNOWN")
        manifest.transition("VERIFIED")
        _json_print(
            {
                "status": "PASS",
                "mode": "EXECUTE_SYNTHETIC",
                "production_execution_enabled": False,
                "backup_created": True,
                "backup_and_staging_source_match": True,
                "migration_runs": 3,
                "migration_idempotent": True,
                "previous_image_compatible": True,
                "atomic_primitive": "os.replace",
                "target_parent_fsynced": True,
                "manifest_state": "VERIFIED",
                "publish_state": "VERIFIED",
            }
        )
        return 0
    except Exception as exc:
        error = exc if isinstance(exc, OrchestratorError) else OrchestratorError(type(exc).__name__)
        publish_state = "UNKNOWN" if published else error.publish_state
        try:
            manifest.transition("FAILED", PUBLISH_STATE=publish_state, ERROR_TYPE=error.code)
        except Exception:
            pass
        _json_print(
            {
                "status": "FAILED",
                "error_type": error.code,
                "publish_state": publish_state,
                "automatic_retry_allowed": False,
                "manual_recovery_required": published,
                "backup_available": backup.exists(),
            }
        )
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Staged SQLite schema migration orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "execute-synthetic"):
        child = subparsers.add_parser(command)
        child.add_argument("--source-db", required=True)
        child.add_argument("--backup-dir", required=True)
        child.add_argument("--staging-root", required=True)
        child.add_argument("--target-image-id", required=True)
        child.add_argument("--previous-image-id", required=True)
        child.add_argument("--expected-source-revision", required=True)
        if command == "execute-synthetic":
            child.add_argument("--synthetic-root", required=True)
            child.add_argument("--test-crash-after", choices=CRASH_GATES, help=argparse.SUPPRESS)
            child.add_argument(
                "--test-fail-phase",
                choices=(
                    "household",
                    "weekly",
                    "shopping",
                    "integrity",
                    "previous_compatibility",
                    "atomic_publish",
                    "target_dir_fsync",
                ),
                help=argparse.SUPPRESS,
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return plan(args)
        if args.command == "execute-synthetic":
            return execute_synthetic(args)
        raise OrchestratorError("PRODUCTION_EXECUTION_DISABLED")
    except OrchestratorError as exc:
        _json_print(
            {
                "status": "FAILED",
                "error_type": exc.code,
                "production_execution_enabled": False,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
