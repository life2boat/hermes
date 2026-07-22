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
from typing import Any, Callable, Sequence, TextIO


# This entrypoint must not leave ignored bytecode inside an approved root.
sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import hermes_production_deploy as deployment  # noqa: E402
from scripts.hermes_execution_authority import (  # noqa: E402
    ExecutionAuthorityBundle,
    ExecutionAuthorityError,
    exact_repository_provenance,
    load_execution_authority,
)
from scripts.hermes_staged_schema_migrate import (  # noqa: E402
    OrchestratorError,
    SourceIdentity,
    _StagedCleanupTransport,
    _execute_authorized_staged,
    _merge_cleanup_transport,
    _fsync_directory,
    _issue_production_authorization,
    _prepare_authorized_production_execution,
    _target_schema_contract,
    _target_schema_fingerprint,
)


PLAN_VERSION = 4
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
OPERATIONS_ROOT_APPROVAL_FIELDS = frozenset(
    {
        "APPROVAL_VERSION",
        "CREATED_AT",
        "EXPIRES_AT",
        "TARGET_MAIN_SHA",
        "APPROVED_REPOSITORY_ROOT",
        "REPOSITORY_ROOT_DEVICE",
        "REPOSITORY_ROOT_INODE",
        "REPOSITORY_ROOT_UID",
        "REPOSITORY_ROOT_GID",
        "REPOSITORY_ROOT_MODE",
        "REPOSITORY_ROOT_TREE_SHA",
        "DEPLOYMENT_CONTRACT_PATH",
        "DEPLOYMENT_CONTRACT_DEVICE",
        "DEPLOYMENT_CONTRACT_INODE",
        "DEPLOYMENT_CONTRACT_SHA256",
        "PRODUCTION_MIGRATION_ENTRYPOINT_SHA256",
        "STAGED_IMPLEMENTATION_SHA256",
        "RUNBOOK_SHA256",
        "MIGRATION_IMAGE_ID",
        "MIGRATION_IMAGE_REVISION",
        "DIRTY_LEGACY_ROOT_PRESERVED",
        "PRODUCTION_DB_ACCESS_AUTHORIZED",
        "PRODUCTION_PLAN_ONLY_AUTHORIZED",
        "PRODUCTION_EXECUTE_AUTHORIZED",
        "DEPLOY_AUTHORIZED",
    }
)
CLEAN_START_POLICY_FIELDS = frozenset(
    {
        "POLICY_VERSION",
        "DATA_POLICY",
        "CREATED_AT",
        "TARGET_MAIN_SHA",
        "MIGRATION_IMAGE_ID",
        "PRODUCTION_DB_SOURCE_SHA256",
        "FAMILY_SHOPPING_BACKFILL_REQUIRED",
        "LEGACY_FAMILY_SHOPPING_DATA_MAY_BE_RESET",
        "MEMORY_OS_DATA_MUST_BE_PRESERVED",
        "NUTRITION_DIARY_DATA_MUST_BE_PRESERVED",
        "TELEGRAM_ADMIN_CONFIGURATION_MUST_BE_PRESERVED",
        "OUT_OF_SCOPE_TABLES_MUST_BE_PRESERVED",
        "EXECUTION_AUTHORIZED",
        "DELETION_PERFORMED",
    }
)
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
        "SOURCE_USER_VERSION",
        "SOURCE_SCHEMA_FINGERPRINT",
        "SOURCE_PARENT_IDENTITY",
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
        "OPERATIONS_ROOT_APPROVAL_PATH",
        "OPERATIONS_ROOT_APPROVAL_DEVICE",
        "OPERATIONS_ROOT_APPROVAL_INODE",
        "OPERATIONS_ROOT_APPROVAL_SIZE",
        "OPERATIONS_ROOT_APPROVAL_UID",
        "OPERATIONS_ROOT_APPROVAL_GID",
        "OPERATIONS_ROOT_APPROVAL_MODE",
        "OPERATIONS_ROOT_APPROVAL_SHA256",
        "OPERATIONS_ROOT_APPROVAL_EXPIRES_AT",
        "OPERATIONS_ROOT_APPROVAL_TREE_SHA",
        "CLEAN_START_POLICY_PATH",
        "CLEAN_START_POLICY_DEVICE",
        "CLEAN_START_POLICY_INODE",
        "CLEAN_START_POLICY_SIZE",
        "CLEAN_START_POLICY_UID",
        "CLEAN_START_POLICY_GID",
        "CLEAN_START_POLICY_MODE",
        "CLEAN_START_POLICY_SHA256",
        "CLEAN_START_POLICY_VERSION",
        "CLEAN_START_DATA_POLICY",
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


class _StructuredArgumentError(RuntimeError):
    """An invalid public CLI invocation with no raw argument details."""


class _CleanupFailure(RuntimeError):
    """One or more sanitized cleanup operations failed."""

    def __init__(self, codes: Sequence[str]) -> None:
        self.codes = tuple(dict.fromkeys(codes))
        super().__init__("CLEANUP_FAILED")


class _PrimaryAndCleanupFailure(RuntimeError):
    """Preserve a primary exception alongside cleanup failures."""

    def __init__(
        self,
        primary: Exception,
        cleanup_codes: Sequence[str],
    ) -> None:
        self.primary = primary
        self.cleanup_codes = tuple(dict.fromkeys(cleanup_codes))
        super().__init__("PRIMARY_AND_CLEANUP_FAILED")


def _cleanup_codes(
    exc: Exception,
    fallback: str,
) -> tuple[str, ...]:
    if isinstance(exc, _CleanupFailure):
        return exc.codes
    if isinstance(exc, _PrimaryAndCleanupFailure):
        return exc.cleanup_codes
    return (fallback,)


def _run_cleanup(
    *steps: tuple[str, Callable[[], None]],
) -> None:
    failures: list[str] = []
    for code, callback in steps:
        try:
            callback()
        except Exception as exc:
            failures.extend(_cleanup_codes(exc, code))
    if failures:
        raise _CleanupFailure(failures)


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
        _run_cleanup(
            ("PLAN_FILE_CLOSE_FAILED", lambda: os.close(self.file_fd)),
            ("PLAN_PARENT_CLOSE_FAILED", lambda: os.close(self.parent_fd)),
        )


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
        _run_cleanup(
            ("PINNED_DIRECTORY_CLOSE_FAILED", lambda: os.close(self.fd)),
        )


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
        _run_cleanup(
            (
                "DEPLOYMENT_CONTRACT_FILE_CLOSE_FAILED",
                lambda: os.close(self.file_fd),
            ),
            (
                "DEPLOYMENT_CONTRACT_PARENT_CLOSE_FAILED",
                lambda: os.close(self.parent_fd),
            ),
        )


@dataclass
class PinnedEvidenceDocument:
    path: Path
    parent_fd: int
    file_fd: int
    device: int
    inode: int
    size: int
    uid: int
    gid: int
    mode: int
    sha256: str
    payload: dict[str, Any]
    code_prefix: str

    def path_matches(self) -> bool:
        try:
            metadata = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            descriptor_metadata = os.fstat(self.file_fd)
            descriptor_sha = _sha256_fd(self.file_fd)
        except OSError:
            return False
        identity = (
            self.device,
            self.inode,
            self.size,
            self.uid,
            self.gid,
            self.mode,
        )
        return (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_uid,
            metadata.st_gid,
            stat.S_IMODE(metadata.st_mode),
        ) == identity and (
            descriptor_metadata.st_dev,
            descriptor_metadata.st_ino,
            descriptor_metadata.st_size,
            descriptor_metadata.st_uid,
            descriptor_metadata.st_gid,
            stat.S_IMODE(descriptor_metadata.st_mode),
        ) == identity and descriptor_sha == self.sha256

    def close(self) -> None:
        _run_cleanup(
            (
                f"{self.code_prefix}_FILE_CLOSE_FAILED",
                lambda: os.close(self.file_fd),
            ),
            (
                f"{self.code_prefix}_PARENT_CLOSE_FAILED",
                lambda: os.close(self.parent_fd),
            ),
        )


@dataclass
class ExecutionEvidence:
    parent_fd: int
    name: str
    payload: dict[str, Any]

    def checkpoint(self, **updates: Any) -> None:
        self.payload.update(updates)
        _write_json_durable_at(self.parent_fd, self.name, self.payload)

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
    operations_root_approval: PinnedEvidenceDocument
    clean_start_policy: PinnedEvidenceDocument
    execution_authority: ExecutionAuthorityBundle
    target_schema_version: str
    target_schema_fingerprint: str

    def close(self) -> None:
        _run_cleanup(
            (
                "EXECUTION_AUTHORITY_CLOSE_FAILED",
                self.execution_authority.close,
            ),
            (
                "CLEAN_START_POLICY_CLOSE_FAILED",
                self.clean_start_policy.close,
            ),
            (
                "OPERATIONS_ROOT_APPROVAL_CLOSE_FAILED",
                self.operations_root_approval.close,
            ),
            (
                "DEPLOYMENT_CONTRACT_CLOSE_FAILED",
                self.deployment_contract.close,
            ),
            ("EVIDENCE_PARENT_CLOSE_FAILED", self.evidence_parent.close),
            ("STAGING_PARENT_CLOSE_FAILED", self.staging_parent.close),
            ("BACKUP_PARENT_CLOSE_FAILED", self.backup_parent.close),
        )


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


def _sha256_fd(
    fd: int,
    *,
    code: str = "FILE_READ_CONTRACT_VIOLATION",
) -> str:
    before = os.fstat(fd)
    digest = hashlib.sha256()
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(1024 * 1024, before.st_size - offset), offset)
        if not chunk:
            raise ProductionGateError(code)
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(fd, 1, offset):
        raise ProductionGateError(code)
    after = os.fstat(fd)
    if (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mode,
        before.st_nlink,
    ) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mode,
        after.st_nlink,
    ):
        raise ProductionGateError(code)
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        return _sha256_fd(fd)
    finally:
        os.close(fd)


def _read_fd_bytes(fd: int, *, maximum: int, code: str) -> bytes:
    before = os.fstat(fd)
    if before.st_size > maximum:
        raise ProductionGateError(code)
    data = b""
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(fd, min(65536, before.st_size - offset), offset)
        if not chunk:
            raise ProductionGateError(code)
        data += chunk
        offset += len(chunk)
    if os.pread(fd, 1, offset):
        raise ProductionGateError(code)
    after = os.fstat(fd)
    if (
        offset != before.st_size
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mode,
            before.st_nlink,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mode,
            after.st_nlink,
        )
    ):
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
    if effective_gid != 0:
        raise ProductionGateError("ROOT_EGID_REQUIRED")
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
        user_version_row = connection.execute("PRAGMA user_version").fetchone()
        if (
            user_version_row is None
            or not isinstance(user_version_row[0], int)
            or user_version_row[0] < 0
        ):
            raise ProductionGateError("SOURCE_USER_VERSION_INVALID")
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
        source_sha = _sha256_fd(
            fd,
            code="SOURCE_READ_CONTRACT_VIOLATION",
        )
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
            "SOURCE_USER_VERSION": int(user_version_row[0]),
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


def _open_evidence_document(
    path_value: str,
    expected_sha: str,
    *,
    code_prefix: str,
    expected_fields: frozenset[str],
) -> PinnedEvidenceDocument:
    path = _absolute_path(path_value, f"{code_prefix}_PATH")
    _no_symlink_chain(path)
    if SHA_RE.fullmatch(expected_sha) is None:
        raise ProductionGateError(f"EXPECTED_{code_prefix}_SHA256_INVALID")
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
        owner_uid = os.geteuid()  # windows-footgun: ok
        owner_gid = os.getegid()  # windows-footgun: ok
        parent_metadata = os.fstat(parent_fd)
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != owner_uid
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != owner_uid
            or metadata.st_gid != owner_gid
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > MAX_DOCUMENT_BYTES
        ):
            raise ProductionGateError(f"{code_prefix}_FILE_METADATA_INVALID")
        data = _read_fd_bytes(
            file_fd,
            maximum=MAX_DOCUMENT_BYTES,
            code=f"{code_prefix}_FILE_TOO_LARGE",
        )
        actual_sha = _sha256_bytes(data)
        if actual_sha != expected_sha:
            raise ProductionGateError(f"{code_prefix}_SHA256_MISMATCH")
        try:
            payload = json.loads(data.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionGateError(f"{code_prefix}_JSON_INVALID") from exc
        if not isinstance(payload, dict) or _canonical_json(payload) != data:
            raise ProductionGateError(f"{code_prefix}_JSON_NOT_CANONICAL")
        if set(payload) != expected_fields:
            raise ProductionGateError(f"{code_prefix}_FIELDS_INVALID")
        current = os.fstat(file_fd)
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_uid,
            metadata.st_gid,
            stat.S_IMODE(metadata.st_mode),
        )
        if (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_uid,
            current.st_gid,
            stat.S_IMODE(current.st_mode),
        ) != identity or (
            path_metadata.st_dev,
            path_metadata.st_ino,
            path_metadata.st_size,
            path_metadata.st_uid,
            path_metadata.st_gid,
            stat.S_IMODE(path_metadata.st_mode),
        ) != identity:
            raise ProductionGateError(f"{code_prefix}_PATH_SUBSTITUTION")
        return PinnedEvidenceDocument(
            path=path,
            parent_fd=parent_fd,
            file_fd=file_fd,
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
            size=int(metadata.st_size),
            uid=int(metadata.st_uid),
            gid=int(metadata.st_gid),
            mode=stat.S_IMODE(metadata.st_mode),
            sha256=actual_sha,
            payload=payload,
            code_prefix=code_prefix,
        )
    except Exception:
        os.close(file_fd)
        os.close(parent_fd)
        raise


def _repository_provenance(repository_root: Path) -> tuple[str, str]:
    try:
        return exact_repository_provenance(repository_root)
    except ExecutionAuthorityError as exc:
        raise ProductionGateError(exc.code) from exc


def _typed_mapping_matches(
    payload: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    return all(
        type(payload.get(name)) is type(value) and payload.get(name) == value
        for name, value in expected.items()
    )


def _validate_operations_root_approval(
    document: PinnedEvidenceDocument,
    *,
    repository_root: Path,
    migration_image_id: str,
    migration_revision: str,
    deployment_contract: PinnedDeploymentContract,
) -> None:
    payload = document.payload
    if type(payload.get("APPROVAL_VERSION")) is not int or payload.get("APPROVAL_VERSION") != 1:
        raise ProductionGateError("OPERATIONS_ROOT_APPROVAL_VERSION_INVALID")
    created_at = _parse_timestamp(
        payload.get("CREATED_AT"),
        "OPERATIONS_ROOT_APPROVAL_CREATED_AT_INVALID",
    )
    expires_at = _parse_timestamp(
        payload.get("EXPIRES_AT"),
        "OPERATIONS_ROOT_APPROVAL_EXPIRY_INVALID",
    )
    now = _now()
    if (
        created_at > now
        or expires_at <= created_at
        or expires_at - created_at > timedelta(days=1)
        or now >= expires_at
    ):
        raise ProductionGateError("OPERATIONS_ROOT_APPROVAL_EXPIRED")
    head, tree = _repository_provenance(repository_root)
    root_record = _directory_record(repository_root, private=False)
    root_fields = {
        "APPROVED_REPOSITORY_ROOT": str(repository_root),
        "REPOSITORY_ROOT_DEVICE": root_record["DEVICE"],
        "REPOSITORY_ROOT_INODE": root_record["INODE"],
        "REPOSITORY_ROOT_UID": root_record["UID"],
        "REPOSITORY_ROOT_GID": root_record["GID"],
        "REPOSITORY_ROOT_MODE": root_record["MODE"],
        "REPOSITORY_ROOT_TREE_SHA": tree,
        "TARGET_MAIN_SHA": head,
        "MIGRATION_IMAGE_ID": migration_image_id,
        "MIGRATION_IMAGE_REVISION": migration_revision,
        "DEPLOYMENT_CONTRACT_PATH": str(deployment_contract.path),
        "DEPLOYMENT_CONTRACT_DEVICE": deployment_contract.device,
        "DEPLOYMENT_CONTRACT_INODE": deployment_contract.inode,
        "DEPLOYMENT_CONTRACT_SHA256": deployment_contract.sha256,
        "PRODUCTION_MIGRATION_ENTRYPOINT_SHA256": _sha256(
            repository_root / "scripts/hermes_production_staged_migrate.py"
        ),
        "STAGED_IMPLEMENTATION_SHA256": _sha256(
            repository_root / "scripts/hermes_staged_schema_migrate.py"
        ),
        "RUNBOOK_SHA256": _sha256(
            repository_root
            / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
        ),
    }
    approval_flags = {
        "DIRTY_LEGACY_ROOT_PRESERVED": True,
        "PRODUCTION_DB_ACCESS_AUTHORIZED": False,
        "PRODUCTION_PLAN_ONLY_AUTHORIZED": True,
        "PRODUCTION_EXECUTE_AUTHORIZED": False,
        "DEPLOY_AUTHORIZED": False,
    }
    if (
        head != migration_revision
        or not _typed_mapping_matches(payload, root_fields)
        or any(payload.get(name) is not value for name, value in approval_flags.items())
    ):
        raise ProductionGateError("OPERATIONS_ROOT_APPROVAL_MISMATCH")


def _validate_clean_start_policy(
    document: PinnedEvidenceDocument,
    *,
    source_sha256: str,
    migration_image_id: str,
    migration_revision: str,
) -> None:
    payload = document.payload
    created_at = _parse_timestamp(
        payload.get("CREATED_AT"),
        "CLEAN_START_POLICY_CREATED_AT_INVALID",
    )
    if created_at > _now():
        raise ProductionGateError("CLEAN_START_POLICY_CREATED_AT_INVALID")
    expected = {
        "POLICY_VERSION": 1,
        "DATA_POLICY": "NO_CLIENTS_CLEAN_START",
        "TARGET_MAIN_SHA": migration_revision,
        "MIGRATION_IMAGE_ID": migration_image_id,
        "PRODUCTION_DB_SOURCE_SHA256": source_sha256,
    }
    policy_flags = {
        "FAMILY_SHOPPING_BACKFILL_REQUIRED": False,
        "LEGACY_FAMILY_SHOPPING_DATA_MAY_BE_RESET": True,
        "MEMORY_OS_DATA_MUST_BE_PRESERVED": True,
        "NUTRITION_DIARY_DATA_MUST_BE_PRESERVED": True,
        "TELEGRAM_ADMIN_CONFIGURATION_MUST_BE_PRESERVED": True,
        "OUT_OF_SCOPE_TABLES_MUST_BE_PRESERVED": True,
        "EXECUTION_AUTHORIZED": False,
        "DELETION_PERFORMED": False,
    }
    if not _typed_mapping_matches(payload, expected) or any(
        payload.get(name) is not value for name, value in policy_flags.items()
    ):
        raise ProductionGateError("CLEAN_START_POLICY_MISMATCH")


def _document_plan_fields(
    prefix: str,
    document: PinnedEvidenceDocument,
) -> dict[str, int | str]:
    return {
        f"{prefix}_PATH": str(document.path),
        f"{prefix}_DEVICE": document.device,
        f"{prefix}_INODE": document.inode,
        f"{prefix}_SIZE": document.size,
        f"{prefix}_UID": document.uid,
        f"{prefix}_GID": document.gid,
        f"{prefix}_MODE": document.mode,
        f"{prefix}_SHA256": document.sha256,
    }


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
) -> tuple[Path, Path, Path, Path, Path, Path, Path]:
    repository_root = _canonical_repository_root(args.repository_root)
    db_path = _absolute_path(args.db_path, "DB_PATH")
    backup_parent = _absolute_path(args.backup_parent, "BACKUP_PARENT")
    staging_parent = _absolute_path(args.staging_parent, "STAGING_PARENT")
    evidence_parent = _absolute_path(args.evidence_parent, "EVIDENCE_PARENT")
    operations_root_approval = _absolute_path(
        args.operations_root_approval,
        "OPERATIONS_ROOT_APPROVAL_PATH",
    )
    clean_start_policy = _absolute_path(
        args.clean_start_policy,
        "CLEAN_START_POLICY_PATH",
    )
    if socket.gethostname() != args.expected_hostname:
        raise ProductionGateError("HOSTNAME_MISMATCH")
    if REVISION_RE.fullmatch(args.migration_image_revision) is None:
        raise ProductionGateError("MIGRATION_IMAGE_REVISION_INVALID")
    if SHA_RE.fullmatch(args.expected_source_sha256) is None:
        raise ProductionGateError("EXPECTED_SOURCE_SHA256_INVALID")
    if SHA_RE.fullmatch(args.expected_operations_root_approval_sha256) is None:
        raise ProductionGateError(
            "EXPECTED_OPERATIONS_ROOT_APPROVAL_SHA256_INVALID"
        )
    if SHA_RE.fullmatch(args.expected_clean_start_policy_sha256) is None:
        raise ProductionGateError("EXPECTED_CLEAN_START_POLICY_SHA256_INVALID")
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
    _pairwise_disjoint((operations_root_approval, clean_start_policy))
    for document in (operations_root_approval, clean_start_policy):
        if (
            document == db_path
            or document.is_relative_to(repository_root)
            or document.is_relative_to(backup_parent)
            or document.is_relative_to(staging_parent)
        ):
            raise ProductionGateError("EVIDENCE_PATH_LOCATION_INVALID")
    return (
        repository_root,
        db_path,
        backup_parent,
        staging_parent,
        evidence_parent,
        operations_root_approval,
        clean_start_policy,
    )


def create_plan(args: argparse.Namespace) -> int:
    root_identity = _root_identity()
    (
        repository_root,
        db_path,
        backup_parent,
        staging_parent,
        evidence_parent,
        operations_root_approval_path,
        clean_start_policy_path,
    ) = _validate_plan_inputs(args)
    backup_record = _directory_record(backup_parent, private=True)
    staging_record = _directory_record(staging_parent, private=True)
    evidence_record = _directory_record(evidence_parent, private=True)
    _inspect_image(args.migration_image_id, args.migration_image_revision)
    _inspect_image(args.previous_image_id, None)
    target_schema = _target_schema_contract()
    deployment_contract: PinnedDeploymentContract | None = None
    operations_root_approval: PinnedEvidenceDocument | None = None
    clean_start_policy: PinnedEvidenceDocument | None = None
    try:
        deployment_contract = _open_canonical_deployment_contract(repository_root)
        operations_root_approval = _open_evidence_document(
            str(operations_root_approval_path),
            args.expected_operations_root_approval_sha256,
            code_prefix="OPERATIONS_ROOT_APPROVAL",
            expected_fields=OPERATIONS_ROOT_APPROVAL_FIELDS,
        )
        _validate_operations_root_approval(
            operations_root_approval,
            repository_root=repository_root,
            migration_image_id=args.migration_image_id,
            migration_revision=args.migration_image_revision,
            deployment_contract=deployment_contract,
        )
        clean_start_policy = _open_evidence_document(
            str(clean_start_policy_path),
            args.expected_clean_start_policy_sha256,
            code_prefix="CLEAN_START_POLICY",
            expected_fields=CLEAN_START_POLICY_FIELDS,
        )
        identity, schema_fingerprint, integrity, foreign_keys = (
            _read_only_source(db_path)
        )
        expected_identity = {
            "SOURCE_DEVICE": args.expected_source_device,
            "SOURCE_INODE": args.expected_source_inode,
            "SOURCE_SIZE": args.expected_source_size,
            "SOURCE_SHA256": args.expected_source_sha256,
        }
        if any(
            identity[name] != value
            for name, value in expected_identity.items()
        ):
            raise ProductionGateError("EXPECTED_SOURCE_IDENTITY_MISMATCH")
        _validate_clean_start_policy(
            clean_start_policy,
            source_sha256=str(identity["SOURCE_SHA256"]),
            migration_image_id=args.migration_image_id,
            migration_revision=args.migration_image_revision,
        )
        if integrity != "ok" or foreign_keys != 0:
            raise ProductionGateError("SOURCE_DATABASE_INVALID")
        if identity["SOURCE_DEVICE"] != staging_record["DEVICE"]:
            raise ProductionGateError("CROSS_FILESYSTEM_STAGING")
        _require_free_bytes(staging_parent, args.expected_free_bytes)
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
            "SOURCE_PARENT_IDENTITY": _directory_record(
                db_path.parent, private=False
            ),
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
            **_document_plan_fields(
                "OPERATIONS_ROOT_APPROVAL",
                operations_root_approval,
            ),
            "OPERATIONS_ROOT_APPROVAL_EXPIRES_AT": (
                operations_root_approval.payload["EXPIRES_AT"]
            ),
            "OPERATIONS_ROOT_APPROVAL_TREE_SHA": (
                operations_root_approval.payload["REPOSITORY_ROOT_TREE_SHA"]
            ),
            **_document_plan_fields(
                "CLEAN_START_POLICY",
                clean_start_policy,
            ),
            "CLEAN_START_POLICY_VERSION": clean_start_policy.payload[
                "POLICY_VERSION"
            ],
            "CLEAN_START_DATA_POLICY": clean_start_policy.payload[
                "DATA_POLICY"
            ],
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
        cleanup_steps: list[tuple[str, Callable[[], None]]] = []
        if clean_start_policy is not None:
            cleanup_steps.append(
                ("CLEAN_START_POLICY_CLOSE_FAILED", clean_start_policy.close)
            )
        if operations_root_approval is not None:
            cleanup_steps.append(
                (
                    "OPERATIONS_ROOT_APPROVAL_CLOSE_FAILED",
                    operations_root_approval.close,
                )
            )
        if deployment_contract is not None:
            cleanup_steps.append(
                (
                    "DEPLOYMENT_CONTRACT_CLOSE_FAILED",
                    deployment_contract.close,
                )
            )
        _run_cleanup(*cleanup_steps)


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
    migration_image = _expect_plan_string(
        plan,
        "MIGRATION_IMAGE_ID",
        IMAGE_ID_RE,
    )
    approval_sha = _expect_plan_string(
        plan, "OPERATIONS_ROOT_APPROVAL_SHA256", SHA_RE
    )
    policy_sha = _expect_plan_string(
        plan, "CLEAN_START_POLICY_SHA256", SHA_RE
    )
    if operation_id != args.confirm_operation_id:
        raise ProductionGateError("PLAN_OPERATION_ID_MISMATCH")
    if source_sha != args.confirm_source_sha256:
        raise ProductionGateError("PLAN_SOURCE_SHA256_CONFIRMATION_MISMATCH")
    if revision != args.confirm_image_revision:
        raise ProductionGateError("PLAN_IMAGE_REVISION_CONFIRMATION_MISMATCH")
    if approval_sha != args.confirm_operations_root_approval_sha256:
        raise ProductionGateError(
            "PLAN_OPERATIONS_ROOT_APPROVAL_SHA256_CONFIRMATION_MISMATCH"
        )
    if policy_sha != args.confirm_clean_start_policy_sha256:
        raise ProductionGateError(
            "PLAN_CLEAN_START_POLICY_SHA256_CONFIRMATION_MISMATCH"
        )
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
    operations_root_approval: PinnedEvidenceDocument | None = None
    clean_start_policy: PinnedEvidenceDocument | None = None
    execution_authority: ExecutionAuthorityBundle | None = None
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
        if not _typed_mapping_matches(plan, contract_fields):
            raise ProductionGateError("DEPLOYMENT_CONTRACT_DRIFT")

        operations_root_approval = _open_evidence_document(
            _expect_plan_string(plan, "OPERATIONS_ROOT_APPROVAL_PATH"),
            approval_sha,
            code_prefix="OPERATIONS_ROOT_APPROVAL",
            expected_fields=OPERATIONS_ROOT_APPROVAL_FIELDS,
        )
        _validate_operations_root_approval(
            operations_root_approval,
            repository_root=repository_root,
            migration_image_id=migration_image,
            migration_revision=revision,
            deployment_contract=deployment_contract,
        )
        approval_fields: dict[str, Any] = {
            **_document_plan_fields(
                "OPERATIONS_ROOT_APPROVAL",
                operations_root_approval,
            ),
            "OPERATIONS_ROOT_APPROVAL_EXPIRES_AT": (
                operations_root_approval.payload["EXPIRES_AT"]
            ),
            "OPERATIONS_ROOT_APPROVAL_TREE_SHA": (
                operations_root_approval.payload["REPOSITORY_ROOT_TREE_SHA"]
            ),
        }
        if not _typed_mapping_matches(plan, approval_fields):
            raise ProductionGateError("OPERATIONS_ROOT_APPROVAL_IDENTITY_DRIFT")

        clean_start_policy = _open_evidence_document(
            _expect_plan_string(plan, "CLEAN_START_POLICY_PATH"),
            policy_sha,
            code_prefix="CLEAN_START_POLICY",
            expected_fields=CLEAN_START_POLICY_FIELDS,
        )
        _validate_clean_start_policy(
            clean_start_policy,
            source_sha256=source_sha,
            migration_image_id=migration_image,
            migration_revision=revision,
        )
        policy_fields: dict[str, Any] = {
            **_document_plan_fields("CLEAN_START_POLICY", clean_start_policy),
            "CLEAN_START_POLICY_VERSION": clean_start_policy.payload[
                "POLICY_VERSION"
            ],
            "CLEAN_START_DATA_POLICY": clean_start_policy.payload[
                "DATA_POLICY"
            ],
        }
        if not _typed_mapping_matches(plan, policy_fields):
            raise ProductionGateError("CLEAN_START_POLICY_IDENTITY_DRIFT")

        try:
            execution_authority = load_execution_authority(
                authority_path=args.final_authority,
                authority_sha256=args.expected_final_authority_sha256,
                plan_path=pinned.path,
                plan_sha256=pinned.sha256,
                plan=plan,
                repository_root=repository_root,
            )
        except ExecutionAuthorityError as exc:
            raise ProductionGateError(exc.code) from exc

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
            "SOURCE_USER_VERSION",
        ):
            if plan.get(name) != identity[name]:
                raise ProductionGateError("SOURCE_IDENTITY_DRIFT")
        if plan.get("SOURCE_SCHEMA_FINGERPRINT") != source_schema:
            raise ProductionGateError("SOURCE_SCHEMA_DRIFT")
        if integrity != "ok" or foreign_keys != 0:
            raise ProductionGateError("SOURCE_DATABASE_INVALID")
        source_parent_identity = _directory_record(
            db_path.parent, private=False
        )
        if plan.get("SOURCE_PARENT_IDENTITY") != source_parent_identity:
            raise ProductionGateError("SOURCE_PARENT_IDENTITY_DRIFT")
        try:
            execution_authority.validate_source(
                identity=identity,
                schema_fingerprint=source_schema,
                parent_identity=source_parent_identity,
            )
        except ExecutionAuthorityError as exc:
            raise ProductionGateError(exc.code) from exc
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
            operations_root_approval=operations_root_approval,
            clean_start_policy=clean_start_policy,
            execution_authority=execution_authority,
            target_schema_version=expected_target.version,
            target_schema_fingerprint=expected_target.fingerprint,
        )
    except Exception as primary:
        cleanup_steps: list[tuple[str, Callable[[], None]]] = []
        if execution_authority is not None:
            cleanup_steps.append(
                (
                    "EXECUTION_AUTHORITY_CLOSE_FAILED",
                    execution_authority.close,
                )
            )
        if clean_start_policy is not None:
            cleanup_steps.append(
                ("CLEAN_START_POLICY_CLOSE_FAILED", clean_start_policy.close)
            )
        if operations_root_approval is not None:
            cleanup_steps.append(
                (
                    "OPERATIONS_ROOT_APPROVAL_CLOSE_FAILED",
                    operations_root_approval.close,
                )
            )
        if deployment_contract is not None:
            cleanup_steps.append(
                (
                    "DEPLOYMENT_CONTRACT_CLOSE_FAILED",
                    deployment_contract.close,
                )
            )
        if evidence_parent is not None:
            cleanup_steps.append(
                ("EVIDENCE_PARENT_CLOSE_FAILED", evidence_parent.close)
            )
        if staging_parent is not None:
            cleanup_steps.append(
                ("STAGING_PARENT_CLOSE_FAILED", staging_parent.close)
            )
        if backup_parent is not None:
            cleanup_steps.append(
                ("BACKUP_PARENT_CLOSE_FAILED", backup_parent.close)
            )
        try:
            _run_cleanup(*cleanup_steps)
        except _CleanupFailure as cleanup:
            raise _PrimaryAndCleanupFailure(
                primary,
                cleanup.codes,
            ) from primary
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
    primary: Exception | None = None
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
    except Exception as exc:
        primary = exc
        raise
    finally:
        try:
            os.close(fd)
        except Exception as cleanup:
            codes = _cleanup_codes(
                cleanup,
                "INTERNAL_MANIFEST_CLOSE_FAILED",
            )
            if primary is not None:
                raise _PrimaryAndCleanupFailure(
                    primary,
                    codes,
                ) from primary
            raise _CleanupFailure(codes) from cleanup


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


@dataclass
class _ExecutionOutcome:
    exit_code: int
    payload: dict[str, Any]
    stream: TextIO
    primary_error_type: str | None = None


def _safe_exception_code(exc: Exception) -> str:
    if isinstance(exc, _PrimaryAndCleanupFailure):
        return _safe_exception_code(exc.primary)
    if isinstance(exc, (ProductionGateError, OrchestratorError)):
        return exc.code
    if isinstance(exc, _CleanupFailure):
        return "CLEANUP_FAILED"
    return type(exc).__name__


def _safe_cleanup_metadata(value: object, fallback: str) -> str:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", value):
        return value
    return fallback


def _sanitized_cleanup_failures(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    failures: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        failures.append(
            {
                "resource_kind": _safe_cleanup_metadata(
                    item.get("resource_kind"),
                    "UNKNOWN_RESOURCE",
                ),
                "cleanup_phase": _safe_cleanup_metadata(
                    item.get("cleanup_phase"),
                    "UNKNOWN_CLEANUP_PHASE",
                ),
                "error_type": _safe_cleanup_metadata(
                    item.get("error_type"),
                    "CleanupError",
                ),
                "error_code": _safe_cleanup_metadata(
                    item.get("error_code"),
                    "OWNED_RESOURCE_CLEANUP_FAILED",
                ),
            }
        )
    return failures


def _cleanup_count(value: object, minimum: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    return minimum


def _failure_outcome(
    operation_id: str | None,
    result: dict[str, Any],
    *,
    durable_evidence_updated: bool,
    primary_error_type: str | None = None,
    stream: TextIO | None = None,
) -> _ExecutionOutcome:
    updates = _failure_updates(result)
    cleanup_failures = _sanitized_cleanup_failures(
        result.get("cleanup_failures")
    )
    raw_cleanup_codes = result.get("cleanup_failure_codes", [])
    if not isinstance(raw_cleanup_codes, (list, tuple)):
        raw_cleanup_codes = []
    cleanup_codes = [
        _safe_cleanup_metadata(code, "OWNED_RESOURCE_CLEANUP_FAILED")
        for code in raw_cleanup_codes
        if isinstance(code, str)
    ]
    if not cleanup_codes:
        cleanup_codes = [
            failure["error_code"] for failure in cleanup_failures
        ]
    cleanup_resource_kinds = [
        failure["resource_kind"] for failure in cleanup_failures
    ]
    cleanup_count = _cleanup_count(
        result.get("cleanup_exception_count"),
        len(cleanup_failures),
    )
    primary_present = bool(
        result.get(
            "primary_exception_present",
            primary_error_type is not None,
        )
    )
    primary_classification = (
        result.get("primary_exit_classification")
        or primary_error_type
        or (updates["EXIT_CLASSIFICATION"] if primary_present else None)
    )
    primary_exception_type = None
    primary_error_code = None
    if primary_present:
        primary_exception_type = _safe_cleanup_metadata(
            result.get("primary_error_type"),
            primary_error_type or "PrimaryError",
        )
        primary_error_code = _safe_cleanup_metadata(
            result.get("primary_error_code"),
            primary_classification or "PRIMARY_FAILURE",
        )
    payload: dict[str, Any] = {
        "status": "FAILED",
        "error_type": updates["ERROR_TYPE"],
        "exit_classification": updates["EXIT_CLASSIFICATION"],
        "publish_state": updates["PUBLISH_STATE"],
        "target_may_have_changed": updates["TARGET_MAY_HAVE_CHANGED"],
        "database_mutated": updates["DATABASE_MUTATED"],
        "backup_created": updates["BACKUP_CREATED"],
        "automatic_retry_allowed": False,
        "manual_recovery_required": updates[
            "MANUAL_RECOVERY_REQUIRED"
        ],
        "durable_evidence_updated": durable_evidence_updated,
        "durable_evidence_persisted": durable_evidence_updated,
        "primary_exit_classification": primary_classification,
        "primary_publish_state": result.get(
            "primary_publish_state",
            updates["PUBLISH_STATE"],
        ),
        "primary_target_may_have_changed": bool(
            result.get(
                "primary_target_may_have_changed",
                updates["TARGET_MAY_HAVE_CHANGED"],
            )
        ),
        "primary_automatic_retry_allowed": bool(
            result.get("primary_automatic_retry_allowed", False)
        ),
        "primary_manual_recovery_required": bool(
            result.get(
                "primary_manual_recovery_required",
                updates["MANUAL_RECOVERY_REQUIRED"],
            )
        ),
        "primary_error_type": primary_exception_type,
        "primary_error_code": primary_error_code,
        "primary_exception_present": primary_present,
        "primary_exception_preserved": bool(
            result.get("primary_exception_preserved", primary_present)
        ),
        "cleanup_exception_recorded": bool(
            result.get(
                "cleanup_exception_recorded",
                cleanup_count > 0,
            )
        ),
        "cleanup_exception_count": cleanup_count,
        "cleanup_failures": cleanup_failures,
        "cleanup_resource_kinds": cleanup_resource_kinds,
        "cleanup_failure_codes": cleanup_codes,
    }
    if operation_id is not None:
        payload["operation_id"] = operation_id
    return _ExecutionOutcome(
        exit_code=1,
        payload=payload,
        stream=sys.stdout if stream is None else stream,
        primary_error_type=primary_error_type,
    )


def _post_exchange_uncertain_outcome(
    evidence: ExecutionEvidence | None,
    operation_id: str | None,
    reason: str | None,
    *,
    publish_state: str,
) -> _ExecutionOutcome:
    result = {
        "error_type": "PUBLISH_UNCERTAIN",
        "exit_classification": "PUBLISH_UNCERTAIN",
        "publish_state": publish_state,
        "target_may_have_changed": True,
        "manual_recovery_required": True,
        "automatic_retry_allowed": False,
    }
    evidence_updated = False
    if evidence is not None:
        try:
            _record_failure(evidence, result)
            evidence_updated = True
        except Exception:
            evidence_updated = False
    return _failure_outcome(
        operation_id,
        result,
        durable_evidence_updated=evidence_updated,
        primary_error_type=reason,
        stream=sys.stderr,
    )


def _publish_state_requires_uncertainty(
    publish_state: str,
    target_may_have_changed: bool,
) -> bool:
    return target_may_have_changed or publish_state not in {
        "BEFORE_EXCHANGE",
        "EXCHANGE_REVERSED",
    }


def _apply_cleanup_failure(
    outcome: _ExecutionOutcome,
    codes: Sequence[str],
    *,
    evidence: ExecutionEvidence | None,
    publish_state: str,
) -> None:
    sanitized_codes = [
        _safe_cleanup_metadata(code, "OUTER_RESOURCE_CLEANUP_FAILED")
        for code in codes
    ]
    combined = list(outcome.payload.get("cleanup_failure_codes", []))
    combined.extend(sanitized_codes)
    cleanup_failures = _sanitized_cleanup_failures(
        outcome.payload.get("cleanup_failures")
    )
    cleanup_failures.extend(
        {
            "resource_kind": "PRODUCTION_GATE_RESOURCE",
            "cleanup_phase": "OUTER_RESOURCE_RELEASE",
            "error_type": "CleanupError",
            "error_code": code,
        }
        for code in sanitized_codes
    )
    prior_classification = str(
        outcome.payload.get("exit_classification", "")
    )
    had_primary = bool(
        outcome.payload.get("primary_exception_present")
        or (
            outcome.exit_code != 0
            and outcome.primary_error_type is not None
        )
    )
    if had_primary and outcome.primary_error_type is not None:
        outcome.payload.setdefault(
            "primary_error_type",
            outcome.primary_error_type,
        )
    outcome.payload["primary_exception_present"] = had_primary
    outcome.payload["primary_exception_preserved"] = bool(
        outcome.payload.get("primary_exception_preserved") or had_primary
    )
    outcome.payload["cleanup_exception_recorded"] = True
    outcome.payload["cleanup_exception_count"] = _cleanup_count(
        outcome.payload.get("cleanup_exception_count"),
        len(cleanup_failures) - len(sanitized_codes),
    ) + len(sanitized_codes)
    outcome.payload["cleanup_failures"] = cleanup_failures
    outcome.payload["cleanup_failure_codes"] = combined
    outcome.payload["cleanup_resource_kinds"] = [
        failure["resource_kind"] for failure in cleanup_failures
    ]

    requires_uncertainty = _publish_state_requires_uncertainty(
        publish_state,
        bool(outcome.payload.get("target_may_have_changed")),
    )
    if requires_uncertainty:
        outcome.exit_code = 1
        outcome.stream = sys.stderr
        outcome.payload.update(
            {
                "status": "FAILED",
                "error_type": "PUBLISH_UNCERTAIN",
                "exit_classification": "PUBLISH_UNCERTAIN",
                "publish_state": publish_state,
                "target_may_have_changed": True,
                "database_mutated": None,
                "automatic_retry_allowed": False,
                "manual_recovery_required": True,
            }
        )
    elif outcome.exit_code == 0:
        outcome.exit_code = 1
        outcome.payload.update(
            {
                "status": "FAILED",
                "error_type": "CLEANUP_FAILED",
                "exit_classification": "CLEANUP_FAILED",
                "publish_state": "BEFORE_EXCHANGE",
                "target_may_have_changed": False,
                "database_mutated": False,
                "automatic_retry_allowed": False,
                "manual_recovery_required": False,
            }
        )
    elif had_primary:
        outcome.payload["exit_classification"] = prior_classification
        outcome.payload["error_type"] = prior_classification

    evidence_updated = False
    if evidence is not None:
        try:
            if requires_uncertainty and evidence.payload.get("STATE") not in {
                "PUBLISH_UNCERTAIN",
                "MANUAL_RECOVERY_REQUIRED",
            }:
                _record_failure(
                    evidence,
                    {
                        "error_type": "PUBLISH_UNCERTAIN",
                        "exit_classification": "PUBLISH_UNCERTAIN",
                        "publish_state": publish_state,
                        "target_may_have_changed": True,
                        "manual_recovery_required": True,
                    },
                )
            cleanup_resource_kinds = [
                failure["resource_kind"] for failure in cleanup_failures
            ]
            evidence.checkpoint(
                FINAL_EXIT_CLASSIFICATION=outcome.payload[
                    "exit_classification"
                ],
                PRIMARY_ERROR_TYPE=outcome.payload.get(
                    "primary_error_type",
                    outcome.primary_error_type,
                ),
                PRIMARY_ERROR_CODE=outcome.payload.get(
                    "primary_error_code"
                ),
                PRIMARY_EXIT_CLASSIFICATION=outcome.payload.get(
                    "primary_exit_classification"
                ),
                PRIMARY_PUBLISH_STATE=outcome.payload.get(
                    "primary_publish_state"
                ),
                PRIMARY_EXCEPTION_PRESENT=had_primary,
                PRIMARY_EXCEPTION_PRESERVED=outcome.payload.get(
                    "primary_exception_preserved",
                    had_primary,
                ),
                CLEANUP_EXCEPTION_RECORDED=True,
                CLEANUP_EXCEPTION_COUNT=outcome.payload[
                    "cleanup_exception_count"
                ],
                CLEANUP_FAILURES=cleanup_failures,
                CLEANUP_RESOURCE_KINDS=cleanup_resource_kinds,
                CLEANUP_FAILURE_CODES=combined,
                DURABLE_EVIDENCE_UPDATED=True,
            )
            evidence_updated = True
        except Exception:
            evidence_updated = False
    outcome.payload["durable_evidence_updated"] = evidence_updated
    outcome.payload["durable_evidence_persisted"] = evidence_updated


def _finalize_execution_evidence(
    outcome: _ExecutionOutcome,
    evidence: ExecutionEvidence | None,
    *,
    publish_state: str,
) -> None:
    if evidence is None:
        return
    try:
        evidence.checkpoint(
            FINAL_EXIT_CLASSIFICATION=outcome.payload.get(
                "exit_classification",
                "PASS",
            ),
            PRIMARY_ERROR_TYPE=outcome.payload.get(
                "primary_error_type",
                outcome.primary_error_type,
            ),
            PRIMARY_ERROR_CODE=outcome.payload.get(
                "primary_error_code"
            ),
            PRIMARY_EXIT_CLASSIFICATION=outcome.payload.get(
                "primary_exit_classification"
            ),
            PRIMARY_PUBLISH_STATE=outcome.payload.get(
                "primary_publish_state"
            ),
            PRIMARY_TARGET_MAY_HAVE_CHANGED=outcome.payload.get(
                "primary_target_may_have_changed"
            ),
            PRIMARY_AUTOMATIC_RETRY_ALLOWED=outcome.payload.get(
                "primary_automatic_retry_allowed"
            ),
            PRIMARY_MANUAL_RECOVERY_REQUIRED=outcome.payload.get(
                "primary_manual_recovery_required"
            ),
            PRIMARY_EXCEPTION_PRESENT=outcome.payload.get(
                "primary_exception_present",
                False,
            ),
            PRIMARY_EXCEPTION_PRESERVED=outcome.payload.get(
                "primary_exception_preserved",
                False,
            ),
            CLEANUP_EXCEPTION_RECORDED=outcome.payload.get(
                "cleanup_exception_recorded",
                False,
            ),
            CLEANUP_EXCEPTION_COUNT=outcome.payload.get(
                "cleanup_exception_count",
                0,
            ),
            CLEANUP_FAILURES=outcome.payload.get(
                "cleanup_failures",
                [],
            ),
            CLEANUP_RESOURCE_KINDS=outcome.payload.get(
                "cleanup_resource_kinds", []
            ),
            CLEANUP_FAILURE_CODES=outcome.payload.get(
                "cleanup_failure_codes",
                [],
            ),
            DURABLE_EVIDENCE_UPDATED=True,
        )
        outcome.payload["durable_evidence_updated"] = True
        outcome.payload["durable_evidence_persisted"] = True
    except Exception:
        _apply_cleanup_failure(
            outcome,
            ("EXECUTION_EVIDENCE_FINALIZATION_FAILED",),
            evidence=None,
            publish_state=publish_state,
        )


def _quiescence_error(exc: OrchestratorError) -> str:
    if exc.code in {
        "SOURCE_NOT_QUIESCENT",
        "SOURCE_SQLITE_LEASE_FAILED",
        "SOURCE_SQLITE_SIDECAR_PRESENT",
    }:
        return "QUIESCENCE_FAILED"
    return exc.code


def _pinned_authorities_match(
    pinned: PinnedPlan,
    validated: ValidatedExecution,
) -> bool:
    return (
        pinned.path_matches()
        and validated.deployment_contract.path_matches()
        and validated.operations_root_approval.path_matches()
        and validated.clean_start_policy.path_matches()
        and validated.execution_authority.path_matches()
        and validated.backup_parent.path_matches()
        and validated.staging_parent.path_matches()
        and validated.evidence_parent.path_matches()
    )


def _execute_plan_outcome(
    args: argparse.Namespace,
) -> _ExecutionOutcome:
    pinned: PinnedPlan | None = None
    validated: ValidatedExecution | None = None
    prepared: Any = None
    evidence: ExecutionEvidence | None = None
    operation_id: str | None = None
    publish_state = "BEFORE_EXCHANGE"
    outcome: _ExecutionOutcome | None = None
    try:
        root_identity = _root_identity()
        pinned = _open_plan(args.plan, args.expected_plan_sha256)
        validated = _revalidate_plan(args, pinned, root_identity)
        plan = pinned.payload
        operation_id = str(plan["OPERATION_ID"])
        if not _pinned_authorities_match(pinned, validated):
            raise ProductionGateError("PINNED_AUTHORITY_DRIFT")
        try:
            runtime_matches = validated.execution_authority.runtime_matches()
        except ExecutionAuthorityError as exc:
            raise ProductionGateError(exc.code) from exc
        if not runtime_matches:
            raise ProductionGateError("CURRENT_RUNTIME_IMAGE_DRIFT")
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
        except _StagedCleanupTransport as transport:
            result = _merge_cleanup_transport(transport)
            primary_error = str(
                result.get("primary_exit_classification")
                or result.get("exit_classification")
                or "STAGED_PREPARATION_FAILED"
            )
            outcome = _failure_outcome(
                operation_id,
                result,
                durable_evidence_updated=False,
                primary_error_type=primary_error,
            )
            return outcome
        except OrchestratorError as exc:
            code = _quiescence_error(exc)
            outcome = _failure_outcome(
                operation_id,
                {
                    "error_type": code,
                    "exit_classification": code,
                    "publish_state": "BEFORE_EXCHANGE",
                    "target_may_have_changed": False,
                    "manual_recovery_required": False,
                    "backup_available": False,
                },
                durable_evidence_updated=False,
                primary_error_type=code,
            )
            return outcome
        if not _pinned_authorities_match(pinned, validated):
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
                "OPERATIONS_ROOT_APPROVAL_SHA256": plan[
                    "OPERATIONS_ROOT_APPROVAL_SHA256"
                ],
                "CLEAN_START_POLICY_SHA256": plan["CLEAN_START_POLICY_SHA256"],
                "FINAL_AUTHORITY_SHA256": (
                    validated.execution_authority.final_authority.sha256
                ),
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
        publish_state = "EXCHANGE_STARTED"
        with redirect_stdout(captured):
            return_code = _execute_authorized_staged(
                validated.staged_args,
                prepared=prepared,
            )
        prepared = None
        result = _parse_staged_output(captured.getvalue())
        publish_state = str(
            result.get("publish_state", "EXCHANGE_STARTED")
        )
        if return_code != 0 or result.get("status") != "PASS":
            primary_present = bool(
                result.get("primary_exception_present", True)
            )
            primary_error = (
                str(
                    result.get("primary_exit_classification")
                    or result.get("failure_reason")
                    or result.get("exit_classification")
                    or result.get("error_type")
                    or "STAGED_EXECUTION_FAILED"
                )
                if primary_present
                else None
            )
            try:
                _record_failure(evidence, result)
            except Exception:
                result = dict(result)
                result.update(
                    {
                        "status": "FAILED",
                        "error_type": "PUBLISH_UNCERTAIN",
                        "exit_classification": "PUBLISH_UNCERTAIN",
                        "publish_state": publish_state,
                        "target_may_have_changed": True,
                        "automatic_retry_allowed": False,
                        "manual_recovery_required": True,
                    }
                )
                outcome = _failure_outcome(
                    operation_id,
                    result,
                    durable_evidence_updated=False,
                    primary_error_type=primary_error,
                    stream=sys.stderr,
                )
                _apply_cleanup_failure(
                    outcome,
                    ("EXECUTION_EVIDENCE_UPDATE_FAILED",),
                    evidence=None,
                    publish_state=publish_state,
                )
                return outcome
            outcome = _failure_outcome(
                operation_id,
                result,
                durable_evidence_updated=True,
                primary_error_type=primary_error,
            )
            return outcome

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
        if not _pinned_authorities_match(pinned, validated):
            raise ProductionGateError(
                "PINNED_AUTHORITY_DRIFT_AFTER_EXCHANGE"
            )
        publish_state = "FINAL_VERIFIED"
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
        outcome = _ExecutionOutcome(
            exit_code=0,
            payload={
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
                "target_may_have_changed": True,
                "automatic_retry_allowed": False,
                "manual_recovery_required": False,
                "production_execution_enabled": True,
                "durable_evidence_updated": True,
                "durable_evidence_persisted": True,
                "primary_exception_present": False,
                "primary_exception_preserved": False,
                "cleanup_exception_recorded": False,
                "cleanup_exception_count": 0,
                "cleanup_failures": [],
                "cleanup_failure_codes": [],
            },
            stream=sys.stdout,
        )
        return outcome
    except Exception as exc:
        if isinstance(exc, _PrimaryAndCleanupFailure):
            primary: Exception | None = exc.primary
            embedded_cleanup = exc.cleanup_codes
        elif isinstance(exc, _CleanupFailure):
            primary = None
            embedded_cleanup = exc.codes
        else:
            primary = exc
            embedded_cleanup = ()
        primary_code = (
            _safe_exception_code(primary)
            if primary is not None
            else "CLEANUP_FAILED"
        )
        if _publish_state_requires_uncertainty(
            publish_state,
            publish_state != "BEFORE_EXCHANGE",
        ):
            outcome = _post_exchange_uncertain_outcome(
                evidence,
                operation_id,
                primary_code if primary is not None else None,
                publish_state=publish_state,
            )
        else:
            outcome = _failure_outcome(
                operation_id,
                {
                    "error_type": primary_code,
                    "exit_classification": primary_code,
                    "publish_state": "BEFORE_EXCHANGE",
                    "target_may_have_changed": False,
                    "manual_recovery_required": False,
                    "backup_available": False,
                },
                durable_evidence_updated=False,
                primary_error_type=(
                    primary_code if primary is not None else None
                ),
            )
        if embedded_cleanup:
            _apply_cleanup_failure(
                outcome,
                embedded_cleanup,
                evidence=evidence,
                publish_state=publish_state,
            )
        return outcome
    finally:
        cleanup_codes: list[str] = []
        if prepared is not None:
            try:
                prepared.close()
            except Exception as exc:
                cleanup_codes.extend(
                    _cleanup_codes(exc, "PREPARED_EXECUTION_CLOSE_FAILED")
                )
        if validated is not None:
            try:
                validated.close()
            except Exception as exc:
                cleanup_codes.extend(
                    _cleanup_codes(exc, "VALIDATED_EXECUTION_CLOSE_FAILED")
                )
        if outcome is not None and cleanup_codes:
            _apply_cleanup_failure(
                outcome,
                cleanup_codes,
                evidence=evidence,
                publish_state=publish_state,
            )
        if outcome is not None:
            _finalize_execution_evidence(
                outcome,
                evidence,
                publish_state=publish_state,
            )
        if pinned is not None:
            try:
                pinned.close()
            except Exception as exc:
                if outcome is not None:
                    _apply_cleanup_failure(
                        outcome,
                        _cleanup_codes(
                            exc,
                            "PINNED_PLAN_CLOSE_FAILED",
                        ),
                        evidence=evidence,
                        publish_state=publish_state,
                    )


def _sanitized_stderr_fallback(
    outcome: _ExecutionOutcome,
) -> None:
    cleanup_failures = _sanitized_cleanup_failures(
        outcome.payload.get("cleanup_failures")
    )
    cleanup_failures.append(
        {
            "resource_kind": "FINAL_RESULT_STREAM",
            "cleanup_phase": "FINAL_RESULT_EMIT",
            "error_type": "OutputError",
            "error_code": "FINAL_RESULT_EMIT_FAILED",
        }
    )
    cleanup_codes = [
        failure["error_code"] for failure in cleanup_failures
    ]
    cleanup_resource_kinds = [
        failure["resource_kind"] for failure in cleanup_failures
    ]
    payload = {
        "status": "FAILED",
        "error_type": "PUBLISH_UNCERTAIN",
        "exit_classification": "PUBLISH_UNCERTAIN",
        "publish_state": str(
            outcome.payload.get("publish_state", "EXECUTION_STATE_UNKNOWN")
        ),
        "target_may_have_changed": True,
        "automatic_retry_allowed": False,
        "manual_recovery_required": True,
        "durable_evidence_updated": False,
        "durable_evidence_persisted": False,
        "primary_exception_present": outcome.exit_code != 0,
        "primary_exception_preserved": outcome.exit_code != 0,
        "cleanup_exception_recorded": True,
        "cleanup_exception_count": _cleanup_count(
            outcome.payload.get("cleanup_exception_count"),
            len(cleanup_failures) - 1,
        ) + 1,
        "cleanup_failures": cleanup_failures,
        "cleanup_resource_kinds": cleanup_resource_kinds,
        "cleanup_failure_codes": cleanup_codes,
    }
    operation_id = outcome.payload.get("operation_id")
    if isinstance(operation_id, str):
        payload["operation_id"] = operation_id
    print(
        json.dumps(payload, ensure_ascii=True, sort_keys=True),
        file=sys.stderr,
    )


def execute_plan(args: argparse.Namespace) -> int:
    outcome = _execute_plan_outcome(args)
    try:
        _json_emit(outcome.payload, stream=outcome.stream)
    except Exception:
        _sanitized_stderr_fallback(outcome)
        return 1
    return outcome.exit_code


class _StructuredArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise _StructuredArgumentError()


def build_parser() -> argparse.ArgumentParser:
    parser = _StructuredArgumentParser(
        description="Explicit root-only production staged migration gate"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--repository-root", required=True)
    plan_parser.add_argument("--db-path", required=True)
    plan_parser.add_argument("--backup-parent", required=True)
    plan_parser.add_argument("--staging-parent", required=True)
    plan_parser.add_argument("--evidence-parent", required=True)
    plan_parser.add_argument("--operations-root-approval", required=True)
    plan_parser.add_argument(
        "--expected-operations-root-approval-sha256",
        required=True,
    )
    plan_parser.add_argument("--clean-start-policy", required=True)
    plan_parser.add_argument(
        "--expected-clean-start-policy-sha256",
        required=True,
    )
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
    execute_parser.add_argument(
        "--confirm-operations-root-approval-sha256",
        required=True,
    )
    execute_parser.add_argument(
        "--confirm-clean-start-policy-sha256",
        required=True,
    )
    execute_parser.add_argument("--final-authority", required=True)
    execute_parser.add_argument("--expected-final-authority-sha256", required=True)
    return parser


def _argument_error_payload() -> dict[str, Any]:
    return {
        "status": "FAILED",
        "error_type": "ARGUMENT_ERROR",
        "exit_classification": "ARGUMENT_ERROR",
        "publish_state": "BEFORE_EXCHANGE",
        "target_may_have_changed": False,
        "database_mutated": False,
        "automatic_retry_allowed": False,
        "manual_recovery_required": False,
        "durable_evidence_updated": False,
        "durable_evidence_persisted": False,
        "production_execution_enabled": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except _StructuredArgumentError:
        _json_emit(_argument_error_payload(), stream=sys.stderr)
        return 2
    try:
        if args.command == "plan":
            return create_plan(args)
        if args.command == "execute":
            return execute_plan(args)
        raise ProductionGateError("EXPLICIT_SUBCOMMAND_REQUIRED")
    except (ProductionGateError, OrchestratorError, OSError) as exc:
        code = _safe_exception_code(exc)
        if args.command == "execute":
            _json_emit(
                {
                    "status": "FAILED",
                    "error_type": "PUBLISH_UNCERTAIN",
                    "exit_classification": "PUBLISH_UNCERTAIN",
                    "primary_error_type": code,
                    "publish_state": "EXECUTION_STATE_UNKNOWN",
                    "target_may_have_changed": True,
                    "automatic_retry_allowed": False,
                    "manual_recovery_required": True,
                    "durable_evidence_updated": False,
                    "durable_evidence_persisted": False,
                    "production_execution_enabled": False,
                },
                stream=sys.stderr,
            )
            return 1
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
