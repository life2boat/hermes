from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from gateway.healbite_household_bootstrap import detect_schema_state
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_shopping import HealBiteShoppingStore
from gateway.healbite_shopping_schema import (
    SHOPPING_CONTRIBUTIONS_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
    ShoppingSchemaState,
)
from gateway.healbite_weekly_menu_schema import WEEKLY_MENU_INGREDIENTS_TABLE, WeeklyMenuSchemaState
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore
from scripts import healbite_schema_migrate

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "healbite_schema_migrate.py"


def _run_cli(db_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--db-path", str(db_path), "--json", *extra],
        text=True,
        capture_output=True,
        check=False,
    )


def _json_result(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout
    return json.loads(result.stdout)


def _table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    return int(row[0])


def _table_exists(db_path: Path, table: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None


def _index_exists(db_path: Path, index: str) -> bool:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
            (index,),
        ).fetchone() is not None


def _fresh_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "healbite.sqlite"
    db_path.touch(mode=0o600)
    os.chmod(db_path, 0o600)
    return db_path


def _init_household(db_path: Path) -> None:
    HealBiteHouseholdStore(db_path=db_path, ensure_schema_on_init=False).ensure_schema()


def _init_weekly(db_path: Path) -> None:
    HealBiteWeeklyMenuStore(db_path=db_path).initialize_schema()


def _init_shopping(db_path: Path) -> None:
    HealBiteShoppingStore(db_path=db_path).initialize_schema()


def _insert_legacy_weekly_data(db_path: Path) -> None:
    _init_household(db_path)
    _init_weekly(db_path)
    household_id = str(uuid4())
    member_id = str(uuid4())
    now = "2026-07-13 00:00:00"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO households (id, owner_user_id, name, status, default_timezone, created_at, updated_at, version) "
            "VALUES (?, 1001, NULL, 'active', 'UTC', ?, ?, 1)",
            (household_id, now, now),
        )
        conn.execute(
            "INSERT INTO household_members "
            "(id, household_id, linked_user_id, display_name, member_type, role, status, age_band, created_at, updated_at, version) "
            "VALUES (?, ?, 1001, NULL, 'primary', 'owner', 'active', NULL, ?, ?, 1)",
            (member_id, household_id, now, now),
        )
        for week, revision_number in (("2026-06-29", 1), ("2026-07-06", 1)):
            series_id = str(uuid4())
            revision_id = str(uuid4())
            conn.execute(
                "INSERT INTO household_weekly_menu_series (id, household_id, week_start, created_at, updated_at, version) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (series_id, household_id, week, now, now),
            )
            conn.execute(
                "INSERT INTO household_weekly_menus "
                "(id, series_id, household_id, revision_number, status, source_revision_id, created_by_member_id, "
                "created_at, updated_at, published_at, archived_at, version) "
                "VALUES (?, ?, ?, ?, 'published', NULL, ?, ?, ?, ?, NULL, 1)",
                (revision_id, series_id, household_id, revision_number, member_id, now, now, now),
            )
            conn.execute(
                "INSERT INTO household_weekly_menu_entries "
                "(id, menu_id, household_id, local_date, meal_slot, position, title, description, servings, origin, created_at, updated_at, version) "
                "VALUES (?, ?, ?, ?, 'breakfast', 1, 'Synthetic breakfast', NULL, NULL, 'manual', ?, ?, 1)",
                (str(uuid4()), revision_id, household_id, week, now, now),
            )
        conn.execute("DROP INDEX idx_weekly_menu_ingredients_entry_position_unique")
        conn.execute(f"DROP TABLE {WEEKLY_MENU_INGREDIENTS_TABLE}")
        conn.commit()


def test_missing_db_path_is_rejected() -> None:
    result = subprocess.run([sys.executable, str(SCRIPT), "--json"], text=True, capture_output=True, check=False)
    assert result.returncode == 2


def test_relative_db_path_is_rejected() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db-path", "relative.sqlite", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == healbite_schema_migrate.EXIT_INVALID_ARGUMENTS
    assert json.loads(result.stdout)["exit_classification"] == "DB_PATH_NOT_ABSOLUTE"


def test_missing_db_is_not_created_without_allow_create(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.sqlite"
    result = _run_cli(db_path)
    assert result.returncode == healbite_schema_migrate.EXIT_INVALID_ARGUMENTS
    assert not db_path.exists()
    assert _json_result(result)["exit_classification"] == "DB_PATH_MISSING"


def test_allow_create_creates_mode_0600_and_migrates(tmp_path: Path) -> None:
    db_path = tmp_path / "created.sqlite"
    result = _run_cli(db_path, "--allow-create")
    payload = _json_result(result)
    assert result.returncode == 0
    assert payload["database_path_classification"] == "absolute_created_regular"
    assert payload["mode_after"] == "0600"
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert detect_schema_state(sqlite3.connect(db_path)) == "canonical"
    assert HealBiteWeeklyMenuStore(db_path=db_path).schema_state() is WeeklyMenuSchemaState.CANONICAL
    assert HealBiteShoppingStore(db_path=db_path).schema_state() is ShoppingSchemaState.CANONICAL


def test_symlink_db_path_is_denied(tmp_path: Path) -> None:
    target = _fresh_db(tmp_path)
    link = tmp_path / "link.sqlite"
    link.symlink_to(target)
    result = _run_cli(link)
    assert result.returncode == healbite_schema_migrate.EXIT_INVALID_ARGUMENTS
    assert _json_result(result)["exit_classification"] == "DB_PATH_SYMLINK"


def test_fresh_db_full_migration_order_and_sanitized_output(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    result = _run_cli(db_path)
    payload = _json_result(result)
    assert result.returncode == 0
    assert [phase["name"] for phase in payload["phases"]] == ["household", "weekly", "shopping"]
    assert payload["data_backfilled"] is False
    assert str(db_path) not in result.stdout
    assert "id" not in payload
    assert _table_count(db_path, SHOPPING_LISTS_TABLE) == 0
    assert _table_count(db_path, SHOPPING_ITEMS_TABLE) == 0


def test_legacy_weekly_schema_is_migrated_without_backfill(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    _insert_legacy_weekly_data(db_path)
    before_menus = _table_count(db_path, "household_weekly_menus")
    before_entries = _table_count(db_path, "household_weekly_menu_entries")
    result = _run_cli(db_path)
    assert result.returncode == 0
    assert _table_count(db_path, "household_weekly_menus") == before_menus == 2
    assert _table_count(db_path, "household_weekly_menu_entries") == before_entries == 2
    assert _table_count(db_path, WEEKLY_MENU_INGREDIENTS_TABLE) == 0
    assert _table_count(db_path, SHOPPING_LISTS_TABLE) == 0
    assert _table_count(db_path, SHOPPING_ITEMS_TABLE) == 0


def test_repeated_runs_are_idempotent(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    first = _run_cli(db_path)
    second = _run_cli(db_path)
    third = _run_cli(db_path)
    assert first.returncode == second.returncode == third.returncode == 0
    assert _json_result(second)["schema_changed"] is False
    assert _json_result(third)["schema_changed"] is False


def test_partial_household_present_weekly_absent_recovers(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    _init_household(db_path)
    result = _run_cli(db_path)
    assert result.returncode == 0
    assert HealBiteWeeklyMenuStore(db_path=db_path).schema_state() is WeeklyMenuSchemaState.CANONICAL
    assert HealBiteShoppingStore(db_path=db_path).schema_state() is ShoppingSchemaState.CANONICAL


def test_partial_weekly_present_shopping_absent_recovers(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    _init_household(db_path)
    _init_weekly(db_path)
    result = _run_cli(db_path)
    assert result.returncode == 0
    assert HealBiteShoppingStore(db_path=db_path).schema_state() is ShoppingSchemaState.CANONICAL


def test_partial_shopping_without_contributions_recovers(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    _init_household(db_path)
    _init_weekly(db_path)
    _init_shopping(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_household_shopping_contributions_item_source_unique")
        conn.execute(f"DROP TABLE {SHOPPING_CONTRIBUTIONS_TABLE}")
        conn.commit()
    result = _run_cli(db_path)
    assert result.returncode == 0
    assert _table_exists(db_path, SHOPPING_CONTRIBUTIONS_TABLE)
    assert HealBiteShoppingStore(db_path=db_path).schema_state() is ShoppingSchemaState.CANONICAL


def test_missing_indexes_recover(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    _init_household(db_path)
    _init_weekly(db_path)
    _init_shopping(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_weekly_menu_series_id_household")
        conn.execute("DROP INDEX idx_household_shopping_lists_household_week_status")
        conn.commit()
    result = _run_cli(db_path)
    assert result.returncode == 0
    assert _index_exists(db_path, "idx_weekly_menu_series_id_household")
    assert _index_exists(db_path, "idx_household_shopping_lists_household_week_status")


def test_unknown_schema_fails_closed_and_skips_later_phases(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE households (id TEXT PRIMARY KEY)")
        conn.commit()
    result = _run_cli(db_path)
    payload = _json_result(result)
    assert result.returncode == healbite_schema_migrate.EXIT_SCHEMA_PRECONDITION
    assert payload["phases"][0]["name"] == "household"
    assert payload["phases"][0]["status"] == "failed"
    assert len(payload["phases"]) == 1
    assert not _table_exists(db_path, "household_weekly_menu_series")


def test_component_order_is_enforced(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    result = _run_cli(db_path, "--components", "shopping,weekly")
    assert result.returncode == healbite_schema_migrate.EXIT_INVALID_ARGUMENTS
    assert _json_result(result)["exit_classification"] == "COMPONENTS_OUT_OF_ORDER"


def test_mode_and_owner_are_preserved(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    before = db_path.stat()
    result = _run_cli(db_path)
    after = db_path.stat()
    payload = _json_result(result)
    assert result.returncode == 0
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode) == 0o600
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert payload["owner_before"] == payload["owner_after"]


def test_public_cli_does_not_import_runtime_systems() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    forbidden = ("gateway.run", "platforms.telegram", "qdrant", "scheduler", "auxiliary_client")
    assert all(token not in source for token in forbidden)


def test_runbook_uses_public_cli_instead_of_authoritative_inline_exec() -> None:
    runbook = (Path(__file__).resolve().parents[2] / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md").read_text(encoding="utf-8")
    d3 = runbook.split("### D3 Weekly schema initialization", 1)[1].split(
        "## D4 - Disabled-State Observation", 1
    )[0]
    assert "scripts/healbite_schema_migrate.py" in d3
    assert "store.initialize_schema()" not in d3
    assert "docker exec \"$" not in d3
