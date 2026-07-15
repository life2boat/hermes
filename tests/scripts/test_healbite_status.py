from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.healbite_shopping_schema import SHOPPING_SCHEMA_SQL, ShoppingSchemaState
from gateway.healbite_weekly_menu_schema import WEEKLY_MENU_SCHEMA_SQL, WeeklyMenuSchemaState


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "healbite_status.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("healbite_status", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
healbite_status = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = healbite_status
SCRIPT_SPEC.loader.exec_module(healbite_status)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_base_db(path: Path, *, include_users: bool = False) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE households (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE household_members (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE nutrition_log (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)")
        conn.execute("INSERT INTO nutrition_log (user_id) VALUES (1)")
        if include_users:
            conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")


def _add_audit_triggers(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE audit_events (kind TEXT NOT NULL)")
        for table in ("households", "household_members", "nutrition_log"):
            for op in ("INSERT", "UPDATE", "DELETE"):
                conn.execute(
                    f'''
                    CREATE TRIGGER {table}_{op.lower()}_audit
                    AFTER {op} ON {table}
                    BEGIN
                        INSERT INTO audit_events(kind) VALUES ('{table}:{op.lower()}');
                    END
                    '''
                )


def _canonical_weekly_db(path: Path) -> None:
    _make_base_db(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(WEEKLY_MENU_SCHEMA_SQL)


def _canonical_shopping_db(path: Path) -> None:
    _canonical_weekly_db(path)
    with sqlite3.connect(path) as conn:
        conn.executescript(SHOPPING_SCHEMA_SQL)


def test_authorizer_proves_status_attempts_no_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "status.db"
    _canonical_shopping_db(db_path)

    write_actions = {
        getattr(sqlite3, name)
        for name in (
            "SQLITE_INSERT",
            "SQLITE_UPDATE",
            "SQLITE_DELETE",
            "SQLITE_CREATE_INDEX",
            "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX",
            "SQLITE_CREATE_TEMP_TABLE",
            "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW",
            "SQLITE_CREATE_TRIGGER",
            "SQLITE_CREATE_VIEW",
            "SQLITE_CREATE_VTABLE",
            "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE",
            "SQLITE_DROP_TEMP_INDEX",
            "SQLITE_DROP_TEMP_TABLE",
            "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW",
            "SQLITE_DROP_TRIGGER",
            "SQLITE_DROP_VIEW",
            "SQLITE_DROP_VTABLE",
            "SQLITE_ALTER_TABLE",
            "SQLITE_REINDEX",
            "SQLITE_ANALYZE",
            "SQLITE_ATTACH",
            "SQLITE_DETACH",
        )
        if hasattr(sqlite3, name)
    }
    denied: list[int] = []

    def _authorizer(action: int, _arg1, _arg2, _db_name, _source) -> int:
        if action in write_actions:
            denied.append(action)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    with healbite_status.open_read_only_db(db_path) as conn:
        conn.set_authorizer(_authorizer)
        result = healbite_status.inspect_db_connection(conn, db_path=db_path)

    assert denied == []
    assert result["sqlite_total_changes"] == 0
    assert result["weekly_schema"] == WeeklyMenuSchemaState.CANONICAL.value
    assert result["shopping_schema"] == ShoppingSchemaState.CANONICAL.value


def test_status_creates_no_new_journal_or_wal_and_preserves_file_bytes(tmp_path: Path) -> None:
    db_path = tmp_path / "status.db"
    _make_base_db(db_path, include_users=True)
    before_names = {item.name for item in tmp_path.iterdir()}
    before_hash = _sha256(db_path)
    before_size = db_path.stat().st_size
    with sqlite3.connect(db_path) as conn:
        before_schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        before_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        before_application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])

    result = healbite_status.inspect_db_path(db_path)

    after_names = {item.name for item in tmp_path.iterdir()}
    after_hash = _sha256(db_path)
    after_size = db_path.stat().st_size
    with sqlite3.connect(db_path) as conn:
        after_schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        after_user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        after_application_id = int(conn.execute("PRAGMA application_id").fetchone()[0])

    created = after_names - before_names
    assert not any(name.endswith(("-wal", "-shm", "-journal")) for name in created)
    assert before_hash == after_hash
    assert before_size == after_size
    assert before_schema_version == after_schema_version == result["schema_version"]
    assert before_user_version == after_user_version == result["user_version"]
    assert before_application_id == after_application_id == result["application_id"]
    assert result["sqlite_total_changes"] == 0


def test_status_triggers_no_audit_events(tmp_path: Path) -> None:
    db_path = tmp_path / "status.db"
    _make_base_db(db_path, include_users=True)
    _add_audit_triggers(db_path)

    result = healbite_status.inspect_db_path(db_path)

    with sqlite3.connect(db_path) as conn:
        audit_events = int(conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0])
    assert result["sqlite_total_changes"] == 0
    assert audit_events == 0


def test_missing_optional_tables_report_safe_states(tmp_path: Path) -> None:
    db_path = tmp_path / "status.db"
    _make_base_db(db_path, include_users=False)

    result = healbite_status.inspect_db_path(db_path)

    assert result["profiles_table_present"] is False
    assert result["weekly_schema"] == WeeklyMenuSchemaState.NOT_INITIALIZED.value
    assert result["shopping_schema"] == ShoppingSchemaState.DEPENDENCY_MISSING.value
    assert result["weekly_tables"] == 0
    assert result["shopping_tables"] == 0
    assert result["weekly_business_rows"] == 0
    assert result["shopping_business_rows"] == 0


def test_partial_and_incompatible_weekly_schema_are_reported_without_mutation(tmp_path: Path) -> None:
    partial_db = tmp_path / "weekly-partial.db"
    _make_base_db(partial_db)
    with sqlite3.connect(partial_db) as conn:
        conn.execute(next(statement.strip() for statement in WEEKLY_MENU_SCHEMA_SQL.split(";") if statement.strip()))
    partial = healbite_status.inspect_db_path(partial_db)
    assert partial["weekly_schema"] == WeeklyMenuSchemaState.PARTIAL.value
    assert partial["shopping_schema"] == ShoppingSchemaState.DEPENDENCY_MISSING.value

    incompatible_db = tmp_path / "weekly-incompatible.db"
    _make_base_db(incompatible_db)
    with sqlite3.connect(incompatible_db) as conn:
        conn.executescript(
            '''
            CREATE TABLE household_weekly_menu_series (id TEXT PRIMARY KEY);
            CREATE TABLE household_weekly_menus (id TEXT PRIMARY KEY);
            CREATE TABLE household_weekly_menu_entries (id TEXT PRIMARY KEY);
            CREATE TABLE household_weekly_menu_idempotency (id TEXT PRIMARY KEY);
            '''
        )
    incompatible = healbite_status.inspect_db_path(incompatible_db)
    assert incompatible["weekly_schema"] == WeeklyMenuSchemaState.INCOMPATIBLE.value
    assert incompatible["shopping_schema"] == ShoppingSchemaState.DEPENDENCY_MISSING.value


def test_partial_and_incompatible_shopping_schema_are_reported_without_mutation(tmp_path: Path) -> None:
    partial_db = tmp_path / "shopping-partial.db"
    _canonical_weekly_db(partial_db)
    with sqlite3.connect(partial_db) as conn:
        conn.execute(next(statement.strip() for statement in SHOPPING_SCHEMA_SQL.split(";") if statement.strip()))
    partial = healbite_status.inspect_db_path(partial_db)
    assert partial["weekly_schema"] == WeeklyMenuSchemaState.CANONICAL.value
    assert partial["shopping_schema"] == ShoppingSchemaState.PARTIAL.value

    incompatible_db = tmp_path / "shopping-incompatible.db"
    _canonical_weekly_db(incompatible_db)
    with sqlite3.connect(incompatible_db) as conn:
        conn.executescript(
            '''
            CREATE TABLE household_shopping_lists (id TEXT PRIMARY KEY);
            CREATE TABLE household_shopping_items (id TEXT PRIMARY KEY);
            CREATE TABLE household_shopping_idempotency (id TEXT PRIMARY KEY);
            '''
        )
    incompatible = healbite_status.inspect_db_path(incompatible_db)
    assert incompatible["shopping_schema"] == ShoppingSchemaState.INCOMPATIBLE.value


@pytest.mark.parametrize(
    ("path_value", "error_type"),
    [
        ("", "DB_PATH_EMPTY"),
        ("file:/tmp/test.db?mode=ro", "DB_PATH_UNSAFE"),
        ("http://example.com/test.db", "DB_PATH_UNSAFE"),
    ],
)
def test_validate_status_db_path_rejects_unsafe_values(path_value: str, error_type: str) -> None:
    with pytest.raises(healbite_status.StatusDBError) as excinfo:
        healbite_status.validate_status_db_path(path_value)
    assert excinfo.value.error_type == error_type


def test_status_rejects_missing_directory_invalid_and_locked_db_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing_db = tmp_path / "missing.db"
    with pytest.raises(healbite_status.StatusDBError) as excinfo:
        healbite_status.inspect_db_path(missing_db)
    assert excinfo.value.error_type == "DB_PATH_MISSING"

    with pytest.raises(healbite_status.StatusDBError) as excinfo:
        healbite_status.inspect_db_path(tmp_path)
    assert excinfo.value.error_type == "DB_PATH_NOT_REGULAR"

    invalid_db = tmp_path / "invalid.db"
    invalid_db.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(healbite_status.StatusDBError) as excinfo:
        healbite_status.inspect_db_path(invalid_db)
    assert excinfo.value.error_type == "DB_INVALID_SQLITE"

    real_connect = healbite_status.sqlite3.connect

    def _locked_connect(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(healbite_status.sqlite3, "connect", _locked_connect)
    with pytest.raises(healbite_status.StatusDBError) as excinfo:
        healbite_status.open_read_only_db(invalid_db)
    assert excinfo.value.error_type == "DB_LOCKED"
    monkeypatch.setattr(healbite_status.sqlite3, "connect", real_connect)


def test_collect_status_snapshot_classifies_provider_markers_without_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "status.db"
    _make_base_db(db_path)
    monkeypatch.setattr(healbite_status, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(healbite_status, "get_config_path", lambda: str(tmp_path / "config.yaml"))
    cfg = SimpleNamespace(platforms={})

    snapshot = healbite_status.collect_status_snapshot(
        db_path=db_path,
        env={},
        config_loader=lambda: cfg,
        check_vision_fn=lambda: False,
    )
    runtime = snapshot["runtime"]
    assert runtime["provider_calls"] == 0
    assert runtime["provider_call_failures"] == 0
    assert runtime["provider_auth_failures"] == 0
    assert runtime["provider_unavailable_without_call"] == 0
    assert runtime["provider_not_configured"] == 1
    assert runtime["generation_calls"] == 0


def test_provider_marker_classification_distinguishes_actual_failure_and_auth_failure() -> None:
    failed = healbite_status.classify_provider_markers(
        provider_call_attempted=True,
        provider_call_failed=True,
        provider_auth_failed=False,
        provider_available=True,
        provider_configured=True,
        generation_invoked=True,
    )
    assert failed.as_dict() == {
        "provider_calls": 1,
        "provider_call_failures": 1,
        "provider_auth_failures": 0,
        "provider_unavailable_without_call": 0,
        "provider_not_configured": 0,
        "generation_calls": 1,
    }

    auth_failed = healbite_status.classify_provider_markers(
        provider_call_attempted=True,
        provider_call_failed=True,
        provider_auth_failed=True,
        provider_available=True,
        provider_configured=True,
        generation_invoked=True,
    )
    assert auth_failed.provider_calls == 1
    assert auth_failed.provider_call_failures == 1
    assert auth_failed.provider_auth_failures == 1


def test_feature_disabled_provider_markers_stay_zero() -> None:
    markers = healbite_status.classify_provider_markers(
        provider_call_attempted=False,
        provider_available=True,
        provider_configured=True,
        generation_invoked=False,
    )
    assert markers.as_dict() == {
        "provider_calls": 0,
        "provider_call_failures": 0,
        "provider_auth_failures": 0,
        "provider_unavailable_without_call": 0,
        "provider_not_configured": 0,
        "generation_calls": 0,
    }


def test_collect_status_snapshot_marks_provider_unavailable_without_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "status.db"
    _make_base_db(db_path)
    monkeypatch.setattr(healbite_status, "get_hermes_home", lambda: str(tmp_path / "hermes-home"))
    monkeypatch.setattr(healbite_status, "get_config_path", lambda: str(tmp_path / "config.yaml"))
    cfg = SimpleNamespace(platforms={})

    snapshot = healbite_status.collect_status_snapshot(
        db_path=db_path,
        env={"GEMINI_API_KEY": "present"},
        config_loader=lambda: cfg,
        check_vision_fn=lambda: False,
    )
    runtime = snapshot["runtime"]
    assert runtime["provider_calls"] == 0
    assert runtime["provider_call_failures"] == 0
    assert runtime["provider_auth_failures"] == 0
    assert runtime["provider_unavailable_without_call"] == 1
    assert runtime["provider_not_configured"] == 0
    assert runtime["generation_calls"] == 0
