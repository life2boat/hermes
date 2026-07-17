#!/usr/bin/env python3
"""Root-only, hash-bound authorization gate for staged production migration."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pwd
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
from typing import Any, Sequence, TextIO


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import hermes_production_deploy as deployment  # noqa: E402
from scripts.hermes_staged_schema_migrate import (  # noqa: E402
    OrchestratorError,
    SourceIdentity,
    _execute_authorized_staged,
    _fsync_directory,
    _issue_production_authorization,
    _prepare_authorized_production_execution,
    _target_schema_contract,
    _target_schema_fingerprint,
)


PLAN_VERSION = 2
MAX_DOCUMENT_BYTES = 1024 * 1024
SHA_RE = re.compile(r"[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
CANONICAL_CONTRACT_RELATIVE_PATH = Path("deploy/hermes-production.json")
EXPECTED_FEATURE_FLAGS = {
    "HEALBITE_HOUSEHOLDS_ENABLED": "false",
    "HEALBITE_HOUSEHOLDS_ALLOWLIST": "",
    "HEALBITE_SHOPPING_LIST_ENABLED": "false",
    "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
}
SUCCESS_STATES = ("QUIESCENCE_HELD", "COMPLETED")
FAILURE_STATES = frozenset(
    {"PRE_PUBLISH_FAILED", "PUBLISH_UNCERTAIN", "MANUAL_RECOVERY_REQUIRED"}
)
PLAN_FIELDS = frozenset(
    {
        "PLAN_VERSION",
        "OPERATION_ID",
        "CREATED_AT",
        "EXPIRES_AT",
        "HOSTNAME",
        "PLAN_CREATOR_UID",
        "PLAN_CREATOR_GID",
        "PLAN_CREATOR_USERNAME",
        "PLAN_CREATOR_PROCESS_UID",
        "PLAN_CREATOR_PROCESS_GID",
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
        "TARGET_SCHEMA_FINGERPRINT",
        "REPOSITORY_ROOT",
        "DEPLOYMENT_CONTRACT_CANONICAL_PATH",
        "DEPLOYMENT_CONTRACT_DEVICE",
        "DEPLOYMENT_CONTRACT_INODE",
        "DEPLOYMENT_CONTRACT_SIZE",
        "DEPLOYMENT_CONTRACT_SHA256",
        "DEPLOYMENT_CONTRACT_VERSION",
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


class ProductionGateError(RuntimeError):
    """A public production authorization check failed closed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RootIdentity:
    effective_uid: int
    effective_gid: int
    username: str
    process_uid: int
    process_gid: int


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
            metadata = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            return False
        return (metadata.st_dev, metadata.st_ino) == (self.device, self.inode)

    def close(self) -> None:
        os.close(self.file_fd)
        os.close(self.parent_fd)


@dataclass
class PinnedDirectory:
    path: Path
    fd: int
    record: dict[str, int | str]

    def path_matches(self) -> bool:
        try:
            path_metadata = self.path.lstat()
            descriptor_metadata = os.fstat(self.fd)
        except OSError:
            return False
        return (
            path_metadata.st_dev,
            path_metadata.st_ino,
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
        ) == (
            self.record["DEVICE"],
            self.record["INODE"],
            self.record["DEVICE"],
            self.record["INODE"],
        )

    def close(self) -> None:
        os.close(self.fd)


@dataclass
class PinnedDeploymentContract:
    path: Path
    parent_fd: int
    file_fd: int
    device: int
    inode: int
    size: int
    sha256: str
    version: int
    feature_flags: dict[str, str]

    def path_matches(self) -> bool:
        try:
            metadata = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
        except OSError:
            return False
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ) == (self.device, self.inode, self.size)

    def close(self) -> None:
        os.close(self.file_fd)
        os.close(self.parent_fd)


@dataclass
class ExecutionEvidence:
    parent_fd: int
    name: str
    payload: dict[str, Any]

    def transition(self, state: str, **updates: Any) -> None:
        history = list(self.payload["STATE_HISTORY"])
        if state in SUCCESS_STATES:
            current = history[-1]
            if (
                current in SUCCESS_STATES
                and SUCCESS_STATES.index(state) <= SUCCESS_STATES.index(current)
            ):
                raise ProductionGateError("NON_MONOTONIC_EXECUTION_STATE")
        elif state not in FAILURE_STATES:
            raise ProductionGateError("UNKNOWN_EXECUTION_STATE")
        elif state in history:
            raise ProductionGateError("DUPLICATE_FAILURE_STATE")
        history.append(state)
        self.payload.update(updates)
        self.payload["STATE"] = state
        self.payload["STATE_HISTORY"] = history
        _write_json_durable_at(self.parent_fd, self.name, self.payload)


@dataclass
class ValidatedExecution:
    staged_args: argparse.Namespace
    source_identity: SourceIdentity
    backup_parent: PinnedDirectory
    staging_parent: PinnedDirectory
    evidence_parent: PinnedDirectory
    deployment_contract: PinnedDeploymentContract
    target_schema_version: str
    target_schema_fingerprint: str

    def close(self) -> None:
        self.deployment_contract.close()
        self.evidence_parent.close()
        self.staging_parent.close()
        self.backup_parent.close()


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


def _json_emit(
    payload: dict[str, Any], *, stream: TextIO | None = None
) -> None:
    stream = sys.stdout if stream is None else stream
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True), file=stream)


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
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


def _read_fd_bytes(fd: int, *, maximum: int, code: str) -> bytes:
    metadata = os.fstat(fd)
    if metadata.st_size > maximum:
        raise ProductionGateError(code)
    data = b""
    offset = 0
    while len(data) <= maximum:
        chunk = os.pread(fd, min(65536, maximum + 1 - len(data)), offset)
        if not chunk:
            break
        data += chunk
        offset += len(chunk)
    if len(data) > maximum:
        raise ProductionGateError(code)
    return data


def _write_json_durable_at(
    parent_fd: int,
    name: str,
    payload: dict[str, Any],
) -> None:
    if "/" in name or name in {"", ".", ".."}:
        raise ProductionGateError("DURABLE_MANIFEST_NAME_INVALID")
    encoded = _canonical_json(payload)
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != 0
    ):
        raise ProductionGateError("DURABLE_MANIFEST_METADATA_INVALID")


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = os.open(path.parent, flags)
    try:
        _write_json_durable_at(parent_fd, path.name, payload)
    finally:
        os.close(parent_fd)


def _root_identity() -> RootIdentity:
    getters = (
        getattr(os, "geteuid", None),
        getattr(os, "getegid", None),
        getattr(os, "getuid", None),
        getattr(os, "getgid", None),
    )
    if not all(callable(getter) for getter in getters):
        raise ProductionGateError("POSIX_IDENTITY_REQUIRED")
    effective_uid = int(getters[0]())
    if effective_uid != 0:
        raise ProductionGateError("ROOT_EUID_REQUIRED")
    effective_gid = int(getters[1]())
    try:
        username = pwd.getpwuid(effective_uid).pw_name
    except KeyError as exc:
        raise ProductionGateError("ROOT_USERNAME_UNAVAILABLE") from exc
    return RootIdentity(
        effective_uid=effective_uid,
        effective_gid=effective_gid,
        username=username,
        process_uid=int(getters[2]()),
        process_gid=int(getters[3]()),
    )


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


def _canonical_repository_root(value: str) -> Path:
    root = _absolute_path(value, "REPOSITORY_ROOT")
    _no_symlink_chain(root)
    if root != REPO_ROOT or root.resolve(strict=True) != REPO_ROOT:
        raise ProductionGateError("REPOSITORY_ROOT_MISMATCH")
    return root


def _directory_record(path: Path, *, private: bool) -> dict[str, int | str]:
    _no_symlink_chain(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise ProductionGateError("OPERATION_PARENT_NOT_DIRECTORY")
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != 0:
        raise ProductionGateError("OPERATION_PARENT_NOT_ROOT_OWNED")
    if mode & 0o022 or (private and mode != 0o700):
        raise ProductionGateError("OPERATION_PARENT_MODE_UNSAFE")
    return {
        "PATH": str(path),
        "DEVICE": int(metadata.st_dev),
        "INODE": int(metadata.st_ino),
        "UID": int(metadata.st_uid),
        "GID": int(metadata.st_gid),
        "MODE": mode,
    }


def _assert_root_private_directory(path: Path, code: str) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProductionGateError(code)


def _open_directory_record(record: object, code: str) -> PinnedDirectory:
    if not isinstance(record, dict):
        raise ProductionGateError(code)
    required = {"PATH", "DEVICE", "INODE", "UID", "GID", "MODE"}
    if set(record) != required or not isinstance(record["PATH"], str):
        raise ProductionGateError(code)
    path = _absolute_path(record["PATH"], code)
    actual = _directory_record(path, private=True)
    if actual != record:
        raise ProductionGateError(code)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags)
    metadata = os.fstat(fd)
    if (metadata.st_dev, metadata.st_ino) != (
        record["DEVICE"],
        record["INODE"],
    ):
        os.close(fd)
        raise ProductionGateError(code)
    return PinnedDirectory(path=path, fd=fd, record=dict(record))


def _assert_source_parent_controlled(path: Path) -> None:
    metadata = path.parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ProductionGateError("SOURCE_PARENT_NOT_ROOT_CONTROLLED")


def _sidecars(path: Path) -> list[Path]:
    return [
        Path(f"{path}{suffix}")
        for suffix in SIDECAR_SUFFIXES
        if Path(f"{path}{suffix}").exists()
    ]


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
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise ProductionGateError("SOURCE_PATH_SUBSTITUTION")
        if _sidecars(path):
            raise ProductionGateError("UNSUPPORTED_SQLITE_SIDECAR")
        connection = sqlite3.connect(
            f"file:{path}?mode=ro",
            uri=True,
            timeout=0,
            isolation_level=None,
        )
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=0")
        journal_mode = str(
            connection.execute("PRAGMA journal_mode").fetchone()[0]
        ).lower()
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
        integrity = str(
            connection.execute("PRAGMA integrity_check").fetchone()[0]
        ).lower()
        foreign_keys = len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        )
        source_sha = _sha256_fd(fd)
        current = os.fstat(fd)
        current_path = path.lstat()
        if (
            (current.st_dev, current.st_ino, current.st_size)
            != (metadata.st_dev, metadata.st_ino, metadata.st_size)
            or (current_path.st_dev, current_path.st_ino)
            != (metadata.st_dev, metadata.st_ino)
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
        sqlite_code = getattr(exc, "sqlite_errorcode", None)
        if isinstance(sqlite_code, int) and (sqlite_code & 0xFF) in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }:
            raise ProductionGateError("QUIESCENCE_FAILED") from exc
        raise ProductionGateError("SOURCE_SQLITE_VALIDATION_FAILED") from exc
    finally:
        if connection is not None:
            try:
                connection.rollback()
            finally:
                connection.close()
        os.close(fd)


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


def _open_canonical_deployment_contract(
    repository_root: Path,
) -> PinnedDeploymentContract:
    path = repository_root / CANONICAL_CONTRACT_RELATIVE_PATH
    _no_symlink_chain(path)
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
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
            or metadata.st_size > MAX_DOCUMENT_BYTES
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise ProductionGateError("DEPLOYMENT_CONTRACT_INVALID")
        data = _read_fd_bytes(
            file_fd,
            maximum=MAX_DOCUMENT_BYTES,
            code="DEPLOYMENT_CONTRACT_INVALID",
        )
        current = os.fstat(file_fd)
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_size,
        ) != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
        ):
            raise ProductionGateError("DEPLOYMENT_CONTRACT_DRIFT")
        try:
            contract = deployment.load_contract(
                repository_root,
                manifest_bytes=data,
            )
        except deployment.DeploymentContractError as exc:
            code = re.sub(r"[^A-Z0-9]+", "_", exc.code.upper()).strip("_")
            raise ProductionGateError(
                f"DEPLOYMENT_CONTRACT_{code}"
            ) from exc
        if (
            contract.manifest_path != path
            or contract.feature_gates != EXPECTED_FEATURE_FLAGS
        ):
            raise ProductionGateError("FEATURE_FLAGS_INVALID")
        return PinnedDeploymentContract(
            path=path,
            parent_fd=parent_fd,
            file_fd=file_fd,
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            size=int(metadata.st_size),
            sha256=_sha256_bytes(data),
            version=contract.version,
            feature_flags=dict(contract.feature_gates),
        )
    except Exception:
        os.close(file_fd)
        os.close(parent_fd)
        raise


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
            if (
                left == right
                or left.is_relative_to(right)
                or right.is_relative_to(left)
            ):
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


def _validate_plan_inputs(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, Path, Path]:
    repository_root = _canonical_repository_root(args.repository_root)
    db_path = _absolute_path(args.db_path, "DB_PATH")
    backup_parent = _absolute_path(args.backup_parent, "BACKUP_PARENT")
    staging_parent = _absolute_path(args.staging_parent, "STAGING_PARENT")
    evidence_parent = _absolute_path(args.evidence_parent, "EVIDENCE_PARENT")
    if socket.gethostname() != args.expected_hostname:
        raise ProductionGateError("HOSTNAME_MISMATCH")
    if REVISION_RE.fullmatch(args.migration_image_revision) is None:
        raise ProductionGateError("MIGRATION_IMAGE_REVISION_INVALID")
    if SHA_RE.fullmatch(args.expected_source_sha256) is None:
        raise ProductionGateError("EXPECTED_SOURCE_SHA256_INVALID")
    if args.expires_in_seconds < 60 or args.expires_in_seconds > 86400:
        raise ProductionGateError("PLAN_EXPIRY_INVALID")
    _pairwise_disjoint(
        (
            db_path,
            backup_parent,
            staging_parent,
            evidence_parent,
            repository_root,
        )
    )
    return (
        repository_root,
        db_path,
        backup_parent,
        staging_parent,
        evidence_parent,
    )


def create_plan(args: argparse.Namespace) -> int:
    root_identity = _root_identity()
    (
        repository_root,
        db_path,
        backup_parent,
        staging_parent,
        evidence_parent,
    ) = _validate_plan_inputs(args)
    backup_record = _directory_record(backup_parent, private=True)
    staging_record = _directory_record(staging_parent, private=True)
    evidence_record = _directory_record(evidence_parent, private=True)
    identity, schema_fingerprint, integrity, foreign_keys = _read_only_source(
        db_path
    )
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
    target_schema = _target_schema_contract()
    deployment_contract = _open_canonical_deployment_contract(repository_root)
    try:
        operation_id = uuid.uuid4().hex
        operation_directory = evidence_parent / operation_id
        operation_directory.mkdir(mode=0o700)
        os.chmod(operation_directory, 0o700)
        _assert_root_private_directory(
            operation_directory,
            "PLAN_OPERATION_DIRECTORY_NOT_ROOT_OWNED",
        )
        _fsync_directory(evidence_parent)
        created_at = _now()
        plan_payload: dict[str, Any] = {
            "PLAN_VERSION": PLAN_VERSION,
            "OPERATION_ID": operation_id,
            "CREATED_AT": _timestamp(created_at),
            "EXPIRES_AT": _timestamp(
                created_at + timedelta(seconds=args.expires_in_seconds)
            ),
            "HOSTNAME": args.expected_hostname,
            "PLAN_CREATOR_UID": root_identity.effective_uid,
            "PLAN_CREATOR_GID": root_identity.effective_gid,
            "PLAN_CREATOR_USERNAME": root_identity.username,
            "PLAN_CREATOR_PROCESS_UID": root_identity.process_uid,
            "PLAN_CREATOR_PROCESS_GID": root_identity.process_gid,
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
            "TARGET_SCHEMA_VERSION": target_schema.version,
            "TARGET_SCHEMA_FINGERPRINT": target_schema.fingerprint,
            "REPOSITORY_ROOT": str(repository_root),
            "DEPLOYMENT_CONTRACT_CANONICAL_PATH": str(
                deployment_contract.path
            ),
            "DEPLOYMENT_CONTRACT_DEVICE": deployment_contract.device,
            "DEPLOYMENT_CONTRACT_INODE": deployment_contract.inode,
            "DEPLOYMENT_CONTRACT_SIZE": deployment_contract.size,
            "DEPLOYMENT_CONTRACT_SHA256": deployment_contract.sha256,
            "DEPLOYMENT_CONTRACT_VERSION": deployment_contract.version,
            "EXPECTED_FEATURE_FLAGS": deployment_contract.feature_flags,
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
        _json_emit(
            {
                "status": "PASS",
                "mode": "PLAN",
                "operation_id": operation_id,
                "plan_path": str(plan_path),
                "plan_sha256": plan_sha,
                "plan_creator_uid": root_identity.effective_uid,
                "plan_creator_gid": root_identity.effective_gid,
                "plan_manifest_fsynced": True,
                "plan_parent_fsynced": True,
                "plan_read_only": True,
                "production_execution_enabled": False,
            }
        )
        return 0
    finally:
        deployment_contract.close()


def _open_plan(path_value: str, expected_sha: str) -> PinnedPlan:
    path = _absolute_path(path_value, "PLAN_PATH")
    _no_symlink_chain(path)
    if SHA_RE.fullmatch(expected_sha) is None:
        raise ProductionGateError("EXPECTED_PLAN_SHA256_INVALID")
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = os.open(path.parent, parent_flags)
    try:
        file_fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except Exception:
        os.close(parent_fd)
        raise
    try:
        parent_metadata = os.fstat(parent_fd)
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != 0
            or metadata.st_size > MAX_DOCUMENT_BYTES
        ):
            raise ProductionGateError("PLAN_FILE_METADATA_INVALID")
        data = _read_fd_bytes(
            file_fd,
            maximum=MAX_DOCUMENT_BYTES,
            code="PLAN_FILE_TOO_LARGE",
        )
        actual_sha = _sha256_bytes(data)
        if actual_sha != expected_sha:
            raise ProductionGateError("PLAN_SHA256_MISMATCH")
        try:
            payload = json.loads(data.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionGateError("PLAN_JSON_INVALID") from exc
        if not isinstance(payload, dict) or _canonical_json(payload) != data:
            raise ProductionGateError("PLAN_JSON_NOT_CANONICAL")
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
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


def _expect_plan_string(
    plan: dict[str, Any],
    name: str,
    pattern: re.Pattern[str] | None = None,
) -> str:
    value = plan.get(name)
    if not isinstance(value, str) or (
        pattern is not None and pattern.fullmatch(value) is None
    ):
        raise ProductionGateError("PLAN_CONTRACT_INVALID")
    return value


def _expect_plan_int(plan: dict[str, Any], name: str) -> int:
    value = plan.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProductionGateError("PLAN_CONTRACT_INVALID")
    return value


def _revalidate_plan(
    args: argparse.Namespace,
    pinned: PinnedPlan,
    root_identity: RootIdentity,
) -> ValidatedExecution:
    plan = pinned.payload
    if set(plan) != PLAN_FIELDS:
        raise ProductionGateError("PLAN_FIELDS_INVALID")
    if (
        plan.get("PLAN_VERSION") != PLAN_VERSION
        or plan.get("PLAN_STATE") != "PLANNED"
    ):
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
    if any(
        plan.get(name) is not value
        for name, value in expected_booleans.items()
    ):
        raise ProductionGateError("PLAN_SAFETY_CONTRACT_INVALID")
    if (
        plan.get("PLAN_CREATOR_UID") != 0
        or plan.get("PLAN_CREATOR_UID") != root_identity.effective_uid
        or plan.get("PLAN_CREATOR_GID") != root_identity.effective_gid
        or plan.get("PLAN_CREATOR_USERNAME") != root_identity.username
        or plan.get("PLAN_CREATOR_PROCESS_UID") != root_identity.process_uid
        or plan.get("PLAN_CREATOR_PROCESS_GID") != root_identity.process_gid
    ):
        raise ProductionGateError("PLAN_CREATOR_IDENTITY_MISMATCH")
    operation_id = _expect_plan_string(plan, "OPERATION_ID", OPERATION_ID_RE)
    source_sha = _expect_plan_string(plan, "SOURCE_SHA256", SHA_RE)
    revision = _expect_plan_string(
        plan,
        "MIGRATION_IMAGE_REVISION",
        REVISION_RE,
    )
    if operation_id != args.confirm_operation_id:
        raise ProductionGateError("PLAN_OPERATION_ID_MISMATCH")
    if source_sha != args.confirm_source_sha256:
        raise ProductionGateError("PLAN_SOURCE_SHA256_CONFIRMATION_MISMATCH")
    if revision != args.confirm_image_revision:
        raise ProductionGateError("PLAN_IMAGE_REVISION_CONFIRMATION_MISMATCH")
    expires_at = _parse_timestamp(plan.get("EXPIRES_AT"), "PLAN_EXPIRY_INVALID")
    created_at = _parse_timestamp(
        plan.get("CREATED_AT"),
        "PLAN_CREATED_AT_INVALID",
    )
    if expires_at <= created_at or expires_at - created_at > timedelta(days=1):
        raise ProductionGateError("PLAN_EXPIRY_INVALID")
    if _now() >= expires_at:
        raise ProductionGateError("PLAN_EXPIRED")
    if socket.gethostname() != _expect_plan_string(plan, "HOSTNAME"):
        raise ProductionGateError("HOSTNAME_DRIFT")

    backup_parent: PinnedDirectory | None = None
    staging_parent: PinnedDirectory | None = None
    evidence_parent: PinnedDirectory | None = None
    deployment_contract: PinnedDeploymentContract | None = None
    try:
        evidence_parent = _open_directory_record(
            plan.get("EVIDENCE_PARENT_IDENTITY"),
            "EVIDENCE_PARENT_DRIFT",
        )
        expected_plan_path = (
            evidence_parent.path / operation_id / "plan.json"
        )
        if pinned.path != expected_plan_path or not pinned.path_matches():
            raise ProductionGateError("PLAN_PATH_SUBSTITUTION")
        operation_record = _directory_record(
            pinned.path.parent,
            private=True,
        )
        if operation_record["PATH"] != str(
            evidence_parent.path / operation_id
        ):
            raise ProductionGateError("PLAN_OPERATION_DIRECTORY_INVALID")
        backup_parent = _open_directory_record(
            plan.get("BACKUP_PARENT_IDENTITY"),
            "BACKUP_PARENT_DRIFT",
        )
        staging_parent = _open_directory_record(
            plan.get("STAGING_PARENT_IDENTITY"),
            "STAGING_PARENT_DRIFT",
        )
        if (
            plan.get("BACKUP_PARENT") != str(backup_parent.path)
            or plan.get("STAGING_PARENT") != str(staging_parent.path)
            or plan.get("EVIDENCE_PARENT") != str(evidence_parent.path)
        ):
            raise ProductionGateError("PLAN_PATH_FIELDS_INVALID")

        repository_root = _canonical_repository_root(
            _expect_plan_string(plan, "REPOSITORY_ROOT")
        )
        deployment_contract = _open_canonical_deployment_contract(
            repository_root
        )
        contract_fields = {
            "DEPLOYMENT_CONTRACT_CANONICAL_PATH": str(
                deployment_contract.path
            ),
            "DEPLOYMENT_CONTRACT_DEVICE": deployment_contract.device,
            "DEPLOYMENT_CONTRACT_INODE": deployment_contract.inode,
            "DEPLOYMENT_CONTRACT_SIZE": deployment_contract.size,
            "DEPLOYMENT_CONTRACT_SHA256": deployment_contract.sha256,
            "DEPLOYMENT_CONTRACT_VERSION": deployment_contract.version,
            "EXPECTED_FEATURE_FLAGS": deployment_contract.feature_flags,
        }
        if any(plan.get(name) != value for name, value in contract_fields.items()):
            raise ProductionGateError("DEPLOYMENT_CONTRACT_DRIFT")

        db_path = _absolute_path(
            _expect_plan_string(plan, "DB_CANONICAL_PATH"),
            "DB_PATH",
        )
        _pairwise_disjoint(
            (
                db_path,
                backup_parent.path,
                staging_parent.path,
                evidence_parent.path,
                repository_root,
            )
        )
        identity, source_schema, integrity, foreign_keys = _read_only_source(
            db_path
        )
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
        if plan.get("SOURCE_SCHEMA_FINGERPRINT") != source_schema:
            raise ProductionGateError("SOURCE_SCHEMA_DRIFT")
        if integrity != "ok" or foreign_keys != 0:
            raise ProductionGateError("SOURCE_DATABASE_INVALID")
        if (
            plan.get("EXPECTED_FILESYSTEM_DEVICE")
            != identity["SOURCE_DEVICE"]
            or os.fstat(staging_parent.fd).st_dev
            != identity["SOURCE_DEVICE"]
        ):
            raise ProductionGateError("FILESYSTEM_DRIFT")
        expected_free = _expect_plan_int(plan, "EXPECTED_FREE_BYTES")
        if expected_free <= 0:
            raise ProductionGateError("PLAN_FREE_SPACE_INVALID")
        _require_free_bytes(staging_parent.path, expected_free)
        migration_image = _expect_plan_string(
            plan,
            "MIGRATION_IMAGE_ID",
            IMAGE_ID_RE,
        )
        previous_image = _expect_plan_string(
            plan,
            "PREVIOUS_IMAGE_ID",
            IMAGE_ID_RE,
        )
        _inspect_image(migration_image, revision)
        _inspect_image(previous_image, None)
        expected_target = _target_schema_contract()
        if (
            plan.get("TARGET_SCHEMA_VERSION") != expected_target.version
            or plan.get("TARGET_SCHEMA_FINGERPRINT")
            != expected_target.fingerprint
        ):
            raise ProductionGateError("TARGET_SCHEMA_CONTRACT_MISMATCH")
        for path in (
            backup_parent.path / f"backup-{operation_id}.sqlite",
            backup_parent.path / f"manifest-{operation_id}.json",
            staging_parent.path / f"staging-{operation_id}",
            pinned.path.parent / "execution.json",
        ):
            if path.exists() or path.is_symlink():
                raise ProductionGateError("OPERATION_ARTIFACT_COLLISION")
        source_identity = SourceIdentity(
            device=int(plan["SOURCE_DEVICE"]),
            inode=int(plan["SOURCE_INODE"]),
            uid=int(plan["SOURCE_UID"]),
            gid=int(plan["SOURCE_GID"]),
            mode=int(plan["SOURCE_MODE"]),
            size=int(plan["SOURCE_SIZE"]),
            sha256=str(plan["SOURCE_SHA256"]),
        )
        staged_args = argparse.Namespace(
            source_db=str(db_path),
            backup_dir=str(backup_parent.path),
            staging_root=str(staging_parent.path),
            target_image_id=migration_image,
            previous_image_id=previous_image,
            expected_source_revision=revision,
            synthetic_root=None,
        )
        return ValidatedExecution(
            staged_args=staged_args,
            source_identity=source_identity,
            backup_parent=backup_parent,
            staging_parent=staging_parent,
            evidence_parent=evidence_parent,
            deployment_contract=deployment_contract,
            target_schema_version=expected_target.version,
            target_schema_fingerprint=expected_target.fingerprint,
        )
    except Exception:
        if deployment_contract is not None:
            deployment_contract.close()
        if evidence_parent is not None:
            evidence_parent.close()
        if staging_parent is not None:
            staging_parent.close()
        if backup_parent is not None:
            backup_parent.close()
        raise


def _read_internal_manifest(
    backup_parent: PinnedDirectory,
    operation_id: str,
) -> dict[str, Any]:
    name = f"manifest-{operation_id}.json"
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=backup_parent.fd,
    )
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_DOCUMENT_BYTES
        ):
            raise ProductionGateError("INTERNAL_MANIFEST_INVALID")
        data = _read_fd_bytes(
            fd,
            maximum=MAX_DOCUMENT_BYTES,
            code="INTERNAL_MANIFEST_INVALID",
        )
        try:
            payload = json.loads(data.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionGateError("INTERNAL_MANIFEST_INVALID") from exc
        path_metadata = os.stat(
            name,
            dir_fd=backup_parent.fd,
            follow_symlinks=False,
        )
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise ProductionGateError("INTERNAL_MANIFEST_SUBSTITUTED")
        if (
            not isinstance(payload, dict)
            or payload.get("OPERATION_ID") != operation_id
        ):
            raise ProductionGateError("INTERNAL_MANIFEST_INVALID")
        return payload
    finally:
        os.close(fd)


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


def _failure_updates(result: dict[str, Any]) -> dict[str, Any]:
    target_changed = bool(result.get("target_may_have_changed"))
    manual = bool(result.get("manual_recovery_required"))
    classification = str(
        result.get("exit_classification")
        or result.get("error_type")
        or "STAGED_EXECUTION_FAILED"
    )
    return {
        "PUBLISH_STATE": str(
            result.get("publish_state", "BEFORE_EXCHANGE")
        ),
        "TARGET_MAY_HAVE_CHANGED": target_changed,
        "AUTOMATIC_RETRY_ALLOWED": False,
        "MANUAL_RECOVERY_REQUIRED": manual,
        "ERROR_TYPE": classification,
        "EXIT_CLASSIFICATION": classification,
        "BACKUP_CREATED": bool(result.get("backup_available", False)),
        "DATABASE_MUTATED": None if target_changed else False,
    }


def _record_failure(
    evidence: ExecutionEvidence,
    result: dict[str, Any],
) -> None:
    updates = _failure_updates(result)
    if updates["TARGET_MAY_HAVE_CHANGED"]:
        evidence.transition("PUBLISH_UNCERTAIN", **updates)
        evidence.transition("MANUAL_RECOVERY_REQUIRED", **updates)
    else:
        evidence.transition("PRE_PUBLISH_FAILED", **updates)
        if updates["MANUAL_RECOVERY_REQUIRED"]:
            evidence.transition("MANUAL_RECOVERY_REQUIRED", **updates)


def _emit_failure(
    operation_id: str,
    result: dict[str, Any],
    *,
    stream: TextIO | None = None,
) -> None:
    stream = sys.stdout if stream is None else stream
    updates = _failure_updates(result)
    _json_emit(
        {
            "status": "FAILED",
            "error_type": updates["ERROR_TYPE"],
            "exit_classification": updates["EXIT_CLASSIFICATION"],
            "operation_id": operation_id,
            "publish_state": updates["PUBLISH_STATE"],
            "target_may_have_changed": updates["TARGET_MAY_HAVE_CHANGED"],
            "database_mutated": updates["DATABASE_MUTATED"],
            "backup_created": updates["BACKUP_CREATED"],
            "automatic_retry_allowed": False,
            "manual_recovery_required": updates[
                "MANUAL_RECOVERY_REQUIRED"
            ],
        },
        stream=stream,
    )


def _post_exchange_uncertain(
    evidence: ExecutionEvidence,
    operation_id: str,
    reason: str,
    *,
    publish_state: str = "EXCHANGE_COMPLETED_NOT_VERIFIED",
) -> int:
    result = {
        "error_type": "PUBLISH_UNCERTAIN",
        "exit_classification": "PUBLISH_UNCERTAIN",
        "failure_reason": reason,
        "publish_state": publish_state,
        "target_may_have_changed": True,
        "manual_recovery_required": True,
        "automatic_retry_allowed": False,
    }
    evidence_persisted = True
    try:
        _record_failure(evidence, result)
    except Exception:
        evidence_persisted = False
    _json_emit(
        {
            "status": "FAILED",
            "error_type": "PUBLISH_UNCERTAIN",
            "exit_classification": "PUBLISH_UNCERTAIN",
            "operation_id": operation_id,
            "publish_state": publish_state,
            "target_may_have_changed": True,
            "automatic_retry_allowed": False,
            "manual_recovery_required": True,
            "durable_evidence_persisted": evidence_persisted,
        },
        stream=sys.stderr,
    )
    return 1


def _quiescence_error(exc: OrchestratorError) -> str:
    if exc.code in {
        "SOURCE_NOT_QUIESCENT",
        "SOURCE_SQLITE_LEASE_FAILED",
        "SOURCE_SQLITE_SIDECAR_PRESENT",
    }:
        return "QUIESCENCE_FAILED"
    return exc.code


def execute_plan(args: argparse.Namespace) -> int:
    root_identity = _root_identity()
    pinned = _open_plan(args.plan, args.expected_plan_sha256)
    validated: ValidatedExecution | None = None
    prepared: Any = None
    try:
        validated = _revalidate_plan(args, pinned, root_identity)
        plan = pinned.payload
        operation_id = str(plan["OPERATION_ID"])
        authorization = _issue_production_authorization(
            operation_id=operation_id,
            plan_sha256=pinned.sha256,
            source_identity=validated.source_identity,
            image_revision=str(plan["MIGRATION_IMAGE_REVISION"]),
            target_schema_version=validated.target_schema_version,
            target_schema_fingerprint=validated.target_schema_fingerprint,
        )
        try:
            prepared = _prepare_authorized_production_execution(
                validated.staged_args,
                authorization=authorization,
                expected_source_identity=validated.source_identity,
                backup_parent_fd=validated.backup_parent.fd,
            )
        except OrchestratorError as exc:
            code = _quiescence_error(exc)
            _emit_failure(
                operation_id,
                {
                    "error_type": code,
                    "exit_classification": code,
                    "publish_state": "BEFORE_EXCHANGE",
                    "target_may_have_changed": False,
                    "manual_recovery_required": False,
                    "backup_available": False,
                },
            )
            return 1
        if (
            not pinned.path_matches()
            or not validated.deployment_contract.path_matches()
            or not validated.backup_parent.path_matches()
            or not validated.staging_parent.path_matches()
            or not validated.evidence_parent.path_matches()
        ):
            raise ProductionGateError("PINNED_AUTHORITY_DRIFT")

        evidence = ExecutionEvidence(
            parent_fd=pinned.parent_fd,
            name="execution.json",
            payload={
                "PLAN_SHA256": pinned.sha256,
                "OPERATION_ID": operation_id,
                "SOURCE_SHA256_BEFORE": plan["SOURCE_SHA256"],
                "SOURCE_SCHEMA_BEFORE": plan[
                    "SOURCE_SCHEMA_FINGERPRINT"
                ],
                "TARGET_SCHEMA_VERSION": validated.target_schema_version,
                "TARGET_SCHEMA_FINGERPRINT": (
                    validated.target_schema_fingerprint
                ),
                "MIGRATION_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
                "MIGRATION_IMAGE_REVISION": plan[
                    "MIGRATION_IMAGE_REVISION"
                ],
                "PREVIOUS_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
                "PUBLISH_STATE": "BEFORE_EXCHANGE",
                "TARGET_MAY_HAVE_CHANGED": False,
                "AUTOMATIC_RETRY_ALLOWED": False,
                "MANUAL_RECOVERY_REQUIRED": False,
                "FINAL_TARGET_SHA256": None,
                "COMPLETED_AT": None,
                "STATE": "QUIESCENCE_HELD",
                "STATE_HISTORY": ["QUIESCENCE_HELD"],
                "QUIESCENCE_ACQUIRED_BEFORE_EXECUTION_EVIDENCE": True,
            },
        )
        _write_json_durable_at(
            evidence.parent_fd,
            evidence.name,
            evidence.payload,
        )

        captured = io.StringIO()
        try:
            with redirect_stdout(captured):
                return_code = _execute_authorized_staged(
                    validated.staged_args,
                    prepared=prepared,
                )
            prepared = None
            result = _parse_staged_output(captured.getvalue())
        except Exception as exc:
            reason = (
                exc.code
                if isinstance(exc, (ProductionGateError, OrchestratorError))
                else type(exc).__name__
            )
            return _post_exchange_uncertain(
                evidence,
                operation_id,
                reason,
            )
        if return_code != 0 or result.get("status") != "PASS":
            try:
                _record_failure(evidence, result)
            except Exception as exc:
                publish_state = str(
                    result.get("publish_state", "BEFORE_EXCHANGE")
                )
                if (
                    bool(result.get("target_may_have_changed"))
                    or publish_state
                    not in {"BEFORE_EXCHANGE", "EXCHANGE_REVERSED"}
                ):
                    reason = (
                        exc.code
                        if isinstance(
                            exc, (ProductionGateError, OrchestratorError)
                        )
                        else type(exc).__name__
                    )
                    return _post_exchange_uncertain(
                        evidence,
                        operation_id,
                        reason,
                        publish_state=publish_state,
                    )
                raise
            _emit_failure(operation_id, result)
            return 1

        try:
            internal = _read_internal_manifest(
                validated.backup_parent,
                operation_id,
            )
            if (
                internal.get("STATE") != "VERIFIED"
                or internal.get("PUBLISH_STATE") != "FINAL_VERIFIED"
            ):
                raise ProductionGateError(
                    "INTERNAL_MANIFEST_NOT_FINAL_VERIFIED"
                )
            db_path = Path(str(plan["DB_CANONICAL_PATH"]))
            final_identity, _whole_schema, integrity, foreign_keys = (
                _read_only_source(db_path)
            )
            if integrity != "ok" or foreign_keys != 0:
                raise ProductionGateError(
                    "FINAL_DATABASE_VALIDATION_FAILED"
                )
            actual_target_fingerprint = _target_schema_fingerprint(db_path)
            if (
                actual_target_fingerprint
                != validated.target_schema_fingerprint
            ):
                raise ProductionGateError(
                    "FINAL_TARGET_SCHEMA_MISMATCH"
                )
            if (
                not pinned.path_matches()
                or not validated.deployment_contract.path_matches()
                or not validated.backup_parent.path_matches()
                or not validated.staging_parent.path_matches()
                or not validated.evidence_parent.path_matches()
            ):
                raise ProductionGateError(
                    "PINNED_AUTHORITY_DRIFT_AFTER_EXCHANGE"
                )
            evidence.transition(
                "COMPLETED",
                BACKUP_SHA256=internal.get("BACKUP_SHA256"),
                STAGING_SHA256=internal.get("STAGING_SHA256"),
                TARGET_SCHEMA_AFTER=actual_target_fingerprint,
                PUBLISH_STATE="FINAL_VERIFIED",
                TARGET_MAY_HAVE_CHANGED=True,
                AUTOMATIC_RETRY_ALLOWED=False,
                MANUAL_RECOVERY_REQUIRED=False,
                FINAL_TARGET_SHA256=final_identity["SOURCE_SHA256"],
                COMPLETED_AT=_timestamp(_now()),
                BACKUP_CREATED=True,
                BACKUP_FILE_FSYNCED=True,
                BACKUP_PARENT_FSYNCED=True,
                BACKUP_SOURCE_IDENTITY_MATCH=(
                    internal.get("BACKUP_SHA256")
                    == plan["SOURCE_SHA256"]
                ),
                FINAL_SCHEMA_MATCHES_PLANNED_TARGET=True,
            )
        except Exception as exc:
            reason = (
                exc.code
                if isinstance(exc, (ProductionGateError, OrchestratorError))
                else type(exc).__name__
            )
            return _post_exchange_uncertain(
                evidence,
                operation_id,
                reason,
                publish_state="FINAL_VERIFIED",
            )
        try:
            _json_emit(
                {
                    "status": "PASS",
                    "mode": "EXECUTE",
                    "operation_id": operation_id,
                    "plan_sha256": pinned.sha256,
                    "publish_state": "FINAL_VERIFIED",
                    "manifest_state": "COMPLETED",
                    "target_schema_version": (
                        validated.target_schema_version
                    ),
                    "target_schema_fingerprint_match": True,
                    "automatic_retry_allowed": False,
                    "manual_recovery_required": False,
                    "production_execution_enabled": True,
                }
            )
        except Exception as exc:
            reason = (
                exc.code
                if isinstance(exc, (ProductionGateError, OrchestratorError))
                else type(exc).__name__
            )
            return _post_exchange_uncertain(
                evidence,
                operation_id,
                reason,
                publish_state="FINAL_VERIFIED",
            )
        return 0
    finally:
        if prepared is not None:
            prepared.close()
        if validated is not None:
            validated.close()
        pinned.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Explicit root-only production staged migration gate"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--repository-root", required=True)
    plan_parser.add_argument("--db-path", required=True)
    plan_parser.add_argument("--backup-parent", required=True)
    plan_parser.add_argument("--staging-parent", required=True)
    plan_parser.add_argument("--evidence-parent", required=True)
    plan_parser.add_argument("--migration-image-id", required=True)
    plan_parser.add_argument("--migration-image-revision", required=True)
    plan_parser.add_argument("--previous-image-id", required=True)
    plan_parser.add_argument("--expected-hostname", required=True)
    plan_parser.add_argument(
        "--expected-source-device",
        required=True,
        type=_nonnegative_int,
    )
    plan_parser.add_argument(
        "--expected-source-inode",
        required=True,
        type=_positive_int,
    )
    plan_parser.add_argument(
        "--expected-source-size",
        required=True,
        type=_nonnegative_int,
    )
    plan_parser.add_argument("--expected-source-sha256", required=True)
    plan_parser.add_argument(
        "--expected-free-bytes",
        required=True,
        type=_positive_int,
    )
    plan_parser.add_argument(
        "--expires-in-seconds",
        required=True,
        type=_positive_int,
    )
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
        code = (
            exc.code
            if isinstance(exc, (ProductionGateError, OrchestratorError))
            else type(exc).__name__
        )
        _json_emit(
            {
                "status": "FAILED",
                "error_type": code,
                "exit_classification": code,
                "publish_state": "BEFORE_EXCHANGE",
                "target_may_have_changed": False,
                "automatic_retry_allowed": False,
                "manual_recovery_required": False,
                "production_execution_enabled": False,
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
