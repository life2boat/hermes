
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

from gateway.healbite_household_bootstrap import bootstrap_households
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, HOUSEHOLDS_TABLE
from gateway.healbite_households import HealBiteHouseholdStore

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "household_bootstrap.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("household_bootstrap", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
household_bootstrap = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = household_bootstrap
SCRIPT_SPEC.loader.exec_module(household_bootstrap)


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


def _counts(db_path: Path) -> tuple[int, int]:
    with _connect(db_path) as conn:
        return (
            int(conn.execute(f"SELECT COUNT(*) FROM {HOUSEHOLDS_TABLE}").fetchone()[0]),
            int(conn.execute(f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE}").fetchone()[0]),
        )


def test_dry_run_default_does_not_initialize_or_write(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    before = _hash(db_path)

    result = bootstrap_households(db_path)

    assert result["mode"] == "dry-run"
    assert result["error_type"] == "SCHEMA_NOT_INITIALIZED"
    assert result["initializer_called"] is False
    assert _hash(db_path) == before
    with _connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert HOUSEHOLDS_TABLE not in tables
    assert HOUSEHOLD_MEMBERS_TABLE not in tables


def test_apply_missing_schema_refused_without_writes(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)

    result = bootstrap_households(db_path, apply=True, eligible_users_file=_eligible_file(tmp_path, 101))

    assert result["exit_code"] == 4
    assert result["verdict"] == "SCHEMA NOT INITIALIZED ? APPLY REFUSED"


def test_apply_without_eligibility_policy_refused_when_metadata_insufficient(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    HealBiteHouseholdStore(db_path=db_path)

    result = bootstrap_households(db_path, apply=True)

    assert result["exit_code"] == 6
    assert result["eligibility_state"] == "unverified"
    assert result["verdict"] == "ELIGIBILITY POLICY REQUIRED ? APPLY REFUSED"
    assert _counts(db_path) == (0, 0)


@pytest.mark.parametrize("lines", [["101", "101"], ["101", "999"], ["-1"], ["9223372036854775808"]])
def test_eligible_file_validation_fails_closed(tmp_path, lines):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101,))
    HealBiteHouseholdStore(db_path=db_path)
    eligible = _eligible_file(tmp_path, *lines)

    result = bootstrap_households(db_path, apply=True, eligible_users_file=eligible)

    assert result["exit_code"] == 6
    assert result["eligibility_state"] == "invalid"
    assert _counts(db_path) == (0, 0)


def test_clean_first_apply_and_second_apply_zero_delta(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101, 202, 303))
    HealBiteHouseholdStore(db_path=db_path)
    eligible = _eligible_file(tmp_path, 101, 202, 303)

    first = bootstrap_households(db_path, apply=True, eligible_users_file=eligible, batch_size=2)
    after_first = _counts(db_path)
    second = bootstrap_households(db_path, apply=True, eligible_users_file=eligible, batch_size=2)

    assert first["result"] == "success"
    assert first["created_count"] == 3
    assert first["already_existing_count"] == 0
    assert first["batch_count"] == 2
    assert after_first == (3, 3)
    assert second["result"] == "success"
    assert second["created_count"] == 0
    assert second["already_existing_count"] == 3
    assert _counts(db_path) == after_first
    assert second["post_owner_pointer_mismatches"] == 0
    assert second["post_duplicate_active_linked_users"] == 0
    assert second["post_households_without_owner"] == 0
    assert second["post_orphan_members"] == 0


def test_interrupted_resume_is_idempotent(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101, 202))
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    eligible = _eligible_file(tmp_path, 101, 202)

    result = bootstrap_households(db_path, apply=True, eligible_users_file=eligible)

    assert result["created_count"] == 1
    assert result["already_existing_count"] == 1
    assert _counts(db_path) == (2, 2)


def test_apply_conflict_preflight_refuses_before_writes(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101, 202))
    first = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET owner_user_id=202 WHERE id=?", (first.household.id,))
    eligible = _eligible_file(tmp_path, 101, 202)
    before = _counts(db_path)

    result = bootstrap_households(db_path, apply=True, eligible_users_file=eligible)

    assert result["exit_code"] == 7
    assert result["verdict"] == "HOUSEHOLD STATE CONFLICT ? APPLY REFUSED"
    assert _counts(db_path) == before


def test_missing_path_and_production_path_guard(tmp_path):
    missing = tmp_path / "missing.db"
    assert bootstrap_households(missing)["exit_code"] == 3
    guarded = bootstrap_households("/home/hermes/healbite.db", apply=True, eligible_users_file=tmp_path / "none")
    assert guarded["error_type"] == "PRODUCTION_PATH_APPLY_REFUSED"


def test_production_path_guard_blocks_initializer_and_aliases(monkeypatch, tmp_path):
    production = tmp_path / "prod.db"
    _users(production)
    HealBiteHouseholdStore(db_path=production)
    monkeypatch.setattr("gateway.healbite_household_bootstrap.PRODUCTION_DB_PATH", production)

    direct = bootstrap_households(production, initialize_schema=True)
    assert direct["error_type"] == "PRODUCTION_PATH_APPLY_REFUSED"

    alias = tmp_path / "alias.db"
    try:
        alias.symlink_to(production)
    except (OSError, NotImplementedError):
        alias = None
    if alias is not None:
        result = bootstrap_households(alias, apply=True, eligible_users_file=_eligible_file(tmp_path, 101))
        assert result["error_type"] == "PRODUCTION_PATH_APPLY_REFUSED"

    hardlink = tmp_path / "hardlink.db"
    try:
        hardlink.hardlink_to(production)
    except (OSError, NotImplementedError):
        hardlink = None
    if hardlink is not None:
        result = bootstrap_households(hardlink, apply=True, eligible_users_file=_eligible_file(tmp_path, 101))
        assert result["error_type"] == "PRODUCTION_PATH_APPLY_REFUSED"


def test_cli_no_production_override_flag(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)

    with pytest.raises(SystemExit) as exc:
        household_bootstrap.main(["--db", str(db_path), "--allow-production-path"])

    assert exc.value.code == 2


def test_invalid_batch_size_exit_code(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    assert bootstrap_households(db_path, batch_size=0)["exit_code"] == 2
    assert bootstrap_households(db_path, batch_size=1001)["exit_code"] == 2


def test_cli_json_is_safe_and_hides_ids_file_and_uuids(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(3131313131,))
    HealBiteHouseholdStore(db_path=db_path)
    eligible = _eligible_file(tmp_path, 3131313131)
    output = io.StringIO()

    with contextlib.redirect_stdout(output):
        assert household_bootstrap.main(["--db", str(db_path), "--apply", "--eligible-users-file", str(eligible), "--json"]) == 0

    payload = output.getvalue()
    assert "3131313131" not in payload
    assert str(eligible) not in payload
    result = json.loads(payload)
    assert result["created_count"] == 1
    assert result["result"] == "success"

@pytest.mark.parametrize("mode", [0o640, 0o604, 0o777])
def test_eligible_file_permissions_fail_closed(tmp_path, mode):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101,))
    HealBiteHouseholdStore(db_path=db_path)
    eligible = _eligible_file(tmp_path, 101)
    eligible.chmod(mode)

    result = bootstrap_households(db_path, apply=True, eligible_users_file=eligible)

    assert result["exit_code"] == 6
    assert result["eligibility_state"] == "invalid"
    assert result["eligible_file_security"] == "invalid"
    assert _counts(db_path) == (0, 0)


def test_eligible_file_symlink_and_directory_fail_closed(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path, ids=(101,))
    HealBiteHouseholdStore(db_path=db_path)
    target = _eligible_file(tmp_path, 101)

    symlink = tmp_path / "eligible-link.txt"
    try:
        symlink.symlink_to(target)
    except (OSError, NotImplementedError):
        symlink = None
    if symlink is not None:
        result = bootstrap_households(db_path, apply=True, eligible_users_file=symlink)
        assert result["exit_code"] == 6
        assert result["eligible_file_security"] == "invalid"
        assert _counts(db_path) == (0, 0)

    result = bootstrap_households(db_path, apply=True, eligible_users_file=tmp_path)
    assert result["exit_code"] == 6
    assert result["eligible_file_security"] == "invalid"
    assert _counts(db_path) == (0, 0)
