
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gateway.healbite_household_bootstrap import bootstrap_households
from gateway.healbite_household_production_auth import (
    ProductionAuthorizationError,
    _validate_file_stat,
    prepare_production_authorization,
)
from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, HOUSEHOLDS_TABLE
from gateway.healbite_households import HealBiteHouseholdStore

VALID_REVISION = "a" * 40
OTHER_REVISION = "b" * 40
NOW = datetime(2026, 7, 3, 5, 0, 0, tzinfo=UTC)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _users(db_path: Path, ids=(101,)) -> None:
    with _connect(db_path) as conn:
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY, username TEXT)")
        for actor_id in ids:
            conn.execute("INSERT INTO users (user_id, username) VALUES (?, 'synthetic')", (actor_id,))


def _eligible_file(tmp_path: Path, *ids: int) -> Path:
    path = tmp_path / "eligible.txt"
    path.write_text("\n".join(str(item) for item in ids) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _counts(db_path: Path) -> tuple[int, int]:
    with _connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if HOUSEHOLDS_TABLE not in tables or HOUSEHOLD_MEMBERS_TABLE not in tables:
            return (0, 0)
        return (
            int(conn.execute(f"SELECT COUNT(*) FROM {HOUSEHOLDS_TABLE}").fetchone()[0]),
            int(conn.execute(f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE}").fetchone()[0]),
        )


def _capability(
    tmp_path: Path,
    db_path: Path,
    *,
    action: str = "household_bootstrap_apply",
    revision: str = VALID_REVISION,
    issued: datetime | None = None,
    expires: datetime | None = None,
    nonce: str = "0123456789abcdef0123456789abcdef",
    extra: dict[str, object] | None = None,
    omit: set[str] | None = None,
) -> Path:
    db_stat = db_path.stat()
    payload: dict[str, object] = {
        "schema_version": 1,
        "action": action,
        "database_realpath": str(db_path.resolve(strict=True)),
        "database_device": db_stat.st_dev,
        "database_inode": db_stat.st_ino,
        "expected_revision": revision,
        "issued_at_utc": (issued or (datetime.now(UTC) - timedelta(minutes=1))).isoformat().replace("+00:00", "Z"),
        "expires_at_utc": (expires or ((issued or (datetime.now(UTC) - timedelta(minutes=1))) + timedelta(minutes=10))).isoformat().replace("+00:00", "Z"),
        "nonce": nonce,
    }
    if extra:
        payload.update(extra)
    if omit:
        for key in omit:
            payload.pop(key, None)
    path = tmp_path / f"cap-{action}-{len(list(tmp_path.glob('cap-*')))}.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def _revision() -> str:
    return VALID_REVISION


def _now() -> datetime:
    return NOW


def test_valid_capability_claim_and_consume_is_one_time(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    cap = _capability(tmp_path, db_path, issued=NOW - timedelta(minutes=1))

    prepared = prepare_production_authorization(cap, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)
    claimed = prepared.claim()

    assert not cap.exists()
    assert claimed.path.exists()
    with pytest.raises(ProductionAuthorizationError) as exc:
        prepare_production_authorization(claimed.path, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)
    assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_REPLAY_REFUSED"
    claimed.consume_success()
    assert not claimed.path.exists()


@pytest.mark.parametrize(
    ("mutate", "error_type"),
    [
        ({"extra": {"unexpected": "value"}}, "PRODUCTION_AUTHORIZATION_SCHEMA_INVALID"),
        ({"omit": {"nonce"}}, "PRODUCTION_AUTHORIZATION_SCHEMA_INVALID"),
        ({"revision": OTHER_REVISION}, "PRODUCTION_AUTHORIZATION_REVISION_MISMATCH"),
        ({"revision": "abcdef"}, "PRODUCTION_AUTHORIZATION_REVISION_INVALID"),
        ({"issued": NOW - timedelta(minutes=10), "expires": NOW - timedelta(minutes=1)}, "PRODUCTION_AUTHORIZATION_EXPIRED"),
        ({"issued": NOW + timedelta(minutes=2)}, "PRODUCTION_AUTHORIZATION_NOT_YET_VALID"),
        ({"expires": NOW + timedelta(minutes=30)}, "PRODUCTION_AUTHORIZATION_TIME_INVALID"),
        ({"nonce": "short"}, "PRODUCTION_AUTHORIZATION_INVALID_NONCE"),
    ],
)
def test_invalid_capabilities_fail_closed(tmp_path, mutate, error_type):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    if "issued" not in mutate:
        mutate = {"issued": NOW - timedelta(minutes=1), **mutate}
    cap = _capability(tmp_path, db_path, **mutate)

    with pytest.raises(ProductionAuthorizationError) as exc:
        prepare_production_authorization(cap, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)

    assert exc.value.error_type == error_type
    assert cap.exists()


def test_wrong_db_identity_refused(tmp_path):
    db_path = tmp_path / "healbite.db"
    other = tmp_path / "other.db"
    _users(db_path)
    _users(other)
    cap = _capability(tmp_path, db_path, issued=NOW - timedelta(minutes=1))

    with pytest.raises(ProductionAuthorizationError) as exc:
        prepare_production_authorization(cap, action="household_bootstrap_apply", db_path=other, revision_provider=_revision, now_provider=_now)

    assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_DATABASE_MISMATCH"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o777])
def test_group_or_world_readable_capability_refused(tmp_path, mode):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    cap = _capability(tmp_path, db_path, issued=NOW - timedelta(minutes=1))
    cap.chmod(mode)

    with pytest.raises(ProductionAuthorizationError) as exc:
        prepare_production_authorization(cap, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)

    assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_FILE_INVALID"


def test_symlink_hardlink_and_directory_capabilities_refused(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    cap = _capability(tmp_path, db_path, issued=NOW - timedelta(minutes=1))
    symlink = tmp_path / "cap-link.json"
    try:
        symlink.symlink_to(cap)
    except (OSError, NotImplementedError):
        symlink = None
    if symlink is not None:
        with pytest.raises(ProductionAuthorizationError) as exc:
            prepare_production_authorization(symlink, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)
        assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_FILE_INVALID"

    hardlink = tmp_path / "cap-hardlink.json"
    try:
        hardlink.hardlink_to(cap)
    except (OSError, NotImplementedError):
        hardlink = None
    if hardlink is not None:
        with pytest.raises(ProductionAuthorizationError) as exc:
            prepare_production_authorization(cap, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)
        assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_FILE_INVALID"

    with pytest.raises(ProductionAuthorizationError) as exc:
        prepare_production_authorization(tmp_path, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)
    assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_FILE_INVALID"


def test_schema_and_bootstrap_actions_are_separate(tmp_path):
    db_path = tmp_path / "healbite.db"
    _users(db_path)
    schema_cap = _capability(tmp_path, db_path, action="household_schema_initialize", issued=NOW - timedelta(minutes=1))
    bootstrap_cap = _capability(tmp_path, db_path, action="household_bootstrap_apply", issued=NOW - timedelta(minutes=1))

    with pytest.raises(ProductionAuthorizationError) as exc:
        prepare_production_authorization(schema_cap, action="household_bootstrap_apply", db_path=db_path, revision_provider=_revision, now_provider=_now)
    assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_ACTION_REFUSED"

    with pytest.raises(ProductionAuthorizationError) as exc:
        prepare_production_authorization(bootstrap_cap, action="household_schema_initialize", db_path=db_path, revision_provider=_revision, now_provider=_now)
    assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_ACTION_REFUSED"


def test_bootstrap_production_without_capability_refused(monkeypatch, tmp_path):
    db_path = tmp_path / "prod.db"
    _users(db_path)
    monkeypatch.setattr("gateway.healbite_household_bootstrap.PRODUCTION_DB_PATH", db_path)

    result = bootstrap_households(db_path, initialize_schema=True)

    assert result["error_type"] == "PRODUCTION_AUTHORIZATION_REQUIRED"
    assert result["exit_code"] == 8
    assert _counts(db_path) == (0, 0)


def test_rehearsal_copy_does_not_require_capability(tmp_path):
    db_path = tmp_path / "copy.db"
    _users(db_path)

    result = bootstrap_households(db_path, initialize_schema=True)

    assert result["initializer_called"] is True
    assert result["result"] == "success"
    assert _counts(db_path) == (0, 0)


def test_production_schema_initialization_with_valid_capability(monkeypatch, tmp_path):
    db_path = tmp_path / "prod.db"
    _users(db_path)
    monkeypatch.setattr("gateway.healbite_household_bootstrap.PRODUCTION_DB_PATH", db_path)
    cap = _capability(tmp_path, db_path, action="household_schema_initialize")
    monkeypatch.setattr("gateway.healbite_household_production_auth._DEFAULT_REVISION_FILE", tmp_path / "rev")
    (tmp_path / "rev").write_text(VALID_REVISION, encoding="utf-8")

    result = bootstrap_households(db_path, initialize_schema=True, production_authorization_file=cap)

    assert result["initializer_called"] is True
    assert not cap.exists()
    assert not cap.with_name(f"{cap.name}.claimed").exists()
    assert _counts(db_path) == (0, 0)


def test_production_bootstrap_with_valid_capability_and_second_new_capability(monkeypatch, tmp_path):
    db_path = tmp_path / "prod.db"
    _users(db_path, ids=(101, 202))
    HealBiteHouseholdStore(db_path=db_path)
    eligible = _eligible_file(tmp_path, 101, 202)
    monkeypatch.setattr("gateway.healbite_household_bootstrap.PRODUCTION_DB_PATH", db_path)
    monkeypatch.setattr("gateway.healbite_household_production_auth._DEFAULT_REVISION_FILE", tmp_path / "rev")
    (tmp_path / "rev").write_text(VALID_REVISION, encoding="utf-8")
    first_cap = _capability(tmp_path, db_path, action="household_bootstrap_apply", nonce="0123456789abcdef0123456789abcdea")
    second_cap = _capability(tmp_path, db_path, action="household_bootstrap_apply", nonce="0123456789abcdef0123456789abcdeb")

    first = bootstrap_households(db_path, apply=True, eligible_users_file=eligible, production_authorization_file=first_cap)
    second = bootstrap_households(db_path, apply=True, eligible_users_file=eligible, production_authorization_file=second_cap)

    assert first["result"] == "success"
    assert first["created_count"] == 2
    assert second["result"] == "success"
    assert second["created_count"] == 0
    assert second["already_existing_count"] == 2
    assert _counts(db_path) == (2, 2)
    assert not first_cap.exists()
    assert not second_cap.exists()


def test_bootstrap_capability_consumed_on_post_claim_execution_failure(monkeypatch, tmp_path):
    db_path = tmp_path / "prod.db"
    _users(db_path, ids=(101,))
    HealBiteHouseholdStore(db_path=db_path)
    eligible = _eligible_file(tmp_path, 101)
    monkeypatch.setattr("gateway.healbite_household_bootstrap.PRODUCTION_DB_PATH", db_path)
    monkeypatch.setattr("gateway.healbite_household_production_auth._DEFAULT_REVISION_FILE", tmp_path / "rev")
    (tmp_path / "rev").write_text(VALID_REVISION, encoding="utf-8")
    cap = _capability(tmp_path, db_path, action="household_bootstrap_apply")

    def fail(self, actor_id):
        raise sqlite3.OperationalError("private failure body")

    monkeypatch.setattr(HealBiteHouseholdStore, "get_or_create_personal_household", fail)
    result = bootstrap_households(db_path, apply=True, eligible_users_file=eligible, production_authorization_file=cap)

    assert result["error_type"] == "APPLY_EXECUTION_FAILED"
    assert not cap.exists()
    assert cap.with_name(f"{cap.name}.claimed").exists()
    payload = json.dumps(result, sort_keys=True)
    assert "private failure body" not in payload
    assert str(cap) not in payload
    assert _counts(db_path) == (0, 0)



def test_wrong_owner_capability_refused(monkeypatch):
    current_uid = os.geteuid()
    wrong_uid = current_uid + 100000 if current_uid != 0 else 100000
    monkeypatch.setattr("gateway.healbite_household_production_auth.os.geteuid", lambda: wrong_uid + 1)
    fake_stat = os.stat_result((0o100600, 0, 0, 1, wrong_uid, 0, 1, 0, 0, 0))

    with pytest.raises(ProductionAuthorizationError) as exc:
        _validate_file_stat(Path("/run/hermes-household-bootstrap-auth/capability.json"), fake_stat)

    assert exc.value.error_type == "PRODUCTION_AUTHORIZATION_FILE_INVALID"


def test_audit_cli_does_not_accept_production_authorization_argument(tmp_path):
    from tests.scripts.test_household_db_audit import household_db_audit

    db_path = tmp_path / "healbite.db"
    _users(db_path)

    with pytest.raises(SystemExit) as exc:
        household_db_audit.main(["--db", str(db_path), "--production-authorization-file", str(tmp_path / "cap.json")])

    assert exc.value.code == 2

def test_cli_accepts_production_authorization_argument_without_public_override(tmp_path):
    from tests.scripts.test_household_bootstrap import household_bootstrap

    db_path = tmp_path / "healbite.db"
    _users(db_path)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = household_bootstrap.main(["--db", str(db_path), "--production-authorization-file", str(tmp_path / "missing"), "--json"])

    assert rc == 4
    assert "production_authorization_file" not in output.getvalue()
