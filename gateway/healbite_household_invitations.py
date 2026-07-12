from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from gateway.healbite_household_schema import (
    HOUSEHOLD_INVITATIONS_TABLE,
    HOUSEHOLD_MEMBERS_TABLE,
    HOUSEHOLDS_TABLE,
    HouseholdInvitationStatus,
    HouseholdMemberStatus,
    HouseholdMemberType,
    HouseholdRole,
    HouseholdStatus,
    new_household_invitation_id,
    new_household_member_id,
    require_canonical_uuid4,
)
from gateway.healbite_households import HealBiteHouseholdStore

_MAX_LIFETIME = timedelta(days=30)
_CREATE_ROLES = frozenset({HouseholdRole.ADULT_ADMIN, HouseholdRole.ADULT_MEMBER, HouseholdRole.DEPENDENT})
_ADMIN_CREATE_ROLES = frozenset({HouseholdRole.ADULT_MEMBER, HouseholdRole.DEPENDENT})


class HouseholdInvitationError(Exception):
    pass


class HouseholdInvitationValidationError(ValueError):
    pass


class HouseholdInvitationAccessError(HouseholdInvitationError):
    pass


class HouseholdInvitationNotFoundError(HouseholdInvitationError):
    pass


class HouseholdInvitationConflictError(HouseholdInvitationError):
    pass


class HouseholdInvitationStateError(HouseholdInvitationError):
    pass


class HouseholdInvitationIntegrityError(HouseholdInvitationError):
    pass


@dataclass(frozen=True, slots=True)
class HouseholdInvitation:
    id: str
    household_id: str
    invitee_user_id: int
    invited_by_user_id: int
    invited_by_member_id: str
    proposed_role: HouseholdRole
    status: HouseholdInvitationStatus
    created_at: str
    expires_at: str
    responded_at: str | None
    revoked_at: str | None
    version: int


@dataclass(frozen=True, slots=True)
class HouseholdInvitationAcceptance:
    invitation: HouseholdInvitation
    household_member_id: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HouseholdInvitationValidationError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _expiry(value: datetime | str, *, now: datetime) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HouseholdInvitationValidationError("invalid invitation expiry") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise HouseholdInvitationValidationError("invalid invitation expiry")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HouseholdInvitationValidationError("invalid invitation expiry")
    parsed = parsed.astimezone(timezone.utc)
    if parsed <= now or parsed - now > _MAX_LIFETIME:
        raise HouseholdInvitationValidationError("invalid invitation expiry")
    return _timestamp(parsed)


def _positive_int(value: object) -> int:
    if isinstance(value, bool):
        raise HouseholdInvitationValidationError("invalid actor")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise HouseholdInvitationValidationError("invalid actor") from exc
    if normalized <= 0 or normalized > 2**63 - 1:
        raise HouseholdInvitationValidationError("invalid actor")
    return normalized


def _role(value: HouseholdRole | str) -> HouseholdRole:
    try:
        role = value if isinstance(value, HouseholdRole) else HouseholdRole(str(value))
    except ValueError as exc:
        raise HouseholdInvitationValidationError("invalid invitation role") from exc
    if role not in _CREATE_ROLES:
        raise HouseholdInvitationValidationError("invalid invitation role")
    return role


def _key(value: object) -> str:
    normalized = str(value).strip()
    if not 1 <= len(normalized) <= 128:
        raise HouseholdInvitationValidationError("invalid idempotency key")
    return normalized


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class HealBiteHouseholdInvitationStore:
    """Internal transactional store; callers must use the actor-scoped service."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        ensure_schema_on_init: bool = True,
        invitation_id_factory: Callable[[], str] = new_household_invitation_id,
        member_id_factory: Callable[[], str] = new_household_member_id,
        now_factory: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._households = HealBiteHouseholdStore(db_path=db_path, ensure_schema_on_init=ensure_schema_on_init)
        self.db_path = self._households.db_path
        self._invitation_id_factory = invitation_id_factory
        self._member_id_factory = member_id_factory
        self._now_factory = now_factory

    def _now(self) -> datetime:
        value = self._now_factory()
        if value.tzinfo is None or value.utcoffset() is None:
            raise HouseholdInvitationIntegrityError("invalid runtime clock")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _users_identity_column(conn: sqlite3.Connection) -> str:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "user_id" in columns:
            return "user_id"
        if "telegram_id" in columns:
            return "telegram_id"
        raise HouseholdInvitationIntegrityError("users identity unavailable")

    def _user_exists(self, conn: sqlite3.Connection, user_id: int) -> bool:
        column = self._users_identity_column(conn)
        return conn.execute(f"SELECT 1 FROM users WHERE {column} = ? LIMIT 1", (user_id,)).fetchone() is not None

    @staticmethod
    def _active_actor(conn: sqlite3.Connection, actor_user_id: int, household_id: str | None = None) -> sqlite3.Row:
        rows = conn.execute(
            f"""
            SELECT m.id AS member_id, m.household_id, m.role, m.status AS member_status,
                   h.status AS household_status, h.owner_user_id
            FROM {HOUSEHOLD_MEMBERS_TABLE} m
            JOIN {HOUSEHOLDS_TABLE} h ON h.id = m.household_id
            WHERE m.linked_user_id = ? AND m.status = 'active'
            ORDER BY m.created_at, m.id LIMIT 2
            """,
            (actor_user_id,),
        ).fetchall()
        if len(rows) != 1:
            raise HouseholdInvitationAccessError("invitation access denied")
        row = rows[0]
        if household_id is not None and str(row["household_id"]) != household_id:
            raise HouseholdInvitationAccessError("invitation access denied")
        HealBiteHouseholdInvitationStore._assert_household_active(conn, str(row["household_id"]))
        return row

    @staticmethod
    def _assert_household_active(conn: sqlite3.Connection, household_id: str) -> None:
        household = conn.execute(
            f"SELECT status, owner_user_id FROM {HOUSEHOLDS_TABLE} WHERE id = ? LIMIT 1",
            (household_id,),
        ).fetchone()
        if household is None or str(household["status"]) != HouseholdStatus.ACTIVE.value:
            raise HouseholdInvitationAccessError("invitation access denied")
        owners = conn.execute(
            f"SELECT linked_user_id FROM {HOUSEHOLD_MEMBERS_TABLE} "
            "WHERE household_id = ? AND role = 'owner' AND status = 'active' LIMIT 2",
            (household_id,),
        ).fetchall()
        if len(owners) != 1 or int(owners[0][0]) != int(household["owner_user_id"]):
            raise HouseholdInvitationIntegrityError("invalid household owner")

    @staticmethod
    def _row(row: sqlite3.Row) -> HouseholdInvitation:
        return HouseholdInvitation(
            id=str(row["id"]),
            household_id=str(row["household_id"]),
            invitee_user_id=int(row["invitee_user_id"]),
            invited_by_user_id=int(row["invited_by_user_id"]),
            invited_by_member_id=str(row["invited_by_member_id"]),
            proposed_role=HouseholdRole(str(row["proposed_role"])),
            status=HouseholdInvitationStatus(str(row["status"])),
            created_at=str(row["created_at"]),
            expires_at=str(row["expires_at"]),
            responded_at=None if row["responded_at"] is None else str(row["responded_at"]),
            revoked_at=None if row["revoked_at"] is None else str(row["revoked_at"]),
            version=int(row["version"]),
        )

    @staticmethod
    def _load(conn: sqlite3.Connection, invitation_id: str) -> sqlite3.Row | None:
        return conn.execute(
            f"SELECT * FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ? LIMIT 1",
            (invitation_id,),
        ).fetchone()

    @staticmethod
    def _expire_due(conn: sqlite3.Connection, *, now_text: str, invitation_id: str | None = None, invitee: int | None = None) -> None:
        clauses = ["status = 'pending'", "expires_at <= ?"]
        params: list[object] = [now_text, now_text]
        if invitation_id is not None:
            clauses.append("id = ?")
            params.append(invitation_id)
        if invitee is not None:
            clauses.append("invitee_user_id = ?")
            params.append(invitee)
        conn.execute(
            f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} "
            "SET status = 'expired', responded_at = ?, version = version + 1 WHERE " + " AND ".join(clauses),
            tuple(params),
        )

    @staticmethod
    def _assert_create_permission(actor_role: HouseholdRole, proposed_role: HouseholdRole) -> None:
        if actor_role is HouseholdRole.OWNER and proposed_role in _CREATE_ROLES:
            return
        if actor_role is HouseholdRole.ADULT_ADMIN and proposed_role in _ADMIN_CREATE_ROLES:
            return
        raise HouseholdInvitationAccessError("invitation access denied")

    def _create(
        self,
        actor_user_id: int,
        household_id: str,
        invitee_user_id: int,
        proposed_role: HouseholdRole,
        created_at: str,
        expires_at: str,
        idempotency_key: str,
        payload_fingerprint: str,
    ) -> HouseholdInvitation:
        now_text = created_at
        with self._households._owned_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                actor = self._active_actor(conn, actor_user_id, household_id)
                self._assert_create_permission(HouseholdRole(str(actor["role"])), proposed_role)
                if actor_user_id == invitee_user_id or not self._user_exists(conn, invitee_user_id):
                    raise HouseholdInvitationValidationError("invalid invitation target")
                if conn.execute(
                    f"SELECT 1 FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = ? LIMIT 1",
                    (invitee_user_id,),
                ).fetchone() is not None:
                    raise HouseholdInvitationConflictError("invitation target unavailable")
                self._expire_due(conn, now_text=now_text, invitee=invitee_user_id)
                replay = conn.execute(
                    f"SELECT * FROM {HOUSEHOLD_INVITATIONS_TABLE} "
                    "WHERE household_id = ? AND invited_by_member_id = ? AND create_idempotency_key = ? LIMIT 1",
                    (household_id, str(actor["member_id"]), idempotency_key),
                ).fetchone()
                if replay is not None:
                    if str(replay["create_payload_fingerprint"]) != payload_fingerprint:
                        raise HouseholdInvitationConflictError("idempotency conflict")
                    conn.commit()
                    return self._row(replay)
                existing = conn.execute(
                    f"SELECT * FROM {HOUSEHOLD_INVITATIONS_TABLE} "
                    "WHERE household_id = ? AND invitee_user_id = ? AND proposed_role = ? AND status = 'pending' LIMIT 1",
                    (household_id, invitee_user_id, proposed_role.value),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return self._row(existing)
                invitation_id = require_canonical_uuid4(self._invitation_id_factory())
                conn.execute(
                    f"""
                    INSERT INTO {HOUSEHOLD_INVITATIONS_TABLE}
                        (id, household_id, invitee_user_id, invited_by_user_id, invited_by_member_id,
                         proposed_role, status, created_at, expires_at, responded_at, revoked_at,
                         create_idempotency_key, create_payload_fingerprint, terminal_operation,
                         terminal_actor_user_id, terminal_idempotency_key, version)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL, ?, ?, NULL, NULL, NULL, 1)
                    """,
                    (
                        invitation_id,
                        household_id,
                        invitee_user_id,
                        actor_user_id,
                        str(actor["member_id"]),
                        proposed_role.value,
                        now_text,
                        expires_at,
                        idempotency_key,
                        payload_fingerprint,
                    ),
                )
                created = self._load(conn, invitation_id)
                if created is None:
                    raise HouseholdInvitationIntegrityError("invitation creation failed")
                conn.commit()
                return self._row(created)
            except sqlite3.IntegrityError as exc:
                self._households._rollback_preserving_error(conn)
                raise HouseholdInvitationConflictError("invitation creation conflict") from exc
            except Exception:
                self._households._rollback_preserving_error(conn)
                raise

    def _list_pending_for_invitee(self, actor_user_id: int) -> list[HouseholdInvitation]:
        now_text = _timestamp(self._now())
        with self._households._owned_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not self._user_exists(conn, actor_user_id):
                    raise HouseholdInvitationAccessError("invitation access denied")
                self._expire_due(conn, now_text=now_text, invitee=actor_user_id)
                rows = conn.execute(
                    f"SELECT i.* FROM {HOUSEHOLD_INVITATIONS_TABLE} i "
                    f"JOIN {HOUSEHOLDS_TABLE} h ON h.id = i.household_id "
                    "WHERE i.invitee_user_id = ? AND i.status = 'pending' AND h.status = 'active' "
                    "ORDER BY i.created_at, i.id",
                    (actor_user_id,),
                ).fetchall()
                for row in rows:
                    self._assert_household_active(conn, str(row["household_id"]))
                conn.commit()
                return [self._row(row) for row in rows]
            except Exception:
                self._households._rollback_preserving_error(conn)
                raise

    def _get_for_actor(self, actor_user_id: int, invitation_id: str) -> HouseholdInvitation:
        now_text = _timestamp(self._now())
        with self._households._owned_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"""
                    SELECT i.* FROM {HOUSEHOLD_INVITATIONS_TABLE} i
                    WHERE i.id = ? AND (
                        i.invitee_user_id = ?
                        OR EXISTS (
                            SELECT 1 FROM {HOUSEHOLD_MEMBERS_TABLE} m
                            JOIN {HOUSEHOLDS_TABLE} h ON h.id = m.household_id
                            WHERE m.household_id = i.household_id
                              AND m.linked_user_id = ?
                              AND m.status = 'active'
                              AND h.status = 'active'
                              AND (
                                  m.role = 'owner'
                                  OR (m.role = 'adult_admin' AND i.invited_by_user_id = ?)
                              )
                        )
                    )
                    LIMIT 1
                    """,
                    (invitation_id, actor_user_id, actor_user_id, actor_user_id),
                ).fetchone()
                if row is None:
                    raise HouseholdInvitationNotFoundError("invitation unavailable")
                try:
                    self._assert_household_active(conn, str(row["household_id"]))
                except HouseholdInvitationError:
                    raise HouseholdInvitationNotFoundError("invitation unavailable") from None
                self._expire_due(conn, now_text=now_text, invitation_id=invitation_id)
                current = self._load(conn, invitation_id)
                if current is None:
                    raise HouseholdInvitationIntegrityError("invitation disappeared")
                conn.commit()
                return self._row(current)
            except Exception:
                self._households._rollback_preserving_error(conn)
                raise

    def _accept(self, actor_user_id: int, invitation_id: str, idempotency_key: str) -> HouseholdInvitationAcceptance:
        now_text = _timestamp(self._now())
        with self._households._owned_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT * FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ? AND invitee_user_id = ? LIMIT 1",
                    (invitation_id, actor_user_id),
                ).fetchone()
                if row is None:
                    raise HouseholdInvitationNotFoundError("invitation unavailable")
                self._expire_due(conn, now_text=now_text, invitation_id=invitation_id)
                row = self._load(conn, invitation_id)
                assert row is not None
                if str(row["status"]) == HouseholdInvitationStatus.EXPIRED.value:
                    conn.commit()
                    raise HouseholdInvitationStateError("invitation unavailable")
                try:
                    self._assert_household_active(conn, str(row["household_id"]))
                except HouseholdInvitationAccessError:
                    raise HouseholdInvitationStateError("invitation unavailable") from None
                if str(row["status"]) == HouseholdInvitationStatus.ACCEPTED.value:
                    member = conn.execute(
                        f"SELECT id FROM {HOUSEHOLD_MEMBERS_TABLE} "
                        "WHERE household_id = ? AND linked_user_id = ? AND status = 'active' LIMIT 1",
                        (str(row["household_id"]), actor_user_id),
                    ).fetchone()
                    if member is None:
                        raise HouseholdInvitationIntegrityError("accepted membership missing")
                    conn.commit()
                    return HouseholdInvitationAcceptance(self._row(row), str(member["id"]))
                if str(row["status"]) != HouseholdInvitationStatus.PENDING.value:
                    raise HouseholdInvitationStateError("invitation unavailable")
                if conn.execute(
                    f"SELECT 1 FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE linked_user_id = ? LIMIT 1",
                    (actor_user_id,),
                ).fetchone() is not None:
                    raise HouseholdInvitationConflictError("invitation target unavailable")
                member_id = require_canonical_uuid4(self._member_id_factory())
                conn.execute(
                    f"""
                    INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                        (id, household_id, linked_user_id, display_name, member_type, role, status,
                         age_band, created_at, updated_at, version)
                    VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, 1)
                    """,
                    (
                        member_id,
                        str(row["household_id"]),
                        actor_user_id,
                        HouseholdMemberType.LINKED_ADULT.value,
                        str(row["proposed_role"]),
                        HouseholdMemberStatus.ACTIVE.value,
                        now_text,
                        now_text,
                    ),
                )
                updated = conn.execute(
                    f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} "
                    "SET status = 'accepted', responded_at = ?, terminal_operation = 'accept', "
                    "terminal_actor_user_id = ?, terminal_idempotency_key = ?, version = version + 1 "
                    "WHERE id = ? AND status = 'pending' AND version = ?",
                    (now_text, actor_user_id, idempotency_key, invitation_id, int(row["version"])),
                )
                if updated.rowcount != 1:
                    raise HouseholdInvitationConflictError("invitation changed concurrently")
                accepted = self._load(conn, invitation_id)
                assert accepted is not None
                conn.commit()
                return HouseholdInvitationAcceptance(self._row(accepted), member_id)
            except sqlite3.IntegrityError as exc:
                self._households._rollback_preserving_error(conn)
                raise HouseholdInvitationConflictError("invitation acceptance conflict") from exc
            except Exception:
                self._households._rollback_preserving_error(conn)
                raise

    def _respond(self, actor_user_id: int, invitation_id: str, idempotency_key: str) -> HouseholdInvitation:
        now_text = _timestamp(self._now())
        with self._households._owned_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT * FROM {HOUSEHOLD_INVITATIONS_TABLE} WHERE id = ? AND invitee_user_id = ? LIMIT 1",
                    (invitation_id, actor_user_id),
                ).fetchone()
                if row is None:
                    raise HouseholdInvitationNotFoundError("invitation unavailable")
                self._expire_due(conn, now_text=now_text, invitation_id=invitation_id)
                row = self._load(conn, invitation_id)
                assert row is not None
                if str(row["status"]) == HouseholdInvitationStatus.EXPIRED.value:
                    conn.commit()
                    raise HouseholdInvitationStateError("invitation unavailable")
                try:
                    self._assert_household_active(conn, str(row["household_id"]))
                except HouseholdInvitationError:
                    raise HouseholdInvitationNotFoundError("invitation unavailable") from None
                if str(row["status"]) == HouseholdInvitationStatus.REFUSED.value:
                    conn.commit()
                    return self._row(row)
                if str(row["status"]) != HouseholdInvitationStatus.PENDING.value:
                    raise HouseholdInvitationStateError("invitation unavailable")
                updated = conn.execute(
                    f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} "
                    "SET status = 'refused', responded_at = ?, terminal_operation = 'refuse', "
                    "terminal_actor_user_id = ?, terminal_idempotency_key = ?, version = version + 1 "
                    "WHERE id = ? AND status = 'pending'",
                    (now_text, actor_user_id, idempotency_key, invitation_id),
                )
                if updated.rowcount != 1:
                    raise HouseholdInvitationConflictError("invitation changed concurrently")
                current = self._load(conn, invitation_id)
                assert current is not None
                conn.commit()
                return self._row(current)
            except Exception:
                self._households._rollback_preserving_error(conn)
                raise

    def _revoke(self, actor_user_id: int, invitation_id: str, idempotency_key: str) -> HouseholdInvitation:
        now_text = _timestamp(self._now())
        with self._households._owned_connection() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"""
                    SELECT i.*, m.role AS actor_role
                    FROM {HOUSEHOLD_INVITATIONS_TABLE} i
                    JOIN {HOUSEHOLD_MEMBERS_TABLE} m ON m.household_id = i.household_id
                    JOIN {HOUSEHOLDS_TABLE} h ON h.id = i.household_id
                    WHERE i.id = ? AND m.linked_user_id = ? AND m.status = 'active' AND h.status = 'active'
                    LIMIT 1
                    """,
                    (invitation_id, actor_user_id),
                ).fetchone()
                if row is None:
                    raise HouseholdInvitationNotFoundError("invitation unavailable")
                try:
                    self._assert_household_active(conn, str(row["household_id"]))
                except HouseholdInvitationError:
                    raise HouseholdInvitationNotFoundError("invitation unavailable") from None
                role = HouseholdRole(str(row["actor_role"]))
                allowed = role is HouseholdRole.OWNER or (
                    role is HouseholdRole.ADULT_ADMIN
                    and int(row["invited_by_user_id"]) == actor_user_id
                    and HouseholdRole(str(row["proposed_role"])) in _ADMIN_CREATE_ROLES
                )
                if not allowed:
                    raise HouseholdInvitationAccessError("invitation access denied")
                self._expire_due(conn, now_text=now_text, invitation_id=invitation_id)
                row = self._load(conn, invitation_id)
                assert row is not None
                if str(row["status"]) == HouseholdInvitationStatus.EXPIRED.value:
                    conn.commit()
                    raise HouseholdInvitationStateError("invitation unavailable")
                if str(row["status"]) == HouseholdInvitationStatus.REVOKED.value:
                    conn.commit()
                    return self._row(row)
                if str(row["status"]) != HouseholdInvitationStatus.PENDING.value:
                    raise HouseholdInvitationStateError("invitation unavailable")
                updated = conn.execute(
                    f"UPDATE {HOUSEHOLD_INVITATIONS_TABLE} "
                    "SET status = 'revoked', revoked_at = ?, terminal_operation = 'revoke', "
                    "terminal_actor_user_id = ?, terminal_idempotency_key = ?, version = version + 1 "
                    "WHERE id = ? AND status = 'pending'",
                    (now_text, actor_user_id, idempotency_key, invitation_id),
                )
                if updated.rowcount != 1:
                    raise HouseholdInvitationConflictError("invitation changed concurrently")
                current = self._load(conn, invitation_id)
                assert current is not None
                conn.commit()
                return self._row(current)
            except Exception:
                self._households._rollback_preserving_error(conn)
                raise


class HealBiteHouseholdInvitationService:
    """Actor-scoped Family invitation API backed by SQLite membership and ownership."""

    def __init__(self, store: HealBiteHouseholdInvitationStore) -> None:
        self._store = store

    def create_household_invitation(
        self,
        actor_user_id: object,
        household_id: str,
        invitee_user_id: object,
        proposed_role: HouseholdRole | str,
        expires_at: datetime | str,
        idempotency_key: object,
    ) -> HouseholdInvitation:
        actor = _positive_int(actor_user_id)
        invitee = _positive_int(invitee_user_id)
        household = require_canonical_uuid4(household_id)
        role = _role(proposed_role)
        now = self._store._now()
        expiry = _expiry(expires_at, now=now)
        key = _key(idempotency_key)
        payload = _fingerprint({"invitee": invitee, "role": role.value, "expires_at": expiry})
        return self._store._create(actor, household, invitee, role, _timestamp(now), expiry, key, payload)

    def list_pending_invitations_for_actor(self, actor_user_id: object) -> list[HouseholdInvitation]:
        return self._store._list_pending_for_invitee(_positive_int(actor_user_id))

    def get_invitation_for_actor(self, actor_user_id: object, invitation_id: str) -> HouseholdInvitation:
        return self._store._get_for_actor(_positive_int(actor_user_id), require_canonical_uuid4(invitation_id))

    def accept_invitation(self, actor_user_id: object, invitation_id: str, idempotency_key: object) -> HouseholdInvitationAcceptance:
        return self._store._accept(
            _positive_int(actor_user_id),
            require_canonical_uuid4(invitation_id),
            _key(idempotency_key),
        )

    def refuse_invitation(self, actor_user_id: object, invitation_id: str, idempotency_key: object) -> HouseholdInvitation:
        return self._store._respond(
            _positive_int(actor_user_id),
            require_canonical_uuid4(invitation_id),
            _key(idempotency_key),
        )

    def revoke_invitation(self, actor_user_id: object, invitation_id: str, idempotency_key: object) -> HouseholdInvitation:
        return self._store._revoke(
            _positive_int(actor_user_id),
            require_canonical_uuid4(invitation_id),
            _key(idempotency_key),
        )
