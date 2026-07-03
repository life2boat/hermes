
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from gateway.healbite_household_bootstrap import audit_household_db
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, HOUSEHOLDS_TABLE, new_household_id, new_household_member_id
from gateway.healbite_households import HealBiteHouseholdStore

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "household_db_audit.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("household_db_audit", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
household_db_audit = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = household_db_audit
SCRIPT_SPEC.loader.exec_module(household_db_audit)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _users(db_path: Path, ids=(101, 202)) -> None:
    with _connect(db_path) as conn:
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT)")
        for actor_id in ids:
            conn.execute("INSERT INTO users (user_id, username) VALUES (?, 'synthetic')", (actor_id,))


def _eligible_file(tmp_path: Path, *ids: int | str) -> Path:
    path = tmp_path / "eligible.txt"
    path.write_text("\n".join(str(item) for item in ids) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_audit_missing_path_does_not_create_file(tmp_path):
    db_path = tmp_path / "missing.db"

    result = audit_household_db(db_path)

    assert result["result"] == "failed"
    assert result["error_type"] == "DB_NOT_FOUND"
    assert not db_path.exists()


def test_audit_not_initialized_is_read_only_and_unverified(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    before = _hash(db_path)

    first = audit_household_db(db_path)
    second = audit_household_db(db_path)

    assert first == second
    assert first["result"] == "success"
    assert first["schema_state"] == "not_initialized"
    assert first["integrity"] == "ok"
    assert first["identity_column"] == "user_id"
    assert first["eligibility_state"] == "unverified"
    assert first["eligible_users_total"] is None
    assert _hash(db_path) == before


def test_audit_canonical_empty_and_unknown_tables_are_safe(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    HealBiteHouseholdStore(db_path=db_path)
    with _connect(db_path) as conn:
        conn.execute("CREATE TABLE unrelated_table (id INTEGER PRIMARY KEY)")

    result = audit_household_db(db_path, _eligible_file(tmp_path, 101, 202))

    assert result["result"] == "success"
    assert result["schema_state"] == "canonical"
    assert result["households_total"] == 0
    assert result["members_total"] == 0
    assert result["eligible_users_total"] == 2
    assert result["eligible_users_missing"] == 2


@pytest.mark.parametrize(
    ("setup", "state"),
    [
        ("household_only", "partial"),
        ("member_only", "partial"),
        ("missing_index", "partial"),
        ("unexpected_shape", "unexpected"),
    ],
)
def test_audit_schema_state_matrix(tmp_path, setup, state):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    if setup == "household_only":
        with _connect(db_path) as conn:
            conn.execute(f"CREATE TABLE {HOUSEHOLDS_TABLE} (id TEXT PRIMARY KEY)")
    elif setup == "member_only":
        with _connect(db_path) as conn:
            conn.execute(f"CREATE TABLE {HOUSEHOLD_MEMBERS_TABLE} (id TEXT PRIMARY KEY)")
    elif setup == "missing_index":
        HealBiteHouseholdStore(db_path=db_path)
        with _connect(db_path) as conn:
            conn.execute("DROP INDEX idx_household_members_active_owner")
    else:
        with _connect(db_path) as conn:
            conn.execute(f"CREATE TABLE {HOUSEHOLDS_TABLE} (id TEXT PRIMARY KEY)")
            conn.execute(f"CREATE TABLE {HOUSEHOLD_MEMBERS_TABLE} (id TEXT PRIMARY KEY)")

    result = audit_household_db(db_path)

    assert result["result"] == "failed"
    assert result["schema_state"] == state


def test_audit_detects_conflicts_even_when_constraints_bypassed(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101, 202))
    first = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET owner_user_id=202 WHERE id=?", (first.household.id,))
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, 202, 'primary', 'owner', 'active', 't', 't')
            """,
            (new_household_member_id(), new_household_id()),
        )

    result = audit_household_db(db_path, _eligible_file(tmp_path, 101, 202))

    assert result["owner_pointer_mismatches"] >= 1
    assert result["orphan_members"] >= 1


def test_audit_corrupt_db_safe_failure(tmp_path):
    db_path = tmp_path / "healbite.db"
    db_path.write_bytes(b"not sqlite")

    result = audit_household_db(db_path)

    assert result["result"] == "failed"
    assert result["error_type"] in {"SQLITE_DATABASE_ERROR", "SQLITE_INTEGRITY_CHECK_FAILED"}
    assert result["integrity"] == "failed"


def test_audit_cli_json_does_not_expose_ids_or_uuid(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(3131313131,))
    personal = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(3131313131)
    eligible = _eligible_file(tmp_path, 3131313131)
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        assert household_db_audit.main(["--db", str(db_path), "--eligible-users-file", str(eligible), "--json"]) == 0

    payload = output.getvalue()
    assert "3131313131" not in payload
    assert personal.household.id not in payload
    assert personal.member.id not in payload
    assert str(eligible) not in payload
    result = json.loads(payload)
    assert result["eligible_users_total"] == 1



def _create_constraintless_household_schema(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE {HOUSEHOLDS_TABLE} (
                id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                name TEXT NULL,
                status TEXT NOT NULL,
                default_timezone TEXT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE {HOUSEHOLD_MEMBERS_TABLE} (
                id TEXT PRIMARY KEY,
                household_id TEXT NOT NULL,
                linked_user_id INTEGER NULL,
                display_name TEXT NULL,
                member_type TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                age_band TEXT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                version INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            f"CREATE UNIQUE INDEX idx_household_members_active_linked_user ON {HOUSEHOLD_MEMBERS_TABLE} (linked_user_id) WHERE linked_user_id IS NOT NULL AND status='active'"
        )
        conn.execute(
            f"CREATE UNIQUE INDEX idx_household_members_active_owner ON {HOUSEHOLD_MEMBERS_TABLE} (household_id) WHERE role='owner' AND status='active'"
        )


def test_audit_detects_invalid_uuid_version_enum_in_constraintless_fixture(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101,))
    _create_constraintless_household_schema(db_path)
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLDS_TABLE} (id, owner_user_id, status, created_at, updated_at, version)
            VALUES ('not-a-uuid', 101, 'archived', 't', 't', 0)
            """
        )
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at, version)
            VALUES ('also-bad', 'not-a-uuid', 101, 'bogus', 'owner', 'active', 't', 't', 0)
            """
        )

    result = audit_household_db(db_path)

    assert result["schema_state"] == "canonical"
    assert result["invalid_uuid"] >= 2
    assert result["invalid_version"] >= 2
    assert result["invalid_enum"] >= 2


def test_audit_detects_duplicate_linked_users_when_index_missing(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101,))
    first = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    with _connect(db_path) as conn:
        conn.execute("DROP INDEX idx_household_members_active_linked_user")
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, 101, 'linked_adult', 'adult_member', 'active', 't', 't')
            """,
            (new_household_member_id(), first.household.id),
        )

    result = audit_household_db(db_path)

    assert result["schema_state"] == "partial"
    assert result["duplicate_active_linked_users"] == 1


def test_audit_detects_active_member_in_non_active_household(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101,))
    first = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET status='disabled' WHERE id=?", (first.household.id,))

    result = audit_household_db(db_path)

    assert result["active_member_in_non_active_household"] == 1


def test_audit_permission_denied_like_error_is_sanitized(monkeypatch, tmp_path):
    db_path = tmp_path / "healbite.db"
    db_path.write_bytes(b"placeholder")

    def deny(_path):
        raise sqlite3.OperationalError("permission denied CANARY_PRIVATE_PATH")

    monkeypatch.setattr("gateway.healbite_household_bootstrap._read_only_connect", deny)
    result = audit_household_db(db_path)

    payload = json.dumps(result, sort_keys=True)
    assert result["error_type"] == "SQLITE_DATABASE_ERROR"
    assert "CANARY_PRIVATE_PATH" not in payload
    assert str(db_path) not in payload
