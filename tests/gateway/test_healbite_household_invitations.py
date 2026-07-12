from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gateway.healbite_household_invitations import (
    HealBiteHouseholdInvitationService,
    HealBiteHouseholdInvitationStore,
    HouseholdInvitationAccessError,
    HouseholdInvitationConflictError,
    HouseholdInvitationIntegrityError,
    HouseholdInvitationNotFoundError,
    HouseholdInvitationStateError,
    HouseholdInvitationValidationError,
)
from gateway.healbite_household_schema import (
    HOUSEHOLD_INVITATIONS_TABLE,
    HOUSEHOLD_MEMBERS_TABLE,
    HOUSEHOLDS_TABLE,
    HouseholdInvitationStatus,
    HouseholdRole,
    new_household_id,
    new_household_invitation_id,
    new_household_member_id,
)
from gateway.healbite_households import HealBiteHouseholdStore


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _users(db_path: Path, *user_ids: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT)")
        conn.executemany("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, 'synthetic')", [(item,) for item in user_ids])


def _seed(db_path: Path, *, targets: tuple[int, ...] = (202, 303, 404)):
    _users(db_path, 101, *targets)
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(101)
    clock = _Clock()
    store = HealBiteHouseholdInvitationStore(db_path, now_factory=clock)
    service = HealBiteHouseholdInvitationService(store)
    return personal, clock, store, service


def _seed_legacy(db_path: Path, identity_column: str):
    assert identity_column in {"user_id", "telegram_id"}
    with _connect(db_path) as conn:
        conn.execute(f"CREATE TABLE users ({identity_column} INTEGER PRIMARY KEY, username TEXT)")
        conn.executemany(
            f"INSERT INTO users ({identity_column}, username) VALUES (?, 'synthetic')",
            [(101,), (202,)],
        )
    households = HealBiteHouseholdStore(db_path=db_path)
    personal = households.get_or_create_personal_household(101)
    clock = _Clock()
    store = HealBiteHouseholdInvitationStore(db_path, now_factory=clock)
    return personal, clock, store, HealBiteHouseholdInvitationService(store)


def _add_member(db_path: Path, household_id: str, user_id: int, role: HouseholdRole, *, status: str = "active") -> str:
    _users(db_path, user_id)
    member_id = new_household_member_id()
    with _connect(db_path) as conn:
        conn.execute(
            f"""
            INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                (id, household_id, linked_user_id, display_name, member_type, role, status,
                 age_band, created_at, updated_at, version)
            VALUES (?, ?, ?, NULL, 'linked_adult', ?, ?, NULL, 't', 't', 1)
            """,
            (member_id, household_id, user_id, role.value, status),
        )
    return member_id


def _invite(service, clock, household_id: str, invitee: int = 202, *, actor: int = 101, role=HouseholdRole.ADULT_MEMBER, key="create-1"):
    return service.create_household_invitation(
        actor,
        household_id,
        invitee,
        role,
        clock.value + timedelta(days=1),
        key,
    )


def _count(db_path: Path, table: str) -> int:
    with _connect(db_path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_invitation_schema_is_additive_idempotent_and_indexed(tmp_path):
    db_path = tmp_path / "schema.db"
    _users(db_path, 101)
    HealBiteHouseholdStore(db_path=db_path)
    HealBiteHouseholdStore(db_path=db_path)
    with _connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert HOUSEHOLD_INVITATIONS_TABLE in tables
        assert {
            "idx_household_invitations_create_idempotency",
            "idx_household_invitations_pending_unique",
            "idx_household_invitations_incoming",
            "idx_household_invitations_household",
        } <= indexes
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_schema_rejects_invalid_role_status_and_foreign_household(tmp_path):
    personal, clock, _store, service = _seed(tmp_path / "constraints.db")
    invitation = _invite(service, clock, personal.household.id)
    with _connect(tmp_path / "constraints.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} SET proposed_role = 'owner' WHERE id = ?", (invitation.id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} SET status = 'bad' WHERE id = ?", (invitation.id,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} SET household_id = ? WHERE id = ?", (new_household_id(), invitation.id))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} "
                "SET terminal_operation = 'accept', terminal_actor_user_id = invitee_user_id, terminal_idempotency_key = 'bad' "
                "WHERE id = ?",
                (invitation.id,),
            )


@pytest.mark.parametrize(
    ("actor_role", "proposed_role", "allowed"),
    [
        (HouseholdRole.OWNER, HouseholdRole.ADULT_ADMIN, True),
        (HouseholdRole.OWNER, HouseholdRole.ADULT_MEMBER, True),
        (HouseholdRole.OWNER, HouseholdRole.DEPENDENT, True),
        (HouseholdRole.ADULT_ADMIN, HouseholdRole.ADULT_MEMBER, True),
        (HouseholdRole.ADULT_ADMIN, HouseholdRole.DEPENDENT, True),
        (HouseholdRole.ADULT_ADMIN, HouseholdRole.ADULT_ADMIN, False),
        (HouseholdRole.ADULT_MEMBER, HouseholdRole.ADULT_MEMBER, False),
        (HouseholdRole.DEPENDENT, HouseholdRole.ADULT_MEMBER, False),
    ],
)
def test_create_authorization_matrix(tmp_path, actor_role, proposed_role, allowed):
    db_path = tmp_path / f"{actor_role.value}-{proposed_role.value}.db"
    personal, clock, _store, service = _seed(db_path)
    actor = 101
    if actor_role is not HouseholdRole.OWNER:
        actor = 303
        _add_member(db_path, personal.household.id, actor, actor_role)
    if allowed:
        assert _invite(service, clock, personal.household.id, actor=actor, role=proposed_role).status is HouseholdInvitationStatus.PENDING
    else:
        with pytest.raises(HouseholdInvitationAccessError):
            _invite(service, clock, personal.household.id, actor=actor, role=proposed_role)


def test_owner_role_self_existing_member_nonmember_and_inactive_are_denied(tmp_path):
    db_path = tmp_path / "denied.db"
    personal, clock, _store, service = _seed(db_path)
    with pytest.raises(HouseholdInvitationValidationError):
        _invite(service, clock, personal.household.id, invitee=101)
    with pytest.raises(HouseholdInvitationValidationError):
        _invite(service, clock, personal.household.id, role=HouseholdRole.OWNER)
    _add_member(db_path, personal.household.id, 404, HouseholdRole.ADULT_ADMIN)
    with pytest.raises(HouseholdInvitationValidationError):
        _invite(service, clock, personal.household.id, actor=404, role=HouseholdRole.OWNER)
    _add_member(db_path, personal.household.id, 303, HouseholdRole.ADULT_MEMBER)
    with pytest.raises(HouseholdInvitationConflictError):
        _invite(service, clock, personal.household.id, invitee=303)
    with pytest.raises(HouseholdInvitationAccessError):
        _invite(service, clock, personal.household.id, actor=505)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLD_MEMBERS_TABLE} SET status = 'disabled' WHERE linked_user_id = 101")
    with pytest.raises(HouseholdInvitationAccessError):
        _invite(service, clock, personal.household.id)


def test_inactive_household_and_invalid_expiry_are_denied(tmp_path):
    db_path = tmp_path / "expiry.db"
    personal, clock, _store, service = _seed(db_path)
    for expiry in (clock.value, clock.value + timedelta(days=31), clock.value.replace(tzinfo=None)):
        with pytest.raises(HouseholdInvitationValidationError):
            service.create_household_invitation(101, personal.household.id, 202, "adult_member", expiry, "k")
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET status = 'disabled' WHERE id = ?", (personal.household.id,))
    with pytest.raises(HouseholdInvitationAccessError):
        _invite(service, clock, personal.household.id)


def test_create_idempotency_and_pending_duplicate_return_existing(tmp_path):
    db_path = tmp_path / "idempotency.db"
    personal, clock, _store, service = _seed(db_path)
    first = _invite(service, clock, personal.household.id)
    replay = _invite(service, clock, personal.household.id)
    duplicate = _invite(service, clock, personal.household.id, key="another-key")
    assert first.id == replay.id == duplicate.id
    assert _count(db_path, HOUSEHOLD_INVITATIONS_TABLE) == 1
    with pytest.raises(HouseholdInvitationConflictError):
        _invite(service, clock, personal.household.id, role=HouseholdRole.DEPENDENT)
    with _connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"""
                INSERT INTO {HOUSEHOLD_INVITATIONS_TABLE}
                SELECT ?, household_id, invitee_user_id, invited_by_user_id, invited_by_member_id,
                       proposed_role, status, created_at, expires_at, responded_at, revoked_at,
                       'other', create_payload_fingerprint, terminal_operation, terminal_actor_user_id,
                       terminal_idempotency_key, version
                FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ?
                """,
                (new_household_invitation_id(), first.id),
            )


def test_invitee_listing_get_and_existence_hiding(tmp_path):
    db_path = tmp_path / "reads.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    assert [item.id for item in service.list_pending_invitations_for_actor(202)] == [invitation.id]
    assert service.get_invitation_for_actor(202, invitation.id).id == invitation.id
    failures = []
    for invitation_id in (invitation.id, new_household_invitation_id()):
        with pytest.raises(HouseholdInvitationNotFoundError) as excinfo:
            service.get_invitation_for_actor(404, invitation_id)
        failures.append((type(excinfo.value), str(excinfo.value)))
    assert failures[0] == failures[1]


def test_household_invitation_reads_are_limited_to_owner_and_creating_admin(tmp_path):
    db_path = tmp_path / "household-reads.db"
    personal, clock, _store, service = _seed(db_path, targets=(202, 303, 404, 505))
    _add_member(db_path, personal.household.id, 303, HouseholdRole.ADULT_ADMIN)
    _add_member(db_path, personal.household.id, 404, HouseholdRole.ADULT_MEMBER)
    owner_invite = _invite(service, clock, personal.household.id, invitee=202, key="owner")
    admin_invite = _invite(service, clock, personal.household.id, invitee=505, actor=303, key="admin")

    assert service.get_invitation_for_actor(101, owner_invite.id).id == owner_invite.id
    assert service.get_invitation_for_actor(303, admin_invite.id).id == admin_invite.id
    with pytest.raises(HouseholdInvitationNotFoundError):
        service.get_invitation_for_actor(303, owner_invite.id)
    with pytest.raises(HouseholdInvitationNotFoundError):
        service.get_invitation_for_actor(404, owner_invite.id)


def test_accept_is_atomic_and_replay_safe(tmp_path):
    db_path = tmp_path / "accept.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    first = service.accept_invitation(202, invitation.id, "accept-1")
    second = service.accept_invitation(202, invitation.id, "accept-2")
    assert first.invitation.status is HouseholdInvitationStatus.ACCEPTED
    assert second.household_member_id == first.household_member_id
    with _connect(db_path) as conn:
        assert conn.execute(
            f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = 202 AND status = 'active'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("identity_column", ["user_id", "telegram_id"])
def test_accept_rejects_invitee_deleted_from_supported_legacy_users_schema(tmp_path, identity_column):
    db_path = tmp_path / f"deleted-invitee-{identity_column}.db"
    personal, clock, _store, service = _seed_legacy(db_path, identity_column)
    invitation = _invite(service, clock, personal.household.id)
    with _connect(db_path) as conn:
        conn.execute(f"DELETE FROM users WHERE {identity_column} = ?", (202,))

    for key in ("accept-missing-1", "accept-missing-2"):
        with pytest.raises(HouseholdInvitationNotFoundError, match="invitation unavailable"):
            service.accept_invitation(202, invitation.id, key)

    with _connect(db_path) as conn:
        assert conn.execute(
            f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = 202"
        ).fetchone()[0] == 0
        row = conn.execute(
            f"SELECT status, responded_at, terminal_operation FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ?",
            (invitation.id,),
        ).fetchone()
    assert tuple(row) == ("pending", None, None)


def test_accept_fails_closed_for_unknown_users_identity_schema(tmp_path):
    db_path = tmp_path / "unknown-users-schema.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    with _connect(db_path) as conn:
        conn.execute("ALTER TABLE users RENAME TO legacy_users")
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")

    with pytest.raises(HouseholdInvitationIntegrityError, match="users identity unavailable"):
        service.accept_invitation(202, invitation.id, "accept-unknown-schema")

    with _connect(db_path) as conn:
        assert conn.execute(
            f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = 202"
        ).fetchone()[0] == 0
        assert conn.execute(
            f"SELECT status FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ?", (invitation.id,)
        ).fetchone()[0] == "pending"


def test_accept_existence_check_and_membership_insert_share_write_transaction(tmp_path, monkeypatch):
    db_path = tmp_path / "accept-delete-race.db"
    personal, clock, store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    checked = threading.Event()
    release = threading.Event()
    original_user_exists = store._user_exists

    def pause_after_check(conn, user_id):
        exists = original_user_exists(conn, user_id)
        checked.set()
        assert release.wait(timeout=5)
        return exists

    monkeypatch.setattr(store, "_user_exists", pause_after_check)
    outcomes = []

    def accept():
        try:
            outcomes.append(service.accept_invitation(202, invitation.id, "accept-race"))
        except Exception as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)

    thread = threading.Thread(target=accept)
    thread.start()
    assert checked.wait(timeout=5)
    try:
        with sqlite3.connect(db_path, timeout=0.05) as conn:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                conn.execute("DELETE FROM users WHERE user_id = ?", (202,))
    finally:
        release.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(outcomes) == 1 and not isinstance(outcomes[0], Exception)
    with _connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users WHERE user_id = ?", (202,)).fetchone()[0] == 1
        assert conn.execute(
            f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = 202 AND status = 'active'"
        ).fetchone()[0] == 1


@pytest.mark.parametrize("role", [HouseholdRole.ADULT_ADMIN, HouseholdRole.ADULT_MEMBER, HouseholdRole.DEPENDENT])
def test_accept_preserves_the_proposed_role(tmp_path, role):
    db_path = tmp_path / f"accept-{role.value}.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id, role=role)
    service.accept_invitation(202, invitation.id, "accept")
    with _connect(db_path) as conn:
        row = conn.execute(
            f"SELECT role FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = 202 AND status = 'active'"
        ).fetchone()
    assert row[0] == role.value


def test_wrong_actor_and_terminal_or_expired_invites_cannot_be_accepted(tmp_path):
    db_path = tmp_path / "terminal.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    with pytest.raises(HouseholdInvitationNotFoundError):
        service.accept_invitation(303, invitation.id, "wrong")
    service.refuse_invitation(202, invitation.id, "refuse")
    with pytest.raises(HouseholdInvitationStateError):
        service.accept_invitation(202, invitation.id, "late")
    expired = _invite(service, clock, personal.household.id, invitee=303, key="exp")
    clock.value += timedelta(days=2)
    with pytest.raises(HouseholdInvitationStateError):
        service.accept_invitation(303, expired.id, "late")
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT status FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ?", (expired.id,)).fetchone()[0] == "expired"


def test_accept_rejects_existing_membership_in_another_household(tmp_path):
    db_path = tmp_path / "existing.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(202)
    with pytest.raises(HouseholdInvitationConflictError):
        service.accept_invitation(202, invitation.id, "accept")
    assert service.get_invitation_for_actor(202, invitation.id).status is HouseholdInvitationStatus.PENDING


def test_create_rejects_invitee_with_existing_household(tmp_path):
    db_path = tmp_path / "existing-at-create.db"
    personal, clock, _store, service = _seed(db_path)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(202)
    with pytest.raises(HouseholdInvitationConflictError):
        _invite(service, clock, personal.household.id)


def test_concurrent_accept_creates_one_membership(tmp_path):
    db_path = tmp_path / "concurrent.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    outcomes = []
    lock = threading.Lock()

    def worker(key):
        try:
            result = service.accept_invitation(202, invitation.id, key)
        except Exception as exc:  # pragma: no cover - asserted below
            result = exc
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(f"accept-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert all(not isinstance(item, Exception) for item in outcomes)
    assert len({item.household_member_id for item in outcomes}) == 1
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = 202").fetchone()[0] == 1


@pytest.mark.parametrize("trigger_target", ["membership", "invitation"])
def test_accept_failure_rolls_back_membership_and_status(tmp_path, trigger_target):
    db_path = tmp_path / f"rollback-{trigger_target}.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    with _connect(db_path) as conn:
        if trigger_target == "membership":
            conn.execute(
                f"CREATE TRIGGER fail_accept BEFORE INSERT ON {HOUSEHOLD_MEMBERS_TABLE} "
                "WHEN NEW.linked_user_id = 202 BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
        else:
            conn.execute(
                f"CREATE TRIGGER fail_accept BEFORE UPDATE ON {HOUSEHOLD_INVITATIONS_TABLE} "
                "WHEN NEW.status = 'accepted' BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
    with pytest.raises(HouseholdInvitationConflictError):
        service.accept_invitation(202, invitation.id, "accept")
    with _connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = 202").fetchone()[0] == 0
        assert conn.execute(f"SELECT status FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ?", (invitation.id,)).fetchone()[0] == "pending"


def test_refuse_is_invitee_only_idempotent_and_reinvite_preserves_history(tmp_path):
    db_path = tmp_path / "refuse.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    with pytest.raises(HouseholdInvitationNotFoundError):
        service.refuse_invitation(303, invitation.id, "wrong")
    first = service.refuse_invitation(202, invitation.id, "refuse-1")
    replay = service.refuse_invitation(202, invitation.id, "refuse-2")
    reinvite = _invite(service, clock, personal.household.id, key="create-2")
    assert first.status is replay.status is HouseholdInvitationStatus.REFUSED
    assert reinvite.id != invitation.id
    assert _count(db_path, HOUSEHOLD_INVITATIONS_TABLE) == 2


def test_terminal_states_cannot_be_reversed(tmp_path):
    db_path = tmp_path / "terminal-transitions.db"
    personal, clock, _store, service = _seed(db_path, targets=(202, 303))
    accepted = _invite(service, clock, personal.household.id)
    service.accept_invitation(202, accepted.id, "accept")
    with pytest.raises(HouseholdInvitationStateError):
        service.refuse_invitation(202, accepted.id, "refuse")
    refused = _invite(service, clock, personal.household.id, invitee=303, key="refused")
    service.refuse_invitation(303, refused.id, "refuse")
    with pytest.raises(HouseholdInvitationStateError):
        service.accept_invitation(303, refused.id, "accept")
    revoked = _invite(service, clock, personal.household.id, invitee=303, key="revoked")
    service.revoke_invitation(101, revoked.id, "revoke")
    with pytest.raises(HouseholdInvitationStateError):
        service.accept_invitation(303, revoked.id, "accept-again")


def test_owner_and_admin_revoke_policy_and_reinvite(tmp_path):
    db_path = tmp_path / "revoke.db"
    personal, clock, _store, service = _seed(db_path, targets=(202, 303, 404, 505))
    _add_member(db_path, personal.household.id, 303, HouseholdRole.ADULT_ADMIN)
    admin_invite = _invite(service, clock, personal.household.id, invitee=202, actor=303, key="admin")
    assert service.revoke_invitation(303, admin_invite.id, "revoke").status is HouseholdInvitationStatus.REVOKED
    assert service.revoke_invitation(303, admin_invite.id, "replay").status is HouseholdInvitationStatus.REVOKED
    privileged = _invite(service, clock, personal.household.id, invitee=404, role=HouseholdRole.ADULT_ADMIN, key="priv")
    with pytest.raises(HouseholdInvitationAccessError):
        service.revoke_invitation(303, privileged.id, "forbidden")
    assert service.revoke_invitation(101, privileged.id, "owner").status is HouseholdInvitationStatus.REVOKED
    assert _invite(service, clock, personal.household.id, invitee=404, role=HouseholdRole.ADULT_ADMIN, key="again").id != privileged.id


def test_expiry_is_lazy_terminal_and_allows_reinvite(tmp_path):
    db_path = tmp_path / "expired.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    clock.value += timedelta(days=2)
    assert service.list_pending_invitations_for_actor(202) == []
    assert service.get_invitation_for_actor(202, invitation.id).status is HouseholdInvitationStatus.EXPIRED
    reinvite = _invite(service, clock, personal.household.id, key="new")
    assert reinvite.id != invitation.id
    assert _count(db_path, HOUSEHOLD_INVITATIONS_TABLE) == 2


def test_inactive_household_invitee_paths_fail_closed(tmp_path):
    db_path = tmp_path / "inactive-household.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET status = 'disabled' WHERE id = ?", (personal.household.id,))
    assert service.list_pending_invitations_for_actor(202) == []
    with pytest.raises(HouseholdInvitationNotFoundError):
        service.get_invitation_for_actor(202, invitation.id)
    with pytest.raises(HouseholdInvitationNotFoundError):
        service.refuse_invitation(202, invitation.id, "refuse")


def test_foreign_and_random_revoke_are_indistinguishable(tmp_path):
    db_path = tmp_path / "foreign-revoke.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    HealBiteHouseholdStore(db_path=db_path).get_or_create_personal_household(404)
    failures = []
    for invitation_id in (invitation.id, new_household_invitation_id()):
        with pytest.raises(HouseholdInvitationNotFoundError) as excinfo:
            service.revoke_invitation(404, invitation_id, "revoke")
        failures.append((type(excinfo.value), str(excinfo.value)))
    assert failures[0] == failures[1]


def test_corrupt_owner_pointer_fails_closed_for_invitation_paths(tmp_path):
    db_path = tmp_path / "owner-corruption.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id)
    with _connect(db_path) as conn:
        conn.execute(f"UPDATE {HOUSEHOLDS_TABLE} SET owner_user_id = 999 WHERE id = ?", (personal.household.id,))
    with pytest.raises(HouseholdInvitationIntegrityError, match="invalid household owner"):
        _invite(service, clock, personal.household.id, invitee=303, key="corrupt")
    with pytest.raises(HouseholdInvitationNotFoundError):
        service.get_invitation_for_actor(202, invitation.id)


def test_invitation_failures_do_not_log_ids_or_idempotency_keys(tmp_path, caplog, capsys):
    db_path = tmp_path / "privacy.db"
    personal, clock, _store, service = _seed(db_path)
    invitation = _invite(service, clock, personal.household.id, key="private-key")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(HouseholdInvitationNotFoundError) as excinfo:
        service.get_invitation_for_actor(404, invitation.id)
    captured = capsys.readouterr()
    combined = "\n".join((str(excinfo.value), captured.out, captured.err, caplog.text))
    assert invitation.id not in combined
    assert personal.household.id not in combined
    assert "private-key" not in combined
    assert "404" not in combined


def test_public_service_is_the_only_non_test_invitation_entrypoint():
    source = Path("gateway/healbite_household_invitations.py").read_text(encoding="utf-8")
    assert "class HealBiteHouseholdInvitationService" in source
    assert "def get_invitation_for_actor" in source
    assert "def _get_for_actor" in source
    assert "def get_invitation_by_id" not in source
