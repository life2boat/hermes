"""Hardened exact-image test harness for migration-core validation.

The exact-image boundary deliberately covers imports plus forward, idempotent,
and rollback behavior of the migration core. Public staged publication,
quiescence, crash recovery, and atomic exchange remain covered by host unit
contracts in ``test_hermes_staged_schema_migrate.py``. This helper never opens
host paths or production data.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Sequence
from typing import Any


IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
SCENARIOS = frozenset({"import", "forward-idempotency", "failure-rollback"})
TMPFS_SPEC = "/tmp:rw,nosuid,nodev,noexec,size=256m"
RUNTIME_IDENTITY = "10000:10000"


class HarnessExecutionError(RuntimeError):
    """Report a sanitized harness failure without forwarding container output."""


HARNESS_PROGRAM = r'''
from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore
from scripts import healbite_schema_migrate as migration


def private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def legacy_fixture(root: Path, name: str) -> Path:
    parent = private_directory(root / name)
    database = parent / "database.sqlite"
    database.touch(mode=0o600)
    HealBiteHouseholdStore(db_path=database, ensure_schema_on_init=False).ensure_schema()
    HealBiteWeeklyMenuStore(db_path=database).initialize_schema()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP INDEX idx_weekly_menu_ingredients_entry_position_unique")
        connection.execute("DROP TABLE household_weekly_menu_entry_ingredients")
        connection.commit()
    os.chmod(database, 0o600)
    return database


def schema_signature(database: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    return tuple((str(kind), str(name), str(sql)) for kind, name, sql in rows)


def sqlite_valid(database: Path) -> bool:
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = int(connection.execute("PRAGMA foreign_key_check").fetchone() is not None)
    return integrity == "ok" and foreign_keys == 0


scenario = sys.argv[1]
root = private_directory(Path("/tmp/healbite-staged-image-harness"))

if scenario == "import":
    payload = {
        "scenario": scenario,
        "status": "pass",
        "migration_module_imported": migration.__name__ == "scripts.healbite_schema_migrate",
        "repository_root": str(Path(migration.__file__).resolve()).startswith("/opt/hermes/"),
    }
elif scenario == "forward-idempotency":
    database = legacy_fixture(root, "forward")
    first = migration.run_migration(db_path=str(database), staged_copy=True)
    second = migration.run_migration(db_path=str(database), staged_copy=True)
    payload = {
        "scenario": scenario,
        "status": "pass",
        "forward_exit_code": first.exit_code,
        "forward_committed": first.migration_commit_state == "COMMITTED",
        "forward_schema_changed": first.schema_changed,
        "idempotent_exit_code": second.exit_code,
        "idempotent_committed": second.migration_commit_state == "COMMITTED",
        "idempotent_schema_changed": second.schema_changed,
        "path_mode": second.path_mode,
        "sqlite_valid": sqlite_valid(database),
        "private_directory": stat.S_IMODE(database.parent.stat().st_mode) == 0o700,
        "private_database": stat.S_IMODE(database.stat().st_mode) == 0o600,
    }
elif scenario == "failure-rollback":
    database = legacy_fixture(root, "failure")
    before = schema_signature(database)

    def fail_after_weekly(component: str, _connection: sqlite3.Connection) -> None:
        if component == "weekly":
            raise RuntimeError("injected test-only migration failure")

    result = migration.run_migration(
        db_path=str(database),
        staged_copy=True,
        _component_hook=fail_after_weekly,
    )
    payload = {
        "scenario": scenario,
        "status": "pass",
        "failure_exit_classification": result.exit_classification,
        "migration_commit_state": result.migration_commit_state,
        "schema_may_have_changed": result.schema_may_have_changed,
        "safe_to_rerun": result.safe_to_rerun,
        "schema_unchanged": schema_signature(database) == before,
        "sqlite_valid": sqlite_valid(database),
    }
else:
    raise SystemExit(64)

print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
'''


def build_docker_command(image_id: str, scenario: str) -> tuple[str, ...]:
    """Return a shell-free, anonymous, hardened Docker argv."""
    if IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise ValueError("exact immutable image ID required")
    if scenario not in SCENARIOS:
        raise ValueError("unknown harness scenario")
    return (
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--tmpfs",
        TMPFS_SPEC,
        "--user",
        RUNTIME_IDENTITY,
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        image_id,
        "-B",
        "-",
        scenario,
    )


def run_scenario(
    image_id: str,
    scenario: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Run one ephemeral scenario and return its sanitized aggregate payload."""
    result = runner(
        build_docker_command(image_id, scenario),
        input=HARNESS_PROGRAM,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise HarnessExecutionError(f"scenario_failed:{scenario}:exit_{result.returncode}")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HarnessExecutionError(f"scenario_invalid_output:{scenario}") from exc
    if not isinstance(payload, dict) or payload.get("scenario") != scenario or payload.get("status") != "pass":
        raise HarnessExecutionError(f"scenario_contract_failed:{scenario}")
    return payload


def executable_forbidden_patterns(command: Sequence[str]) -> tuple[str, ...]:
    """Return forbidden executable argv tokens, excluding documentation text."""
    forbidden_tokens = {
        "-v",
        "--volume",
        "--mount",
        "--name",
        "create",
        "exec",
        "stop",
        "kill",
        "rm",
        "--force",
        "/var/run/docker.sock",
        "host",
    }
    return tuple(token for token in command if token in forbidden_tokens)
