from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from gateway.healbite_household_schema import HOUSEHOLD_MEMBERS_TABLE, HOUSEHOLDS_TABLE, HouseholdRole
from gateway.healbite_households import HealBiteHouseholdStore, HouseholdValidationError
from gateway.healbite_user_profile import (
    HealBiteUserProfileStore,
    USER_DELETE_BLOCKED_ACTIVE_HOUSEHOLD_RELATION,
    UserDeleteBlockedActiveHouseholdRelationError,
)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _profile_store(db_path: Path, identity_column: str, *user_ids: int) -> HealBiteUserProfileStore:
    assert identity_column in {"user_id", "telegram_id"}
    with _connect(db_path) as conn:
        conn.execute(f"CREATE TABLE users ({identity_column} INTEGER PRIMARY KEY, username TEXT, created_at TEXT)")
    store = HealBiteUserProfileStore(db_path=db_path)
    for user_id in user_ids:
        store.upsert_user_profile(user_id=user_id, username="synthetic")
    return store


def _add_active_member(db_path: Path, household_id: str, user_id: int, role: HouseholdRole) -> str:
    member_id = str(uuid.uuid4())
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, display_name, member_type, role, status,
                 age_band, created_at, updated_at, version)
            VALUES (?, ?, ?, NULL, 'linked_adult', ?, 'active', NULL, 't', 't', 1)
            """,
            (member_id, household_id, user_id, role.value),
        )
    return member_id


def _count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple[object, ...] = ()) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])


@pytest.mark.parametrize("identity_column", ["user_id", "telegram_id"])
@pytest.mark.parametrize(
    "role",
    [HouseholdRole.OWNER, HouseholdRole.ADULT_ADMIN, HouseholdRole.ADULT_MEMBER, HouseholdRole.DEPENDENT],
)
def test_delete_blocks_every_active_household_role_without_partial_writes(tmp_path, identity_column, role):
    db_path = tmp_path / f"blocked-{identity_column}-{role.value}.db"
    profiles = _profile_store(db_path, identity_column, 1001, 1002)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(1001)
    target = 1001 if role is HouseholdRole.OWNER else 1002
    if role is not HouseholdRole.OWNER:
        _add_active_member(db_path, personal.household.id, target, role)

    with _connect(db_path) as conn:
        before = (
            _count(conn, "users"),
            _count(conn, HOUSEHOLDS_TABLE),
            _count(conn, HOUSEHOLD_MEMBERS_TABLE),
            int(conn.execute(f"SELECT owner_user_id FROM {HOUSEHOLDS_TABLE}").fetchone()[0]),
        )

    for _ in range(2):
        with pytest.raises(UserDeleteBlockedActiveHouseholdRelationError) as exc_info:
            profiles.delete_user_profile(target)
        assert str(exc_info.value) == USER_DELETE_BLOCKED_ACTIVE_HOUSEHOLD_RELATION

    with _connect(db_path) as conn:
        after = (
            _count(conn, "users"),
            _count(conn, HOUSEHOLDS_TABLE),
            _count(conn, HOUSEHOLD_MEMBERS_TABLE),
            int(conn.execute(f"SELECT owner_user_id FROM {HOUSEHOLDS_TABLE}").fetchone()[0]),
        )
        identity = "user_id" if "user_id" in {row[1] for row in conn.execute("PRAGMA table_info(users)")} else "telegram_id"
        assert _count(conn, "users", f"WHERE {identity} = ?", (target,)) == 1
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE, "WHERE linked_user_id = ?", (target,)) == 1
    assert after == before


@pytest.mark.parametrize("identity_column", ["user_id", "telegram_id"])
def test_delete_allows_unrelated_user_and_preserves_other_household(tmp_path, identity_column):
    db_path = tmp_path / f"allowed-{identity_column}.db"
    profiles = _profile_store(db_path, identity_column, 1001, 1002)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(1001)

    profiles.delete_user_profile(1002)

    with _connect(db_path) as conn:
        identity = "user_id" if "user_id" in {row[1] for row in conn.execute("PRAGMA table_info(users)")} else "telegram_id"
        assert _count(conn, "users", f"WHERE {identity} = ?", (1002,)) == 0
        assert _count(conn, "users", f"WHERE {identity} = ?", (1001,)) == 1
        assert _count(conn, HOUSEHOLDS_TABLE) == 1
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 1
        assert int(conn.execute(f"SELECT owner_user_id FROM {HOUSEHOLDS_TABLE}").fetchone()[0]) == 1001
        assert personal.member.linked_user_id == 1001


@pytest.mark.parametrize("identity_column", ["user_id", "telegram_id"])
def test_delete_allows_terminal_membership_and_preserves_history(tmp_path, identity_column):
    db_path = tmp_path / f"terminal-{identity_column}.db"
    profiles = _profile_store(db_path, identity_column, 1001, 1002)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(1001)
    member_id = _add_active_member(db_path, personal.household.id, 1002, HouseholdRole.ADULT_MEMBER)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = 'removed' WHERE id = ?", (member_id,))

    profiles.delete_user_profile(1002)

    with _connect(db_path) as conn:
        identity = "user_id" if "user_id" in {row[1] for row in conn.execute("PRAGMA table_info(users)")} else "telegram_id"
        assert _count(conn, "users", f"WHERE {identity} = ?", (1002,)) == 0
        row = conn.execute(
            f"SELECT linked_user_id, status FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE id = ?", (member_id,)
        ).fetchone()
    assert tuple(row) == (1002, "removed")


def test_delete_fails_closed_for_unknown_users_schema(tmp_path):
    db_path = tmp_path / "unknown-users.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    with _connect(db_path) as conn:
        conn.execute("ALTER TABLE users RENAME TO legacy_users")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")

    with pytest.raises(UserDeleteBlockedActiveHouseholdRelationError):
        profiles.delete_user_profile(1001)

    with _connect(db_path) as conn:
        assert _count(conn, "legacy_users") == 1


def test_delete_fails_closed_for_incomplete_household_schema(tmp_path):
    db_path = tmp_path / "incomplete-household.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    with _connect(db_path) as conn:
        conn.execute("CREATE TABLE households (id TEXT PRIMARY KEY, owner_user_id INTEGER, status TEXT)")

    with pytest.raises(UserDeleteBlockedActiveHouseholdRelationError):
        profiles.delete_user_profile(1001)

    assert profiles.get_user_profile(1001) is not None


def test_delete_fails_closed_for_corrupt_inactive_membership_owner_pointer(tmp_path):
    db_path = tmp_path / "corrupt-owner.db"
    profiles = _profile_store(db_path, "user_id", 1001, 1002)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(1001)
    member_id = _add_active_member(db_path, personal.household.id, 1002, HouseholdRole.ADULT_MEMBER)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = 'removed' WHERE id = ?", (member_id,))
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET owner_user_id = ? WHERE id = ?", (1002, personal.household.id))

    with pytest.raises(UserDeleteBlockedActiveHouseholdRelationError):
        profiles.delete_user_profile(1002)

    assert profiles.get_user_profile(1002) is not None


def test_delete_fails_closed_for_invalid_membership_state(tmp_path):
    db_path = tmp_path / "invalid-membership.db"
    profiles = _profile_store(db_path, "user_id", 1001, 1002)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(1001)
    member_id = _add_active_member(db_path, personal.household.id, 1002, HouseholdRole.ADULT_MEMBER)
    with _connect(db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = 'ambiguous' WHERE id = ?", (member_id,))

    with pytest.raises(UserDeleteBlockedActiveHouseholdRelationError):
        profiles.delete_user_profile(1002)

    assert profiles.get_user_profile(1002) is not None


def test_failure_on_first_delete_preserves_original_error_and_user(tmp_path):
    db_path = tmp_path / "first-delete-failure.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    with _connect(db_path) as conn:
        conn.execute("CREATE TRIGGER fail_user_delete BEFORE DELETE ON users BEGIN SELECT RAISE(ABORT, 'user delete blocked'); END")

    with pytest.raises(sqlite3.IntegrityError, match="user delete blocked"):
        profiles.delete_user_profile(1001)

    assert profiles.get_user_profile(1001) is not None


def test_failure_after_user_delete_rolls_back_all_profile_rows(tmp_path):
    db_path = tmp_path / "post-delete-failure.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO profiles (telegram_id, created_at, updated_at) VALUES (?, 't', 't')", (1001,)
        )
        conn.execute(
            "CREATE TRIGGER fail_profile_delete BEFORE DELETE ON profiles "
            "BEGIN SELECT RAISE(ABORT, 'profile delete blocked'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="profile delete blocked"):
        profiles.delete_user_profile(1001)

    assert profiles.get_user_profile(1001) is not None


class _TrackingConnection(sqlite3.Connection):
    rollback_calls = 0
    commit_calls = 0
    close_calls = 0

    def rollback(self) -> None:
        type(self).rollback_calls += 1
        super().rollback()

    def commit(self) -> None:
        type(self).commit_calls += 1
        super().commit()

    def close(self) -> None:
        type(self).close_calls += 1
        super().close()


class _TrackingProfileStore(HealBiteUserProfileStore):
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, factory=_TrackingConnection, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def test_blocked_delete_rolls_back_closes_owned_connection_and_preserves_error(tmp_path):
    db_path = tmp_path / "resource-lifetime.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(1001)
    tracking = _TrackingProfileStore(db_path=db_path, ensure_schema_on_init=False)
    _TrackingConnection.rollback_calls = 0
    _TrackingConnection.close_calls = 0

    with pytest.raises(UserDeleteBlockedActiveHouseholdRelationError) as exc_info:
        tracking.delete_user_profile(1001)

    assert str(exc_info.value) == USER_DELETE_BLOCKED_ACTIVE_HOUSEHOLD_RELATION
    assert _TrackingConnection.rollback_calls == 1
    assert _TrackingConnection.close_calls == 1
    assert profiles.get_user_profile(1001) is not None


def test_successful_delete_commits_and_closes_owned_connection(tmp_path):
    db_path = tmp_path / "resource-success.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    tracking = _TrackingProfileStore(db_path=db_path, ensure_schema_on_init=False)
    _TrackingConnection.rollback_calls = 0
    _TrackingConnection.commit_calls = 0
    _TrackingConnection.close_calls = 0

    tracking.delete_user_profile(1001)

    assert _TrackingConnection.rollback_calls == 0
    assert _TrackingConnection.commit_calls == 1
    assert _TrackingConnection.close_calls == 1
    assert profiles.get_user_profile(1001) is None


class _CleanupFailConnection(sqlite3.Connection):
    def rollback(self) -> None:
        super().rollback()
        raise RuntimeError("rollback cleanup failed")

    def close(self) -> None:
        super().close()
        raise RuntimeError("close cleanup failed")


class _CleanupFailProfileStore(HealBiteUserProfileStore):
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, factory=_CleanupFailConnection, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn


def test_cleanup_failures_do_not_mask_blocked_delete_error(tmp_path):
    db_path = tmp_path / "cleanup-error.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(1001)
    failing = _CleanupFailProfileStore(db_path=db_path, ensure_schema_on_init=False)

    with pytest.raises(UserDeleteBlockedActiveHouseholdRelationError) as exc_info:
        failing.delete_user_profile(1001)

    assert str(exc_info.value) == USER_DELETE_BLOCKED_ACTIVE_HOUSEHOLD_RELATION
    assert profiles.get_user_profile(1001) is not None


class _PausingDeleteConnection(sqlite3.Connection):
    reached_delete = threading.Event()
    release_delete = threading.Event()

    def execute(self, sql, parameters=()):
        if sql.lstrip().upper().startswith("DELETE FROM USERS"):
            type(self).reached_delete.set()
            assert type(self).release_delete.wait(timeout=5)
        return super().execute(sql, parameters)


class _PausingDeleteStore(HealBiteUserProfileStore):
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            factory=_PausingDeleteConnection,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        return conn


def test_delete_wins_race_and_later_membership_creation_fails_closed(tmp_path):
    db_path = tmp_path / "delete-membership-race.db"
    profiles = _profile_store(db_path, "user_id", 1001)
    households = HealBiteHouseholdStore(db_path=db_path)
    deleting = _PausingDeleteStore(db_path=db_path, ensure_schema_on_init=False)
    _PausingDeleteConnection.reached_delete.clear()
    _PausingDeleteConnection.release_delete.clear()
    outcomes = []

    def delete_user():
        try:
            deleting.delete_user_profile(1001)
            outcomes.append("deleted")
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    def create_membership():
        try:
            households.get_or_create_personal_household(1001)
            outcomes.append("membership")
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    delete_thread = threading.Thread(target=delete_user)
    delete_thread.start()
    assert _PausingDeleteConnection.reached_delete.wait(timeout=5)
    membership_thread = threading.Thread(target=create_membership)
    membership_thread.start()
    threading.Event().wait(0.1)
    _PausingDeleteConnection.release_delete.set()
    delete_thread.join(timeout=5)
    membership_thread.join(timeout=5)

    assert not delete_thread.is_alive() and not membership_thread.is_alive()
    assert "deleted" in outcomes
    assert any(isinstance(item, HouseholdValidationError) for item in outcomes)
    with _connect(db_path) as conn:
        assert _count(conn, "users") == 0
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 0
