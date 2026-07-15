#!/usr/bin/env python3
"""Public migration-only CLI for HealBite household, weekly, and shopping schemas."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_shopping import HealBiteShoppingStore
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore


class ExitClassification(str, Enum):
    SUCCESS = "SUCCESS"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UNSAFE_PATH = "UNSAFE_PATH"
    MISSING_DATABASE = "MISSING_DATABASE"
    INCOMPATIBLE_SCHEMA = "INCOMPATIBLE_SCHEMA"
    DATABASE_LOCKED = "DATABASE_LOCKED"
    DATABASE_READ_ONLY = "DATABASE_READ_ONLY"
    DATABASE_PERMISSION_DENIED = "DATABASE_PERMISSION_DENIED"
    MIGRATION_FAILED = "MIGRATION_FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CONTRACT_DRIFT = "CONTRACT_DRIFT"


class MigrationCommitState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    ROLLED_BACK = "ROLLED_BACK"
    COMMITTED = "COMMITTED"
    UNKNOWN = "UNKNOWN"


class PathMode(str, Enum):
    PROTECTED_EXISTING = "PROTECTED_EXISTING"
    SYNTHETIC_CREATE = "SYNTHETIC_CREATE"
    STAGED_COPY = "STAGED_COPY"


EXIT_SUCCESS = 0
EXIT_INVALID_ARGUMENTS = 2
EXIT_UNSAFE_PATH = 3
EXIT_MISSING_DATABASE = 4
EXIT_SCHEMA_PRECONDITION = 5
EXIT_DATABASE_LOCKED = 6
EXIT_DATABASE_READ_ONLY = 7
EXIT_DATABASE_PERMISSION_DENIED = 8
EXIT_MIGRATION_FAILURE = 9
EXIT_CLEANUP_FAILURE = 10
EXIT_CONTRACT_DRIFT = 11

EXIT_CODES: dict[ExitClassification, int] = {
    ExitClassification.SUCCESS: EXIT_SUCCESS,
    ExitClassification.INVALID_ARGUMENT: EXIT_INVALID_ARGUMENTS,
    ExitClassification.UNSAFE_PATH: EXIT_UNSAFE_PATH,
    ExitClassification.MISSING_DATABASE: EXIT_MISSING_DATABASE,
    ExitClassification.INCOMPATIBLE_SCHEMA: EXIT_SCHEMA_PRECONDITION,
    ExitClassification.DATABASE_LOCKED: EXIT_DATABASE_LOCKED,
    ExitClassification.DATABASE_READ_ONLY: EXIT_DATABASE_READ_ONLY,
    ExitClassification.DATABASE_PERMISSION_DENIED: EXIT_DATABASE_PERMISSION_DENIED,
    ExitClassification.MIGRATION_FAILED: EXIT_MIGRATION_FAILURE,
    ExitClassification.CLEANUP_FAILED: EXIT_CLEANUP_FAILURE,
    ExitClassification.CONTRACT_DRIFT: EXIT_CONTRACT_DRIFT,
}

EXIT_CLASSIFICATION_PRECEDENCE: tuple[ExitClassification, ...] = (
    ExitClassification.INVALID_ARGUMENT,
    ExitClassification.UNSAFE_PATH,
    ExitClassification.MISSING_DATABASE,
    ExitClassification.DATABASE_PERMISSION_DENIED,
    ExitClassification.DATABASE_READ_ONLY,
    ExitClassification.DATABASE_LOCKED,
    ExitClassification.INCOMPATIBLE_SCHEMA,
    ExitClassification.MIGRATION_FAILED,
    ExitClassification.CLEANUP_FAILED,
    ExitClassification.CONTRACT_DRIFT,
)
_EXIT_PRECEDENCE_RANK = {
    classification: rank for rank, classification in enumerate(EXIT_CLASSIFICATION_PRECEDENCE)
}

ALL_COMPONENTS = ("household", "weekly", "shopping")


class SchemaClassification(str, Enum):
    ABSENT = "ABSENT"
    KNOWN_COMPATIBLE_PARTIAL = "KNOWN_COMPATIBLE_PARTIAL"
    CURRENT = "CURRENT"
    INCOMPATIBLE = "INCOMPATIBLE"


class MigrationError(RuntimeError):
    def __init__(
        self,
        classification: ExitClassification,
        *,
        detail_code: str | None = None,
        phase: str | None = None,
    ) -> None:
        super().__init__(detail_code or classification.value)
        self.classification = classification
        self.detail_code = detail_code or classification.value
        self.exit_code = EXIT_CODES[classification]
        self.phase = phase


@dataclass(frozen=True)
class ProcessIdentity:
    uid: int
    gid: int
    groups: frozenset[int]


@dataclass(frozen=True)
class DatabaseTarget:
    path: Path
    classification: str
    mode_before: str | None
    owner_before: str | None
    identity_before: tuple[int, int] | None
    path_mode: str = PathMode.PROTECTED_EXISTING.value
    parent_identity_before: tuple[int, int] | None = None
    parent_owner_before: tuple[int, int] | None = None
    parent_mode_before: int | None = None
    synthetic_parent: Path | None = None
    synthetic_parent_identity: tuple[int, int] | None = None
    synthetic_parent_mode_before: int | None = None
    synthetic_parent_fd: int | None = None


@dataclass(frozen=True)
class PhaseResult:
    name: str
    status: str
    schema_state: str | None = None
    changed: bool = False
    error_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "status": self.status, "changed": self.changed}
        if self.schema_state is not None:
            payload["schema_state"] = self.schema_state
        if self.error_type is not None:
            payload["error_type"] = self.error_type
        return payload


@dataclass(frozen=True)
class MigrationResult:
    status: str
    exit_code: int
    database_path_classification: str
    phases: tuple[PhaseResult, ...]
    schema_changed: bool
    migration_commit_state: str = MigrationCommitState.NOT_STARTED.value
    schema_may_have_changed: bool = False
    cleanup_failed: bool = False
    safe_to_rerun: bool = True
    data_backfilled: bool = False
    exit_classification: str = ExitClassification.SUCCESS.value
    error_type: str | None = None
    mode_before: str | None = None
    mode_after: str | None = None
    owner_before: str | None = None
    owner_after: str | None = None
    path_mode: str = PathMode.PROTECTED_EXISTING.value

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "database_path_classification": self.database_path_classification,
            "phases": [phase.as_dict() for phase in self.phases],
            "schema_changed": self.schema_changed,
            "migration_commit_state": self.migration_commit_state,
            "schema_may_have_changed": self.schema_may_have_changed,
            "cleanup_failed": self.cleanup_failed,
            "safe_to_rerun": self.safe_to_rerun,
            "data_backfilled": self.data_backfilled,
            "exit_classification": self.exit_classification,
            "mode_before": self.mode_before,
            "mode_after": self.mode_after,
            "owner_before": self.owner_before,
            "owner_after": self.owner_after,
            "path_mode": self.path_mode,
        }
        if self.error_type is not None:
            payload["error_type"] = self.error_type
        return payload


def _file_mode(metadata: os.stat_result) -> str:
    return f"{stat.S_IMODE(metadata.st_mode):04o}"


def _file_owner(metadata: os.stat_result) -> str:
    return f"{metadata.st_uid}:{metadata.st_gid}"


def _current_identity() -> ProcessIdentity:
    geteuid = getattr(os, "geteuid", None)
    getegid = getattr(os, "getegid", None)
    getgroups = getattr(os, "getgroups", None)
    if not callable(geteuid) or not callable(getegid) or not callable(getgroups):
        raise MigrationError(ExitClassification.CONTRACT_DRIFT, detail_code="POSIX_IDENTITY_UNAVAILABLE")
    return ProcessIdentity(uid=int(geteuid()), gid=int(getegid()), groups=frozenset(int(group) for group in getgroups()))


def _identity_has_mode(
    metadata: os.stat_result,
    identity: ProcessIdentity,
    owner_bit: int,
    group_bit: int,
    other_bit: int,
) -> bool:
    if identity.uid == 0:
        return True
    if identity.uid == metadata.st_uid:
        return bool(metadata.st_mode & owner_bit)
    if metadata.st_gid == identity.gid or metadata.st_gid in identity.groups:
        return bool(metadata.st_mode & group_bit)
    return bool(metadata.st_mode & other_bit)


def _identity_can_write(metadata: os.stat_result, identity: ProcessIdentity) -> bool:
    return _identity_has_mode(metadata, identity, stat.S_IWUSR, stat.S_IWGRP, stat.S_IWOTH)


def _path_components(path: Path) -> tuple[Path, ...]:
    current = Path(path.anchor)
    components = [current]
    for part in path.parts[1:]:
        current /= part
        components.append(current)
    return tuple(components)


def _validate_directory_chain(parent: Path, identity: ProcessIdentity) -> None:
    for component in _path_components(parent):
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="PATH_COMPONENT_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="PATH_COMPONENT_SYMLINK")
        if not stat.S_ISDIR(metadata.st_mode):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="PATH_COMPONENT_NOT_DIRECTORY")
        if _identity_can_write(metadata, identity):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="PATH_DIRECTORY_WRITABLE")


def _validate_no_symlink_directory_chain(parent: Path) -> None:
    for component in _path_components(parent):
        try:
            metadata = component.lstat()
        except OSError as exc:
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="PATH_COMPONENT_UNAVAILABLE") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="PATH_COMPONENT_SYMLINK")
        if not stat.S_ISDIR(metadata.st_mode):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="PATH_COMPONENT_NOT_DIRECTORY")


def _classify_existing_path(path: Path, identity: ProcessIdentity) -> DatabaseTarget:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise MigrationError(ExitClassification.MISSING_DATABASE, detail_code="DB_PATH_MISSING") from exc
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_UNAVAILABLE") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_SYMLINK")
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_NOT_REGULAR")
    _validate_directory_chain(path.parent, identity)
    if not _identity_can_write(metadata, identity):
        raise MigrationError(ExitClassification.DATABASE_PERMISSION_DENIED, detail_code="DB_PATH_NOT_WRITABLE")
    return DatabaseTarget(
        path=path,
        classification="absolute_existing_regular_safe_parent",
        mode_before=_file_mode(metadata),
        owner_before=_file_owner(metadata),
        identity_before=(metadata.st_dev, metadata.st_ino),
        path_mode=PathMode.PROTECTED_EXISTING.value,
    )


def _classify_staged_copy(path: Path, identity: ProcessIdentity) -> DatabaseTarget:
    try:
        metadata = path.lstat()
        parent_metadata = path.parent.lstat()
    except FileNotFoundError as exc:
        raise MigrationError(ExitClassification.MISSING_DATABASE, detail_code="DB_PATH_MISSING") from exc
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_UNAVAILABLE") from exc
    _validate_no_symlink_directory_chain(path.parent)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_DB_NOT_REGULAR")
    if metadata.st_nlink != 1:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_DB_LINK_COUNT_INVALID")
    if metadata.st_uid != identity.uid or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_DB_METADATA_INVALID")
    if parent_metadata.st_uid != identity.uid or stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_PARENT_NOT_PRIVATE")
    if not _identity_can_write(metadata, identity) or not _identity_can_write(parent_metadata, identity):
        raise MigrationError(ExitClassification.DATABASE_PERMISSION_DENIED, detail_code="STAGED_PATH_NOT_WRITABLE")

    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    target_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        target_flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(path.parent, parent_flags)
        try:
            target_fd = os.open(path.name, target_flags, dir_fd=parent_fd)
            try:
                opened_parent = os.fstat(parent_fd)
                opened_target = os.fstat(target_fd)
            finally:
                os.close(target_fd)
        finally:
            os.close(parent_fd)
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_PATH_OPEN_FAILED") from exc
    if (
        (opened_parent.st_dev, opened_parent.st_ino) != (parent_metadata.st_dev, parent_metadata.st_ino)
        or (opened_target.st_dev, opened_target.st_ino) != (metadata.st_dev, metadata.st_ino)
        or opened_target.st_nlink != 1
    ):
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_PATH_REPLACED")
    return DatabaseTarget(
        path=path,
        classification="absolute_existing_staged_copy_private_parent",
        mode_before=_file_mode(metadata),
        owner_before=_file_owner(metadata),
        identity_before=(metadata.st_dev, metadata.st_ino),
        path_mode=PathMode.STAGED_COPY.value,
        parent_identity_before=(parent_metadata.st_dev, parent_metadata.st_ino),
        parent_owner_before=(parent_metadata.st_uid, parent_metadata.st_gid),
        parent_mode_before=stat.S_IMODE(parent_metadata.st_mode),
    )


def _resolve_synthetic_target(path: Path, identity: ProcessIdentity) -> DatabaseTarget:
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_UNAVAILABLE") from exc
    else:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_TARGET_EXISTS")

    _validate_no_symlink_directory_chain(path.parent)
    try:
        parent_metadata = path.parent.lstat()
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_UNAVAILABLE") from exc
    if parent_metadata.st_uid != identity.uid:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_OWNER_MISMATCH")
    if stat.S_IMODE(parent_metadata.st_mode) != 0o700:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_NOT_PRIVATE")

    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(path.parent, parent_flags)
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_OPEN_FAILED") from exc
    opened_parent = os.fstat(parent_fd)
    if (
        (opened_parent.st_dev, opened_parent.st_ino) != (parent_metadata.st_dev, parent_metadata.st_ino)
        or opened_parent.st_uid != identity.uid
        or stat.S_IMODE(opened_parent.st_mode) != 0o700
    ):
        os.close(parent_fd)
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_REPLACED")

    old_umask = os.umask(0o077)
    fd: int | None = None
    created = False
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path.name, flags, 0o600, dir_fd=parent_fd)
        created = True
    except FileExistsError as exc:
        os.close(parent_fd)
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_CREATE_COLLISION") from exc
    except OSError as exc:
        os.close(parent_fd)
        raise MigrationError(
            ExitClassification.DATABASE_PERMISSION_DENIED,
            detail_code="SYNTHETIC_CREATE_FAILED",
        ) from exc
    finally:
        os.umask(old_umask)

    try:
        if fd is None:
            raise MigrationError(ExitClassification.CONTRACT_DRIFT, detail_code="SYNTHETIC_TARGET_FD_MISSING")
        opened_target = os.fstat(fd)
        if not stat.S_ISREG(opened_target.st_mode):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_TARGET_NOT_REGULAR")
        if opened_target.st_uid != identity.uid or stat.S_IMODE(opened_target.st_mode) != 0o600:
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_TARGET_METADATA_MISMATCH")
        os.fchmod(parent_fd, 0o500)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_TARGET_NOT_REGULAR")
        if (
            (metadata.st_dev, metadata.st_ino) != (opened_target.st_dev, opened_target.st_ino)
            or metadata.st_uid != identity.uid
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_TARGET_METADATA_MISMATCH")
        protected_parent = os.fstat(parent_fd)
        if (
            (protected_parent.st_dev, protected_parent.st_ino)
            != (parent_metadata.st_dev, parent_metadata.st_ino)
            or stat.S_IMODE(protected_parent.st_mode) != 0o500
        ):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_NOT_PROTECTED")
    except Exception:
        try:
            os.fchmod(parent_fd, 0o700)
            if created:
                os.unlink(path.name, dir_fd=parent_fd)
        except OSError:
            pass
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)
        raise
    os.close(fd)

    return DatabaseTarget(
        path=path,
        classification="absolute_synthetic_created_private_parent",
        mode_before=_file_mode(metadata),
        owner_before=_file_owner(metadata),
        identity_before=(metadata.st_dev, metadata.st_ino),
        path_mode=PathMode.SYNTHETIC_CREATE.value,
        synthetic_parent=path.parent,
        synthetic_parent_identity=(parent_metadata.st_dev, parent_metadata.st_ino),
        synthetic_parent_mode_before=0o700,
        synthetic_parent_fd=parent_fd,
    )


def _resolve_db_path(
    raw_path: str | None,
    *,
    synthetic_create: bool,
    staged_copy: bool = False,
    identity: ProcessIdentity | None = None,
) -> DatabaseTarget:
    if not raw_path:
        raise MigrationError(ExitClassification.INVALID_ARGUMENT, detail_code="DB_PATH_REQUIRED")
    path = Path(raw_path)
    if not path.is_absolute():
        raise MigrationError(ExitClassification.INVALID_ARGUMENT, detail_code="DB_PATH_NOT_ABSOLUTE")
    effective_identity = identity or _current_identity()
    if synthetic_create and staged_copy:
        raise MigrationError(ExitClassification.INVALID_ARGUMENT, detail_code="PATH_MODES_MUTUALLY_EXCLUSIVE")
    if synthetic_create:
        return _resolve_synthetic_target(path, effective_identity)
    if staged_copy:
        return _classify_staged_copy(path, effective_identity)
    return _classify_existing_path(path, effective_identity)


def _verify_synthetic_parent_protected(target: DatabaseTarget) -> None:
    if target.synthetic_parent is None:
        return
    if target.synthetic_parent_fd is None:
        raise MigrationError(ExitClassification.CONTRACT_DRIFT, detail_code="SYNTHETIC_PARENT_FD_MISSING")
    try:
        opened_metadata = os.fstat(target.synthetic_parent_fd)
        metadata = target.synthetic_parent.lstat()
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_LOST") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_REPLACED")
    if (metadata.st_dev, metadata.st_ino) != target.synthetic_parent_identity:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_REPLACED")
    if (opened_metadata.st_dev, opened_metadata.st_ino) != target.synthetic_parent_identity:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_REPLACED")
    if stat.S_IMODE(metadata.st_mode) != 0o500 or stat.S_IMODE(opened_metadata.st_mode) != 0o500:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="SYNTHETIC_PARENT_NOT_PROTECTED")


def _restore_synthetic_parent(target: DatabaseTarget) -> None:
    if target.synthetic_parent is None:
        return
    if target.synthetic_parent_fd is None:
        raise MigrationError(ExitClassification.CONTRACT_DRIFT, detail_code="SYNTHETIC_PARENT_FD_MISSING")
    verification_error: Exception | None = None
    try:
        _verify_synthetic_parent_protected(target)
    except Exception as exc:
        verification_error = exc
    try:
        os.fchmod(target.synthetic_parent_fd, target.synthetic_parent_mode_before or 0o700)
    finally:
        os.close(target.synthetic_parent_fd)
    if verification_error is not None:
        raise verification_error


def _verify_identity_unchanged(target: DatabaseTarget) -> tuple[str | None, str | None]:
    if target.identity_before is None:
        return None, None
    try:
        metadata = target.path.lstat()
    except OSError as exc:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_LOST") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_REPLACED")
    if (metadata.st_dev, metadata.st_ino) != target.identity_before:
        raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="DB_PATH_REPLACED")
    if target.path_mode == PathMode.STAGED_COPY.value:
        try:
            parent_metadata = target.path.parent.lstat()
        except OSError as exc:
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_PARENT_LOST") from exc
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or (parent_metadata.st_dev, parent_metadata.st_ino) != target.parent_identity_before
            or (parent_metadata.st_uid, parent_metadata.st_gid) != target.parent_owner_before
            or stat.S_IMODE(parent_metadata.st_mode) != target.parent_mode_before
        ):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_PARENT_REPLACED")
        if (
            metadata.st_nlink != 1
            or _file_mode(metadata) != "0600"
            or (metadata.st_uid, metadata.st_gid) != target.parent_owner_before
        ):
            raise MigrationError(ExitClassification.UNSAFE_PATH, detail_code="STAGED_DB_METADATA_CHANGED")
    return _file_mode(metadata), _file_owner(metadata)


def _normalize_schema_sql(value: str) -> str:
    normalized = re.sub(r"\bif\s+not\s+exists\b", "", value, flags=re.IGNORECASE)
    return " ".join(normalized.lower().split())


_CREATE_OBJECT_RE = re.compile(
    r"^create\s+(?:unique\s+)?(?P<type>table|index)\s+(?:if\s+not\s+exists\s+)?(?P<name>[a-zA-Z0-9_]+)",
    re.IGNORECASE,
)


def _expected_schema_objects(statements: Sequence[str]) -> dict[str, tuple[str, str]]:
    objects: dict[str, tuple[str, str]] = {}
    for statement in statements:
        match = _CREATE_OBJECT_RE.match(statement.strip())
        if match is None:
            raise MigrationError(ExitClassification.CONTRACT_DRIFT, detail_code="UNSUPPORTED_SCHEMA_STATEMENT")
        name = match.group("name")
        objects[name] = (match.group("type").lower(), _normalize_schema_sql(statement))
    return objects


_LEGACY_PARTIAL_OMISSIONS: dict[str, frozenset[str]] = {
    "weekly": frozenset(
        {
            "household_weekly_menu_entry_ingredients",
            "idx_weekly_menu_ingredients_entry_position_unique",
        }
    ),
    "shopping": frozenset(
        {
            "household_shopping_item_contributions",
            "idx_household_shopping_contributions_item_source_unique",
        }
    ),
}


def _is_known_compatible_partial(
    component: str,
    actual_names: set[str],
    expected: dict[str, tuple[str, str]],
) -> bool:
    expected_names = tuple(expected)
    if any(actual_names == set(expected_names[:length]) for length in range(1, len(expected_names))):
        return True
    expected_tables = {name for name, (object_type, _sql) in expected.items() if object_type == "table"}
    if expected_tables.issubset(actual_names):
        return True
    omitted = _LEGACY_PARTIAL_OMISSIONS.get(component)
    return omitted is not None and actual_names == set(expected_names) - omitted


def _classify_component_schema(
    conn: sqlite3.Connection,
    component: str,
    statements: Sequence[str],
) -> SchemaClassification:
    expected = _expected_schema_objects(statements)
    placeholders = ",".join("?" for _ in expected)
    rows = conn.execute(
        f"SELECT type, name, sql FROM sqlite_master WHERE name IN ({placeholders})",
        tuple(expected),
    ).fetchall()
    if not rows:
        return SchemaClassification.ABSENT
    for row in rows:
        name = str(row[1])
        expected_type, expected_sql = expected[name]
        actual_sql = "" if row[2] is None else _normalize_schema_sql(str(row[2]))
        if str(row[0]).lower() != expected_type or actual_sql != expected_sql:
            return SchemaClassification.INCOMPATIBLE
    actual_names = {str(row[1]) for row in rows}
    if len(actual_names) == len(expected):
        return SchemaClassification.CURRENT
    if _is_known_compatible_partial(component, actual_names, expected):
        return SchemaClassification.KNOWN_COMPATIBLE_PARTIAL
    return SchemaClassification.INCOMPATIBLE


def _component_statements() -> dict[str, tuple[str, ...]]:
    return {
        "household": HealBiteHouseholdStore.schema_statements(),
        "weekly": HealBiteWeeklyMenuStore.schema_statements(),
        "shopping": HealBiteShoppingStore.schema_statements(),
    }


def _preflight_all_schemas(conn: sqlite3.Connection) -> dict[str, SchemaClassification]:
    plans = {
        name: _classify_component_schema(conn, name, statements)
        for name, statements in _component_statements().items()
    }
    incompatible = next((name for name, state in plans.items() if state is SchemaClassification.INCOMPATIBLE), None)
    if incompatible is not None:
        raise MigrationError(
            ExitClassification.INCOMPATIBLE_SCHEMA,
            detail_code="SCHEMA_OBJECT_INCOMPATIBLE",
            phase=incompatible,
        )
    if plans["weekly"] is not SchemaClassification.ABSENT and plans["household"] is SchemaClassification.ABSENT:
        raise MigrationError(
            ExitClassification.INCOMPATIBLE_SCHEMA,
            detail_code="WEEKLY_DEPENDENCY_MISSING",
            phase="weekly",
        )
    if plans["shopping"] is not SchemaClassification.ABSENT and plans["weekly"] is SchemaClassification.ABSENT:
        raise MigrationError(
            ExitClassification.INCOMPATIBLE_SCHEMA,
            detail_code="SHOPPING_DEPENDENCY_MISSING",
            phase="shopping",
        )
    return plans


def _sqlite_integrity(conn: sqlite3.Connection) -> None:
    value = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    if value.lower() != "ok":
        raise MigrationError(ExitClassification.INCOMPATIBLE_SCHEMA, detail_code="SQLITE_INTEGRITY_CHECK_FAILED")


def _parse_components(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ALL_COMPONENTS
    components = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not components:
        raise MigrationError(ExitClassification.INVALID_ARGUMENT, detail_code="COMPONENTS_EMPTY")
    if any(component not in ALL_COMPONENTS for component in components):
        raise MigrationError(ExitClassification.INVALID_ARGUMENT, detail_code="COMPONENTS_UNKNOWN")
    if len(set(components)) != len(components):
        raise MigrationError(ExitClassification.INVALID_ARGUMENT, detail_code="COMPONENTS_DUPLICATE")
    positions = [ALL_COMPONENTS.index(component) for component in components]
    if positions != sorted(positions):
        raise MigrationError(ExitClassification.INVALID_ARGUMENT, detail_code="COMPONENTS_OUT_OF_ORDER")
    return components


def _attach_cleanup_note(primary_error: Exception, cleanup_name: str) -> None:
    try:
        primary_error.add_note(f"{cleanup_name}_failed")
    except Exception:
        pass


def _attach_migration_state(
    error: Exception,
    *,
    commit_state: MigrationCommitState,
    schema_may_have_changed: bool,
    cleanup_failed: bool,
    safe_to_rerun: bool,
    phases: tuple[PhaseResult, ...] = (),
    schema_changed: bool = False,
) -> None:
    values = {
        "_healbite_commit_state": commit_state.value,
        "_healbite_schema_may_have_changed": schema_may_have_changed,
        "_healbite_cleanup_failed": cleanup_failed,
        "_healbite_safe_to_rerun": safe_to_rerun,
        "_healbite_phases": phases,
        "_healbite_schema_changed": schema_changed,
    }
    for name, value in values.items():
        try:
            setattr(error, name, value)
        except Exception:
            pass


def _migration_state_from_error(
    error: Exception,
) -> tuple[MigrationCommitState, bool, bool, bool, tuple[PhaseResult, ...], bool]:
    raw_state = getattr(error, "_healbite_commit_state", MigrationCommitState.NOT_STARTED.value)
    try:
        commit_state = MigrationCommitState(str(raw_state))
    except ValueError:
        commit_state = MigrationCommitState.UNKNOWN
    phases = getattr(error, "_healbite_phases", ())
    return (
        commit_state,
        bool(getattr(error, "_healbite_schema_may_have_changed", False)),
        bool(getattr(error, "_healbite_cleanup_failed", False)),
        bool(getattr(error, "_healbite_safe_to_rerun", commit_state is not MigrationCommitState.UNKNOWN)),
        phases if isinstance(phases, tuple) else (),
        bool(getattr(error, "_healbite_schema_changed", False)),
    )


def _mark_cleanup_failure(primary_error: Exception, cleanup_name: str) -> None:
    _attach_cleanup_note(primary_error, cleanup_name)
    commit_state, may_have_changed, _cleanup_failed, safe_to_rerun, phases, schema_changed = (
        _migration_state_from_error(primary_error)
    )
    _attach_migration_state(
        primary_error,
        commit_state=commit_state,
        schema_may_have_changed=may_have_changed,
        cleanup_failed=True,
        safe_to_rerun=safe_to_rerun,
        phases=phases,
        schema_changed=schema_changed,
    )


def _rollback_preserving_primary(
    conn: sqlite3.Connection,
    primary_error: Exception,
    *,
    commit_attempted: bool,
) -> None:
    try:
        conn.rollback()
    except Exception:
        _attach_cleanup_note(primary_error, "rollback")
        _attach_migration_state(
            primary_error,
            commit_state=MigrationCommitState.UNKNOWN,
            schema_may_have_changed=True,
            cleanup_failed=True,
            safe_to_rerun=False,
        )
        return
    if commit_attempted:
        _attach_migration_state(
            primary_error,
            commit_state=MigrationCommitState.UNKNOWN,
            schema_may_have_changed=True,
            cleanup_failed=False,
            safe_to_rerun=False,
        )
    else:
        _attach_migration_state(
            primary_error,
            commit_state=MigrationCommitState.ROLLED_BACK,
            schema_may_have_changed=False,
            cleanup_failed=False,
            safe_to_rerun=True,
        )


def _apply_component(conn: sqlite3.Connection, name: str, statements: Sequence[str]) -> None:
    try:
        for statement in statements:
            conn.execute(statement)
    except Exception as exc:
        try:
            setattr(exc, "_healbite_migration_phase", name)
        except Exception:
            pass
        raise


def _migrate_borrowed_connection(
    conn: sqlite3.Connection,
    *,
    selected: tuple[str, ...],
    transaction_hook: Callable[[sqlite3.Connection], None] | None = None,
    before_ddl_hook: Callable[[], None] | None = None,
    component_hook: Callable[[str, sqlite3.Connection], None] | None = None,
) -> tuple[tuple[PhaseResult, ...], bool]:
    commit_attempted = False
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        if transaction_hook is not None:
            transaction_hook(conn)
        _sqlite_integrity(conn)
        plans = _preflight_all_schemas(conn)
        if before_ddl_hook is not None:
            before_ddl_hook()
        phases: list[PhaseResult] = []
        changed_any = False
        statements_by_component = _component_statements()
        for name in selected:
            before = plans[name]
            if before is not SchemaClassification.CURRENT:
                _apply_component(conn, name, statements_by_component[name])
            after = _classify_component_schema(conn, name, statements_by_component[name])
            if after is not SchemaClassification.CURRENT:
                raise MigrationError(
                    ExitClassification.CONTRACT_DRIFT,
                    detail_code="SCHEMA_NOT_CURRENT_AFTER_APPLY",
                    phase=name,
                )
            changed = before is not SchemaClassification.CURRENT
            changed_any = changed_any or changed
            phases.append(PhaseResult(name=name, status="success", schema_state=after.value, changed=changed))
            if component_hook is not None:
                component_hook(name, conn)
        commit_attempted = True
        conn.commit()
        return tuple(phases), changed_any
    except Exception as exc:
        _rollback_preserving_primary(conn, exc, commit_attempted=commit_attempted)
        raise


def _select_exit_classification(
    primary: ExitClassification,
    *additional: ExitClassification,
) -> ExitClassification:
    classifications = (primary, *additional)
    return min(classifications, key=lambda item: _EXIT_PRECEDENCE_RANK.get(item, len(_EXIT_PRECEDENCE_RANK)))


def _sqlite_classification(exc: sqlite3.Error) -> ExitClassification:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        primary_code = error_code & 0xFF
        if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
            return ExitClassification.DATABASE_LOCKED
        if primary_code == sqlite3.SQLITE_READONLY:
            return ExitClassification.DATABASE_READ_ONLY
        if primary_code in {sqlite3.SQLITE_PERM, sqlite3.SQLITE_CANTOPEN, sqlite3.SQLITE_AUTH}:
            return ExitClassification.DATABASE_PERMISSION_DENIED
    return ExitClassification.MIGRATION_FAILED


def _as_migration_error(exc: Exception) -> MigrationError:
    if isinstance(exc, MigrationError):
        return exc
    phase = getattr(exc, "_healbite_migration_phase", None)
    if isinstance(exc, sqlite3.Error):
        return MigrationError(_sqlite_classification(exc), detail_code=type(exc).__name__, phase=phase)
    if isinstance(exc, PermissionError):
        return MigrationError(
            ExitClassification.DATABASE_PERMISSION_DENIED,
            detail_code=type(exc).__name__,
            phase=phase,
        )
    return MigrationError(ExitClassification.MIGRATION_FAILED, detail_code=type(exc).__name__, phase=phase)


def _connect_target(
    target: DatabaseTarget,
    *,
    selected: tuple[str, ...],
    connect_factory: Callable[..., sqlite3.Connection],
    before_open_hook: Callable[[], None] | None,
    transaction_hook: Callable[[sqlite3.Connection], None] | None = None,
    before_ddl_hook: Callable[[], None] | None,
    component_hook: Callable[[str, sqlite3.Connection], None] | None = None,
) -> tuple[tuple[PhaseResult, ...], bool]:
    conn: sqlite3.Connection | None = None
    primary_error: Exception | None = None
    cleanup_error: MigrationError | None = None
    phases: tuple[PhaseResult, ...] = ()
    schema_changed = False
    migration_committed = False
    try:
        if before_open_hook is not None:
            before_open_hook()
        _verify_synthetic_parent_protected(target)
        _verify_identity_unchanged(target)
        conn = connect_factory(target.path, timeout=0.2, check_same_thread=False)
        if target.path_mode == PathMode.STAGED_COPY.value:
            journal_mode = str(conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
            conn.execute("PRAGMA synchronous=FULL")
            synchronous = int(conn.execute("PRAGMA synchronous").fetchone()[0])
            if journal_mode != "delete" or synchronous != 2:
                raise MigrationError(ExitClassification.CONTRACT_DRIFT, detail_code="STAGED_SQLITE_DURABILITY_DRIFT")
        elif target.synthetic_parent is not None:
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA temp_store=MEMORY")
        _verify_synthetic_parent_protected(target)
        _verify_identity_unchanged(target)

        def guarded_before_ddl() -> None:
            if before_ddl_hook is not None:
                before_ddl_hook()
            _verify_synthetic_parent_protected(target)
            _verify_identity_unchanged(target)

        phases, schema_changed = _migrate_borrowed_connection(
            conn,
            selected=selected,
            transaction_hook=transaction_hook,
            before_ddl_hook=guarded_before_ddl,
            component_hook=component_hook,
        )
        migration_committed = True
        return phases, schema_changed
    except Exception as exc:
        primary_error = exc
        raise
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as close_error:
                if primary_error is not None:
                    _mark_cleanup_failure(primary_error, "close")
                else:
                    cleanup_error = MigrationError(
                        ExitClassification.CLEANUP_FAILED,
                        detail_code="CONNECTION_CLOSE_FAILED",
                    )
                    _attach_migration_state(
                        cleanup_error,
                        commit_state=MigrationCommitState.COMMITTED,
                        schema_may_have_changed=True,
                        cleanup_failed=True,
                        safe_to_rerun=True,
                        phases=phases,
                        schema_changed=schema_changed,
                    )
                    cleanup_error.__cause__ = close_error
        try:
            _restore_synthetic_parent(target)
        except Exception as restore_error:
            if primary_error is not None:
                _mark_cleanup_failure(primary_error, "synthetic_parent_restore")
            elif cleanup_error is not None:
                _mark_cleanup_failure(cleanup_error, "synthetic_parent_restore")
            else:
                cleanup_error = MigrationError(
                    ExitClassification.CLEANUP_FAILED,
                    detail_code="SYNTHETIC_PARENT_RESTORE_FAILED",
                )
                _attach_migration_state(
                    cleanup_error,
                    commit_state=(
                        MigrationCommitState.COMMITTED if migration_committed else MigrationCommitState.NOT_STARTED
                    ),
                    schema_may_have_changed=migration_committed,
                    cleanup_failed=True,
                    safe_to_rerun=True,
                    phases=phases,
                    schema_changed=schema_changed,
                )
                cleanup_error.__cause__ = restore_error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


def run_migration(
    *,
    db_path: str | None,
    synthetic_create: bool = False,
    staged_copy: bool = False,
    components: str | None = None,
    _identity: ProcessIdentity | None = None,
    _connect_factory: Callable[..., sqlite3.Connection] = sqlite3.connect,
    _target_resolver: Callable[[str | None, bool, ProcessIdentity | None], DatabaseTarget] | None = None,
    _before_open_hook: Callable[[], None] | None = None,
    _transaction_hook: Callable[[sqlite3.Connection], None] | None = None,
    _before_ddl_hook: Callable[[], None] | None = None,
    _component_hook: Callable[[str, sqlite3.Connection], None] | None = None,
) -> MigrationResult:
    phases: tuple[PhaseResult, ...] = ()
    target: DatabaseTarget | None = None
    commit_completed = False
    try:
        selected = _parse_components(components)
        if _target_resolver is None:
            target = _resolve_db_path(
                db_path,
                synthetic_create=synthetic_create,
                staged_copy=staged_copy,
                identity=_identity,
            )
        else:
            if staged_copy:
                raise MigrationError(
                    ExitClassification.CONTRACT_DRIFT,
                    detail_code="STAGED_COPY_CUSTOM_RESOLVER_FORBIDDEN",
                )
            target = _target_resolver(db_path, synthetic_create, _identity)
        phases, schema_changed = _connect_target(
            target,
            selected=selected,
            connect_factory=_connect_factory,
            before_open_hook=_before_open_hook,
            transaction_hook=_transaction_hook,
            before_ddl_hook=_before_ddl_hook,
            component_hook=_component_hook,
        )
        commit_completed = True
        mode_after, owner_after = _verify_identity_unchanged(target)
        return MigrationResult(
            status="success",
            exit_code=EXIT_SUCCESS,
            database_path_classification=target.classification,
            phases=phases,
            schema_changed=schema_changed,
            migration_commit_state=MigrationCommitState.COMMITTED.value,
            schema_may_have_changed=True,
            cleanup_failed=False,
            safe_to_rerun=True,
            mode_before=target.mode_before,
            mode_after=mode_after,
            owner_before=target.owner_before,
            owner_after=owner_after,
            path_mode=target.path_mode,
        )
    except Exception as exc:
        migration_error = _as_migration_error(exc)
        commit_state, may_have_changed, cleanup_failed, safe_to_rerun, state_phases, state_changed = (
            _migration_state_from_error(exc)
        )
        if commit_completed and commit_state is MigrationCommitState.NOT_STARTED:
            commit_state = MigrationCommitState.COMMITTED
            may_have_changed = True
            safe_to_rerun = True
        if state_phases:
            phases = state_phases
        failed_phases = list(phases)
        if migration_error.phase and not any(phase.name == migration_error.phase for phase in failed_phases):
            failed_phases.append(
                PhaseResult(
                    name=migration_error.phase,
                    status="failed",
                    error_type=migration_error.detail_code,
                )
            )
        mode_after = owner_after = None
        if target is not None:
            try:
                mode_after, owner_after = _verify_identity_unchanged(target)
            except MigrationError:
                pass
        final_classification = migration_error.classification
        if cleanup_failed:
            final_classification = _select_exit_classification(
                final_classification,
                ExitClassification.CLEANUP_FAILED,
            )
        return MigrationResult(
            status="failed",
            exit_code=EXIT_CODES[final_classification],
            database_path_classification=target.classification if target else "invalid",
            phases=tuple(failed_phases),
            schema_changed=state_changed or any(phase.changed for phase in failed_phases),
            migration_commit_state=commit_state.value,
            schema_may_have_changed=may_have_changed,
            cleanup_failed=cleanup_failed,
            safe_to_rerun=safe_to_rerun,
            exit_classification=final_classification.value,
            error_type=migration_error.detail_code,
            mode_before=target.mode_before if target else None,
            mode_after=mode_after,
            owner_before=target.owner_before if target else None,
            owner_after=owner_after,
            path_mode=target.path_mode if target else PathMode.PROTECTED_EXISTING.value,
        )


def _print_text(result: MigrationResult) -> None:
    print(f"status={result.status}")
    print(f"exit_classification={result.exit_classification}")
    if result.error_type:
        print(f"error_type={result.error_type}")
    print(f"database_path_classification={result.database_path_classification}")
    print(f"PATH_MODE={result.path_mode}")
    print(f"schema_changed={str(result.schema_changed).lower()}")
    print(f"data_backfilled={str(result.data_backfilled).lower()}")
    print(f"MIGRATION_COMMIT_STATE={result.migration_commit_state}")
    print(f"SCHEMA_MAY_HAVE_CHANGED={str(result.schema_may_have_changed).lower()}")
    print(f"CLEANUP_FAILED={str(result.cleanup_failed).lower()}")
    print(f"SAFE_TO_RERUN={str(result.safe_to_rerun).lower()}")
    for phase in result.phases:
        prefix = phase.name.upper()
        print(f"{prefix}_SCHEMA_STATUS={phase.schema_state or phase.status}")
        if phase.error_type:
            print(f"{prefix}_ERROR_TYPE={phase.error_type}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HealBite migration-only schema CLI",
        epilog=(
            "Exit precedence (highest first): INVALID_ARGUMENT, UNSAFE_PATH, MISSING_DATABASE, "
            "DATABASE_PERMISSION_DENIED, DATABASE_READ_ONLY, DATABASE_LOCKED, INCOMPATIBLE_SCHEMA, "
            "MIGRATION_FAILED, CLEANUP_FAILED, CONTRACT_DRIFT. Exit codes: SUCCESS=0, "
            "INVALID_ARGUMENT=2, UNSAFE_PATH=3, MISSING_DATABASE=4, INCOMPATIBLE_SCHEMA=5, "
            "DATABASE_LOCKED=6, DATABASE_READ_ONLY=7, DATABASE_PERMISSION_DENIED=8, "
            "MIGRATION_FAILED=9, CLEANUP_FAILED=10, CONTRACT_DRIFT=11."
        ),
    )
    parser.add_argument("--db-path", required=True, help="Absolute SQLite DB path to migrate")
    path_modes = parser.add_mutually_exclusive_group()
    path_modes.add_argument(
        "--synthetic-create",
        action="store_true",
        help="Create a test-only DB in an owned private 0700 directory; forbidden for production",
    )
    path_modes.add_argument(
        "--staged-copy",
        action="store_true",
        help="Migrate an existing disposable DB in an owned private 0700 directory",
    )
    parser.add_argument("--components", help="Comma-separated ordered subset: household,weekly,shopping")
    parser.add_argument("--json", action="store_true", help="Emit sanitized JSON output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = run_migration(
        db_path=args.db_path,
        synthetic_create=bool(args.synthetic_create),
        staged_copy=bool(args.staged_copy),
        components=args.components,
    )
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True))
    else:
        _print_text(result)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
