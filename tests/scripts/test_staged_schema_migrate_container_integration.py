from __future__ import annotations

import inspect
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore
from scripts import hermes_staged_schema_migrate as staged


TARGET_IMAGE = os.environ.get("HEALBITE_STAGED_MIGRATION_IMAGE_ID")
PREVIOUS_IMAGE = os.environ.get("HEALBITE_STAGED_PREVIOUS_IMAGE_ID")
TARGET_REVISION = os.environ.get("HEALBITE_STAGED_MIGRATION_REVISION")
ORCHESTRATOR_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hermes_staged_schema_migrate.py"


pytestmark = pytest.mark.skipif(
    not TARGET_IMAGE or not PREVIOUS_IMAGE or not TARGET_REVISION,
    reason="exact staged-migration image contract not supplied",
)


def _private(path: Path, *, uid: int = 0, gid: int = 0) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    os.chown(path, uid, gid)
    return path


def _legacy_source(root: Path) -> Path:
    parent = _private(root / "source", uid=staged.RUNTIME_UID, gid=staged.RUNTIME_GID)
    db_path = parent / "database.sqlite"
    db_path.touch(mode=0o600)
    HealBiteHouseholdStore(db_path=db_path, ensure_schema_on_init=False).ensure_schema()
    HealBiteWeeklyMenuStore(db_path=db_path).initialize_schema()
    now = "2026-07-13 00:00:00"
    household_id = str(uuid4())
    member_id = str(uuid4())
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO households (id, owner_user_id, name, status, default_timezone, created_at, updated_at, version) "
            "VALUES (?, 1001, NULL, 'active', 'UTC', ?, ?, 1)",
            (household_id, now, now),
        )
        conn.execute(
            "INSERT INTO household_members "
            "(id, household_id, linked_user_id, display_name, member_type, role, status, age_band, "
            "created_at, updated_at, version) "
            "VALUES (?, ?, 1001, NULL, 'primary', 'owner', 'active', NULL, ?, ?, 1)",
            (member_id, household_id, now, now),
        )
        for week in ("2026-06-29", "2026-07-06"):
            series_id = str(uuid4())
            revision_id = str(uuid4())
            conn.execute(
                "INSERT INTO household_weekly_menu_series "
                "(id, household_id, week_start, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, 1)",
                (series_id, household_id, week, now, now),
            )
            conn.execute(
                "INSERT INTO household_weekly_menus "
                "(id, series_id, household_id, revision_number, status, source_revision_id, "
                "created_by_member_id, created_at, updated_at, published_at, archived_at, version) "
                "VALUES (?, ?, ?, 1, 'published', NULL, ?, ?, ?, ?, NULL, 1)",
                (revision_id, series_id, household_id, member_id, now, now, now),
            )
        conn.execute("DROP INDEX idx_weekly_menu_ingredients_entry_position_unique")
        conn.execute("DROP TABLE household_weekly_menu_entry_ingredients")
        conn.commit()
    os.chmod(db_path, 0o600)
    os.chown(db_path, staged.RUNTIME_UID, staged.RUNTIME_GID)
    return db_path


def _count(db_path: Path, table: str) -> int:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def test_real_container_staged_migration_and_atomic_publish(tmp_path: Path) -> None:
    assert os.geteuid() == 0, "integration contract requires root orchestrator"
    source = _legacy_source(tmp_path)
    backups = _private(tmp_path / "backups")
    staging_root = _private(tmp_path / "staging")
    before_hash = staged._sha256(source)
    before_published = _count(source, "household_weekly_menus")
    result = subprocess.run(
        [
            sys.executable,
            str(ORCHESTRATOR_SCRIPT),
            "execute-synthetic",
            "--source-db",
            str(source),
            "--backup-dir",
            str(backups),
            "--staging-root",
            str(staging_root),
            "--target-image-id",
            str(TARGET_IMAGE),
            "--previous-image-id",
            str(PREVIOUS_IMAGE),
            "--expected-source-revision",
            str(TARGET_REVISION),
            "--synthetic-root",
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["publish_state"] == "VERIFIED"

    assert before_published == 2
    assert _count(source, "household_weekly_menus") == 2
    assert _count(source, "household_weekly_menu_idempotency") == 0
    assert _count(source, "household_shopping_lists") == 0
    assert _count(source, "household_shopping_items") == 0
    assert staged._sha256(source) != before_hash
    assert staged._sqlite_validation(source) == ("ok", 0)
    assert not staged._sidecars(source)
    assert stat.S_IMODE(source.stat().st_mode) == 0o600
    assert source.stat().st_uid == staged.RUNTIME_UID
    assert source.stat().st_nlink == 1
    backup_files = list(backups.glob("backup-*.sqlite"))
    assert len(backup_files) == 1
    assert staged._sha256(backup_files[0]) == before_hash

    migration_source = inspect.getsource(staged._run_target_migration)
    assert "/home/hermes/healbite.db" not in migration_source
    assert ":/migration:rw" in migration_source


def test_real_container_old_nonwritable_parent_contract_fails(tmp_path: Path) -> None:
    assert os.geteuid() == 0, "integration contract requires root orchestrator"
    parent = _private(tmp_path / "old-in-place", uid=staged.RUNTIME_UID, gid=staged.RUNTIME_GID)
    db_path = parent / "database.sqlite"
    db_path.touch(mode=0o600)
    os.chown(db_path, staged.RUNTIME_UID, staged.RUNTIME_GID)
    os.chmod(parent, 0o500)
    command = [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{staged.RUNTIME_UID}:{staged.RUNTIME_GID}",
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        "-v",
        f"{parent}:/migration:rw",
        str(TARGET_IMAGE),
        "/opt/hermes/scripts/healbite_schema_migrate.py",
        "--db-path",
        "/migration/database.sqlite",
        "--json",
    ]

    result = subprocess.run(command, text=True, capture_output=True, check=False)

    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert staged._sqlite_validation(db_path) == ("ok", 0)
