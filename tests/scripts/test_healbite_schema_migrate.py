from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from gateway.healbite_household_bootstrap import detect_schema_state
from gateway.healbite_households import HealBiteHouseholdStore
from gateway.healbite_shopping import HealBiteShoppingStore
from gateway.healbite_shopping_schema import (
    SHOPPING_CONTRIBUTIONS_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
    ShoppingSchemaState,
)
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_INGREDIENTS_TABLE,
    WEEKLY_MENU_SERIES_TABLE,
    WeeklyMenuSchemaState,
)
from gateway.healbite_weekly_menus import HealBiteWeeklyMenuStore
from scripts import healbite_schema_migrate

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "healbite_schema_migrate.py"


def _trusted_target(db_path: Path, *, classification: str = "synthetic_test_path") -> healbite_schema_migrate.DatabaseTarget:
    metadata = db_path.lstat()
    return healbite_schema_migrate.DatabaseTarget(
        path=db_path,
        classification=classification,
        mode_before=f"{stat.S_IMODE(metadata.st_mode):04o}",
        owner_before=f"{metadata.st_uid}:{metadata.st_gid}",
        identity_before=(metadata.st_dev, metadata.st_ino),
    )


def _trusted_resolver(
    raw_path: str | None,
    synthetic_create: bool,
    _identity: healbite_schema_migrate.ProcessIdentity | None,
) -> healbite_schema_migrate.DatabaseTarget:
    assert raw_path is not None
    db_path = Path(raw_path)
    if not db_path.exists():
        if not synthetic_create:
            raise healbite_schema_migrate.MigrationError(
                healbite_schema_migrate.ExitClassification.MISSING_DATABASE,
                detail_code="DB_PATH_MISSING",
            )
        db_path.touch(mode=0o600)
        os.chmod(db_path, 0o600)
    return _trusted_target(db_path)


def _run_cli(db_path: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    synthetic_create = "--synthetic-create" in extra
    components: str | None = None
    if "--components" in extra:
        index = extra.index("--components")
        components = extra[index + 1]
    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        synthetic_create=synthetic_create,
        components=components,
        _target_resolver=_trusted_resolver,
    )
    return subprocess.CompletedProcess(
        args=[str(SCRIPT)],
        returncode=result.exit_code,
        stdout=json.dumps(result.as_dict(), ensure_ascii=True, sort_keys=True),
        stderr="",
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


def _schema_snapshot(db_path: Path) -> tuple[tuple[str, str, str], ...]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    return tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows)


def _database_snapshot(db_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(db_path) as conn:
        return tuple(conn.iterdump())


def _table_names(db_path: Path) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }


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


class _TrackingConnection:
    def __init__(
        self,
        delegate: sqlite3.Connection,
        *,
        fail_commit: bool = False,
        fail_rollback: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.delegate = delegate
        self.fail_commit = fail_commit
        self.fail_rollback = fail_rollback
        self.fail_close = fail_close
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self.delegate.execute(*args, **kwargs)

    def commit(self) -> None:
        self.commit_count += 1
        if self.fail_commit:
            raise RuntimeError("synthetic commit failure")
        self.delegate.commit()

    def rollback(self) -> None:
        self.rollback_count += 1
        if self.fail_rollback:
            raise RuntimeError("synthetic rollback failure")
        self.delegate.rollback()

    def close(self) -> None:
        self.close_count += 1
        self.delegate.close()
        if self.fail_close:
            raise RuntimeError("synthetic close failure")


def _sqlite_operational_error(code: int, name: str) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("sanitized synthetic sqlite failure")
    error.sqlite_errorcode = code
    error.sqlite_errorname = name
    return error


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
    payload = json.loads(result.stdout)
    assert payload["exit_classification"] == "INVALID_ARGUMENT"
    assert payload["migration_commit_state"] == "NOT_STARTED"
    assert payload["schema_may_have_changed"] is False
    assert payload["cleanup_failed"] is False
    assert payload["safe_to_rerun"] is True
    assert payload["error_type"] == "DB_PATH_NOT_ABSOLUTE"


def test_missing_db_is_not_created_without_synthetic_create(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.sqlite"
    result = _run_cli(db_path)
    assert result.returncode == healbite_schema_migrate.EXIT_MISSING_DATABASE
    assert not db_path.exists()
    assert _json_result(result)["exit_classification"] == "MISSING_DATABASE"


def test_synthetic_create_public_cli_end_to_end(tmp_path: Path) -> None:
    parent = _private_synthetic_parent(tmp_path)
    db_path = parent / "created.sqlite"

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db-path", str(db_path), "--synthetic-create", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = _json_result(result)

    assert result.returncode == 0
    assert payload["database_path_classification"] == "absolute_synthetic_created_private_parent"
    assert payload["migration_commit_state"] == "COMMITTED"
    assert payload["mode_after"] == "0600"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert detect_schema_state(sqlite3.connect(db_path)) == "canonical"
    assert HealBiteWeeklyMenuStore(db_path=db_path).schema_state() is WeeklyMenuSchemaState.CANONICAL
    assert HealBiteShoppingStore(db_path=db_path).schema_state() is ShoppingSchemaState.CANONICAL


def test_staged_copy_public_cli_end_to_end(tmp_path: Path) -> None:
    parent = tmp_path / "staging"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    db_path = parent / "database.sqlite"
    db_path.touch(mode=0o600)
    os.chmod(db_path, 0o600)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db-path", str(db_path), "--staged-copy", "--json"],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = _json_result(result)

    assert result.returncode == 0
    assert payload["path_mode"] == "STAGED_COPY"
    assert payload["database_path_classification"] == "absolute_existing_staged_copy_private_parent"
    assert payload["migration_commit_state"] == "COMMITTED"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert db_path.stat().st_nlink == 1
    assert not Path(f"{db_path}-journal").exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


@pytest.mark.parametrize(
    ("parent_mode", "db_mode", "make_hardlink", "expected_error"),
    [
        (0o750, 0o600, False, "STAGED_PARENT_NOT_PRIVATE"),
        (0o700, 0o640, False, "STAGED_DB_METADATA_INVALID"),
        (0o700, 0o600, True, "STAGED_DB_LINK_COUNT_INVALID"),
    ],
)
def test_staged_copy_rejects_unsafe_metadata(
    tmp_path: Path,
    parent_mode: int,
    db_mode: int,
    make_hardlink: bool,
    expected_error: str,
) -> None:
    parent = tmp_path / "staging"
    parent.mkdir(mode=0o700)
    os.chmod(parent, parent_mode)
    db_path = parent / "database.sqlite"
    db_path.touch(mode=0o600)
    os.chmod(db_path, db_mode)
    if make_hardlink:
        os.link(db_path, parent / "alias.sqlite")

    result = healbite_schema_migrate.run_migration(db_path=str(db_path), staged_copy=True)

    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == expected_error
    assert db_path.stat().st_size == 0


def test_transaction_hook_runs_after_begin_immediate_with_write_lock(tmp_path: Path) -> None:
    parent = tmp_path / "staging"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    db_path = parent / "database.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
    os.chmod(db_path, 0o600)
    before = db_path.read_bytes()
    observed: dict[str, bool] = {}

    def inspect_active_transaction(conn: sqlite3.Connection) -> None:
        observed["begin_immediate"] = conn.in_transaction
        competitor = sqlite3.connect(db_path, timeout=0)
        try:
            competitor.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            observed["write_lock_held"] = isinstance(code, int) and (code & 0xFF) in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }
        finally:
            competitor.close()
        raise RuntimeError("test-only transaction stop")

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        staged_copy=True,
        _transaction_hook=inspect_active_transaction,
    )

    assert observed == {"begin_immediate": True, "write_lock_held": True}
    assert result.exit_classification == "MIGRATION_FAILED"
    assert db_path.read_bytes() == before
    assert result.migration_commit_state == "ROLLED_BACK"


def test_component_hook_is_internal_and_runs_inside_real_transaction(tmp_path: Path) -> None:
    parent = tmp_path / "staging"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    db_path = parent / "database.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
    os.chmod(db_path, 0o600)
    observed: list[str] = []

    def fail_at_weekly(name: str, conn: sqlite3.Connection) -> None:
        assert conn.in_transaction is True
        observed.append(name)
        if name == "weekly":
            raise RuntimeError("test-only component failure")

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        staged_copy=True,
        _component_hook=fail_at_weekly,
    )

    assert observed == ["household", "weekly"]
    assert result.exit_classification == "MIGRATION_FAILED"
    assert result.migration_commit_state == "ROLLED_BACK"
    with sqlite3.connect(db_path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"legacy_rows"}


def test_staged_copy_and_synthetic_create_are_mutually_exclusive(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    db_path = parent / "database.sqlite"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db-path",
            str(db_path),
            "--staged-copy",
            "--synthetic-create",
            "--json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert not db_path.exists()


def test_staged_copy_metadata_change_before_ddl_is_rejected(tmp_path: Path) -> None:
    parent = tmp_path / "staging"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    db_path = parent / "database.sqlite"
    db_path.touch(mode=0o600)
    os.chmod(db_path, 0o600)

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        staged_copy=True,
        _before_ddl_hook=lambda: os.chmod(db_path, 0o640),
    )

    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "STAGED_DB_METADATA_CHANGED"
    assert result.migration_commit_state == "ROLLED_BACK"
    assert not _table_exists(db_path, "households")


def test_symlink_db_path_is_denied(tmp_path: Path) -> None:
    target = _fresh_db(tmp_path)
    link = tmp_path / "link.sqlite"
    link.symlink_to(target)
    result = healbite_schema_migrate.run_migration(db_path=str(link))
    assert result.exit_code == healbite_schema_migrate.EXIT_UNSAFE_PATH
    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "DB_PATH_SYMLINK"


def test_symlink_ancestor_is_denied(tmp_path: Path, monkeypatch) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    db_path = real_parent / "db.sqlite"
    db_path.touch(mode=0o660)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    aliased_db = alias / "db.sqlite"
    monkeypatch.setattr(healbite_schema_migrate, "_path_components", lambda _path: (alias,))
    identity = healbite_schema_migrate.ProcessIdentity(uid=10001, gid=db_path.stat().st_gid, groups=frozenset())
    result = healbite_schema_migrate.run_migration(db_path=str(aliased_db), _identity=identity)
    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "PATH_COMPONENT_SYMLINK"


def test_writable_parent_is_denied(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "writable"
    parent.mkdir(mode=0o770)
    os.chmod(parent, 0o770)
    db_path = parent / "db.sqlite"
    db_path.touch(mode=0o660)
    monkeypatch.setattr(healbite_schema_migrate, "_path_components", lambda _path: (parent,))
    identity = healbite_schema_migrate.ProcessIdentity(uid=10001, gid=parent.stat().st_gid, groups=frozenset())
    result = healbite_schema_migrate.run_migration(db_path=str(db_path), _identity=identity)
    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "PATH_DIRECTORY_WRITABLE"


def test_writable_ancestor_is_denied(tmp_path: Path, monkeypatch) -> None:
    ancestor = tmp_path / "ancestor"
    parent = ancestor / "safe-parent"
    parent.mkdir(parents=True)
    db_path = parent / "db.sqlite"
    db_path.touch(mode=0o660)
    os.chmod(db_path, 0o660)
    os.chmod(ancestor, 0o770)
    os.chmod(parent, 0o550)
    monkeypatch.setattr(healbite_schema_migrate, "_path_components", lambda _path: (ancestor, parent))
    identity = healbite_schema_migrate.ProcessIdentity(uid=10001, gid=ancestor.stat().st_gid, groups=frozenset())
    result = healbite_schema_migrate.run_migration(db_path=str(db_path), _identity=identity)
    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "PATH_DIRECTORY_WRITABLE"


def test_safe_bind_mount_style_path_is_accepted(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    parent = root / "data"
    parent.mkdir(parents=True)
    db_path = parent / "db.sqlite"
    db_path.touch(mode=0o660)
    os.chmod(db_path, 0o660)
    os.chmod(root, 0o550)
    os.chmod(parent, 0o550)
    monkeypatch.setattr(healbite_schema_migrate, "_path_components", lambda _path: (root, parent))
    identity = healbite_schema_migrate.ProcessIdentity(uid=10001, gid=db_path.stat().st_gid, groups=frozenset())
    target = healbite_schema_migrate._resolve_db_path(str(db_path), synthetic_create=False, identity=identity)
    assert target.identity_before == (db_path.stat().st_dev, db_path.stat().st_ino)


def _private_synthetic_parent(tmp_path: Path) -> Path:
    parent = tmp_path / "synthetic-private"
    parent.mkdir(mode=0o700)
    os.chmod(parent, 0o700)
    return parent


def test_synthetic_create_end_to_end_uses_private_parent_and_restores_mode(tmp_path: Path) -> None:
    parent = _private_synthetic_parent(tmp_path)
    db_path = parent / "created.sqlite"
    observed_modes: list[int] = []

    def observe_before_open() -> None:
        observed_modes.append(stat.S_IMODE(parent.stat().st_mode))

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        synthetic_create=True,
        _before_open_hook=observe_before_open,
    )

    assert result.exit_classification == "SUCCESS"
    assert result.database_path_classification == "absolute_synthetic_created_private_parent"
    assert result.migration_commit_state == "COMMITTED"
    assert observed_modes == [0o500]
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    assert _table_exists(db_path, SHOPPING_LISTS_TABLE)


@pytest.mark.parametrize("mode", [0o750, 0o770, 0o755, 0o777])
def test_synthetic_create_rejects_non_private_parent(tmp_path: Path, mode: int) -> None:
    parent = tmp_path / "synthetic-parent"
    parent.mkdir(mode=mode)
    os.chmod(parent, mode)

    result = healbite_schema_migrate.run_migration(
        db_path=str(parent / "created.sqlite"),
        synthetic_create=True,
    )

    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "SYNTHETIC_PARENT_NOT_PRIVATE"


def test_synthetic_create_rejects_parent_owner_mismatch(tmp_path: Path) -> None:
    parent = _private_synthetic_parent(tmp_path)
    metadata = parent.stat()
    identity = healbite_schema_migrate.ProcessIdentity(
        uid=metadata.st_uid + 1000,
        gid=metadata.st_gid,
        groups=frozenset(),
    )

    result = healbite_schema_migrate.run_migration(
        db_path=str(parent / "created.sqlite"),
        synthetic_create=True,
        _identity=identity,
    )

    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "SYNTHETIC_PARENT_OWNER_MISMATCH"


def test_synthetic_create_rejects_existing_target_and_symlink(tmp_path: Path) -> None:
    parent = _private_synthetic_parent(tmp_path)
    existing = parent / "existing.sqlite"
    existing.touch(mode=0o600)
    link = parent / "link.sqlite"
    link.symlink_to(existing)

    for path in (existing, link):
        result = healbite_schema_migrate.run_migration(db_path=str(path), synthetic_create=True)
        assert result.exit_classification == "UNSAFE_PATH"
        assert result.error_type == "SYNTHETIC_TARGET_EXISTS"


def test_synthetic_create_exclusive_collision_is_denied(tmp_path: Path, monkeypatch) -> None:
    parent = _private_synthetic_parent(tmp_path)
    db_path = parent / "created.sqlite"
    real_open = os.open

    def collide(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> int:
        if Path(path) == Path(db_path.name) and kwargs.get("dir_fd") is not None:
            raise FileExistsError("synthetic collision")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", collide)
    result = healbite_schema_migrate.run_migration(db_path=str(db_path), synthetic_create=True)

    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "SYNTHETIC_CREATE_COLLISION"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_synthetic_path_replacement_before_ddl_is_prevented_or_detected(tmp_path: Path) -> None:
    parent = _private_synthetic_parent(tmp_path)
    db_path = parent / "created.sqlite"
    parked = parent / "parked.sqlite"
    substitute = parent / "substitute.sqlite"
    substitute.touch(mode=0o600)
    replacement_denied = False

    def replace_target() -> None:
        nonlocal replacement_denied
        try:
            db_path.rename(parked)
            db_path.symlink_to(substitute)
        except PermissionError:
            replacement_denied = True

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        synthetic_create=True,
        _before_ddl_hook=replace_target,
    )

    if replacement_denied:
        assert result.exit_classification == "SUCCESS"
        assert result.migration_commit_state == "COMMITTED"
        assert _table_exists(db_path, SHOPPING_LISTS_TABLE)
    else:
        assert result.exit_classification == "UNSAFE_PATH"
        assert result.migration_commit_state == "ROLLED_BACK"
    assert not _table_exists(substitute, SHOPPING_LISTS_TABLE)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_synthetic_path_replacement_before_open_is_prevented_or_detected(tmp_path: Path) -> None:
    parent = _private_synthetic_parent(tmp_path)
    db_path = parent / "created.sqlite"
    parked = parent / "parked.sqlite"
    substitute = parent / "substitute.sqlite"
    substitute.touch(mode=0o600)
    replacement_denied = False

    def replace_target() -> None:
        nonlocal replacement_denied
        try:
            db_path.rename(parked)
            db_path.symlink_to(substitute)
        except PermissionError:
            replacement_denied = True

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        synthetic_create=True,
        _before_open_hook=replace_target,
    )

    if replacement_denied:
        assert result.exit_classification == "SUCCESS"
        assert result.migration_commit_state == "COMMITTED"
        assert _table_exists(db_path, SHOPPING_LISTS_TABLE)
    else:
        assert result.exit_classification == "UNSAFE_PATH"
        assert result.migration_commit_state == "NOT_STARTED"
    assert not _table_exists(substitute, SHOPPING_LISTS_TABLE)
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_synthetic_parent_mode_is_restored_after_migration_failure(tmp_path: Path) -> None:
    parent = _private_synthetic_parent(tmp_path)

    def fail_before_ddl() -> None:
        raise RuntimeError("synthetic primary failure")

    result = healbite_schema_migrate.run_migration(
        db_path=str(parent / "created.sqlite"),
        synthetic_create=True,
        _before_ddl_hook=fail_before_ddl,
    )

    assert result.exit_classification == "MIGRATION_FAILED"
    assert result.migration_commit_state == "ROLLED_BACK"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700


def test_path_substitution_before_open_is_rejected_without_mutation(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    original_snapshot = _database_snapshot(db_path)
    substitute = tmp_path / "substitute.sqlite"
    substitute.touch(mode=0o600)
    substitute_snapshot = _database_snapshot(substitute)
    parked = tmp_path / "parked.sqlite"

    def substitute_before_open() -> None:
        db_path.rename(parked)
        db_path.symlink_to(substitute)

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _before_open_hook=substitute_before_open,
    )
    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "DB_PATH_REPLACED"
    assert _database_snapshot(parked) == original_snapshot
    assert _database_snapshot(substitute) == substitute_snapshot


def test_path_substitution_before_ddl_is_rejected_without_mutation(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    original_snapshot = _database_snapshot(db_path)
    substitute = tmp_path / "substitute.sqlite"
    substitute.touch(mode=0o600)
    substitute_snapshot = _database_snapshot(substitute)
    parked = tmp_path / "parked.sqlite"

    def substitute_before_ddl() -> None:
        db_path.rename(parked)
        db_path.symlink_to(substitute)

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _before_ddl_hook=substitute_before_ddl,
    )
    assert result.exit_classification == "UNSAFE_PATH"
    assert result.error_type == "DB_PATH_REPLACED"
    assert _database_snapshot(parked) == original_snapshot
    assert _database_snapshot(substitute) == substitute_snapshot


def test_owned_connection_closes_exactly_once_after_success(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    tracker = _TrackingConnection(sqlite3.connect(db_path))

    def connect_factory(*_args: Any, **_kwargs: Any) -> _TrackingConnection:
        return tracker

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _connect_factory=connect_factory,
    )
    assert result.exit_classification == "SUCCESS"
    assert tracker.commit_count == 1
    assert tracker.rollback_count == 0
    assert tracker.close_count == 1


def test_borrowed_connection_is_not_closed(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    tracker = _TrackingConnection(sqlite3.connect(db_path))
    phases, changed = healbite_schema_migrate._migrate_borrowed_connection(
        tracker,
        selected=healbite_schema_migrate.ALL_COMPONENTS,
    )
    assert changed is True
    assert len(phases) == 3
    assert tracker.close_count == 0
    assert tracker.execute("SELECT 1").fetchone() == (1,)
    tracker.close()


def test_migration_failure_rolls_back_and_closes_owned_connection(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    tracker = _TrackingConnection(sqlite3.connect(db_path))

    def connect_factory(*_args: Any, **_kwargs: Any) -> _TrackingConnection:
        return tracker

    def fail_before_ddl() -> None:
        raise RuntimeError("synthetic primary failure")

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _connect_factory=connect_factory,
        _before_ddl_hook=fail_before_ddl,
    )
    assert result.exit_classification == "MIGRATION_FAILED"
    assert result.error_type == "RuntimeError"
    assert result.migration_commit_state == "ROLLED_BACK"
    assert result.schema_may_have_changed is False
    assert result.cleanup_failed is False
    assert result.safe_to_rerun is True
    assert tracker.commit_count == 0
    assert tracker.rollback_count == 1
    assert tracker.close_count == 1


@pytest.mark.parametrize(("fail_rollback", "fail_close"), [(True, False), (False, True), (True, True)])
def test_cleanup_failures_do_not_change_primary_result_classification(
    tmp_path: Path,
    fail_rollback: bool,
    fail_close: bool,
) -> None:
    db_path = _fresh_db(tmp_path)
    tracker = _TrackingConnection(
        sqlite3.connect(db_path),
        fail_rollback=fail_rollback,
        fail_close=fail_close,
    )

    def connect_factory(*_args: Any, **_kwargs: Any) -> _TrackingConnection:
        return tracker

    def fail_before_ddl() -> None:
        raise ValueError("synthetic primary failure")

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _connect_factory=connect_factory,
        _before_ddl_hook=fail_before_ddl,
    )
    assert result.exit_classification == "MIGRATION_FAILED"
    assert result.error_type == "ValueError"
    assert result.cleanup_failed is (fail_rollback or fail_close)
    if fail_rollback:
        assert result.migration_commit_state == "UNKNOWN"
        assert result.schema_may_have_changed is True
        assert result.safe_to_rerun is False
    else:
        assert result.migration_commit_state == "ROLLED_BACK"
        assert result.schema_may_have_changed is False
        assert result.safe_to_rerun is True
    assert tracker.rollback_count == 1
    assert tracker.close_count == 1


@pytest.mark.parametrize(
    ("fail_rollback", "fail_close", "expected_notes"),
    [
        (True, False, {"rollback_failed"}),
        (False, True, {"close_failed"}),
        (True, True, {"rollback_failed", "close_failed"}),
    ],
)
def test_cleanup_failure_preserves_primary_exception_and_traceback(
    tmp_path: Path,
    fail_rollback: bool,
    fail_close: bool,
    expected_notes: set[str],
) -> None:
    db_path = _fresh_db(tmp_path)
    tracker = _TrackingConnection(
        sqlite3.connect(db_path),
        fail_rollback=fail_rollback,
        fail_close=fail_close,
    )

    def connect_factory(*_args: Any, **_kwargs: Any) -> _TrackingConnection:
        return tracker

    def raise_primary_failure() -> None:
        raise ValueError("synthetic primary failure")

    with pytest.raises(ValueError) as caught:
        healbite_schema_migrate._connect_target(
            _trusted_target(db_path),
            selected=healbite_schema_migrate.ALL_COMPONENTS,
            connect_factory=connect_factory,
            before_open_hook=None,
            before_ddl_hook=raise_primary_failure,
        )

    frame_names = {frame.name for frame in traceback.extract_tb(caught.value.__traceback__)}
    assert "raise_primary_failure" in frame_names
    assert set(getattr(caught.value, "__notes__", ())) == expected_notes
    assert tracker.rollback_count == 1
    assert tracker.close_count == 1


def test_close_failure_after_success_has_explicit_cleanup_classification(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    tracker = _TrackingConnection(sqlite3.connect(db_path), fail_close=True)

    def connect_factory(*_args: Any, **_kwargs: Any) -> _TrackingConnection:
        return tracker

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _connect_factory=connect_factory,
    )
    payload = json.dumps(result.as_dict(), sort_keys=True)
    assert result.exit_classification == "CLEANUP_FAILED"
    assert result.error_type == "CONNECTION_CLOSE_FAILED"
    assert result.migration_commit_state == "COMMITTED"
    assert result.schema_may_have_changed is True
    assert result.cleanup_failed is True
    assert result.safe_to_rerun is True
    assert tracker.commit_count == 1
    assert tracker.close_count == 1
    assert str(db_path) not in payload


def test_commit_failure_reports_unknown_state_even_after_rollback(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    tracker = _TrackingConnection(sqlite3.connect(db_path), fail_commit=True)

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _connect_factory=lambda *_args, **_kwargs: tracker,
    )

    assert result.exit_classification == "MIGRATION_FAILED"
    assert result.migration_commit_state == "UNKNOWN"
    assert result.schema_may_have_changed is True
    assert result.cleanup_failed is False
    assert result.safe_to_rerun is False
    assert tracker.commit_count == 1
    assert tracker.rollback_count == 1
    assert tracker.close_count == 1


def test_success_reports_committed_state(tmp_path: Path) -> None:
    result = _run_cli(_fresh_db(tmp_path))
    payload = _json_result(result)

    assert payload["migration_commit_state"] == "COMMITTED"
    assert payload["schema_may_have_changed"] is True
    assert payload["cleanup_failed"] is False
    assert payload["safe_to_rerun"] is True


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


@pytest.mark.parametrize(
    ("phase", "table"),
    [
        ("household", "households"),
        ("weekly", WEEKLY_MENU_SERIES_TABLE),
        ("shopping", SHOPPING_LISTS_TABLE),
    ],
)
def test_unknown_schema_fails_closed_without_any_mutation(
    tmp_path: Path,
    phase: str,
    table: str,
) -> None:
    db_path = _fresh_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY, marker TEXT NOT NULL)')
        conn.execute(f'INSERT INTO "{table}" (id, marker) VALUES (?, ?)', ("synthetic", "unchanged"))
        conn.commit()
    schema_before = _schema_snapshot(db_path)
    data_before = _database_snapshot(db_path)
    result = _run_cli(db_path)
    payload = _json_result(result)
    assert result.returncode == healbite_schema_migrate.EXIT_SCHEMA_PRECONDITION
    assert payload["exit_classification"] == "INCOMPATIBLE_SCHEMA"
    assert payload["phases"][0]["name"] == phase
    assert payload["phases"][0]["status"] == "failed"
    assert len(payload["phases"]) == 1
    assert _schema_snapshot(db_path) == schema_before
    assert _database_snapshot(db_path) == data_before
    assert len(_table_names(db_path)) == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.total_changes == 0


def test_known_prefixes_of_authoritative_schema_are_recoverable(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute(HealBiteHouseholdStore.schema_statements()[0])
        conn.commit()
    assert _run_cli(db_path).returncode == 0

    weekly_db = tmp_path / "weekly-prefix.sqlite"
    weekly_db.touch(mode=0o600)
    _init_household(weekly_db)
    with sqlite3.connect(weekly_db) as conn:
        conn.execute(HealBiteWeeklyMenuStore.schema_statements()[0])
        conn.commit()
    assert _run_cli(weekly_db).returncode == 0

    shopping_db = tmp_path / "shopping-prefix.sqlite"
    shopping_db.touch(mode=0o600)
    _init_household(shopping_db)
    _init_weekly(shopping_db)
    with sqlite3.connect(shopping_db) as conn:
        conn.execute(HealBiteShoppingStore.schema_statements()[0])
        conn.commit()
    assert _run_cli(shopping_db).returncode == 0


def test_structurally_valid_nonprefix_partial_schema_is_incompatible(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    revision_statement = next(
        statement
        for statement in HealBiteWeeklyMenuStore.schema_statements()
        if statement.lstrip().startswith("CREATE TABLE IF NOT EXISTS household_weekly_menus")
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(revision_statement)
        conn.commit()
    before = _schema_snapshot(db_path)
    result = _run_cli(db_path)
    assert result.returncode == healbite_schema_migrate.EXIT_SCHEMA_PRECONDITION
    assert _json_result(result)["exit_classification"] == "INCOMPATIBLE_SCHEMA"
    assert _schema_snapshot(db_path) == before


@pytest.mark.parametrize("failure_phase", ["household", "weekly"])
def test_late_phase_failure_rolls_back_all_component_ddl(
    tmp_path: Path,
    monkeypatch,
    failure_phase: str,
) -> None:
    db_path = _fresh_db(tmp_path)
    schema_before = _schema_snapshot(db_path)
    called: list[str] = []
    real_apply = healbite_schema_migrate._apply_component

    def fail_after_component(conn: sqlite3.Connection, name: str, statements: tuple[str, ...]) -> None:
        called.append(name)
        real_apply(conn, name, statements)
        if name == failure_phase:
            failure = RuntimeError("synthetic phase failure")
            failure._healbite_migration_phase = name
            raise failure

    monkeypatch.setattr(healbite_schema_migrate, "_apply_component", fail_after_component)
    result = _run_cli(db_path)
    assert result.returncode == healbite_schema_migrate.EXIT_MIGRATION_FAILURE
    assert _json_result(result)["phases"] == [
        {"changed": False, "error_type": "RuntimeError", "name": failure_phase, "status": "failed"}
    ]
    assert _schema_snapshot(db_path) == schema_before
    expected_calls = ["household"] if failure_phase == "household" else ["household", "weekly"]
    assert called == expected_calls


def test_locked_database_has_stable_exit_classification(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    lock = sqlite3.connect(db_path)
    try:
        lock.execute("BEGIN EXCLUSIVE")
        result = healbite_schema_migrate.run_migration(
            db_path=str(db_path),
            _target_resolver=_trusted_resolver,
        )
    finally:
        lock.rollback()
        lock.close()
    assert result.exit_classification == "DATABASE_LOCKED"
    assert result.exit_code == healbite_schema_migrate.EXIT_DATABASE_LOCKED


def test_read_only_database_has_stable_exit_classification(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)

    def read_only_factory(path: Path, **_kwargs: Any) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=0.2)

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _connect_factory=read_only_factory,
    )
    assert result.exit_classification == "DATABASE_READ_ONLY"
    assert result.exit_code == healbite_schema_migrate.EXIT_DATABASE_READ_ONLY


def test_permission_denied_has_stable_exit_classification(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)

    def denied_factory(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        raise _sqlite_operational_error(sqlite3.SQLITE_CANTOPEN, "SQLITE_CANTOPEN")

    result = healbite_schema_migrate.run_migration(
        db_path=str(db_path),
        _target_resolver=_trusted_resolver,
        _connect_factory=denied_factory,
    )
    assert result.exit_classification == "DATABASE_PERMISSION_DENIED"
    assert result.exit_code == healbite_schema_migrate.EXIT_DATABASE_PERMISSION_DENIED


def test_missing_database_precedes_read_only_parent(tmp_path: Path) -> None:
    parent = tmp_path / "read-only"
    parent.mkdir(mode=0o500)
    os.chmod(parent, 0o500)
    try:
        result = healbite_schema_migrate.run_migration(db_path=str(parent / "missing.sqlite"))
    finally:
        os.chmod(parent, 0o700)

    assert result.exit_classification == "MISSING_DATABASE"


def test_locked_database_preserves_primary_classification_when_close_fails(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    lock = sqlite3.connect(db_path)
    tracker = _TrackingConnection(sqlite3.connect(db_path, timeout=0.2), fail_close=True)
    try:
        lock.execute("BEGIN EXCLUSIVE")
        result = healbite_schema_migrate.run_migration(
            db_path=str(db_path),
            _target_resolver=_trusted_resolver,
            _connect_factory=lambda *_args, **_kwargs: tracker,
        )
    finally:
        lock.rollback()
        lock.close()

    assert result.exit_classification == "DATABASE_LOCKED"
    assert result.cleanup_failed is True
    assert result.migration_commit_state == "ROLLED_BACK"
    assert result.safe_to_rerun is True


def test_sqlite_classification_does_not_use_error_message_only() -> None:
    error = sqlite3.OperationalError("database is locked")
    assert healbite_schema_migrate._sqlite_classification(error) is healbite_schema_migrate.ExitClassification.MIGRATION_FAILED


def test_exit_precedence_has_one_stable_source_of_truth() -> None:
    expected = (
        "INVALID_ARGUMENT",
        "UNSAFE_PATH",
        "MISSING_DATABASE",
        "DATABASE_PERMISSION_DENIED",
        "DATABASE_READ_ONLY",
        "DATABASE_LOCKED",
        "INCOMPATIBLE_SCHEMA",
        "MIGRATION_FAILED",
        "CLEANUP_FAILED",
        "CONTRACT_DRIFT",
    )
    assert tuple(item.value for item in healbite_schema_migrate.EXIT_CLASSIFICATION_PRECEDENCE) == expected
    assert (
        healbite_schema_migrate._select_exit_classification(
            healbite_schema_migrate.ExitClassification.DATABASE_LOCKED,
            healbite_schema_migrate.ExitClassification.CLEANUP_FAILED,
        )
        is healbite_schema_migrate.ExitClassification.DATABASE_LOCKED
    )


def test_cli_help_documents_public_exit_classifications() -> None:
    help_text = healbite_schema_migrate.build_parser().format_help()
    for classification in healbite_schema_migrate.ExitClassification:
        assert classification.value in help_text
    precedence_positions = [help_text.index(item.value) for item in healbite_schema_migrate.EXIT_CLASSIFICATION_PRECEDENCE]
    assert precedence_positions == sorted(precedence_positions)


def test_component_order_is_enforced(tmp_path: Path) -> None:
    db_path = _fresh_db(tmp_path)
    result = _run_cli(db_path, "--components", "shopping,weekly")
    assert result.returncode == healbite_schema_migrate.EXIT_INVALID_ARGUMENTS
    payload = _json_result(result)
    assert payload["exit_classification"] == "INVALID_ARGUMENT"
    assert payload["error_type"] == "COMPONENTS_OUT_OF_ORDER"


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
    runbook = (
        Path(__file__).resolve().parents[2]
        / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
    ).read_text(encoding="utf-8")
    d3 = runbook.split("### D3 Weekly schema initialization", 1)[1].split(
        "## D4 - Disabled-State Observation", 1
    )[0]
    assert "scripts/healbite_schema_migrate.py" in d3
    assert "store.initialize_schema()" not in d3
    assert "docker exec" not in d3
    assert "python - <<" not in d3
    assert "--allow-create" not in d3
    assert "synthetic-create and protected-existing modes are forbidden" in d3
    assert ":latest" not in d3
    assert "only the disposable staging directory is mounted read/write" in d3
    assert "production DB path and production parent are not mounted" in d3
    assert "--staged-copy" in d3
    assert "PATH_MODE=STAGED_COPY" in d3
    assert "production execute mode is deliberately absent" in d3
    assert "Direct in-place" in d3
    assert "DATABASE_LOCKED" in d3
    assert "DATABASE_READ_ONLY" in d3
    assert "CLEANUP_FAILED" in d3
    assert "deterministic precedence" in d3
    assert "migration_commit_state = COMMITTED" in d3
    assert "Do not retry automatically" in d3


def test_runbook_documents_fail_closed_sqlite_classification_contract() -> None:
    runbook = (
        Path(__file__).resolve().parents[2]
        / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
    ).read_text(encoding="utf-8")
    d3 = runbook.split("## D3 - Explicit Weekly Then Shopping Schema Initialization", 1)[1].split(
        "## D4 - Disabled-State Observation", 1
    )[0]
    normalized_d3 = " ".join(d3.split())

    assert "sqlite_errorcode" in d3
    assert "sqlite_errorname" in d3
    assert "Exception-message text is never used" in normalized_d3
    assert "MIGRATION_FAILED" in d3
    assert "sanitized message matching" not in d3


def test_runbook_has_no_secondary_production_migration_interface() -> None:
    runbook = (
        Path(__file__).resolve().parents[2]
        / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
    ).read_text(encoding="utf-8")
    assert "docker exec" not in runbook
    assert "python - <<" not in runbook
    assert "--mount type=bind,src=\"/home/hermes/healbite.db\"" not in runbook


def test_runbook_documents_staged_atomic_publish_and_rollback_boundaries() -> None:
    runbook = (
        Path(__file__).resolve().parents[2]
        / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
    ).read_text(encoding="utf-8")
    required = (
        "staged copy plus atomic publish",
        "scripts/hermes_staged_schema_migrate.py",
        "production execution is disabled",
        "normal SQLite DELETE journaling and synchronous FULL remain enabled",
        "same-filesystem",
        "renameat2(..., RENAME_EXCHANGE)",
        "There is no `os.replace` fallback",
        "PUBLISH_STATE=EXCHANGE_STARTED",
        "PUBLISH_STATE=FINAL_VERIFIED",
        "exclusive SQLite-compatible source lease",
        "second exclusive SQLite-compatible lease",
        "reverse exchange",
        "operation-owned staging tree",
        "automatic staging deletion is forbidden",
        "canonical /init entrypoint",
        "automatic retry is prohibited",
        "Image rollback after a successful additive migration uses the migrated DB",
        "Backup restore is an emergency manual action only",
    )
    for marker in required:
        assert marker in runbook
