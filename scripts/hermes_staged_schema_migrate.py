#!/usr/bin/env python3
"""Plan and exercise durable staged SQLite schema publication.

This module owns the staged-copy implementation. Public production authorization
is deliberately kept in a separate host-side gate.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


SHA_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
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
RENAME_EXCHANGE = 2
PUBLISH_STATES = (
    "BEFORE_EXCHANGE",
    "EXCHANGE_STARTED",
    "EXCHANGE_COMPLETED_NOT_VERIFIED",
    "EXCHANGE_VERIFIED_NOT_FSYNCED",
    "PARENT_FSYNCED",
    "FINAL_VERIFIED",
    "EXCHANGE_REVERSED",
)
PUBLISH_MAY_HAVE_CHANGED = frozenset(
    {
        "EXCHANGE_STARTED",
        "EXCHANGE_COMPLETED_NOT_VERIFIED",
        "EXCHANGE_VERIFIED_NOT_FSYNCED",
        "PARENT_FSYNCED",
        "FINAL_VERIFIED",
    }
)
_PRODUCTION_AUTHORIZATION_SEAL = object()


class OrchestratorError(RuntimeError):
    def __init__(self, code: str, *, publish_state: str = "BEFORE_EXCHANGE") -> None:
        super().__init__(code)
        self.code = code
        self.publish_state = publish_state


@dataclass(frozen=True)
class _CleanupFailureRecord:
    resource_kind: str
    cleanup_phase: str
    error_type: str
    error_code: str

    def as_payload(self) -> dict[str, str]:
        return {
            "resource_kind": self.resource_kind,
            "cleanup_phase": self.cleanup_phase,
            "error_type": self.error_type,
            "error_code": self.error_code,
        }


class _CleanupAggregateError(RuntimeError):
    def __init__(self, records: Sequence[_CleanupFailureRecord]) -> None:
        self.records = tuple(records)
        super().__init__("OWNED_RESOURCE_CLEANUP_FAILED")


class _PrimaryAndCleanupError(RuntimeError):
    def __init__(
        self,
        primary: Exception,
        records: Sequence[_CleanupFailureRecord],
    ) -> None:
        self.primary = primary
        self.records = tuple(records)
        super().__init__("PRIMARY_AND_CLEANUP_FAILED")


class _StagedCleanupTransport(RuntimeError):
    def __init__(
        self,
        *,
        result_snapshot: dict[str, Any],
        publish_state: str,
        exchange_started: bool,
        cleanup_failures: Sequence[_CleanupFailureRecord],
        durable_evidence_updated: bool,
    ) -> None:
        self.result_snapshot = dict(result_snapshot)
        self.publish_state = publish_state
        self.exchange_started = exchange_started
        self.cleanup_failures = tuple(cleanup_failures)
        self.durable_evidence_updated = durable_evidence_updated
        super().__init__("STAGED_CLEANUP_TRANSPORT")


def _cleanup_error_type(exc: Exception) -> str:
    value = re.sub(r"[^A-Za-z0-9_]+", "_", type(exc).__name__)
    return value[:80] or "Exception"


def _cleanup_failure_record(
    resource_kind: str,
    cleanup_phase: str,
    error_code: str,
    exc: Exception,
) -> _CleanupFailureRecord:
    resolved_code = (
        exc.code if isinstance(exc, OrchestratorError) else error_code
    )
    return _CleanupFailureRecord(
        resource_kind=resource_kind,
        cleanup_phase=cleanup_phase,
        error_type=_cleanup_error_type(exc),
        error_code=resolved_code,
    )


def _cleanup_records_from_exception(
    resource_kind: str,
    cleanup_phase: str,
    error_code: str,
    exc: Exception,
) -> tuple[_CleanupFailureRecord, ...]:
    if isinstance(exc, _PrimaryAndCleanupError):
        return (
            *_cleanup_records_from_exception(
                resource_kind,
                cleanup_phase,
                error_code,
                exc.primary,
            ),
            *exc.records,
        )
    if isinstance(exc, _CleanupAggregateError):
        return exc.records
    return (
        _cleanup_failure_record(
            resource_kind,
            cleanup_phase,
            error_code,
            exc,
        ),
    )


def _split_primary_cleanup(
    exc: Exception,
) -> tuple[Exception, tuple[_CleanupFailureRecord, ...]]:
    failures: list[_CleanupFailureRecord] = []
    primary = exc
    while isinstance(primary, _PrimaryAndCleanupError):
        failures.extend(primary.records)
        primary = primary.primary
    if isinstance(primary, _CleanupAggregateError):
        failures.extend(primary.records)
        primary = OrchestratorError("CLEANUP_FAILED")
    return primary, tuple(failures)


def _attempt_owned_cleanup(
    resource_kind: str,
    cleanup_phase: str,
    error_code: str,
    callback: Callable[[], None],
) -> tuple[_CleanupFailureRecord, ...]:
    try:
        callback()
    except Exception as exc:
        return _cleanup_records_from_exception(
            resource_kind,
            cleanup_phase,
            error_code,
            exc,
        )
    return ()


def _run_owned_cleanup(
    *steps: tuple[str, str, str, Callable[[], None]],
) -> None:
    failures: list[_CleanupFailureRecord] = []
    for resource_kind, cleanup_phase, error_code, callback in steps:
        failures.extend(
            _attempt_owned_cleanup(
                resource_kind,
                cleanup_phase,
                error_code,
                callback,
            )
        )
    if failures:
        raise _CleanupAggregateError(failures)


def _run_body_with_owned_cleanup(
    body: Callable[[], Any],
    *steps: tuple[str, str, str, Callable[[], None]],
) -> Any:
    try:
        result = body()
    except Exception as primary:
        failures: list[_CleanupFailureRecord] = []
        for resource_kind, cleanup_phase, error_code, callback in steps:
            failures.extend(
                _attempt_owned_cleanup(
                    resource_kind,
                    cleanup_phase,
                    error_code,
                    callback,
                )
            )
        if failures:
            raise _PrimaryAndCleanupError(
                primary,
                failures,
            ) from primary
        raise
    _run_owned_cleanup(*steps)
    return result


def _unlink_path_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _unlink_at_if_exists(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    if not callable(getter):
        raise OrchestratorError("POSIX_IDENTITY_UNAVAILABLE")
    return int(getter())


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


@dataclass(frozen=True)
class TargetSchemaContract:
    version: str
    fingerprint: str



@dataclass
class PinnedDatabase:
    path: Path
    parent_fd: int
    file_fd: int
    identity: SourceIdentity

    def close(self) -> None:
        _run_owned_cleanup(
            (
                "PINNED_DATABASE_FILE_DESCRIPTOR",
                "RESOURCE_RELEASE",
                "PINNED_DATABASE_FILE_CLOSE_FAILED",
                lambda: os.close(self.file_fd),
            ),
            (
                "PINNED_DATABASE_PARENT_DESCRIPTOR",
                "RESOURCE_RELEASE",
                "PINNED_DATABASE_PARENT_CLOSE_FAILED",
                lambda: os.close(self.parent_fd),
            ),
        )


@dataclass
class SQLiteLease:
    connection: sqlite3.Connection
    label: str

    def close(self) -> None:
        _run_owned_cleanup(
            (
                f"{self.label}_SQLITE_LEASE",
                "ROLLBACK",
                f"{self.label}_SQLITE_ROLLBACK_FAILED",
                self.connection.rollback,
            ),
            (
                f"{self.label}_SQLITE_LEASE",
                "CONNECTION_CLOSE",
                f"{self.label}_SQLITE_CONNECTION_CLOSE_FAILED",
                self.connection.close,
            ),
        )

@dataclass(frozen=True)
class _ProductionAuthorization:
    operation_id: str
    plan_sha256: str
    source_identity: SourceIdentity
    image_revision: str
    target_schema_version: str
    target_schema_fingerprint: str
    _seal: object = field(repr=False)


@dataclass
class _PreparedProductionExecution:
    contract: Contract
    source_identity: SourceIdentity
    source_pin: PinnedDatabase | None
    source_lease: SQLiteLease | None
    backup_parent_fd: int | None
    authorization: _ProductionAuthorization
    consumed: bool = False

    def take(
        self,
    ) -> tuple[PinnedDatabase, SQLiteLease, int]:
        if self.consumed or self.source_pin is None or self.source_lease is None:
            raise OrchestratorError("PRODUCTION_AUTHORIZATION_ALREADY_CONSUMED")
        if self.backup_parent_fd is None:
            raise OrchestratorError("PRODUCTION_AUTHORIZATION_DIRECTORY_MISSING")
        self.consumed = True
        source_pin = self.source_pin
        source_lease = self.source_lease
        backup_parent_fd = self.backup_parent_fd
        self.source_pin = None
        self.source_lease = None
        self.backup_parent_fd = None
        return source_pin, source_lease, backup_parent_fd

    def close(self) -> None:
        steps: list[tuple[str, str, str, Callable[[], None]]] = []
        if self.source_lease is not None:
            source_lease = self.source_lease
            self.source_lease = None
            steps.append(
                (
                    "SOURCE_SQLITE_LEASE",
                    "PREPARED_EXECUTION_RELEASE",
                    "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
                    source_lease.close,
                )
            )
        if self.source_pin is not None:
            source_pin = self.source_pin
            self.source_pin = None
            steps.append(
                (
                    "SOURCE_PINNED_DATABASE",
                    "PREPARED_EXECUTION_RELEASE",
                    "SOURCE_PINNED_DATABASE_CLOSE_FAILED",
                    source_pin.close,
                )
            )
        if self.backup_parent_fd is not None:
            backup_parent_fd = self.backup_parent_fd
            self.backup_parent_fd = None
            steps.append(
                (
                    "MANIFEST_PARENT_DESCRIPTOR",
                    "PREPARED_EXECUTION_RELEASE",
                    "MANIFEST_PARENT_CLOSE_FAILED",
                    lambda: os.close(backup_parent_fd),
                )
            )
        _run_owned_cleanup(*steps)



@dataclass
class DurableManifest:
    path: Path
    payload: dict[str, Any]
    parent_fd: int | None = field(default=None, repr=False)
    failure_callback: Callable[[str, str], None] | None = field(default=None, repr=False)

    def checkpoint(self, **updates: Any) -> None:
        self.payload.update(updates)
        self._write()

    def _write(self) -> None:
        if self.failure_callback is not None:
            self.failure_callback("manifest_fsync", str(self.payload.get("PUBLISH_STATE", "BEFORE_EXCHANGE")))
        if self.parent_fd is None:
            _write_json_durable(self.path, self.payload)
        else:
            _write_json_durable_at(
                self.parent_fd, self.path.name, self.payload
            )

    def transition(self, state: str, **updates: Any) -> None:
        if state not in STATE_RANK:
            raise OrchestratorError("UNKNOWN_MANIFEST_STATE")
        previous = str(self.payload.get("STATE", "PLANNED"))
        if state != "FAILED" and STATE_RANK[state] < STATE_RANK.get(previous, -1):
            raise OrchestratorError("NON_MONOTONIC_MANIFEST_STATE")
        self.payload.update(updates)
        self.payload["STATE"] = state
        self._write()


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    handle = path.open("rb")

    def read_hash() -> str:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()

    return str(
        _run_body_with_owned_cleanup(
            read_hash,
            (
                "HASH_SOURCE_FILE_HANDLE",
                "SCOPED_RESOURCE_RELEASE",
                "HASH_SOURCE_FILE_CLOSE_FAILED",
                handle.close,
            ),
        )
    )


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _fsync_file(path: Path) -> None:
    handle = path.open("rb")
    _run_body_with_owned_cleanup(
        lambda: os.fsync(handle.fileno()),
        (
            "FSYNC_FILE_HANDLE",
            "SCOPED_RESOURCE_RELEASE",
            "FSYNC_FILE_CLOSE_FAILED",
            handle.close,
        ),
    )


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    _run_body_with_owned_cleanup(
        lambda: os.fsync(fd),
        (
            "FSYNC_DIRECTORY_DESCRIPTOR",
            "SCOPED_RESOURCE_RELEASE",
            "FSYNC_DIRECTORY_CLOSE_FAILED",
            lambda: os.close(fd),
        ),
    )


def _fsync_fd(fd: int) -> None:
    os.fsync(fd)


def _write_json_durable(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(temporary, flags, 0o600)
    try:
        handle = os.fdopen(fd, "w", encoding="ascii")
    except Exception as primary:
        failures = [
            *_attempt_owned_cleanup(
                "MANIFEST_TEMP_FILE_DESCRIPTOR",
                "ACQUISITION_FAILURE_RELEASE",
                "MANIFEST_TEMP_FILE_CLOSE_FAILED",
                lambda: os.close(fd),
            ),
            *_attempt_owned_cleanup(
                "MANIFEST_TEMP_FILE",
                "ACQUISITION_FAILURE_RELEASE",
                "MANIFEST_TEMP_FILE_UNLINK_FAILED",
                lambda: _unlink_path_if_exists(temporary),
            ),
        ]
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise

    def write_manifest() -> None:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())

    try:
        _run_body_with_owned_cleanup(
            write_manifest,
            (
                "MANIFEST_TEMP_FILE_HANDLE",
                "SCOPED_RESOURCE_RELEASE",
                "MANIFEST_TEMP_FILE_CLOSE_FAILED",
                handle.close,
            ),
        )
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception as primary:
        failures = _attempt_owned_cleanup(
            "MANIFEST_TEMP_FILE",
            "FAILURE_CLEANUP",
            "MANIFEST_TEMP_FILE_UNLINK_FAILED",
            lambda: _unlink_path_if_exists(temporary),
        )
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise


def _write_json_durable_at(
    parent_fd: int,
    name: str,
    payload: dict[str, Any],
) -> None:
    if "/" in name or name in {"", ".", ".."}:
        raise OrchestratorError("MANIFEST_NAME_INVALID")
    encoded = (
        json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n"
    ).encode("ascii")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(temporary, flags, 0o600, dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o600)
        handle = os.fdopen(fd, "wb")
    except Exception as primary:
        failures = [
            *_attempt_owned_cleanup(
                "MANIFEST_TEMP_FILE_DESCRIPTOR",
                "ACQUISITION_FAILURE_RELEASE",
                "MANIFEST_TEMP_FILE_CLOSE_FAILED",
                lambda: os.close(fd),
            ),
            *_attempt_owned_cleanup(
                "MANIFEST_TEMP_FILE",
                "ACQUISITION_FAILURE_RELEASE",
                "MANIFEST_TEMP_FILE_UNLINK_FAILED",
                lambda: _unlink_at_if_exists(parent_fd, temporary),
            ),
        ]
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise

    def write_manifest() -> None:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())

    try:
        _run_body_with_owned_cleanup(
            write_manifest,
            (
                "MANIFEST_TEMP_FILE_HANDLE",
                "SCOPED_RESOURCE_RELEASE",
                "MANIFEST_TEMP_FILE_CLOSE_FAILED",
                handle.close,
            ),
        )
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except Exception as primary:
        failures = _attempt_owned_cleanup(
            "MANIFEST_TEMP_FILE",
            "FAILURE_CLEANUP",
            "MANIFEST_TEMP_FILE_UNLINK_FAILED",
            lambda: _unlink_at_if_exists(parent_fd, temporary),
        )
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise OrchestratorError("MANIFEST_METADATA_INVALID")


def _copy_fd_durable(
    source_fd: int,
    destination: Path,
    *,
    uid: int,
    gid: int,
    phase_prefix: str,
    failure_callback: Callable[[str, str], None] | None = None,
) -> None:
    def fail(suffix: str) -> None:
        if failure_callback is not None:
            failure_callback(f"{phase_prefix}_{suffix}", "BEFORE_EXCHANGE")

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    destination_fd = os.open(destination, flags, 0o600)
    try:
        destination_handle = os.fdopen(destination_fd, "wb")
    except Exception as primary:
        failures = [
            *_attempt_owned_cleanup(
                "COPY_DESTINATION_DESCRIPTOR",
                "ACQUISITION_FAILURE_RELEASE",
                "COPY_DESTINATION_CLOSE_FAILED",
                lambda: os.close(destination_fd),
            ),
            *_attempt_owned_cleanup(
                "COPY_DESTINATION_FILE",
                "ACQUISITION_FAILURE_RELEASE",
                "COPY_DESTINATION_UNLINK_FAILED",
                lambda: _unlink_path_if_exists(destination),
            ),
        ]
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise

    def copy_body() -> None:
        fail("creation")
        offset = 0
        while True:
            chunk = os.pread(source_fd, 1024 * 1024, offset)
            if not chunk:
                break
            destination_handle.write(chunk)
            offset += len(chunk)
        destination_handle.flush()
        os.fsync(destination_handle.fileno())
        fail("file_fsync")

    try:
        _run_body_with_owned_cleanup(
            copy_body,
            (
                "COPY_DESTINATION_FILE_HANDLE",
                "SCOPED_RESOURCE_RELEASE",
                "COPY_DESTINATION_CLOSE_FAILED",
                destination_handle.close,
            ),
        )
    except Exception as primary:
        failures = _attempt_owned_cleanup(
            "COPY_DESTINATION_FILE",
            "FAILURE_CLEANUP",
            "COPY_DESTINATION_UNLINK_FAILED",
            lambda: _unlink_path_if_exists(destination),
        )
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise
    os.chmod(destination, 0o600)
    os.chown(destination, uid, gid)
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    fail("directory_fsync")


def _copy_durable(source: Path, destination: Path, *, uid: int, gid: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    _run_body_with_owned_cleanup(
        lambda: _copy_fd_durable(
            source_fd,
            destination,
            uid=uid,
            gid=gid,
            phase_prefix="copy",
        ),
        (
            "COPY_SOURCE_DESCRIPTOR",
            "SCOPED_RESOURCE_RELEASE",
            "COPY_SOURCE_CLOSE_FAILED",
            lambda: os.close(source_fd),
        ),
    )


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


def _identity_from_fd(fd: int) -> SourceIdentity:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OrchestratorError("PINNED_DATABASE_NOT_SINGLE_REGULAR_FILE")
    return SourceIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
        sha256=_sha256_fd(fd),
    )


def _same_inode(left: SourceIdentity, right: SourceIdentity) -> bool:
    return (left.device, left.inode) == (right.device, right.inode)


def _open_pinned_database(path: Path, *, expected: SourceIdentity | None = None) -> PinnedDatabase:
    _no_symlink_chain(path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(path.parent, directory_flags)
    try:
        file_flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(path.name, file_flags, dir_fd=parent_fd)
    except Exception as primary:
        failures = _attempt_owned_cleanup(
            "PINNED_DATABASE_PARENT_DESCRIPTOR",
            "ACQUISITION_FAILURE_RELEASE",
            "PINNED_DATABASE_PARENT_CLOSE_FAILED",
            lambda: os.close(parent_fd),
        )
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise
    try:
        identity = _identity_from_fd(file_fd)
        path_metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (path_metadata.st_dev, path_metadata.st_ino) != (identity.device, identity.inode):
            raise OrchestratorError("PINNED_DATABASE_PATH_MISMATCH")
        if expected is not None and identity != expected:
            raise OrchestratorError("SOURCE_IDENTITY_CHANGED")
        return PinnedDatabase(path=path, parent_fd=parent_fd, file_fd=file_fd, identity=identity)
    except Exception as primary:
        failures: list[_CleanupFailureRecord] = []
        failures.extend(
            _attempt_owned_cleanup(
                "PINNED_DATABASE_FILE_DESCRIPTOR",
                "ACQUISITION_FAILURE_RELEASE",
                "PINNED_DATABASE_FILE_CLOSE_FAILED",
                lambda: os.close(file_fd),
            )
        )
        failures.extend(
            _attempt_owned_cleanup(
                "PINNED_DATABASE_PARENT_DESCRIPTOR",
                "ACQUISITION_FAILURE_RELEASE",
                "PINNED_DATABASE_PARENT_CLOSE_FAILED",
                lambda: os.close(parent_fd),
            )
        )
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise


def _path_matches_pin(pin: PinnedDatabase) -> bool:
    try:
        metadata = os.stat(pin.path.name, dir_fd=pin.parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return (metadata.st_dev, metadata.st_ino) == (pin.identity.device, pin.identity.inode)


def _lease_conflicts_on_pinned_fd(fd: int) -> bool:
    probe = (
        "import fcntl,os,sys; fd=int(sys.argv[1]); "
        "\ntry:\n fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); sys.exit(0)\n"
        "except BlockingIOError:\n sys.exit(75)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, str(fd)],
        pass_fds=(fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in {0, 75}:
        raise OrchestratorError("SQLITE_LEASE_PROBE_FAILED")
    return result.returncode == 75


def _acquire_sqlite_lease(pin: PinnedDatabase, *, label: str) -> SQLiteLease:
    if _sidecars(pin.path):
        raise OrchestratorError(f"{label}_SQLITE_SIDECAR_PRESENT")
    connection: sqlite3.Connection | None = None

    def release_connection() -> tuple[_CleanupFailureRecord, ...]:
        if connection is None:
            return ()
        failures: list[_CleanupFailureRecord] = []
        failures.extend(
            _attempt_owned_cleanup(
                f"{label}_SQLITE_LEASE",
                "ACQUISITION_FAILURE_ROLLBACK",
                f"{label}_SQLITE_ROLLBACK_FAILED",
                connection.rollback,
            )
        )
        failures.extend(
            _attempt_owned_cleanup(
                f"{label}_SQLITE_LEASE",
                "ACQUISITION_FAILURE_CLOSE",
                f"{label}_SQLITE_CONNECTION_CLOSE_FAILED",
                connection.close,
            )
        )
        return tuple(failures)

    try:
        connection = sqlite3.connect(str(pin.path), timeout=0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=0")
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        if journal_mode != "delete":
            raise OrchestratorError(f"{label}_SQLITE_JOURNAL_MODE_INVALID")
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        if not _path_matches_pin(pin) or not _lease_conflicts_on_pinned_fd(pin.file_fd):
            raise OrchestratorError(f"{label}_SQLITE_LEASE_IDENTITY_MISMATCH")
        return SQLiteLease(connection=connection, label=label)
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        error = (
            OrchestratorError(f"{label}_NOT_QUIESCENT")
            if isinstance(code, int)
            and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            else OrchestratorError(f"{label}_SQLITE_LEASE_FAILED")
        )
        failures = release_connection()
        if failures:
            raise _PrimaryAndCleanupError(error, failures) from exc
        raise error from exc
    except Exception as primary:
        failures = release_connection()
        if failures:
            raise _PrimaryAndCleanupError(primary, failures) from primary
        raise


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


def _sqlite_validation_connection(conn: sqlite3.Connection) -> tuple[str, int]:
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0]).lower()
    foreign_keys = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    return integrity, foreign_keys


def _sqlite_validation(path: Path) -> tuple[str, int]:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)

    def validate() -> tuple[str, int]:
        conn.execute("PRAGMA query_only=ON")
        return _sqlite_validation_connection(conn)

    return _run_body_with_owned_cleanup(
        validate,
        (
            "SQLITE_VALIDATION_CONNECTION",
            "SCOPED_RESOURCE_RELEASE",
            "SQLITE_VALIDATION_CONNECTION_CLOSE_FAILED",
            conn.close,
        ),
    )


def _database_snapshot_connection(
    conn: sqlite3.Connection,
) -> tuple[tuple[tuple[str, str, str], ...], dict[str, int]]:
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
    return objects, counts


def _database_snapshot(path: Path) -> tuple[tuple[tuple[str, str, str], ...], dict[str, int]]:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)

    def snapshot() -> tuple[
        tuple[tuple[str, str, str], ...],
        dict[str, int],
    ]:
        conn.execute("PRAGMA query_only=ON")
        return _database_snapshot_connection(conn)

    return _run_body_with_owned_cleanup(
        snapshot,
        (
            "SQLITE_SNAPSHOT_CONNECTION",
            "SCOPED_RESOURCE_RELEASE",
            "SQLITE_SNAPSHOT_CONNECTION_CLOSE_FAILED",
            conn.close,
        ),
    )


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


def _target_schema_payload() -> dict[str, Any]:
    from scripts.healbite_schema_migrate import (
        _component_statements,
        _expected_schema_objects,
    )

    objects: dict[str, tuple[str, str]] = {}
    components = _component_statements()
    for component in sorted(components):
        for name, contract in _expected_schema_objects(
            components[component]
        ).items():
            if name in objects and objects[name] != contract:
                raise OrchestratorError("EXPECTED_SCHEMA_CONTRACT_DRIFT")
            objects[name] = contract
    return {
        "components": sorted(components),
        "objects": [
            {
                "name": name,
                "type": objects[name][0],
                "sql": objects[name][1],
            }
            for name in sorted(objects)
        ],
    }


def _target_schema_contract() -> TargetSchemaContract:
    payload = _target_schema_payload()
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    return TargetSchemaContract(
        version=f"healbite-schema-{fingerprint[:16]}",
        fingerprint=fingerprint,
    )


def _target_schema_fingerprint_connection(
    conn: sqlite3.Connection,
) -> str:
    from scripts.healbite_schema_migrate import _normalize_schema_sql

    payload = _target_schema_payload()
    expected = {
        str(item["name"]): (str(item["type"]), str(item["sql"]))
        for item in payload["objects"]
    }
    placeholders = ",".join("?" for _ in expected)
    rows = conn.execute(
        f"SELECT type, name, sql FROM sqlite_master "
        f"WHERE name IN ({placeholders})",
        tuple(expected),
    ).fetchall()
    if len(rows) != len(expected):
        raise OrchestratorError("TARGET_SCHEMA_CONTRACT_MISMATCH")
    actual: dict[str, tuple[str, str]] = {}
    for object_type, name, sql in rows:
        object_name = str(name)
        normalized = "" if sql is None else _normalize_schema_sql(str(sql))
        actual[object_name] = (str(object_type).lower(), normalized)
    if actual != expected:
        raise OrchestratorError("TARGET_SCHEMA_CONTRACT_MISMATCH")
    actual_payload = {
        "components": payload["components"],
        "objects": [
            {
                "name": name,
                "type": actual[name][0],
                "sql": actual[name][1],
            }
            for name in sorted(actual)
        ],
    }
    encoded = json.dumps(
        actual_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    if fingerprint != _target_schema_contract().fingerprint:
        raise OrchestratorError("TARGET_SCHEMA_FINGERPRINT_MISMATCH")
    return fingerprint


def _target_schema_fingerprint(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

    def fingerprint() -> str:
        conn.execute("PRAGMA query_only=ON")
        return _target_schema_fingerprint_connection(conn)

    return str(
        _run_body_with_owned_cleanup(
            fingerprint,
            (
                "TARGET_FINGERPRINT_CONNECTION",
                "SCOPED_RESOURCE_RELEASE",
                "TARGET_FINGERPRINT_CONNECTION_CLOSE_FAILED",
                conn.close,
            ),
        )
    )

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


def _rename_exchange(
    left_parent_fd: int,
    left_name: str,
    right_parent_fd: int,
    right_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OrchestratorError("RENAME_EXCHANGE_UNAVAILABLE", publish_state="BEFORE_EXCHANGE")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(
        left_parent_fd,
        os.fsencode(left_name),
        right_parent_fd,
        os.fsencode(right_name),
        RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
        raise OrchestratorError("RENAME_EXCHANGE_UNAVAILABLE", publish_state="BEFORE_EXCHANGE")
    raise OrchestratorError("RENAME_EXCHANGE_FAILED", publish_state="EXCHANGE_STARTED")


def _inode_at(parent_fd: int, name: str) -> tuple[int, int]:
    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OrchestratorError("PUBLISH_PATH_NOT_SINGLE_REGULAR_FILE", publish_state="EXCHANGE_STARTED")
    return metadata.st_dev, metadata.st_ino


def _fsync_publish_parents(source: PinnedDatabase, staging: PinnedDatabase) -> None:
    _fsync_fd(source.parent_fd)
    source_parent = os.fstat(source.parent_fd)
    staging_parent = os.fstat(staging.parent_fd)
    if (source_parent.st_dev, source_parent.st_ino) != (
        staging_parent.st_dev,
        staging_parent.st_ino,
    ):
        _fsync_fd(staging.parent_fd)


def _target_may_have_changed(publish_state: str) -> bool:
    return publish_state in PUBLISH_MAY_HAVE_CHANGED or publish_state == "EXCHANGE_STARTED"


def _cleanup_operation_staging(
    manifest: DurableManifest,
    staging_root: Path,
    *,
    failure_callback: Callable[[str, str], None] | None = None,
) -> bool:
    publish_state = str(manifest.payload.get("PUBLISH_STATE", "BEFORE_EXCHANGE"))
    if publish_state != "BEFORE_EXCHANGE":
        raise OrchestratorError("POST_EXCHANGE_STAGING_CLEANUP_REFUSED", publish_state=publish_state)
    operation_path = Path(str(manifest.payload.get("STAGING_DIRECTORY_PATH", "")))
    if not operation_path.is_absolute() or operation_path.parent != staging_root:
        raise OrchestratorError("STAGING_CLEANUP_PATH_INVALID")

    root_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd = os.open(staging_root, root_flags)
    operation_fd: int | None = None
    operation_missing = False
    primary: Exception | None = None
    cleanup_failures: list[_CleanupFailureRecord] = []
    try:
        root_metadata = os.fstat(root_fd)
        expected_root = (
            manifest.payload.get("STAGING_ROOT_DEVICE"),
            manifest.payload.get("STAGING_ROOT_INODE"),
        )
        if expected_root != (root_metadata.st_dev, root_metadata.st_ino):
            raise OrchestratorError("STAGING_ROOT_IDENTITY_CHANGED")
        if root_metadata.st_uid != _effective_uid() or stat.S_IMODE(root_metadata.st_mode) != 0o700:
            raise OrchestratorError("STAGING_ROOT_NOT_PRIVATE")
        try:
            operation_fd = os.open(operation_path.name, root_flags, dir_fd=root_fd)
        except FileNotFoundError:
            operation_missing = True
        if operation_fd is not None:
            metadata = os.fstat(operation_fd)
            expected_operation = (
                manifest.payload.get("STAGING_DIRECTORY_DEVICE"),
                manifest.payload.get("STAGING_DIRECTORY_INODE"),
            )
            if expected_operation != (metadata.st_dev, metadata.st_ino):
                raise OrchestratorError("STAGING_DIRECTORY_IDENTITY_CHANGED")
            if metadata.st_uid != RUNTIME_UID or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise OrchestratorError("STAGING_DIRECTORY_NOT_PRIVATE")
            if failure_callback is not None:
                failure_callback("staging_cleanup", publish_state)

            target_inode: tuple[int, int] | None = None
            target_path = Path(str(manifest.payload.get("TARGET_PATH", "")))
            try:
                target_metadata = target_path.stat(follow_symlinks=False)
                target_inode = (target_metadata.st_dev, target_metadata.st_ino)
            except (FileNotFoundError, OSError):
                pass
            source_inode = (
                manifest.payload.get("SOURCE_DEVICE"),
                manifest.payload.get("SOURCE_INODE"),
            )
            for name in os.listdir(operation_fd):
                entry = os.stat(name, dir_fd=operation_fd, follow_symlinks=False)
                if not stat.S_ISREG(entry.st_mode):
                    raise OrchestratorError("STAGING_CLEANUP_UNTRUSTED_ENTRY")
                entry_inode = (entry.st_dev, entry.st_ino)
                if entry_inode == source_inode or (target_inode is not None and entry_inode == target_inode):
                    raise OrchestratorError("STAGING_CLEANUP_LIVE_DATABASE_REFUSED")
                os.unlink(name, dir_fd=operation_fd)
            _fsync_fd(operation_fd)
    except Exception as exc:
        primary = exc

    if operation_fd is not None:
        cleanup_failures.extend(
            _attempt_owned_cleanup(
                "STAGING_OPERATION_DIRECTORY_DESCRIPTOR",
                "STAGING_CLEANUP_RELEASE",
                "STAGING_OPERATION_DIRECTORY_CLOSE_FAILED",
                lambda: os.close(operation_fd),
            )
        )
    if primary is None and not cleanup_failures and not operation_missing:
        try:
            os.rmdir(operation_path.name, dir_fd=root_fd)
            _fsync_fd(root_fd)
        except Exception as exc:
            primary = exc
    cleanup_failures.extend(
        _attempt_owned_cleanup(
            "STAGING_ROOT_DIRECTORY_DESCRIPTOR",
            "STAGING_CLEANUP_RELEASE",
            "STAGING_ROOT_DIRECTORY_CLOSE_FAILED",
            lambda: os.close(root_fd),
        )
    )

    if cleanup_failures:
        if primary is not None:
            raise _PrimaryAndCleanupError(
                primary,
                cleanup_failures,
            ) from primary
        raise _CleanupAggregateError(cleanup_failures)
    if primary is not None:
        raise primary
    manifest.checkpoint(STAGING_CLEANED=True)
    return True


def _recover_pre_publish_staging(manifest_path: Path, staging_root: Path) -> bool:
    _no_symlink_chain(manifest_path)
    metadata = manifest_path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OrchestratorError("RECOVERY_MANIFEST_NOT_REGULAR")
    handle = manifest_path.open("r", encoding="ascii")
    payload = _run_body_with_owned_cleanup(
        lambda: json.load(handle),
        (
            "RECOVERY_MANIFEST_FILE_HANDLE",
            "SCOPED_RESOURCE_RELEASE",
            "RECOVERY_MANIFEST_FILE_CLOSE_FAILED",
            handle.close,
        ),
    )
    if not isinstance(payload, dict):
        raise OrchestratorError("RECOVERY_MANIFEST_INVALID")
    publish_state = str(payload.get("PUBLISH_STATE", "BEFORE_EXCHANGE"))
    if publish_state != "BEFORE_EXCHANGE":
        return False
    manifest = DurableManifest(path=manifest_path, payload=payload)
    return _cleanup_operation_staging(manifest, staging_root)


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
            if metadata.st_uid != _effective_uid() or stat.S_IMODE(metadata.st_mode) != 0o700:
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

def _issue_production_authorization(
    *,
    operation_id: str,
    plan_sha256: str,
    source_identity: SourceIdentity,
    image_revision: str,
    target_schema_version: str,
    target_schema_fingerprint: str,
) -> _ProductionAuthorization:
    expected_target = _target_schema_contract()
    if (
        OPERATION_ID_RE.fullmatch(operation_id) is None
        or DIGEST_RE.fullmatch(plan_sha256) is None
        or SHA_RE.fullmatch(image_revision) is None
        or DIGEST_RE.fullmatch(source_identity.sha256) is None
        or target_schema_version != expected_target.version
        or target_schema_fingerprint != expected_target.fingerprint
    ):
        raise OrchestratorError("PRODUCTION_AUTHORIZATION_INVALID")
    return _ProductionAuthorization(
        operation_id=operation_id,
        plan_sha256=plan_sha256,
        source_identity=source_identity,
        image_revision=image_revision,
        target_schema_version=target_schema_version,
        target_schema_fingerprint=target_schema_fingerprint,
        _seal=_PRODUCTION_AUTHORIZATION_SEAL,
    )


def _validate_production_authorization(
    authorization: object,
    *,
    contract: Contract,
    expected_source_identity: SourceIdentity,
) -> _ProductionAuthorization:
    if (
        not isinstance(authorization, _ProductionAuthorization)
        or authorization._seal is not _PRODUCTION_AUTHORIZATION_SEAL
        or authorization.source_identity != expected_source_identity
        or authorization.image_revision != contract.expected_source_revision
    ):
        raise OrchestratorError("PRODUCTION_AUTHORIZATION_REQUIRED")
    expected_target = _target_schema_contract()
    if (
        authorization.target_schema_version != expected_target.version
        or authorization.target_schema_fingerprint
        != expected_target.fingerprint
    ):
        raise OrchestratorError("PRODUCTION_AUTHORIZATION_SCHEMA_DRIFT")
    return authorization


def _prepare_authorized_production_execution(
    args: argparse.Namespace,
    *,
    authorization: object,
    expected_source_identity: SourceIdentity,
    backup_parent_fd: int,
) -> _PreparedProductionExecution:
    contract = _contract(args, synthetic=False)
    checked = _validate_production_authorization(
        authorization,
        contract=contract,
        expected_source_identity=expected_source_identity,
    )
    try:
        descriptor_metadata = os.fstat(backup_parent_fd)
        path_metadata = contract.backup_dir.lstat()
    except OSError as exc:
        raise OrchestratorError("BACKUP_PARENT_PIN_INVALID") from exc
    if (
        not stat.S_ISDIR(descriptor_metadata.st_mode)
        or not stat.S_ISDIR(path_metadata.st_mode)
        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        raise OrchestratorError("BACKUP_PARENT_PIN_INVALID")
    pinned_backup_parent_fd = os.dup(backup_parent_fd)
    source_pin: PinnedDatabase | None = None
    source_lease: SQLiteLease | None = None
    try:
        os.set_inheritable(pinned_backup_parent_fd, False)
        source_identity = _preflight(
            contract,
            synthetic=False,
            inspect_images=True,
        )
        if source_identity != expected_source_identity:
            raise OrchestratorError("SOURCE_IDENTITY_CHANGED")
        source_pin = _open_pinned_database(
            contract.source_db,
            expected=source_identity,
        )
        source_lease = _acquire_sqlite_lease(source_pin, label="SOURCE")
        if _identity_from_fd(source_pin.file_fd) != source_identity:
            raise OrchestratorError("SOURCE_IDENTITY_CHANGED")
        return _PreparedProductionExecution(
            contract=contract,
            source_identity=source_identity,
            source_pin=source_pin,
            source_lease=source_lease,
            backup_parent_fd=pinned_backup_parent_fd,
            authorization=checked,
        )
    except Exception as exc:
        primary, nested_cleanup_failures = _split_primary_cleanup(exc)
        cleanup_failures = list(nested_cleanup_failures)
        if source_lease is not None:
            cleanup_failures.extend(
                _attempt_owned_cleanup(
                    "SOURCE_SQLITE_LEASE",
                    "PREPARATION_FAILURE_RELEASE",
                    "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
                    source_lease.close,
                )
            )
        if source_pin is not None:
            cleanup_failures.extend(
                _attempt_owned_cleanup(
                    "SOURCE_PINNED_DATABASE",
                    "PREPARATION_FAILURE_RELEASE",
                    "SOURCE_PINNED_DATABASE_CLOSE_FAILED",
                    source_pin.close,
                )
            )
        cleanup_failures.extend(
            _attempt_owned_cleanup(
                "MANIFEST_PARENT_DESCRIPTOR",
                "PREPARATION_FAILURE_RELEASE",
                "MANIFEST_PARENT_CLOSE_FAILED",
                lambda: os.close(pinned_backup_parent_fd),
            )
        )
        if cleanup_failures:
            error = (
                primary
                if isinstance(primary, OrchestratorError)
                else OrchestratorError(type(primary).__name__)
            )
            raise _StagedCleanupTransport(
                result_snapshot={
                    "status": "FAILED",
                    "error_type": error.code,
                    "exit_classification": error.code,
                    "failure_reason": None,
                    "publish_state": "BEFORE_EXCHANGE",
                    "target_may_have_changed": False,
                    "automatic_retry_allowed": False,
                    "manual_recovery_required": False,
                    "backup_available": False,
                    "cleanup_failed": False,
                    "cleanup_error_type": None,
                    "manifest_write_failed": False,
                    "false_rollback_reported": False,
                },
                publish_state="BEFORE_EXCHANGE",
                exchange_started=False,
                cleanup_failures=cleanup_failures,
                durable_evidence_updated=False,
            ) from primary
        raise


def _run_target_migration(contract: Contract, staging_dir: Path) -> None:
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
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise OrchestratorError("TARGET_IMAGE_MIGRATION_FAILED")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OrchestratorError("TARGET_IMAGE_RESULT_INVALID") from exc
    if payload.get("status") != "success" or payload.get("path_mode") != "STAGED_COPY":
        raise OrchestratorError("TARGET_IMAGE_RESULT_CONTRACT_FAILED")


def _run_previous_image_probe(contract: Contract, staging_dir: Path) -> dict[str, Any]:
    database = staging_dir / "database.sqlite"
    before_hash = _sha256(database)
    inspect_result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            contract.previous_image_id,
            "--format",
            '{{json .Config.Entrypoint}}\n{{json .Config.Env}}',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if inspect_result.returncode != 0:
        raise OrchestratorError("PREVIOUS_IMAGE_INSPECT_FAILED")
    lines = inspect_result.stdout.splitlines()
    try:
        entrypoint = json.loads(lines[0])
        image_environment = json.loads(lines[1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise OrchestratorError("PREVIOUS_IMAGE_CONFIG_INVALID") from exc
    if entrypoint != ["/init", "/opt/hermes/docker/main-wrapper.sh"]:
        raise OrchestratorError("PREVIOUS_IMAGE_CANONICAL_ENTRYPOINT_MISMATCH")
    sensitive_name = re.compile(r"(?:API_KEY|TOKEN|PASSWORD|SECRET)$")
    for item in image_environment or []:
        name, separator, value = str(item).partition("=")
        if separator and value and sensitive_name.search(name):
            raise OrchestratorError("PREVIOUS_IMAGE_EMBEDS_CREDENTIAL")

    container_name = f"healbite-previous-startup-{uuid.uuid4().hex[:16]}"
    command = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--network",
        "none",
        "--mount",
        f"type=bind,src={database},dst=/home/hermes/healbite.db",
        "-e",
        "HEALBITE_DB_PATH=/home/hermes/healbite.db",
        "-e",
        "HEALBITE_HOUSEHOLDS_ENABLED=false",
        "-e",
        "HEALBITE_WEEKLY_MENU_ENABLED=false",
        "-e",
        "HEALBITE_SHOPPING_LIST_ENABLED=false",
        "-e",
        "HERMES_GATEWAY_NO_SUPERVISE=1",
        "-e",
        "HERMES_GATEWAY_EXIT_DIAG=0",
        contract.previous_image_id,
        "gateway",
        "run",
        "--no-supervise",
    ]
    started = False
    ready = False
    clean_shutdown = False
    logs = ""
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode != 0:
            raise OrchestratorError("PREVIOUS_IMAGE_START_FAILED")
        started = True
        deadline = time.monotonic() + 30
        status_reader = (
            "import json,pathlib; p=pathlib.Path('/opt/data/gateway_state.json'); "
            "print(json.loads(p.read_text()).get('gateway_state','missing') if p.is_file() else 'missing')"
        )
        while time.monotonic() < deadline:
            running = subprocess.run(
                ["docker", "inspect", container_name, "--format", "{{.State.Running}}"],
                text=True,
                capture_output=True,
                check=False,
            )
            if running.returncode != 0 or running.stdout.strip() != "true":
                break
            status = subprocess.run(
                ["docker", "exec", container_name, "/opt/hermes/.venv/bin/python", "-c", status_reader],
                text=True,
                capture_output=True,
                check=False,
            )
            if status.returncode == 0 and status.stdout.strip() == "running":
                ready = True
                break
            time.sleep(0.25)
        if not ready:
            raise OrchestratorError("PREVIOUS_IMAGE_READY_MILESTONE_NOT_REACHED")

        network_mode = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{.HostConfig.NetworkMode}}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if network_mode.returncode != 0 or network_mode.stdout.strip() != "none":
            raise OrchestratorError("PREVIOUS_IMAGE_NETWORK_ISOLATION_FAILED")
        log_result = subprocess.run(
            ["docker", "logs", container_name],
            text=True,
            capture_output=True,
            check=False,
        )
        logs = f"{log_result.stdout}\n{log_result.stderr}".lower()
        if any(marker in logs for marker in ("traceback", "no such table", "no such column", "unknown column")):
            raise OrchestratorError("PREVIOUS_IMAGE_SCHEMA_STARTUP_ERROR")
        stopped = subprocess.run(
            ["docker", "stop", "--time", "10", container_name],
            text=True,
            capture_output=True,
            check=False,
        )
        if stopped.returncode != 0:
            raise OrchestratorError("PREVIOUS_IMAGE_SHUTDOWN_FAILED")
        exit_code = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{.State.ExitCode}}"],
            text=True,
            capture_output=True,
            check=False,
        )
        clean_shutdown = exit_code.returncode == 0 and exit_code.stdout.strip() == "0"
        if not clean_shutdown:
            raise OrchestratorError("PREVIOUS_IMAGE_UNCLEAN_SHUTDOWN")
    finally:
        if started and not clean_shutdown:
            subprocess.run(
                ["docker", "stop", "--time", "10", container_name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        subprocess.run(
            ["docker", "rm", "-f", "-v", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    if _sha256(database) != before_hash or _sidecars(database):
        raise OrchestratorError("PREVIOUS_IMAGE_MUTATED_STAGING")
    return {
        "canonical_entrypoint_used": True,
        "process_started": started,
        "ready_milestone": "gateway_state=running",
        "reached_ready_milestone": ready,
        "clean_shutdown": clean_shutdown,
        "network_requests": 0,
        "database_mutated": False,
        "no_schema_error": True,
        "no_unknown_column_error": True,
        "no_automatic_migration": True,
        "feature_disabled_startup_pass": True,
        "rollback_image_compatibility": True,
    }


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


@dataclass(frozen=True)
class _SyntheticExecutionHooks:
    phase_callback: Callable[[str], None] | None = None
    failure_callback: Callable[[str, str], None] | None = None
    migration_runner: Callable[[Contract, Path], None] = _run_target_migration
    compatibility_probe: Callable[[Contract, Path], Any] = _run_previous_image_probe
    before_exchange_callback: Callable[[], None] | None = None
    lifecycle_callback: Callable[[str, Path], None] | None = None


def _execute_staged_body(
    args: argparse.Namespace,
    *,
    synthetic: bool,
    operation_id: str | None = None,
    expected_source_identity: SourceIdentity | None = None,
    _prepared_production: _PreparedProductionExecution | None = None,
    _synthetic_hooks: _SyntheticExecutionHooks | None = None,
) -> int:
    if synthetic:
        if _prepared_production is not None:
            raise OrchestratorError("SYNTHETIC_PRODUCTION_CONTEXT_REFUSED")
        hooks = _synthetic_hooks or _SyntheticExecutionHooks()
        contract = _contract(args, synthetic=True)
        source_identity = _preflight(
            contract,
            synthetic=True,
            inspect_images=True,
        )
        if (
            expected_source_identity is not None
            and source_identity != expected_source_identity
        ):
            raise OrchestratorError("SOURCE_IDENTITY_CHANGED")
    else:
        if _synthetic_hooks is not None:
            raise OrchestratorError("PRODUCTION_TEST_HOOK_REFUSED")
        if _prepared_production is None:
            raise OrchestratorError("PRODUCTION_AUTHORIZATION_REQUIRED")
        contract = _prepared_production.contract
        source_identity = _prepared_production.source_identity
        authorization = _validate_production_authorization(
            _prepared_production.authorization,
            contract=contract,
            expected_source_identity=source_identity,
        )
        if (
            operation_id is not None
            and operation_id != authorization.operation_id
        ):
            raise OrchestratorError("PRODUCTION_AUTHORIZATION_MISMATCH")
        if (
            expected_source_identity is not None
            and expected_source_identity != source_identity
        ):
            raise OrchestratorError("PRODUCTION_AUTHORIZATION_MISMATCH")
        operation_id = authorization.operation_id
        hooks = _SyntheticExecutionHooks()
    target_schema = _target_schema_contract()

    phase_callback = hooks.phase_callback
    failure_callback = hooks.failure_callback
    migration_runner = hooks.migration_runner
    compatibility_probe = hooks.compatibility_probe
    before_exchange_callback = hooks.before_exchange_callback
    lifecycle_callback = hooks.lifecycle_callback

    def phase(name: str) -> None:
        if phase_callback is not None:
            phase_callback(name)

    def fail(name: str, *, publish_state: str = "BEFORE_EXCHANGE") -> None:
        if failure_callback is not None:
            failure_callback(name, publish_state)

    def lifecycle(name: str, path: Path) -> None:
        if lifecycle_callback is not None:
            lifecycle_callback(name, path)

    operation_id = operation_id or uuid.uuid4().hex
    backup = contract.backup_dir / f"backup-{operation_id}.sqlite"
    staging_dir = contract.staging_root / f"staging-{operation_id}"
    staging_db = staging_dir / "database.sqlite"
    manifest_path = contract.backup_dir / f"manifest-{operation_id}.json"
    source_pin: PinnedDatabase | None = None
    source_lease: SQLiteLease | None = None
    staging_pin: PinnedDatabase | None = None
    staging_lease: SQLiteLease | None = None
    manifest_parent_fd: int | None = None
    manifest: DurableManifest | None = None
    publish_state = "BEFORE_EXCHANGE"
    exchange_started = False
    result_snapshot: dict[str, Any] | None = None
    cleanup_failures: list[_CleanupFailureRecord] = []
    try:
        if _prepared_production is not None:
            (
                source_pin,
                source_lease,
                manifest_parent_fd,
            ) = _prepared_production.take()
        else:
            source_pin = _open_pinned_database(
                contract.source_db, expected=source_identity
            )
            source_lease = _acquire_sqlite_lease(source_pin, label="SOURCE")
        if _identity_from_fd(source_pin.file_fd) != source_identity:
            raise OrchestratorError("SOURCE_IDENTITY_CHANGED")
        baseline_objects, baseline_counts = _database_snapshot_connection(source_lease.connection)
        staging_root_metadata = contract.staging_root.stat()
        manifest = DurableManifest(
            path=manifest_path,
            payload={
                "OPERATION_ID": operation_id,
                "SOURCE_DEVICE": source_identity.device,
                "SOURCE_INODE": source_identity.inode,
                "SOURCE_SHA256": source_identity.sha256,
                "BACKUP_PATH": str(backup),
                "BACKUP_SHA256": None,
                "STAGING_ROOT_DEVICE": staging_root_metadata.st_dev,
                "STAGING_ROOT_INODE": staging_root_metadata.st_ino,
                "STAGING_DIRECTORY_PATH": str(staging_dir),
                "STAGING_DIRECTORY_DEVICE": None,
                "STAGING_DIRECTORY_INODE": None,
                "STAGING_PATH": str(staging_db),
                "STAGING_SHA256": None,
                "TARGET_PATH": str(contract.source_db),
                "TARGET_SCHEMA_VERSION": target_schema.version,
                "TARGET_SCHEMA_FINGERPRINT": target_schema.fingerprint,
                "PUBLISH_STATE": publish_state,
                "TARGET_MAY_HAVE_CHANGED": False,
                "AUTOMATIC_RETRY_ALLOWED": False,
                "MANUAL_RECOVERY_REQUIRED": False,
                "STATE": "PLANNED",
            },
            parent_fd=manifest_parent_fd,
        )
        manifest.transition("PLANNED")
        manifest.failure_callback = failure_callback
        phase("planned")
        lifecycle("source_lease_acquired", contract.source_db)

        _copy_fd_durable(
            source_pin.file_fd,
            backup,
            uid=source_identity.uid,
            gid=source_identity.gid,
            phase_prefix="backup",
            failure_callback=failure_callback,
        )
        backup_sha = _sha256(backup)
        if backup_sha != source_identity.sha256 or _sqlite_validation(backup) != ("ok", 0):
            raise OrchestratorError("BACKUP_VALIDATION_FAILED")
        manifest.transition("BACKED_UP", BACKUP_SHA256=backup_sha)
        phase("backup_fsynced")
        lifecycle("backup_complete", contract.source_db)

        staging_dir.mkdir(mode=0o700)
        os.chmod(staging_dir, 0o700)
        os.chown(staging_dir, RUNTIME_UID, RUNTIME_GID)
        _fsync_directory(contract.staging_root)
        staging_directory_metadata = staging_dir.stat()
        manifest.checkpoint(
            STAGING_DIRECTORY_DEVICE=staging_directory_metadata.st_dev,
            STAGING_DIRECTORY_INODE=staging_directory_metadata.st_ino,
        )
        _copy_fd_durable(
            source_pin.file_fd,
            staging_db,
            uid=RUNTIME_UID,
            gid=RUNTIME_GID,
            phase_prefix="staging",
            failure_callback=failure_callback,
        )
        if _sha256(staging_db) != backup_sha:
            raise OrchestratorError("STAGING_SOURCE_MISMATCH")
        phase("staging_copied")
        lifecycle("staging_copy_complete", contract.source_db)

        for run_number in range(1, 4):
            before = _database_snapshot(staging_db) if run_number > 1 else None
            migration_runner(contract, staging_dir)
            after = _database_snapshot(staging_db)
            if before is not None and after != before:
                raise OrchestratorError("MIGRATION_NOT_IDEMPOTENT")
        migrated_target_fingerprint = _target_schema_fingerprint(staging_db)
        if migrated_target_fingerprint != target_schema.fingerprint:
            raise OrchestratorError("TARGET_SCHEMA_FINGERPRINT_MISMATCH")
        phase("migration_committed")
        manifest.transition(
            "MIGRATED",
            STAGING_SHA256=_sha256(staging_db),
            TARGET_SCHEMA_VERSION=target_schema.version,
            TARGET_SCHEMA_FINGERPRINT=migrated_target_fingerprint,
        )
        lifecycle("migration_complete", contract.source_db)

        integrity, foreign_keys = _sqlite_validation(staging_db)
        fail("integrity_validation")
        if integrity != "ok":
            raise OrchestratorError("MIGRATED_DATABASE_INTEGRITY_FAILED")
        fail("foreign_key_validation")
        if foreign_keys != 0:
            raise OrchestratorError("MIGRATED_DATABASE_FOREIGN_KEYS_FAILED")
        migrated_objects, migrated_counts = _database_snapshot(staging_db)
        if _sidecars(staging_db):
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
        lifecycle("validation_complete", contract.source_db)

        before_previous_probe = _sha256(staging_db)
        previous_startup = compatibility_probe(contract, staging_dir)
        fail("previous_image_startup")
        if _sha256(staging_db) != before_previous_probe:
            raise OrchestratorError("PREVIOUS_IMAGE_MUTATED_STAGING")
        previous_startup_evidence = previous_startup if isinstance(previous_startup, dict) else {}
        manifest.transition(
            "VALIDATED",
            STAGING_SHA256=before_previous_probe,
            PREVIOUS_IMAGE_READY_MILESTONE=previous_startup_evidence.get("ready_milestone"),
        )
        phase("validated")
        lifecycle("previous_startup_complete", contract.source_db)

        staging_identity = _source_identity(staging_db, require_private_parent=True)
        if staging_identity.sha256 != before_previous_probe:
            raise OrchestratorError("STAGING_VALIDATION_IDENTITY_CHANGED")
        staging_pin = _open_pinned_database(staging_db, expected=staging_identity)
        staging_lease = _acquire_sqlite_lease(staging_pin, label="STAGING")
        if _sqlite_validation_connection(staging_lease.connection) != ("ok", 0):
            raise OrchestratorError("STAGING_LEASE_DATABASE_INVALID")
        lifecycle("staging_lease_acquired", contract.source_db)

        phase("before_publish")
        fail("pre_publish_cleanup")
        if not _path_matches_pin(source_pin) or _identity_from_fd(source_pin.file_fd) != source_identity:
            raise OrchestratorError("SOURCE_IDENTITY_CHANGED")
        if not _path_matches_pin(staging_pin) or _identity_from_fd(staging_pin.file_fd) != staging_identity:
            raise OrchestratorError("STAGING_IDENTITY_CHANGED")
        if source_identity.device != staging_identity.device:
            raise OrchestratorError("CROSS_FILESYSTEM_PUBLISH_REFUSED")
        _fsync_fd(staging_pin.file_fd)
        manifest.checkpoint(
            PUBLISH_STATE="EXCHANGE_STARTED",
            TARGET_MAY_HAVE_CHANGED=True,
            AUTOMATIC_RETRY_ALLOWED=False,
            MANUAL_RECOVERY_REQUIRED=True,
        )
        publish_state = "EXCHANGE_STARTED"
        phase("exchange_started")
        exchange_started = True
        if before_exchange_callback is not None:
            before_exchange_callback()
        pre_exchange_target = _inode_at(source_pin.parent_fd, source_pin.path.name)
        pre_exchange_staging = _inode_at(staging_pin.parent_fd, staging_pin.path.name)
        _rename_exchange(
            source_pin.parent_fd,
            source_pin.path.name,
            staging_pin.parent_fd,
            staging_pin.path.name,
        )
        publish_state = "EXCHANGE_COMPLETED_NOT_VERIFIED"
        fail("publish_exchange", publish_state=publish_state)
        manifest.transition("PUBLISHED", PUBLISH_STATE=publish_state)
        phase("after_publish")

        target_identity = _inode_at(source_pin.parent_fd, source_pin.path.name)
        displaced_identity = _inode_at(staging_pin.parent_fd, staging_pin.path.name)
        expected_target = (staging_identity.device, staging_identity.inode)
        expected_displaced = (source_identity.device, source_identity.inode)
        if target_identity != expected_target or displaced_identity != expected_displaced:
            _rename_exchange(
                source_pin.parent_fd,
                source_pin.path.name,
                staging_pin.parent_fd,
                staging_pin.path.name,
            )
            _fsync_publish_parents(source_pin, staging_pin)
            if (
                _inode_at(source_pin.parent_fd, source_pin.path.name) != pre_exchange_target
                or _inode_at(staging_pin.parent_fd, staging_pin.path.name) != pre_exchange_staging
            ):
                raise OrchestratorError("EXCHANGE_REVERSAL_FAILED", publish_state=publish_state)
            publish_state = "EXCHANGE_REVERSED"
            manifest.checkpoint(
                PUBLISH_STATE=publish_state,
                TARGET_MAY_HAVE_CHANGED=False,
                AUTOMATIC_RETRY_ALLOWED=False,
                MANUAL_RECOVERY_REQUIRED=True,
            )
            raise OrchestratorError("CONTRACT_DRIFT", publish_state=publish_state)
        fail("displaced_target_identity_verification", publish_state=publish_state)
        publish_state = "EXCHANGE_VERIFIED_NOT_FSYNCED"
        manifest.checkpoint(PUBLISH_STATE=publish_state)
        phase("exchange_verified")

        phase("before_target_dir_fsync")
        _fsync_publish_parents(source_pin, staging_pin)
        fail("target_parent_fsync", publish_state=publish_state)
        publish_state = "PARENT_FSYNCED"
        manifest.checkpoint(PUBLISH_STATE=publish_state)
        phase("after_target_dir_fsync")
        phase("parent_fsynced")

        if _inode_at(source_pin.parent_fd, source_pin.path.name) != expected_target:
            raise OrchestratorError("PUBLISHED_TARGET_IDENTITY_CHANGED", publish_state=publish_state)
        if _sqlite_validation_connection(staging_lease.connection) != ("ok", 0):
            raise OrchestratorError("PUBLISHED_DATABASE_INVALID", publish_state=publish_state)
        final_target_fingerprint = _target_schema_fingerprint_connection(
            staging_lease.connection
        )
        if final_target_fingerprint != target_schema.fingerprint:
            raise OrchestratorError("TARGET_SCHEMA_FINGERPRINT_MISMATCH")
        fail("final_verification", publish_state=publish_state)
        lifecycle("final_verification", contract.source_db)
        publish_state = "FINAL_VERIFIED"
        manifest.transition(
            "VERIFIED",
            PUBLISH_STATE=publish_state,
            TARGET_MAY_HAVE_CHANGED=True,
            AUTOMATIC_RETRY_ALLOWED=False,
            MANUAL_RECOVERY_REQUIRED=False,
            DISPLACED_SOURCE_PATH=str(staging_db),
            TARGET_SCHEMA_VERSION=target_schema.version,
            TARGET_SCHEMA_FINGERPRINT=final_target_fingerprint,
        )
        phase("final_verified")
        result_snapshot = {
                "status": "PASS",
                "mode": "EXECUTE_SYNTHETIC" if synthetic else "EXECUTE_PRODUCTION",
                "operation_id": operation_id,
                "production_execution_enabled": not synthetic,
                "backup_created": True,
                "backup_and_staging_source_match": True,
                "migration_runs": 3,
                "migration_idempotent": True,
                "previous_image_compatible": True,
                "previous_image_canonical_entrypoint_used": previous_startup_evidence.get(
                    "canonical_entrypoint_used", False
                ),
                "previous_image_reached_ready_milestone": previous_startup_evidence.get(
                    "reached_ready_milestone", False
                ),
                "previous_image_process_started": previous_startup_evidence.get("process_started", False),
                "previous_image_clean_shutdown": previous_startup_evidence.get("clean_shutdown", False),
                "previous_image_network_requests": previous_startup_evidence.get("network_requests", 0),
                "previous_image_no_schema_error": previous_startup_evidence.get("no_schema_error", False),
                "previous_image_no_unknown_column_error": previous_startup_evidence.get(
                    "no_unknown_column_error", False
                ),
                "previous_image_no_automatic_migration": previous_startup_evidence.get(
                    "no_automatic_migration", False
                ),
                "previous_image_no_db_mutation": not previous_startup_evidence.get("database_mutated", True),
                "previous_image_feature_disabled_startup_pass": previous_startup_evidence.get(
                    "feature_disabled_startup_pass", False
                ),
                "rollback_image_compatibility": previous_startup_evidence.get(
                    "rollback_image_compatibility", False
                ),
                "source_sqlite_lease_acquired": True,
                "staging_sqlite_lease_acquired": True,
                "source_lease_held_through_final_verification": True,
                "staging_lease_held_through_final_verification": True,
                "leases_held_through_final_verification": True,
                "poll_only_quiescence_used": False,
                "atomic_primitive": "renameat2_RENAME_EXCHANGE",
                "source_fd_identity_pinned": True,
                "staging_fd_identity_pinned": True,
                "target_parent_fd_pinned": True,
                "displaced_target_inode_verified": True,
                "target_parent_fsynced": True,
                "manifest_state": "VERIFIED",
                "publish_state": publish_state,
                "target_schema_version": target_schema.version,
                "target_schema_fingerprint_match": (
                    final_target_fingerprint == target_schema.fingerprint
                ),
                "target_may_have_changed": True,
                "automatic_retry_allowed": False,
                "manual_recovery_required": False,
            }
        _json_print(result_snapshot)
        return 0
    except Exception as exc:
        primary, nested_cleanup_failures = _split_primary_cleanup(exc)
        cleanup_failures.extend(nested_cleanup_failures)
        error = (
            primary
            if isinstance(primary, OrchestratorError)
            else OrchestratorError(type(primary).__name__)
        )
        if exchange_started and publish_state == "BEFORE_EXCHANGE":
            publish_state = error.publish_state
        publish_uncertain = (
            exchange_started
            and publish_state != "EXCHANGE_REVERSED"
        )
        failure_code = "PUBLISH_UNCERTAIN" if publish_uncertain else error.code
        if not exchange_started:
            if staging_lease is not None:
                cleanup_failures.extend(
                    _attempt_owned_cleanup(
                        "STAGING_SQLITE_LEASE",
                        "PRE_EXCHANGE_RELEASE",
                        "STAGING_SQLITE_LEASE_CLOSE_FAILED",
                        staging_lease.close,
                    )
                )
                staging_lease = None
            if staging_pin is not None:
                cleanup_failures.extend(
                    _attempt_owned_cleanup(
                        "STAGING_PINNED_DATABASE",
                        "PRE_EXCHANGE_RELEASE",
                        "STAGING_PINNED_DATABASE_CLOSE_FAILED",
                        staging_pin.close,
                    )
                )
                staging_pin = None
        cleanup_failed = False
        cleanup_error_type: str | None = None
        manifest_write_failed = False
        if manifest is not None:
            manifest.failure_callback = None
            if not exchange_started and publish_state == "BEFORE_EXCHANGE":
                try:
                    _cleanup_operation_staging(
                        manifest,
                        contract.staging_root,
                        failure_callback=failure_callback,
                    )
                except Exception as cleanup_error:
                    cleanup_failed = True
                    operation_cleanup_failures = (
                        _cleanup_records_from_exception(
                            "OPERATION_STAGING",
                            "PRE_EXCHANGE_STAGING_CLEANUP",
                            "OPERATION_STAGING_CLEANUP_FAILED",
                            cleanup_error,
                        )
                    )
                    cleanup_error_type = (
                        operation_cleanup_failures[0].error_code
                    )
                    cleanup_failures.extend(
                        operation_cleanup_failures
                    )
            target_may_have_changed = _target_may_have_changed(publish_state)
            if publish_uncertain:
                target_may_have_changed = True
            manual_recovery_required = (
                publish_uncertain or publish_state == "EXCHANGE_REVERSED" or cleanup_failed
            )
            try:
                manifest.transition(
                    "FAILED",
                    PUBLISH_STATE=publish_state,
                    TARGET_MAY_HAVE_CHANGED=target_may_have_changed,
                    AUTOMATIC_RETRY_ALLOWED=False,
                    MANUAL_RECOVERY_REQUIRED=manual_recovery_required,
                    ERROR_TYPE=failure_code,
                    FAILURE_REASON=error.code if publish_uncertain else None,
                    CLEANUP_FAILED=cleanup_failed,
                    CLEANUP_ERROR_TYPE=cleanup_error_type,
                )
            except Exception:
                manifest_write_failed = True
        else:
            target_may_have_changed = _target_may_have_changed(publish_state)
            if publish_uncertain:
                target_may_have_changed = True
            manual_recovery_required = publish_uncertain or publish_state == "EXCHANGE_REVERSED"
        result_snapshot = {
                "status": "FAILED",
                "error_type": failure_code,
                "exit_classification": failure_code,
                "failure_reason": error.code if publish_uncertain else None,
                "publish_state": publish_state,
                "target_may_have_changed": target_may_have_changed,
                "automatic_retry_allowed": False,
                "manual_recovery_required": manual_recovery_required,
                "backup_available": backup.exists(),
                "cleanup_failed": cleanup_failed,
                "cleanup_error_type": cleanup_error_type,
                "manifest_write_failed": manifest_write_failed,
                "false_rollback_reported": False,
            }
        _json_print(result_snapshot)
        return 1
    finally:
        if staging_lease is not None:
            cleanup_failures.extend(
                _attempt_owned_cleanup(
                    "STAGING_SQLITE_LEASE",
                    "FINAL_RESOURCE_RELEASE",
                    "STAGING_SQLITE_LEASE_CLOSE_FAILED",
                    staging_lease.close,
                )
            )
            staging_lease = None
        if staging_pin is not None:
            cleanup_failures.extend(
                _attempt_owned_cleanup(
                    "STAGING_PINNED_DATABASE",
                    "FINAL_RESOURCE_RELEASE",
                    "STAGING_PINNED_DATABASE_CLOSE_FAILED",
                    staging_pin.close,
                )
            )
            staging_pin = None
        if source_lease is not None:
            cleanup_failures.extend(
                _attempt_owned_cleanup(
                    "SOURCE_SQLITE_LEASE",
                    "FINAL_RESOURCE_RELEASE",
                    "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
                    source_lease.close,
                )
            )
            source_lease = None
        if source_pin is not None:
            cleanup_failures.extend(
                _attempt_owned_cleanup(
                    "SOURCE_PINNED_DATABASE",
                    "FINAL_RESOURCE_RELEASE",
                    "SOURCE_PINNED_DATABASE_CLOSE_FAILED",
                    source_pin.close,
                )
            )
            source_pin = None
        if manifest_parent_fd is not None:
            cleanup_failures.extend(
                _attempt_owned_cleanup(
                    "MANIFEST_PARENT_DESCRIPTOR",
                    "FINAL_RESOURCE_RELEASE",
                    "MANIFEST_PARENT_CLOSE_FAILED",
                    lambda: os.close(manifest_parent_fd),
                )
            )
            manifest_parent_fd = None
        if cleanup_failures:
            if result_snapshot is None:
                uncertain = (
                    exchange_started
                    and publish_state != "EXCHANGE_REVERSED"
                )
                classification = (
                    "PUBLISH_UNCERTAIN"
                    if uncertain
                    else "CLEANUP_FAILED"
                )
                result_snapshot = {
                    "status": "FAILED",
                    "error_type": classification,
                    "exit_classification": classification,
                    "failure_reason": None,
                    "publish_state": publish_state,
                    "target_may_have_changed": uncertain,
                    "automatic_retry_allowed": False,
                    "manual_recovery_required": uncertain,
                    "backup_available": backup.exists(),
                    "cleanup_failed": True,
                    "cleanup_error_type": None,
                    "manifest_write_failed": True,
                    "false_rollback_reported": False,
                }
            raise _StagedCleanupTransport(
                result_snapshot=result_snapshot,
                publish_state=publish_state,
                exchange_started=exchange_started,
                cleanup_failures=cleanup_failures,
                durable_evidence_updated=False,
            )


def _merge_cleanup_transport(
    transport: _StagedCleanupTransport,
) -> dict[str, Any]:
    payload = dict(transport.result_snapshot)
    primary_present = payload.get("status") != "PASS"
    primary_classification = str(
        payload.get("failure_reason")
        or payload.get("exit_classification")
        or payload.get("error_type")
        or "SUCCESS"
    )
    primary_publish_state = str(
        payload.get("publish_state", transport.publish_state)
    )
    primary_target_changed = bool(
        payload.get("target_may_have_changed", False)
    )
    failures = [
        failure.as_payload()
        for failure in transport.cleanup_failures
    ]
    failure_codes = [
        failure["error_code"]
        for failure in failures
    ]
    post_exchange_uncertain = (
        transport.exchange_started
        and primary_publish_state != "EXCHANGE_REVERSED"
    ) or primary_target_changed

    payload.update(
        {
            "primary_exit_classification": primary_classification,
            "primary_publish_state": primary_publish_state,
            "primary_target_may_have_changed": primary_target_changed,
            "primary_automatic_retry_allowed": bool(
                payload.get("automatic_retry_allowed", False)
            ),
            "primary_manual_recovery_required": bool(
                payload.get("manual_recovery_required", False)
            ),
            "primary_exception_present": primary_present,
            "primary_exception_preserved": primary_present,
            "cleanup_failed": True,
            "cleanup_exception_recorded": True,
            "cleanup_exception_count": len(failures),
            "cleanup_failures": failures,
            "cleanup_failure_codes": failure_codes,
            "cleanup_error_type": (
                payload.get("cleanup_error_type") or failure_codes[0]
            ),
            "automatic_retry_allowed": False,
            "durable_evidence_updated": (
                transport.durable_evidence_updated
            ),
            "false_rollback_reported": False,
        }
    )
    if post_exchange_uncertain:
        payload.update(
            {
                "status": "FAILED",
                "error_type": "PUBLISH_UNCERTAIN",
                "exit_classification": "PUBLISH_UNCERTAIN",
                "publish_state": primary_publish_state,
                "target_may_have_changed": True,
                "manual_recovery_required": True,
            }
        )
    elif not primary_present:
        payload.update(
            {
                "status": "FAILED",
                "error_type": "CLEANUP_FAILED",
                "exit_classification": "CLEANUP_FAILED",
                "publish_state": "BEFORE_EXCHANGE",
                "target_may_have_changed": False,
                "manual_recovery_required": True,
            }
        )
    else:
        payload["manual_recovery_required"] = True
    return payload


def _execute_staged(
    args: argparse.Namespace,
    *,
    synthetic: bool,
    operation_id: str | None = None,
    expected_source_identity: SourceIdentity | None = None,
    _prepared_production: _PreparedProductionExecution | None = None,
    _synthetic_hooks: _SyntheticExecutionHooks | None = None,
) -> int:
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            return_code = _execute_staged_body(
                args,
                synthetic=synthetic,
                operation_id=operation_id,
                expected_source_identity=expected_source_identity,
                _prepared_production=_prepared_production,
                _synthetic_hooks=_synthetic_hooks,
            )
    except _StagedCleanupTransport as transport:
        _json_print(_merge_cleanup_transport(transport))
        return 1
    sys.stdout.write(captured.getvalue())
    return return_code


def execute_synthetic(
    args: argparse.Namespace,
    *,
    _phase_callback: Callable[[str], None] | None = None,
    _failure_callback: Callable[[str, str], None] | None = None,
    _migration_runner: Callable[[Contract, Path], None] = _run_target_migration,
    _compatibility_probe: Callable[[Contract, Path], Any] = _run_previous_image_probe,
    _before_exchange_callback: Callable[[], None] | None = None,
    _lifecycle_callback: Callable[[str, Path], None] | None = None,
) -> int:
    hooks = _SyntheticExecutionHooks(
        phase_callback=_phase_callback,
        failure_callback=_failure_callback,
        migration_runner=_migration_runner,
        compatibility_probe=_compatibility_probe,
        before_exchange_callback=_before_exchange_callback,
        lifecycle_callback=_lifecycle_callback,
    )
    return _execute_staged(
        args,
        synthetic=True,
        _synthetic_hooks=hooks,
    )


def _execute_authorized_staged(
    args: argparse.Namespace,
    *,
    prepared: _PreparedProductionExecution,
) -> int:
    """Run production only through the private, lease-holding authorization context."""
    return _execute_staged(
        args,
        synthetic=False,
        operation_id=prepared.authorization.operation_id,
        expected_source_identity=prepared.source_identity,
        _prepared_production=prepared,
    )


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
