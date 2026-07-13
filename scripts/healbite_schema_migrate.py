#!/usr/bin/env python3
"""Public migration-only CLI for HealBite household, weekly, and shopping schemas."""

from __future__ import annotations

import argparse
import json
import os
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

from gateway.healbite_household_bootstrap import detect_schema_state as detect_household_schema_state
from gateway.healbite_households import HealBiteHouseholdStore, HouseholdError
from gateway.healbite_shopping import HealBiteShoppingStore, ShoppingSchemaError
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore, WeeklyMenuSchemaError

EXIT_SUCCESS = 0
EXIT_INVALID_ARGUMENTS = 2
EXIT_SCHEMA_PRECONDITION = 3
EXIT_MIGRATION_FAILURE = 4
EXIT_CONTRACT_DRIFT = 5

ALL_COMPONENTS = ("household", "weekly", "shopping")


class MigrationError(RuntimeError):
    def __init__(self, code: str, exit_code: int, phase: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.phase = phase


@dataclass(frozen=True)
class DatabaseTarget:
    path: Path
    classification: str
    mode_before: str | None
    owner_before: str | None
    identity_before: tuple[int, int] | None


@dataclass(frozen=True)
class PhaseResult:
    name: str
    status: str
    schema_state: str | None = None
    changed: bool = False
    error_type: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "changed": self.changed,
        }
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
    data_backfilled: bool = False
    exit_classification: str = "success"
    mode_before: str | None = None
    mode_after: str | None = None
    owner_before: str | None = None
    owner_after: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "database_path_classification": self.database_path_classification,
            "phases": [phase.as_dict() for phase in self.phases],
            "schema_changed": self.schema_changed,
            "data_backfilled": self.data_backfilled,
            "exit_classification": self.exit_classification,
            "mode_before": self.mode_before,
            "mode_after": self.mode_after,
            "owner_before": self.owner_before,
            "owner_after": self.owner_after,
        }


class OutputFormat(str, Enum):
    TEXT = "text"
    JSON = "json"


def _file_mode(metadata: os.stat_result) -> str:
    return f"{stat.S_IMODE(metadata.st_mode):04o}"


def _file_owner(metadata: os.stat_result) -> str:
    return f"{metadata.st_uid}:{metadata.st_gid}"


def _classify_existing_path(path: Path) -> DatabaseTarget:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MigrationError("DB_PATH_MISSING", EXIT_INVALID_ARGUMENTS) from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise MigrationError("DB_PATH_SYMLINK", EXIT_INVALID_ARGUMENTS)
    if not stat.S_ISREG(metadata.st_mode):
        raise MigrationError("DB_PATH_NOT_REGULAR", EXIT_INVALID_ARGUMENTS)
    return DatabaseTarget(
        path=path,
        classification="absolute_existing_regular",
        mode_before=_file_mode(metadata),
        owner_before=_file_owner(metadata),
        identity_before=(metadata.st_dev, metadata.st_ino),
    )


def _resolve_db_path(raw_path: str | None, *, allow_create: bool) -> DatabaseTarget:
    if not raw_path:
        raise MigrationError("DB_PATH_REQUIRED", EXIT_INVALID_ARGUMENTS)
    path = Path(raw_path)
    if not path.is_absolute():
        raise MigrationError("DB_PATH_NOT_ABSOLUTE", EXIT_INVALID_ARGUMENTS)
    if path.exists() or path.is_symlink():
        return _classify_existing_path(path)
    if not allow_create:
        raise MigrationError("DB_PATH_MISSING", EXIT_INVALID_ARGUMENTS)
    parent = path.parent
    if not parent.exists():
        raise MigrationError("DB_PARENT_MISSING", EXIT_INVALID_ARGUMENTS)
    try:
        parent_metadata = parent.lstat()
    except OSError as exc:
        raise MigrationError("DB_PARENT_UNAVAILABLE", EXIT_INVALID_ARGUMENTS) from exc
    if stat.S_ISLNK(parent_metadata.st_mode) or not stat.S_ISDIR(parent_metadata.st_mode):
        raise MigrationError("DB_PARENT_UNSAFE", EXIT_INVALID_ARGUMENTS)
    old_umask = os.umask(0o077)
    fd: int | None = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as exc:
        raise MigrationError("DB_CREATE_FAILED", EXIT_INVALID_ARGUMENTS) from exc
    finally:
        os.umask(old_umask)
        if fd is not None:
            os.close(fd)
    target = _classify_existing_path(path)
    return DatabaseTarget(
        path=target.path,
        classification="absolute_created_regular",
        mode_before=target.mode_before,
        owner_before=target.owner_before,
        identity_before=target.identity_before,
    )


def _verify_identity_unchanged(target: DatabaseTarget) -> tuple[str | None, str | None]:
    if target.identity_before is None:
        return None, None
    try:
        metadata = target.path.lstat()
    except OSError as exc:
        raise MigrationError("DB_PATH_LOST", EXIT_MIGRATION_FAILURE) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise MigrationError("DB_PATH_REPLACED", EXIT_MIGRATION_FAILURE)
    if (metadata.st_dev, metadata.st_ino) != target.identity_before:
        raise MigrationError("DB_PATH_REPLACED", EXIT_MIGRATION_FAILURE)
    return _file_mode(metadata), _file_owner(metadata)


def _sqlite_integrity(path: Path) -> None:
    try:
        with sqlite3.connect(path, timeout=30.0) as conn:
            value = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error as exc:
        raise MigrationError("SQLITE_INTEGRITY_CHECK_FAILED", EXIT_SCHEMA_PRECONDITION) from exc
    if value.lower() != "ok":
        raise MigrationError("SQLITE_INTEGRITY_CHECK_FAILED", EXIT_SCHEMA_PRECONDITION)


def _household_phase(path: Path) -> tuple[str, str]:
    try:
        with sqlite3.connect(path, timeout=30.0) as conn:
            before = detect_household_schema_state(conn)
        if before not in {"not_initialized", "canonical"}:
            raise MigrationError("HOUSEHOLD_SCHEMA_NOT_CANONICAL", EXIT_SCHEMA_PRECONDITION, "household")
        HealBiteHouseholdStore(db_path=path, ensure_schema_on_init=False).ensure_schema()
        with sqlite3.connect(path, timeout=30.0) as conn:
            after = detect_household_schema_state(conn)
    except MigrationError:
        raise
    except (HouseholdError, sqlite3.Error) as exc:
        raise MigrationError(type(exc).__name__, EXIT_MIGRATION_FAILURE, "household") from exc
    if after != "canonical":
        raise MigrationError("HOUSEHOLD_SCHEMA_INITIALIZATION_FAILED", EXIT_MIGRATION_FAILURE, "household")
    return before, after


def _weekly_phase(path: Path) -> tuple[str, str]:
    store = HealBiteWeeklyMenuStore(db_path=path)
    before = store.schema_state().value
    try:
        after_state = store.initialize_schema()
    except WeeklyMenuSchemaError as exc:
        raise MigrationError(type(exc).__name__, EXIT_SCHEMA_PRECONDITION, "weekly") from exc
    except sqlite3.Error as exc:
        raise MigrationError(type(exc).__name__, EXIT_MIGRATION_FAILURE, "weekly") from exc
    return before, after_state.value


def _shopping_phase(path: Path) -> tuple[str, str]:
    store = HealBiteShoppingStore(db_path=path)
    before = store.schema_state().value
    try:
        after_state = store.initialize_schema()
    except ShoppingSchemaError as exc:
        raise MigrationError(type(exc).__name__, EXIT_SCHEMA_PRECONDITION, "shopping") from exc
    except sqlite3.Error as exc:
        raise MigrationError(type(exc).__name__, EXIT_MIGRATION_FAILURE, "shopping") from exc
    return before, after_state.value


PHASES: dict[str, Callable[[Path], tuple[str, str]]] = {
    "household": _household_phase,
    "weekly": _weekly_phase,
    "shopping": _shopping_phase,
}


def _parse_components(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ALL_COMPONENTS
    components = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    if not components:
        raise MigrationError("COMPONENTS_EMPTY", EXIT_INVALID_ARGUMENTS)
    unknown = [component for component in components if component not in PHASES]
    if unknown:
        raise MigrationError("COMPONENTS_UNKNOWN", EXIT_INVALID_ARGUMENTS)
    if len(set(components)) != len(components):
        raise MigrationError("COMPONENTS_DUPLICATE", EXIT_INVALID_ARGUMENTS)
    positions = [ALL_COMPONENTS.index(component) for component in components]
    if positions != sorted(positions):
        raise MigrationError("COMPONENTS_OUT_OF_ORDER", EXIT_INVALID_ARGUMENTS)
    return components


def run_migration(*, db_path: str | None, allow_create: bool = False, components: str | None = None) -> MigrationResult:
    phases: list[PhaseResult] = []
    target: DatabaseTarget | None = None
    try:
        selected = _parse_components(components)
        target = _resolve_db_path(db_path, allow_create=allow_create)
        _sqlite_integrity(target.path)
        schema_changed = False
        for name in selected:
            before, after = PHASES[name](target.path)
            changed = before != after
            schema_changed = schema_changed or changed
            phases.append(PhaseResult(name=name, status="success", schema_state=after, changed=changed))
        mode_after, owner_after = _verify_identity_unchanged(target)
        return MigrationResult(
            status="success",
            exit_code=EXIT_SUCCESS,
            database_path_classification=target.classification,
            phases=tuple(phases),
            schema_changed=schema_changed,
            mode_before=target.mode_before,
            mode_after=mode_after,
            owner_before=target.owner_before,
            owner_after=owner_after,
        )
    except MigrationError as exc:
        if exc.phase:
            phases.append(PhaseResult(name=exc.phase, status="failed", error_type=exc.code))
        mode_after = owner_after = None
        if target is not None:
            try:
                mode_after, owner_after = _verify_identity_unchanged(target)
            except MigrationError:
                pass
        return MigrationResult(
            status="failed",
            exit_code=exc.exit_code,
            database_path_classification=target.classification if target else "invalid",
            phases=tuple(phases),
            schema_changed=any(phase.changed for phase in phases),
            exit_classification=exc.code,
            mode_before=target.mode_before if target else None,
            mode_after=mode_after,
            owner_before=target.owner_before if target else None,
            owner_after=owner_after,
        )


def _print_text(result: MigrationResult) -> None:
    print(f"status={result.status}")
    print(f"exit_classification={result.exit_classification}")
    print(f"database_path_classification={result.database_path_classification}")
    print(f"schema_changed={str(result.schema_changed).lower()}")
    print(f"data_backfilled={str(result.data_backfilled).lower()}")
    for phase in result.phases:
        prefix = phase.name.upper()
        print(f"{prefix}_SCHEMA_STATUS={phase.schema_state or phase.status}")
        if phase.error_type:
            print(f"{prefix}_ERROR_TYPE={phase.error_type}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HealBite migration-only schema CLI")
    parser.add_argument("--db-path", required=True, help="Absolute SQLite DB path to migrate")
    parser.add_argument("--allow-create", action="store_true", help="Create a missing explicit DB path with mode 0600")
    parser.add_argument("--components", help="Comma-separated ordered subset: household,weekly,shopping")
    parser.add_argument("--json", action="store_true", help="Emit sanitized JSON output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        result = run_migration(
            db_path=args.db_path,
            allow_create=bool(args.allow_create),
            components=args.components,
        )
    except MigrationError as exc:
        result = MigrationResult(
            status="failed",
            exit_code=exc.exit_code,
            database_path_classification="invalid",
            phases=(),
            schema_changed=False,
            exit_classification=exc.code,
        )
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True))
    else:
        _print_text(result)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
