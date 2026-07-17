#!/usr/bin/env python3
"""Hash-bound production authorization gate for staged SQLite migration."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import uuid
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.hermes_staged_schema_migrate import (  # noqa: E402
    OrchestratorError,
    SourceIdentity,
    execute_production_staged,
)


PLAN_VERSION = 1
MAX_PLAN_BYTES = 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
FEATURE_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]+")
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
SUCCESS_STATES = (
    "PLANNED",
    "PREFLIGHT_VERIFIED",
    "BACKUP_DURABLE",
    "STAGING_MIGRATED",
    "COMPATIBILITY_VERIFIED",
    "QUIESCENCE_HELD",
    "EXCHANGE_STARTED",
    "EXCHANGE_VERIFIED",
    "PARENT_FSYNCED",
    "FINAL_VERIFIED",
    "COMPLETED",
)


PLAN_FIELDS = frozenset(
    {
        "PLAN_VERSION",
        "OPERATION_ID",
        "CREATED_AT",
        "EXPIRES_AT",
        "HOSTNAME",
        "DB_CANONICAL_PATH",
        "SOURCE_DEVICE",
        "SOURCE_INODE",
        "SOURCE_SIZE",
        "SOURCE_MODE",
        "SOURCE_UID",
        "SOURCE_GID",
        "SOURCE_SHA256",
        "SOURCE_SCHEMA_FINGERPRINT",
        "BACKUP_PARENT",
        "STAGING_PARENT",
        "EVIDENCE_PARENT",
        "BACKUP_PARENT_IDENTITY",
        "STAGING_PARENT_IDENTITY",
        "EVIDENCE_PARENT_IDENTITY",
        "MIGRATION_IMAGE_ID",
        "MIGRATION_IMAGE_REVISION",
        "PREVIOUS_IMAGE_ID",
        "TARGET_SCHEMA_VERSION",
        "DEPLOYMENT_CONTRACT_PATH",
        "DEPLOYMENT_CONTRACT_SHA256",
        "EXPECTED_FEATURE_FLAGS",
        "EXPECTED_FREE_BYTES",
        "EXPECTED_FILESYSTEM_DEVICE",
        "PLAN_STATE",
        "PLAN_READ_ONLY",
        "PLAN_DATABASE_MUTATION",
        "PLAN_BACKUP_CREATED",
        "PLAN_STAGING_CREATED",
        "PLAN_CONTAINER_STOPPED",
        "PLAN_CONTAINS_SECRETS",
        "AUTOMATIC_RETRY_ALLOWED",
        "AUTOMATIC_DB_RESTORE_IMPLEMENTED",
    }
)
FAILURE_STATES = frozenset(
    {"PRE_PUBLISH_FAILED", "PUBLISH_UNCERTAIN", "MANUAL_RECOVERY_REQUIRED"}
)


class ProductionGateError(RuntimeError):
    """A public production authorization check failed closed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class PinnedPlan:
    path: Path
    parent_fd: int
    file_fd: int
    device: int
    inode: int
    payload: dict[str, Any]
    sha256: str

    def path_matches(self) -> bool:
        try:
            metadata = os.stat(self.path.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except OSError:
            return False
        return (metadata.st_dev, metadata.st_ino) == (self.device, self.inode)

    def close(self) -> None:
        os.close(self.file_fd)
        os.close(self.parent_fd)


@dataclass
class ExecutionEvidence:
    path: Path
    payload: dict[str, Any]

    def transition(self, state: str, **updates: Any) -> None:
        history = list(self.payload["STATE_HISTORY"])
        if state in SUCCESS_STATES:
            current = history[-1]
            if current in SUCCESS_STATES and SUCCESS_STATES.index(state) <= SUCCESS_STATES.index(current):
                raise ProductionGateError("NON_MONOTONIC_EXECUTION_STATE")
        elif state not in FAILURE_STATES:
            raise ProductionGateError("UNKNOWN_EXECUTION_STATE")
        elif state in history:
            raise ProductionGateError("DUPLICATE_FAILURE_STATE")
        history.append(state)
        self.payload.update(updates)
        self.payload["STATE"] = state
        self.payload["STATE_HISTORY"] = history
        _write_json_durable(self.path, self.payload)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, code: str) -> datetime:
    if not isinstance(value, str):
        raise ProductionGateError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionGateError(code) from exc
    if parsed.tzinfo is None:
        raise ProductionGateError(code)
    return parsed.astimezone(timezone.utc)


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("ascii")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        return _sha256_fd(fd)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    encoded = _canonical_json(payload)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ProductionGateError("DURABLE_MANIFEST_METADATA_INVALID")


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise ProductionGateError("POSIX_IDENTITY_REQUIRED")
    return int(getter())


def _absolute_path(value: str, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise ProductionGateError(f"{code}_NOT_CANONICAL_ABSOLUTE")
    return path


def _no_symlink_chain(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise ProductionGateError("PATH_METADATA_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ProductionGateError("SYMLINK_PATH_REFUSED")


def _directory_record(path: Path, *, private: bool) -> dict[str, int | str]:
    _no_symlink_chain(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProductionGateError("OPERATION_PARENT_NOT_DIRECTORY")
    mode = stat.S_IMODE(metadata.st_mode)
    if private and (metadata.st_uid != _effective_uid() or mode != 0o700):
        raise ProductionGateError("OPERATION_PARENT_NOT_PRIVATE")
    return {
        "PATH": str(path),
        "DEVICE": int(metadata.st_dev),
        "INODE": int(metadata.st_ino),
        "UID": int(metadata.st_uid),
        "GID": int(metadata.st_gid),
        "MODE": mode,
    }


def _assert_directory_record(record: object, code: str) -> Path:
    if not isinstance(record, dict):
        raise ProductionGateError(code)
    required = {"PATH", "DEVICE", "INODE", "UID", "GID", "MODE"}
    if set(record) != required or not isinstance(record["PATH"], str):
        raise ProductionGateError(code)
    path = _absolute_path(record["PATH"], code)
    actual = _directory_record(path, private=True)
    if actual != record:
        raise ProductionGateError(code)
    return path


def _assert_source_parent_controlled(path: Path) -> None:
    metadata = path.parent.lstat()
    allowed_owners = {0, _effective_uid()}
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in allowed_owners
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ProductionGateError("SOURCE_PARENT_NOT_OPERATOR_CONTROLLED")


def _read_only_source(path: Path) -> tuple[dict[str, int | str], str, str, int]:
    _no_symlink_chain(path)
    _assert_source_parent_controlled(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    connection: sqlite3.Connection | None = None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise ProductionGateError("SOURCE_METADATA_INVALID")
        path_metadata = path.lstat()
        if (path_metadata.st_dev, path_metadata.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ProductionGateError("SOURCE_PATH_SUBSTITUTION")
        if _sidecars(path):
            raise ProductionGateError("UNSUPPORTED_SQLITE_SIDECAR")
        uri = f"file:{path}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=0")
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise ProductionGateError("SOURCE_JOURNAL_MODE_UNSUPPORTED")
        connection.execute("BEGIN")
        objects = tuple(
            (str(row[0]), str(row[1]), str(row[2] or ""))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        )
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower()
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        source_sha = _sha256_fd(fd)
        current = os.fstat(fd)
        current_path = path.lstat()
        if (
            (current.st_dev, current.st_ino, current.st_size)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size)
            or (current_path.st_dev, current_path.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise ProductionGateError("SOURCE_CHANGED_DURING_READ")
        if _sidecars(path):
            raise ProductionGateError("UNSUPPORTED_SQLITE_SIDECAR")
        schema_fingerprint = _sha256_bytes(_canonical_json({"objects": objects}))
        identity = {
            "SOURCE_DEVICE": int(metadata.st_dev),
            "SOURCE_INODE": int(metadata.st_ino),
            "SOURCE_SIZE": int(metadata.st_size),
            "SOURCE_MODE": stat.S_IMODE(metadata.st_mode),
            "SOURCE_UID": int(metadata.st_uid),
            "SOURCE_GID": int(metadata.st_gid),
            "SOURCE_SHA256": source_sha,
        }
        return identity, schema_fingerprint, integrity, foreign_keys
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(code, int) and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            raise ProductionGateError("QUIESCENCE_FAILED") from exc
        raise ProductionGateError("SOURCE_SQLITE_VALIDATION_FAILED") from exc
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()
        os.close(fd)


def _sidecars(path: Path) -> list[Path]:
    return [Path(f"{path}{suffix}") for suffix in SIDECAR_SUFFIXES if Path(f"{path}{suffix}").exists()]


def _inspect_image(image_id: str, expected_revision: str | None) -> str:
    if IMAGE_ID_RE.fullmatch(image_id) is None:
        raise ProductionGateError("IMAGE_ID_INVALID")
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            image_id,
            "--format",
            '{{.Id}}\n{{ index .Config.Labels "org.opencontainers.image.revision" }}',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ProductionGateError("IMAGE_NOT_AVAILABLE")
    lines = result.stdout.splitlines()
    if not lines or lines[0] != image_id:
        raise ProductionGateError("IMAGE_IDENTITY_DRIFT")
    revision = lines[1] if len(lines) > 1 else ""
    if expected_revision is not None and revision != expected_revision:
        raise ProductionGateError("IMAGE_REVISION_MISMATCH")
    return revision


def _read_pinned_regular_bytes(path: Path, *, maximum: int) -> bytes:
    _no_symlink_chain(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
        ):
            raise ProductionGateError("DEPLOYMENT_CONTRACT_INVALID")
        data = b""
        while len(data) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > maximum:
            raise ProductionGateError("DEPLOYMENT_CONTRACT_INVALID")
        current = os.fstat(fd)
        path_metadata = path.lstat()
        if (
            (current.st_dev, current.st_ino, current.st_size)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size)
            or (path_metadata.st_dev, path_metadata.st_ino)
            != (metadata.st_dev, metadata.st_ino)
        ):
            raise ProductionGateError("DEPLOYMENT_CONTRACT_DRIFT")
        return data
    finally:
        os.close(fd)


def _feature_flags(path: Path) -> tuple[str, dict[str, str]]:
    data = _read_pinned_regular_bytes(path, maximum=MAX_PLAN_BYTES)
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionGateError("DEPLOYMENT_CONTRACT_INVALID") from exc
    feature_gates = raw.get("feature_gates") if isinstance(raw, dict) else None
    if not isinstance(feature_gates, dict) or not feature_gates:
        raise ProductionGateError("FEATURE_FLAGS_INVALID")
    normalized: dict[str, str] = {}
    for name, value in feature_gates.items():
        if not isinstance(name, str) or FEATURE_NAME_RE.fullmatch(name) is None or not isinstance(value, str):
            raise ProductionGateError("FEATURE_FLAGS_INVALID")
        if name.endswith("_ENABLED") and value.lower() != "false":
            raise ProductionGateError("FEATURE_FLAG_ENABLED")
        if name.endswith("_ALLOWLIST") and value != "":
            raise ProductionGateError("FEATURE_ALLOWLIST_NOT_EMPTY")
        if not name.endswith(("_ENABLED", "_ALLOWLIST")):
            raise ProductionGateError("FEATURE_FLAGS_INVALID")
        normalized[name] = value
    return _sha256_bytes(data), dict(sorted(normalized.items()))

def _assert_feature_flags(path: Path, expected_hash: object, expected_flags: object) -> None:
    if not isinstance(expected_hash, str) or not isinstance(expected_flags, dict):
        raise ProductionGateError("DEPLOYMENT_CONTRACT_PLAN_INVALID")
    actual_hash, actual_flags = _feature_flags(path)
    if actual_hash != expected_hash or actual_flags != expected_flags:
        raise ProductionGateError("FEATURE_FLAGS_DRIFT")


def _free_bytes(path: Path) -> int:
    values = os.statvfs(path)
    return int(values.f_bavail * values.f_frsize)


def _require_free_bytes(path: Path, minimum: int) -> int:
    actual = _free_bytes(path)
    if actual < minimum:
        raise ProductionGateError("INSUFFICIENT_FREE_SPACE")
    return actual


def _pairwise_disjoint(paths: Sequence[Path]) -> None:
    for index, left in enumerate(paths):
        for right in paths[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise ProductionGateError("OPERATION_PATHS_OVERLAP")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _validate_plan_inputs(args: argparse.Namespace) -> tuple[Path, Path, Path, Path, Path]:
    db_path = _absolute_path(args.db_path, "DB_PATH")
    backup_parent = _absolute_path(args.backup_parent, "BACKUP_PARENT")
    staging_parent = _absolute_path(args.staging_parent, "STAGING_PARENT")
    evidence_parent = _absolute_path(args.evidence_parent, "EVIDENCE_PARENT")
    deployment_contract = _absolute_path(args.deployment_contract, "DEPLOYMENT_CONTRACT")
    if socket.gethostname() != args.expected_hostname:
        raise ProductionGateError("HOSTNAME_MISMATCH")
    if REVISION_RE.fullmatch(args.migration_image_revision) is None:
        raise ProductionGateError("MIGRATION_IMAGE_REVISION_INVALID")
    if SHA_RE.fullmatch(args.expected_source_sha256) is None:
        raise ProductionGateError("EXPECTED_SOURCE_SHA256_INVALID")
    if not args.target_schema_version.strip():
        raise ProductionGateError("TARGET_SCHEMA_VERSION_INVALID")
    if args.expires_in_seconds < 60 or args.expires_in_seconds > 86400:
        raise ProductionGateError("PLAN_EXPIRY_INVALID")
    _pairwise_disjoint((db_path, backup_parent, staging_parent, evidence_parent, deployment_contract))
    return db_path, backup_parent, staging_parent, evidence_parent, deployment_contract


def create_plan(args: argparse.Namespace) -> int:
    db_path, backup_parent, staging_parent, evidence_parent, deployment_contract = _validate_plan_inputs(args)
    backup_record = _directory_record(backup_parent, private=True)
    staging_record = _directory_record(staging_parent, private=True)
    evidence_record = _directory_record(evidence_parent, private=True)
    identity, schema_fingerprint, integrity, foreign_keys = _read_only_source(db_path)
    expected_identity = {
        "SOURCE_DEVICE": args.expected_source_device,
        "SOURCE_INODE": args.expected_source_inode,
        "SOURCE_SIZE": args.expected_source_size,
        "SOURCE_SHA256": args.expected_source_sha256,
    }
    if any(identity[name] != value for name, value in expected_identity.items()):
        raise ProductionGateError("EXPECTED_SOURCE_IDENTITY_MISMATCH")
    if integrity != "ok" or foreign_keys != 0:
        raise ProductionGateError("SOURCE_DATABASE_INVALID")
    if identity["SOURCE_DEVICE"] != staging_record["DEVICE"]:
        raise ProductionGateError("CROSS_FILESYSTEM_STAGING")
    _require_free_bytes(staging_parent, args.expected_free_bytes)
    _inspect_image(args.migration_image_id, args.migration_image_revision)
    _inspect_image(args.previous_image_id, None)
    deployment_hash, feature_flags = _feature_flags(deployment_contract)
    operation_id = uuid.uuid4().hex
    operation_directory = evidence_parent / operation_id
    operation_directory.mkdir(mode=0o700)
    os.chmod(operation_directory, 0o700)
    _fsync_directory(evidence_parent)
    created_at = _now()
    plan_payload: dict[str, Any] = {
        "PLAN_VERSION": PLAN_VERSION,
        "OPERATION_ID": operation_id,
        "CREATED_AT": _timestamp(created_at),
        "EXPIRES_AT": _timestamp(created_at + timedelta(seconds=args.expires_in_seconds)),
        "HOSTNAME": args.expected_hostname,
        "DB_CANONICAL_PATH": str(db_path),
        **identity,
        "SOURCE_SCHEMA_FINGERPRINT": schema_fingerprint,
        "BACKUP_PARENT": str(backup_parent),
        "STAGING_PARENT": str(staging_parent),
        "EVIDENCE_PARENT": str(evidence_parent),
        "BACKUP_PARENT_IDENTITY": backup_record,
        "STAGING_PARENT_IDENTITY": staging_record,
        "EVIDENCE_PARENT_IDENTITY": evidence_record,
        "MIGRATION_IMAGE_ID": args.migration_image_id,
        "MIGRATION_IMAGE_REVISION": args.migration_image_revision,
        "PREVIOUS_IMAGE_ID": args.previous_image_id,
        "TARGET_SCHEMA_VERSION": args.target_schema_version,
        "DEPLOYMENT_CONTRACT_PATH": str(deployment_contract),
        "DEPLOYMENT_CONTRACT_SHA256": deployment_hash,
        "EXPECTED_FEATURE_FLAGS": feature_flags,
        "EXPECTED_FREE_BYTES": args.expected_free_bytes,
        "EXPECTED_FILESYSTEM_DEVICE": identity["SOURCE_DEVICE"],
        "PLAN_STATE": "PLANNED",
        "PLAN_READ_ONLY": True,
        "PLAN_DATABASE_MUTATION": False,
        "PLAN_BACKUP_CREATED": False,
        "PLAN_STAGING_CREATED": False,
        "PLAN_CONTAINER_STOPPED": False,
        "PLAN_CONTAINS_SECRETS": False,
        "AUTOMATIC_RETRY_ALLOWED": False,
        "AUTOMATIC_DB_RESTORE_IMPLEMENTED": False,
    }
    plan_path = operation_directory / "plan.json"
    _write_json_durable(plan_path, plan_payload)
    plan_sha = _sha256(plan_path)
    _json_print(
        {
            "status": "PASS",
            "mode": "PLAN",
            "operation_id": operation_id,
            "plan_path": str(plan_path),
            "plan_sha256": plan_sha,
            "plan_manifest_fsynced": True,
            "plan_parent_fsynced": True,
            "plan_read_only": True,
            "production_execution_enabled": False,
        }
    )
    return 0


def _open_plan(path_value: str, expected_sha: str) -> PinnedPlan:
    path = _absolute_path(path_value, "PLAN_PATH")
    _no_symlink_chain(path)
    if SHA_RE.fullmatch(expected_sha) is None:
        raise ProductionGateError("EXPECTED_PLAN_SHA256_INVALID")
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, parent_flags)
    try:
        file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(path.name, file_flags, dir_fd=parent_fd)
    except Exception:
        os.close(parent_fd)
        raise
    try:
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != _effective_uid()
            or metadata.st_size > MAX_PLAN_BYTES
        ):
            raise ProductionGateError("PLAN_FILE_METADATA_INVALID")
        data = b""
        while len(data) <= MAX_PLAN_BYTES:
            chunk = os.read(file_fd, min(65536, MAX_PLAN_BYTES + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_PLAN_BYTES:
            raise ProductionGateError("PLAN_FILE_TOO_LARGE")
        actual_sha = _sha256_bytes(data)
        if actual_sha != expected_sha:
            raise ProductionGateError("PLAN_SHA256_MISMATCH")
        try:
            payload = json.loads(data.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionGateError("PLAN_JSON_INVALID") from exc
        if not isinstance(payload, dict) or _canonical_json(payload) != data:
            raise ProductionGateError("PLAN_JSON_NOT_CANONICAL")
        path_metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (path_metadata.st_dev, path_metadata.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ProductionGateError("PLAN_PATH_SUBSTITUTION")
        return PinnedPlan(
            path=path,
            parent_fd=parent_fd,
            file_fd=file_fd,
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            payload=payload,
            sha256=actual_sha,
        )
    except Exception:
        os.close(file_fd)
        os.close(parent_fd)
        raise


def _expect_plan_string(plan: dict[str, Any], name: str, pattern: re.Pattern[str] | None = None) -> str:
    value = plan.get(name)
    if not isinstance(value, str) or (pattern is not None and pattern.fullmatch(value) is None):
        raise ProductionGateError("PLAN_CONTRACT_INVALID")
    return value


def _revalidate_plan(args: argparse.Namespace, pinned: PinnedPlan) -> tuple[argparse.Namespace, Path]:
    plan = pinned.payload
    if set(plan) != PLAN_FIELDS:
        raise ProductionGateError("PLAN_FIELDS_INVALID")
    if plan.get("PLAN_VERSION") != PLAN_VERSION or plan.get("PLAN_STATE") != "PLANNED":
        raise ProductionGateError("PLAN_VERSION_OR_STATE_INVALID")
    expected_booleans = {
        "PLAN_READ_ONLY": True,
        "PLAN_DATABASE_MUTATION": False,
        "PLAN_BACKUP_CREATED": False,
        "PLAN_STAGING_CREATED": False,
        "PLAN_CONTAINER_STOPPED": False,
        "PLAN_CONTAINS_SECRETS": False,
        "AUTOMATIC_RETRY_ALLOWED": False,
        "AUTOMATIC_DB_RESTORE_IMPLEMENTED": False,
    }
    if any(plan.get(name) is not value for name, value in expected_booleans.items()):
        raise ProductionGateError("PLAN_SAFETY_CONTRACT_INVALID")
    operation_id = _expect_plan_string(plan, "OPERATION_ID", OPERATION_ID_RE)
    source_sha = _expect_plan_string(plan, "SOURCE_SHA256", SHA_RE)
    revision = _expect_plan_string(plan, "MIGRATION_IMAGE_REVISION", REVISION_RE)
    if operation_id != args.confirm_operation_id:
        raise ProductionGateError("PLAN_OPERATION_ID_MISMATCH")
    if source_sha != args.confirm_source_sha256:
        raise ProductionGateError("PLAN_SOURCE_SHA256_CONFIRMATION_MISMATCH")
    if revision != args.confirm_image_revision:
        raise ProductionGateError("PLAN_IMAGE_REVISION_CONFIRMATION_MISMATCH")
    expires_at = _parse_timestamp(plan.get("EXPIRES_AT"), "PLAN_EXPIRY_INVALID")
    created_at = _parse_timestamp(plan.get("CREATED_AT"), "PLAN_CREATED_AT_INVALID")
    if expires_at <= created_at or expires_at - created_at > timedelta(days=1):
        raise ProductionGateError("PLAN_EXPIRY_INVALID")
    if _now() >= expires_at:
        raise ProductionGateError("PLAN_EXPIRED")
    hostname = _expect_plan_string(plan, "HOSTNAME")
    if socket.gethostname() != hostname:
        raise ProductionGateError("HOSTNAME_DRIFT")
    evidence_parent = _assert_directory_record(plan.get("EVIDENCE_PARENT_IDENTITY"), "EVIDENCE_PARENT_DRIFT")
    expected_plan_path = evidence_parent / operation_id / "plan.json"
    if pinned.path != expected_plan_path or not pinned.path_matches():
        raise ProductionGateError("PLAN_PATH_SUBSTITUTION")
    operation_record = _directory_record(pinned.path.parent, private=True)
    if operation_record["PATH"] != str(evidence_parent / operation_id):
        raise ProductionGateError("PLAN_OPERATION_DIRECTORY_INVALID")
    backup_parent = _assert_directory_record(plan.get("BACKUP_PARENT_IDENTITY"), "BACKUP_PARENT_DRIFT")
    staging_parent = _assert_directory_record(plan.get("STAGING_PARENT_IDENTITY"), "STAGING_PARENT_DRIFT")
    if (
        plan.get("BACKUP_PARENT") != str(backup_parent)
        or plan.get("STAGING_PARENT") != str(staging_parent)
        or plan.get("EVIDENCE_PARENT") != str(evidence_parent)
    ):
        raise ProductionGateError("PLAN_PATH_FIELDS_INVALID")
    db_path = _absolute_path(_expect_plan_string(plan, "DB_CANONICAL_PATH"), "DB_PATH")
    deployment_contract = _absolute_path(
        _expect_plan_string(plan, "DEPLOYMENT_CONTRACT_PATH"), "DEPLOYMENT_CONTRACT"
    )
    _pairwise_disjoint((db_path, backup_parent, staging_parent, evidence_parent, deployment_contract))
    identity, schema_fingerprint, integrity, foreign_keys = _read_only_source(db_path)
    _expect_plan_string(plan, "SOURCE_SCHEMA_FINGERPRINT", SHA_RE)
    _expect_plan_string(plan, "DEPLOYMENT_CONTRACT_SHA256", SHA_RE)
    target_schema_version = _expect_plan_string(plan, "TARGET_SCHEMA_VERSION")
    if not target_schema_version.strip():
        raise ProductionGateError("TARGET_SCHEMA_VERSION_INVALID")
    for name in (
        "SOURCE_DEVICE",
        "SOURCE_INODE",
        "SOURCE_SIZE",
        "SOURCE_MODE",
        "SOURCE_UID",
        "SOURCE_GID",
        "SOURCE_SHA256",
    ):
        if plan.get(name) != identity[name]:
            raise ProductionGateError("SOURCE_IDENTITY_DRIFT")
    if plan.get("SOURCE_SCHEMA_FINGERPRINT") != schema_fingerprint:
        raise ProductionGateError("SOURCE_SCHEMA_DRIFT")
    if integrity != "ok" or foreign_keys != 0:
        raise ProductionGateError("SOURCE_DATABASE_INVALID")
    if plan.get("EXPECTED_FILESYSTEM_DEVICE") != identity["SOURCE_DEVICE"]:
        raise ProductionGateError("FILESYSTEM_DRIFT")
    if staging_parent.stat().st_dev != identity["SOURCE_DEVICE"]:
        raise ProductionGateError("FILESYSTEM_DRIFT")
    expected_free = plan.get("EXPECTED_FREE_BYTES")
    if not isinstance(expected_free, int) or expected_free <= 0:
        raise ProductionGateError("PLAN_FREE_SPACE_INVALID")
    _require_free_bytes(staging_parent, expected_free)
    migration_image = _expect_plan_string(plan, "MIGRATION_IMAGE_ID", IMAGE_ID_RE)
    previous_image = _expect_plan_string(plan, "PREVIOUS_IMAGE_ID", IMAGE_ID_RE)
    _inspect_image(migration_image, revision)
    _inspect_image(previous_image, None)
    _assert_feature_flags(
        deployment_contract,
        plan.get("DEPLOYMENT_CONTRACT_SHA256"),
        plan.get("EXPECTED_FEATURE_FLAGS"),
    )
    for path in (
        backup_parent / f"backup-{operation_id}.sqlite",
        backup_parent / f"manifest-{operation_id}.json",
        staging_parent / f"staging-{operation_id}",
    ):
        if path.exists() or path.is_symlink():
            raise ProductionGateError("OPERATION_ARTIFACT_COLLISION")
    staged_args = argparse.Namespace(
        source_db=str(db_path),
        backup_dir=str(backup_parent),
        staging_root=str(staging_parent),
        target_image_id=migration_image,
        previous_image_id=previous_image,
        expected_source_revision=revision,
        synthetic_root=None,
    )
    return staged_args, pinned.path.parent / "execution.json"


def _safe_internal_manifest(plan: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(plan["BACKUP_PARENT"])) / f"manifest-{plan['OPERATION_ID']}.json"
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionGateError("INTERNAL_MANIFEST_UNAVAILABLE") from exc
    if not isinstance(payload, dict) or payload.get("OPERATION_ID") != plan["OPERATION_ID"]:
        raise ProductionGateError("INTERNAL_MANIFEST_INVALID")
    return payload


def _parse_staged_output(value: str) -> dict[str, Any]:
    lines = [line for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ProductionGateError("STAGED_EXECUTION_RESULT_INVALID")
    try:
        payload = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise ProductionGateError("STAGED_EXECUTION_RESULT_INVALID") from exc
    if not isinstance(payload, dict):
        raise ProductionGateError("STAGED_EXECUTION_RESULT_INVALID")
    return payload


def _failure_state(evidence: ExecutionEvidence, result: dict[str, Any]) -> None:
    target_changed = bool(result.get("target_may_have_changed"))
    manual = bool(result.get("manual_recovery_required"))
    updates = {
        "PUBLISH_STATE": str(result.get("publish_state", "BEFORE_EXCHANGE")),
        "TARGET_MAY_HAVE_CHANGED": target_changed,
        "AUTOMATIC_RETRY_ALLOWED": False,
        "MANUAL_RECOVERY_REQUIRED": manual,
        "ERROR_TYPE": str(result.get("error_type", "STAGED_EXECUTION_FAILED")),
        "EXIT_CLASSIFICATION": str(result.get("error_type", "STAGED_EXECUTION_FAILED")),
        "BACKUP_CREATED": bool(result.get("backup_available", False)),
        "DATABASE_MUTATED": None if target_changed else False,
    }
    if target_changed:
        evidence.transition("PUBLISH_UNCERTAIN", **updates)
        evidence.transition("MANUAL_RECOVERY_REQUIRED", **updates)
    else:
        evidence.transition("PRE_PUBLISH_FAILED", **updates)
        if manual:
            evidence.transition("MANUAL_RECOVERY_REQUIRED", **updates)


def execute_plan(args: argparse.Namespace) -> int:
    pinned = _open_plan(args.plan, args.expected_plan_sha256)
    try:
        staged_args, evidence_path = _revalidate_plan(args, pinned)
        plan = pinned.payload
        operation_id = str(plan["OPERATION_ID"])
        evidence = ExecutionEvidence(
            path=evidence_path,
            payload={
                "PLAN_SHA256": pinned.sha256,
                "OPERATION_ID": operation_id,
                "SOURCE_SHA256_BEFORE": plan["SOURCE_SHA256"],
                "BACKUP_SHA256": None,
                "STAGING_SHA256": None,
                "SOURCE_SCHEMA_BEFORE": plan["SOURCE_SCHEMA_FINGERPRINT"],
                "TARGET_SCHEMA_AFTER": None,
                "MIGRATION_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
                "MIGRATION_IMAGE_REVISION": plan["MIGRATION_IMAGE_REVISION"],
                "PREVIOUS_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
                "PUBLISH_STATE": "BEFORE_EXCHANGE",
                "TARGET_MAY_HAVE_CHANGED": False,
                "AUTOMATIC_RETRY_ALLOWED": False,
                "MANUAL_RECOVERY_REQUIRED": False,
                "FINAL_TARGET_SHA256": None,
                "COMPLETED_AT": None,
                "STATE": "PLANNED",
                "STATE_HISTORY": ["PLANNED"],
            },
        )
        _write_json_durable(evidence.path, evidence.payload)
        evidence.transition("PREFLIGHT_VERIFIED")
        phase_mapping = {
            "backup_fsynced": "BACKUP_DURABLE",
            "migration_committed": "STAGING_MIGRATED",
            "validated": "COMPATIBILITY_VERIFIED",
            "before_publish": "QUIESCENCE_HELD",
            "exchange_started": "EXCHANGE_STARTED",
            "exchange_verified": "EXCHANGE_VERIFIED",
            "parent_fsynced": "PARENT_FSYNCED",
            "final_verified": "FINAL_VERIFIED",
        }

        def observe(phase: str) -> None:
            state = phase_mapping.get(phase)
            if state is not None:
                evidence.transition(state)

        captured = io.StringIO()
        try:
            with redirect_stdout(captured):
                return_code = execute_production_staged(
                    staged_args,
                    operation_id=operation_id,
                    expected_source_identity=SourceIdentity(
                        device=int(plan["SOURCE_DEVICE"]),
                        inode=int(plan["SOURCE_INODE"]),
                        uid=int(plan["SOURCE_UID"]),
                        gid=int(plan["SOURCE_GID"]),
                        mode=int(plan["SOURCE_MODE"]),
                        size=int(plan["SOURCE_SIZE"]),
                        sha256=str(plan["SOURCE_SHA256"]),
                    ),
                    phase_callback=observe,
                )
            result = _parse_staged_output(captured.getvalue())
        except OrchestratorError as exc:
            result = {
                "status": "FAILED",
                "error_type": "QUIESCENCE_FAILED"
                if exc.code in {"SOURCE_NOT_QUIESCENT", "SOURCE_SQLITE_LEASE_FAILED"}
                else exc.code,
                "publish_state": exc.publish_state,
                "target_may_have_changed": False,
                "manual_recovery_required": False,
                "backup_available": False,
            }
            return_code = 1
        if return_code != 0 or result.get("status") != "PASS":
            if result.get("error_type") in {
                "SOURCE_NOT_QUIESCENT",
                "SOURCE_SQLITE_LEASE_FAILED",
            }:
                result = dict(result)
                result["error_type"] = "QUIESCENCE_FAILED"
            _failure_state(evidence, result)
            _json_print(
                {
                    "status": "FAILED",
                    "error_type": evidence.payload["ERROR_TYPE"],
                    "exit_classification": evidence.payload["EXIT_CLASSIFICATION"],
                    "operation_id": operation_id,
                    "publish_state": evidence.payload["PUBLISH_STATE"],
                    "target_may_have_changed": evidence.payload["TARGET_MAY_HAVE_CHANGED"],
                    "database_mutated": evidence.payload["DATABASE_MUTATED"],
                    "backup_created": evidence.payload["BACKUP_CREATED"],
                    "automatic_retry_allowed": False,
                    "manual_recovery_required": evidence.payload["MANUAL_RECOVERY_REQUIRED"],
                }
            )
            return 1
        internal = _safe_internal_manifest(plan)
        db_path = Path(str(plan["DB_CANONICAL_PATH"]))
        final_identity, final_schema, integrity, foreign_keys = _read_only_source(db_path)
        if integrity != "ok" or foreign_keys != 0:
            result = {
                "error_type": "FINAL_DATABASE_VALIDATION_FAILED",
                "publish_state": "FINAL_VERIFIED",
                "target_may_have_changed": True,
                "manual_recovery_required": True,
            }
            _failure_state(evidence, result)
            return 1
        evidence.transition(
            "COMPLETED",
            BACKUP_SHA256=internal.get("BACKUP_SHA256"),
            STAGING_SHA256=internal.get("STAGING_SHA256"),
            TARGET_SCHEMA_AFTER=final_schema,
            PUBLISH_STATE="FINAL_VERIFIED",
            TARGET_MAY_HAVE_CHANGED=True,
            AUTOMATIC_RETRY_ALLOWED=False,
            MANUAL_RECOVERY_REQUIRED=False,
            FINAL_TARGET_SHA256=final_identity["SOURCE_SHA256"],
            COMPLETED_AT=_timestamp(_now()),
            BACKUP_CREATED=True,
            BACKUP_FILE_FSYNCED=True,
            BACKUP_PARENT_FSYNCED=True,
            BACKUP_SOURCE_IDENTITY_MATCH=internal.get("BACKUP_SHA256") == plan["SOURCE_SHA256"],
        )
        if not pinned.path_matches():
            raise ProductionGateError("PLAN_PATH_SUBSTITUTED_DURING_EXECUTION")
        _json_print(
            {
                "status": "PASS",
                "mode": "EXECUTE",
                "operation_id": operation_id,
                "plan_sha256": pinned.sha256,
                "publish_state": "FINAL_VERIFIED",
                "manifest_state": "COMPLETED",
                "automatic_retry_allowed": False,
                "manual_recovery_required": False,
                "production_execution_enabled": True,
            }
        )
        return 0
    finally:
        pinned.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Explicit production staged migration gate")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--db-path", required=True)
    plan_parser.add_argument("--backup-parent", required=True)
    plan_parser.add_argument("--staging-parent", required=True)
    plan_parser.add_argument("--evidence-parent", required=True)
    plan_parser.add_argument("--deployment-contract", required=True)
    plan_parser.add_argument("--migration-image-id", required=True)
    plan_parser.add_argument("--migration-image-revision", required=True)
    plan_parser.add_argument("--previous-image-id", required=True)
    plan_parser.add_argument("--expected-hostname", required=True)
    plan_parser.add_argument("--expected-source-device", required=True, type=_nonnegative_int)
    plan_parser.add_argument("--expected-source-inode", required=True, type=_positive_int)
    plan_parser.add_argument("--expected-source-size", required=True, type=_nonnegative_int)
    plan_parser.add_argument("--expected-source-sha256", required=True)
    plan_parser.add_argument("--expected-free-bytes", required=True, type=_positive_int)
    plan_parser.add_argument("--target-schema-version", required=True)
    plan_parser.add_argument("--expires-in-seconds", required=True, type=_positive_int)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--plan", required=True)
    execute_parser.add_argument("--expected-plan-sha256", required=True)
    execute_parser.add_argument("--confirm-operation-id", required=True)
    execute_parser.add_argument("--confirm-source-sha256", required=True)
    execute_parser.add_argument("--confirm-image-revision", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            return create_plan(args)
        if args.command == "execute":
            return execute_plan(args)
        raise ProductionGateError("EXPLICIT_SUBCOMMAND_REQUIRED")
    except (ProductionGateError, OrchestratorError, OSError) as exc:
        code = exc.code if isinstance(exc, (ProductionGateError, OrchestratorError)) else type(exc).__name__
        _json_print(
            {
                "status": "FAILED",
                "error_type": code,
                "automatic_retry_allowed": False,
                "production_execution_enabled": False,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
