from __future__ import annotations

import logging
import uuid
import sqlite3
import threading
from pathlib import Path

import pytest

from gateway.healbite_household_schema import (
    HOUSEHOLD_MEMBERS_TABLE,
    HOUSEHOLD_MEMBER_STATUSES,
    HOUSEHOLD_MEMBER_TYPES,
    HOUSEHOLD_ROLES,
    HOUSEHOLD_STATUSES,
    HOUSEHOLDS_TABLE,
    is_canonical_uuid4,
    new_household_id,
    new_household_member_id,
)
from gateway.healbite_households import (
    HealBiteHouseholdService,
    HealBiteHouseholdStore,
    HouseholdAccessError,
    HouseholdFeatureConfig,
    HouseholdIntegrityError,
    HouseholdMemberStatus,
    HouseholdNotFoundError,
    HouseholdRole,
    HouseholdStatus,
    HouseholdValidationError,
    load_household_feature_config,
)
from gateway.healbite_user_profile import HealBiteUserProfileStore
from gateway.healbite_weight_reminders import HealBiteWeightReminderStore, WEIGHT_REMINDER_SETTINGS_TABLE
from gateway.healbite_weight_tracker import HealBiteWeightTracker, WEIGHT_ENTRIES_TABLE
from gateway.healbite_water_tracker import HealBiteWaterTracker, WATER_INTAKE_TABLE


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _create_users_table(db_path: Path, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE users (
                {identity_column} INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def _insert_user(db_path: Path, user_id: int, *, identity_column: str = "user_id") -> None:
    with _connect(db_path) as conn:
        conn.execute(f"INSERT INTO users ({identity_column}, username) VALUES (?, ?)", (int(user_id), "synthetic"))


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _indexes(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _first_personal(db_path: Path, user_id: int = 101):
    _create_users_table(db_path)
    _insert_user(db_path, user_id)
    return HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(user_id)


def _add_linked_member(db_path: Path, household_id: str, user_id: int, role: HouseholdRole) -> str:
    _insert_user(db_path, user_id)
    member_id = new_household_member_id()
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, display_name, member_type, role, status,
                 created_at, updated_at, version)
            VALUES (?, ?, ?, 'synthetic', 'linked_adult', ?, 'active', 't', 't', 1)
            """,
            (member_id, household_id, user_id, role.value),
        )
    return member_id


class _ConnectionProbe:
    def __init__(self, connection: sqlite3.Connection, *, fail_execute: bool = False, fail_close: bool = False) -> None:
        self.connection = connection
        self.fail_execute = fail_execute
        self.fail_close = fail_close
        self.commit_calls = 0
        self.rollback_calls = 0
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, *args, **kwargs):
        if self.fail_execute:
            raise ValueError("original read failure")
        return self.connection.execute(*args, **kwargs)

    def commit(self):
        self.commit_calls += 1
        return self.connection.commit()

    def rollback(self):
        self.rollback_calls += 1
        return self.connection.rollback()

    def close(self):
        self.closed = True
        self.connection.close()
        if self.fail_close:
            raise RuntimeError("cleanup failure")


def test_schema_constants_match_adr_values():
    assert HOUSEHOLDS_TABLE == "households"
    assert HOUSEHOLD_MEMBERS_TABLE == "household_members"
    assert HOUSEHOLD_STATUSES == ("active", "disabled", "closed")
    assert HOUSEHOLD_MEMBER_STATUSES == ("active", "unlinked", "disabled", "removed")
    assert HOUSEHOLD_ROLES == ("owner", "adult_admin", "adult_member", "dependent")
    assert HOUSEHOLD_MEMBER_TYPES == ("primary", "linked_adult", "unlinked_adult", "dependent")


def test_uuid_helpers_return_canonical_lowercase_uuid4():
    household_id = new_household_id()
    member_id = new_household_member_id()

    assert is_canonical_uuid4(household_id)
    assert is_canonical_uuid4(member_id)
    assert household_id == household_id.lower()
    assert member_id == member_id.lower()
    assert len(household_id) == 36


@pytest.mark.parametrize("bad", ["", "ABCDEF00-0000-4000-8000-000000000000", "1", "not-a-uuid"])
def test_uuid_validation_rejects_noncanonical_values(bad):
    assert not is_canonical_uuid4(bad)


def test_uuid_validation_rejects_non_v4_and_whitespace():
    assert not is_canonical_uuid4(str(uuid.uuid1()))
    assert not is_canonical_uuid4(f" {new_household_id()}")


def test_schema_initializes_idempotently_without_business_rows(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)

    HealBiteHouseholdStore(db_path=db_path)
    HealBiteHouseholdStore(db_path=db_path)

    with _connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert HOUSEHOLDS_TABLE in _tables(conn)
        assert HOUSEHOLD_MEMBERS_TABLE in _tables(conn)
        indexes = _indexes(conn)
        assert "idx_household_members_active_owner" in indexes
        assert "idx_household_members_active_linked_user" in indexes
        assert _count(conn, HOUSEHOLDS_TABLE) == 0
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 0
        assert _count(conn, "users") == 0


def test_schema_initializes_production_shaped_db_without_touching_existing_counts(tmp_path):
    db_path = tmp_path / "healbite.db"
    profile_store = HealBiteUserProfileStore(db_path=db_path)
    profile_store.upsert_user_profile(user_id=101, username="synthetic", daily_kcal_target=2000)
    weight = HealBiteWeightTracker(db_path=db_path)
    weight.add_weight_entry(101, 80.0, source="test")
    water = HealBiteWaterTracker(db_path=db_path)
    water.add_water_intake(101, 250, source="test")
    HealBiteWeightReminderStore(db_path=db_path)

    with _connect(db_path) as conn:
        before = {
            "users": _count(conn, "users"),
            "profiles": _count(conn, "profiles"),
            WEIGHT_ENTRIES_TABLE: _count(conn, WEIGHT_ENTRIES_TABLE),
            WATER_INTAKE_TABLE: _count(conn, WATER_INTAKE_TABLE),
            WEIGHT_REMINDER_SETTINGS_TABLE: _count(conn, WEIGHT_REMINDER_SETTINGS_TABLE),
        }

    HealBiteHouseholdStore(db_path=db_path)
    HealBiteHouseholdStore(db_path=db_path)

    with _connect(db_path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table, count in before.items():
            assert _count(conn, table) == count
        assert _count(conn, HOUSEHOLDS_TABLE) == 0
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 0


def test_household_check_constraints(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    HealBiteHouseholdStore(db_path=db_path)
    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {HOUSEHOLDS_TABLE} (id, owner_user_id, status, created_at, updated_at) VALUES (?, 1, ?, 't', 't')",
                (new_household_id(), "archived"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {HOUSEHOLDS_TABLE} (id, owner_user_id, status, created_at, updated_at, version) VALUES (?, 1, 'active', 't', 't', 0)",
                (new_household_id(),),
            )


@pytest.mark.parametrize(
    ("member_type", "role", "status"),
    [("unknown", "owner", "active"), ("primary", "unknown", "active"), ("primary", "owner", "unknown")],
)
def test_member_check_constraints(member_type, role, status, tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)

    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                    (id, household_id, linked_user_id, display_name, member_type, role, status,
                     age_band, created_at, updated_at, version)
                VALUES (?, ?, 202, NULL, ?, ?, ?, NULL, 't', 't', 1)
                """,
                (new_household_member_id(), result.household.id, member_type, role, status),
            )


def test_member_linked_unlinked_consistency(tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)
    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                    (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
                VALUES (?, ?, NULL, 'linked_adult', 'adult_member', 'active', 't', 't')
                """,
                (new_household_member_id(), result.household.id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                    (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
                VALUES (?, ?, 202, 'dependent', 'dependent', 'active', 't', 't')
                """,
                (new_household_member_id(), result.household.id),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                    (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
                VALUES (?, ?, 202, 'linked_adult', 'adult_member', 'unlinked', 't', 't')
                """,
                (new_household_member_id(), result.household.id),
            )
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, NULL, 'unlinked_adult', 'adult_member', 'unlinked', 't', 't')
            """,
            (new_household_member_id(), result.household.id),
        )


def test_household_delete_restricted_by_member(tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)

    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"DELETE FROM {HOUSEHOLDS_TABLE} WHERE id = ?", (result.household.id,))


def test_member_cannot_reference_missing_household(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    HealBiteHouseholdStore(db_path=db_path)

    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                    (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
                VALUES (?, ?, 101, 'primary', 'owner', 'active', 't', 't')
                """,
                (new_household_member_id(), new_household_id()),
            )


def test_active_linked_user_unique_index_ignores_null_and_removed(tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)
    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                    (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
                VALUES (?, ?, 101, 'primary', 'owner', 'active', 't', 't')
                """,
                (new_household_member_id(), result.household.id),
            )
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, 101, 'linked_adult', 'adult_member', 'removed', 't', 't')
            """,
            (new_household_member_id(), result.household.id),
        )
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, NULL, 'dependent', 'dependent', 'active', 't', 't')
            """,
            (new_household_member_id(), result.household.id),
        )


def test_active_owner_unique_index(tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)
    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                    (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
                VALUES (?, ?, 202, 'linked_adult', 'owner', 'active', 't', 't')
                """,
                (new_household_member_id(), result.household.id),
            )


def test_create_personal_household_and_reads(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    store = HealBiteHouseholdStore(db_path=db_path)

    result = store.get_or_create_personal_household(101)
    household = store._read_household_by_id_internal(result.household.id)
    by_user = store._read_household_for_linked_user_internal(101)
    primary = store._read_primary_member_for_user_internal(101)
    members = store._list_household_members_internal(result.household.id)

    assert result.created is True
    assert household == result.household
    assert by_user == result.household
    assert primary == result.member
    assert members == [result.member]
    assert result.household.owner_user_id == 101
    assert result.member.linked_user_id == 101
    assert result.member.role is HouseholdRole.OWNER
    assert result.member.status is HouseholdMemberStatus.ACTIVE
    assert result.household.status is HouseholdStatus.ACTIVE
    assert result.member.display_name is None


def test_get_or_create_repeated_returns_same_rows_without_timestamp_mutation(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    store = HealBiteHouseholdStore(db_path=db_path)

    first = store.get_or_create_personal_household(101)
    second = store.get_or_create_personal_household(101)

    assert first.household.id == second.household.id
    assert first.member.id == second.member.id
    assert first.household.created_at == second.household.created_at
    assert first.household.updated_at == second.household.updated_at
    assert second.created is False
    with _connect(db_path) as conn:
        assert _count(conn, HOUSEHOLDS_TABLE) == 1
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 1


@pytest.mark.parametrize("bad_actor", [0, -1, True, "abc"])
def test_invalid_application_user_rejected(bad_actor, tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)

    with pytest.raises(HouseholdValidationError):
        store.get_or_create_personal_household(bad_actor)


def test_unknown_application_user_rejected(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)

    with pytest.raises(HouseholdValidationError):
        store.get_or_create_personal_household(999)


def test_telegram_id_users_schema_supported_without_user_fk(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path, identity_column="telegram_id")
    _insert_user(db_path, 101, identity_column="telegram_id")

    result = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(101)

    assert result.household.owner_user_id == 101
    assert result.member.linked_user_id == 101
    with _connect(db_path) as conn:
        foreign_keys = conn.execute(f"PRAGMA foreign_key_list({HOUSEHOLDS_TABLE})").fetchall()
        assert [row for row in foreign_keys if row[2] == "users"] == []


def test_users_identity_column_prefers_user_id_when_both_columns_exist(tmp_path):
    db_path = tmp_path / "healbite.db"
    with _connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE users (
                user_id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("INSERT INTO users (user_id, telegram_id, username) VALUES (101, 202, 'synthetic')")
    store = HealBiteHouseholdStore(db_path=db_path)

    result = store.get_or_create_personal_household(101)

    assert result.household.owner_user_id == 101
    with pytest.raises(HouseholdValidationError):
        store.get_or_create_personal_household(202)


def test_missing_users_identity_column_fails_without_creating_household(tmp_path):
    db_path = tmp_path / "healbite.db"
    with _connect(db_path) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
        conn.execute("INSERT INTO users (id, username) VALUES (101, 'synthetic')")
    store = HealBiteHouseholdStore(db_path=db_path)

    with pytest.raises(HouseholdIntegrityError, match="unsupported users identity schema"):
        store.get_or_create_personal_household(101)

    with _connect(db_path) as conn:
        assert _count(conn, HOUSEHOLDS_TABLE) == 0
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 0


def test_owner_pointer_mismatch_detected(tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET owner_user_id = 202 WHERE id = ?", (result.household.id,))

    store = HealBiteHouseholdStore(db_path=db_path)
    with pytest.raises(HouseholdIntegrityError, match="owner pointer mismatch"):
        store._read_household_by_id_internal(result.household.id)
    with pytest.raises(HouseholdIntegrityError, match="owner pointer mismatch"):
        store._read_primary_member_for_user_internal(101)
    with pytest.raises(HouseholdIntegrityError, match="owner pointer mismatch"):
        HealBiteHouseholdService(store).get_actor_household(101)


def test_partial_household_detected(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)
    household_id = new_household_id()
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLDS_TABLE} (id, owner_user_id, status, created_at, updated_at)
            VALUES (?, 101, 'active', 't', 't')
            """,
            (household_id,),
        )

    with pytest.raises(HouseholdIntegrityError, match="invalid active owner count"):
        store._read_household_by_id_internal(household_id)


def test_duplicate_membership_detected_when_index_missing_in_corrupt_db(tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)
    with _connect(db_path) as conn:
        conn.execute("DROP INDEX idx_household_members_active_linked_user")
        other_household = new_household_id()
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLDS_TABLE} (id, owner_user_id, status, created_at, updated_at)
            VALUES (?, 101, 'active', 't', 't')
            """,
            (other_household,),
        )
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, 101, 'primary', 'owner', 'active', 't', 't')
            """,
            (new_household_member_id(), other_household),
        )

    with pytest.raises(HouseholdIntegrityError, match="duplicate active household membership"):
        store._read_household_for_linked_user_internal(101)
    assert result.household.id


def test_duplicate_owner_detected_when_index_missing_in_corrupt_db(tmp_path):
    db_path = tmp_path / "healbite.db"
    result = _first_personal(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)
    with _connect(db_path) as conn:
        conn.execute("DROP INDEX idx_household_members_active_owner")
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, 202, 'linked_adult', 'owner', 'active', 't', 't')
            """,
            (new_household_member_id(), result.household.id),
        )

    with pytest.raises(HouseholdIntegrityError, match="invalid active owner count"):
        store._read_household_by_id_internal(result.household.id)
    with pytest.raises(HouseholdIntegrityError, match="invalid active owner count"):
        store._read_primary_member_for_user_internal(101)


def test_orphaned_primary_member_detected_when_foreign_keys_were_bypassed(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    store = HealBiteHouseholdStore(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, member_type, role, status, created_at, updated_at)
            VALUES (?, ?, 101, 'primary', 'owner', 'active', 't', 't')
            """,
            (new_household_member_id(), new_household_id()),
        )

    with pytest.raises(HouseholdIntegrityError, match="member references missing household"):
        store._read_primary_member_for_user_internal(101)
    with pytest.raises(HouseholdIntegrityError, match="member references missing household"):
        HealBiteHouseholdService(store).get_actor_household(101)


def test_member_insert_failure_rolls_back_household_insert(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    ids = iter([new_household_id(), "not-a-uuid"])
    store = HealBiteHouseholdStore(
        db_path=db_path,
        household_id_factory=lambda: next(ids),
        member_id_factory=lambda: next(ids),
    )

    with pytest.raises(ValueError):
        store.get_or_create_personal_household(101)

    with _connect(db_path) as conn:
        assert _count(conn, HOUSEHOLDS_TABLE) == 0
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 0


def test_uuid_collision_has_bounded_regeneration(tmp_path):
    db_path = tmp_path / "healbite.db"
    first = _first_personal(db_path)
    _insert_user(db_path, 202)
    generated = [first.household.id, new_household_id()]
    member_ids = [new_household_member_id(), new_household_member_id()]
    store = HealBiteHouseholdStore(
        db_path=db_path,
        household_id_factory=lambda: generated.pop(0),
        member_id_factory=lambda: member_ids.pop(0),
    )

    result = store.get_or_create_personal_household(202)

    assert result.created is True
    assert result.household.id != first.household.id
    with _connect(db_path) as conn:
        assert _count(conn, HOUSEHOLDS_TABLE) == 2
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 2


def _run_concurrent_get_or_create(db_path: Path, callers: int) -> list[object]:
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    barrier = threading.Barrier(callers)
    results: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        store = HealBiteHouseholdStore(db_path=db_path)
        barrier.wait(timeout=5)
        try:
            result = store.get_or_create_personal_household(101)
        except Exception as exc:  # pragma: no cover - asserted by caller
            result = exc
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(callers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    return results


@pytest.mark.parametrize("callers", [2, 5])
def test_concurrent_get_or_create_returns_one_logical_household(tmp_path, callers):
    db_path = tmp_path / "healbite.db"

    results = _run_concurrent_get_or_create(db_path, callers)

    assert len(results) == callers
    assert all(not isinstance(result, Exception) for result in results)
    household_ids = {result.household.id for result in results}  # type: ignore[union-attr]
    member_ids = {result.member.id for result in results}  # type: ignore[union-attr]
    assert len(household_ids) == 1
    assert len(member_ids) == 1
    with _connect(db_path) as conn:
        assert _count(conn, HOUSEHOLDS_TABLE) == 1
        assert _count(conn, HOUSEHOLD_MEMBERS_TABLE) == 1


def test_authorization_context_and_access(tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    service = HealBiteHouseholdService(HealBiteHouseholdStore(db_path=db_path))

    context = service.resolve_actor_household_context(101)

    assert context.actor_user_id == 101
    assert context.role is HouseholdRole.OWNER
    service.assert_household_access(context, context.household_id)
    with pytest.raises(HouseholdAccessError):
        service.assert_household_access(context, new_household_id())


def test_actor_scoped_family_reads_return_only_own_household(tmp_path):
    db_path = tmp_path / "healbite.db"
    personal = _first_personal(db_path)
    member_id = _add_linked_member(db_path, personal.household.id, 202, HouseholdRole.ADULT_MEMBER)
    service = HealBiteHouseholdService(HealBiteHouseholdStore(db_path=db_path))

    assert service.get_actor_household(202).id == personal.household.id
    assert service.get_household_for_actor(202, personal.household.id).id == personal.household.id
    assert service.get_membership_for_actor(202, personal.household.id).id == member_id
    assert {member.id for member in service.list_members_for_actor(202, personal.household.id)} == {
        personal.member.id,
        member_id,
    }


def test_foreign_and_random_household_ids_are_indistinguishable(tmp_path):
    db_path = tmp_path / "healbite.db"
    first = _first_personal(db_path)
    _insert_user(db_path, 202)
    second = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(202)
    service = HealBiteHouseholdService(HealBiteHouseholdStore(db_path=db_path))

    for reader in (
        service.get_household_for_actor,
        service.get_membership_for_actor,
        service.list_members_for_actor,
    ):
        errors = []
        for requested_id in (second.household.id, new_household_id()):
            with pytest.raises(HouseholdAccessError) as excinfo:
                reader(101, requested_id)
            errors.append((type(excinfo.value), str(excinfo.value)))
        assert errors[0] == errors[1]
    assert service.get_actor_household(101).id == first.household.id


@pytest.mark.parametrize(
    ("role", "member_list_allowed"),
    [
        (HouseholdRole.OWNER, True),
        (HouseholdRole.ADULT_ADMIN, True),
        (HouseholdRole.ADULT_MEMBER, True),
        (HouseholdRole.DEPENDENT, False),
    ],
)
def test_actor_scoped_read_authorization_matrix(tmp_path, role, member_list_allowed):
    db_path = tmp_path / f"{role.value}.db"
    personal = _first_personal(db_path)
    actor_user_id = 101 if role is HouseholdRole.OWNER else 202
    if role is not HouseholdRole.OWNER:
        _add_linked_member(db_path, personal.household.id, actor_user_id, role)
    service = HealBiteHouseholdService(HealBiteHouseholdStore(db_path=db_path))

    assert service.get_household_for_actor(actor_user_id, personal.household.id).id == personal.household.id
    assert service.get_membership_for_actor(actor_user_id, personal.household.id).role is role
    if member_list_allowed:
        assert service.list_members_for_actor(actor_user_id, personal.household.id)
    else:
        with pytest.raises(HouseholdAccessError, match="household access denied"):
            service.list_members_for_actor(actor_user_id, personal.household.id)


def test_actor_scoped_reads_fail_closed_for_missing_or_inactive_membership(tmp_path):
    db_path = tmp_path / "healbite.db"
    personal = _first_personal(db_path)
    _insert_user(db_path, 202)
    service = HealBiteHouseholdService(HealBiteHouseholdStore(db_path=db_path))

    with pytest.raises(HouseholdNotFoundError, match="household not found"):
        service.get_actor_household(202)

    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = 'disabled' WHERE id = ?",
            (personal.member.id,),
        )
    with pytest.raises(HouseholdAccessError, match="household access denied"):
        service.get_actor_household(101)

    with _connect(db_path) as conn:
        conn.execute(
            f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = 'active' WHERE id = ?",
            (personal.member.id,),
        )
        conn.execute(
            f"UPDATE {HOUSEHOLDS_TABLE} SET status = 'disabled' WHERE id = ?",
            (personal.household.id,),
        )
    with pytest.raises(HouseholdAccessError, match="household access denied"):
        service.get_actor_household(101)


def test_actor_scoped_reads_do_not_create_or_mutate_business_rows(tmp_path):
    db_path = tmp_path / "healbite.db"
    personal = _first_personal(db_path)
    service = HealBiteHouseholdService(HealBiteHouseholdStore(db_path=db_path))
    with _connect(db_path) as conn:
        before = (_count(conn, HOUSEHOLDS_TABLE), _count(conn, HOUSEHOLD_MEMBERS_TABLE))

    for _ in range(3):
        service.get_actor_household(101)
        service.get_household_for_actor(101, personal.household.id)
        service.get_membership_for_actor(101, personal.household.id)
        service.list_members_for_actor(101, personal.household.id)

    with _connect(db_path) as conn:
        after = (_count(conn, HOUSEHOLDS_TABLE), _count(conn, HOUSEHOLD_MEMBERS_TABLE))
    assert after == before


def test_actor_scoped_reads_close_owned_connections_without_committing(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    personal = _first_personal(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)
    original_connect = store._connect
    probes: list[_ConnectionProbe] = []

    def tracked_connect():
        probe = _ConnectionProbe(original_connect())
        probes.append(probe)
        return probe

    monkeypatch.setattr(store, "_connect", tracked_connect)
    service = HealBiteHouseholdService(store)
    service.get_household_for_actor(101, personal.household.id)
    service.get_membership_for_actor(101, personal.household.id)
    service.list_members_for_actor(101, personal.household.id)

    assert probes
    assert all(probe.closed for probe in probes)
    assert sum(probe.commit_calls for probe in probes) == 0
    assert sum(probe.rollback_calls for probe in probes) == 0


def test_owned_connection_cleanup_does_not_mask_original_read_error(tmp_path, monkeypatch):
    db_path = tmp_path / "healbite.db"
    _first_personal(db_path)
    store = HealBiteHouseholdStore(db_path=db_path)
    original_connect = store._connect
    probe = _ConnectionProbe(original_connect(), fail_execute=True, fail_close=True)
    monkeypatch.setattr(store, "_connect", lambda: probe)

    with pytest.raises(ValueError, match="original read failure"):
        HealBiteHouseholdService(store).get_actor_household(101)
    assert probe.closed is True


def test_raw_household_id_reads_are_internal_only():
    assert not hasattr(HealBiteHouseholdStore, "get_household_by_id")
    assert not hasattr(HealBiteHouseholdStore, "get_household_for_linked_user")
    assert not hasattr(HealBiteHouseholdStore, "get_primary_member_for_user")
    assert not hasattr(HealBiteHouseholdStore, "list_household_members")
    source = Path("gateway/healbite_weekly_menu_generation.py").read_text(encoding="utf-8")
    assert "household_store.list_household_members" not in source
    assert "household_service.list_members_for_actor" in source


@pytest.mark.parametrize(
    ("household_status", "member_status"),
    [
        (HouseholdStatus.DISABLED.value, HouseholdMemberStatus.ACTIVE.value),
        (HouseholdStatus.CLOSED.value, HouseholdMemberStatus.ACTIVE.value),
        (HouseholdStatus.ACTIVE.value, HouseholdMemberStatus.DISABLED.value),
        (HouseholdStatus.ACTIVE.value, HouseholdMemberStatus.REMOVED.value),
    ],
)
def test_authorization_rejects_inactive_context(household_status, member_status, tmp_path):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 101)
    store = HealBiteHouseholdStore(db_path=db_path)
    result = store.get_or_create_personal_household(101)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET status = ? WHERE id = ?", (household_status, result.household.id))
        conn.execute(f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = ? WHERE id = ?", (member_status, result.member.id))
    service = HealBiteHouseholdService(store)

    with pytest.raises(HouseholdAccessError):
        service.resolve_actor_household_context(101)


def test_feature_config_defaults_and_allowlist_semantics():
    assert load_household_feature_config({}) == HouseholdFeatureConfig()
    assert load_household_feature_config({"HEALBITE_HOUSEHOLDS_ENABLED": "false"}).enabled is False
    assert load_household_feature_config({"HEALBITE_HOUSEHOLDS_ENABLED": "0"}).enabled is False
    assert load_household_feature_config({"HEALBITE_HOUSEHOLDS_ENABLED": "yes"}).enabled is True
    config = load_household_feature_config(
        {"HEALBITE_HOUSEHOLDS_ENABLED": "true", "HEALBITE_HOUSEHOLDS_ALLOWLIST": "101, 202,101"}
    )
    assert config.enabled is True
    assert config.allowlist == frozenset({101, 202})
    assert config.allowlist_valid is True
    assert load_household_feature_config({"HEALBITE_HOUSEHOLDS_ALLOWLIST": ""}).allowlist == frozenset()


@pytest.mark.parametrize("raw", ["abc", "101,bad", "-1", "0", "true", "9223372036854775808"])
def test_feature_config_malformed_allowlist_fails_closed(raw):
    config = load_household_feature_config(
        {"HEALBITE_HOUSEHOLDS_ENABLED": "true", "HEALBITE_HOUSEHOLDS_ALLOWLIST": raw}
    )

    assert config.enabled is False
    assert config.allowlist == frozenset()
    assert config.allowlist_valid is False


def test_privacy_no_ids_in_errors_or_logs(tmp_path, caplog, capsys):
    db_path = tmp_path / "healbite.db"
    _create_users_table(db_path)
    _insert_user(db_path, 3131313131)
    result = HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(3131313131)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET owner_user_id = 4242424242 WHERE id = ?", (result.household.id,))

    caplog.set_level(logging.INFO)
    with pytest.raises(HouseholdIntegrityError) as excinfo:
        HealBiteHouseholdStore(db_path=db_path)._read_household_by_id_internal(result.household.id)
    captured = capsys.readouterr()
    combined = "\n".join([str(excinfo.value), captured.out, captured.err, caplog.text])

    assert "3131313131" not in combined
    assert "4242424242" not in combined
    assert result.household.id not in combined
    assert result.member.id not in combined
    assert "synthetic" not in combined


def test_no_nutrition_target_table_or_runtime_bootstrap_present():
    source = Path("gateway/healbite_households.py").read_text(encoding="utf-8")
    assert "nutrition" + "_targets" not in source
    assert "TELE" + "GRAM" not in source


def test_existing_gateway_entrypoints_not_modified():
    assert Path("gateway/run.py").exists()
    assert Path("gateway/platforms/telegram.py").exists()
