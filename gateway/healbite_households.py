from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_household_schema import (
    HOUSEHOLD_MEMBERS_TABLE,
    HOUSEHOLD_MEMBER_STATUSES,
    HOUSEHOLD_MEMBER_TYPES,
    HOUSEHOLD_ROLES,
    HOUSEHOLD_STATUSES,
    HOUSEHOLDS_TABLE,
    HouseholdMemberStatus,
    HouseholdMemberType,
    HouseholdRole,
    HouseholdStatus,
    new_household_id,
    new_household_member_id,
    require_canonical_uuid4,
)

_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_ID_REGENERATION_ATTEMPTS = 5
_SQLITE_MAX_INTEGER = 9223372036854775807


def _quoted_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {HOUSEHOLDS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    owner_user_id INTEGER NOT NULL,
    name TEXT NULL,
    status TEXT NOT NULL CHECK (status IN ({_quoted_values(HOUSEHOLD_STATUSES)})),
    default_timezone TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);
CREATE TABLE IF NOT EXISTS {HOUSEHOLD_MEMBERS_TABLE} (
    id TEXT PRIMARY KEY CHECK (length(id) = 36 AND lower(id) = id),
    household_id TEXT NOT NULL,
    linked_user_id INTEGER NULL,
    display_name TEXT NULL,
    member_type TEXT NOT NULL CHECK (member_type IN ({_quoted_values(HOUSEHOLD_MEMBER_TYPES)})),
    role TEXT NOT NULL CHECK (role IN ({_quoted_values(HOUSEHOLD_ROLES)})),
    status TEXT NOT NULL CHECK (status IN ({_quoted_values(HOUSEHOLD_MEMBER_STATUSES)})),
    age_band TEXT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    FOREIGN KEY (household_id) REFERENCES {HOUSEHOLDS_TABLE}(id) ON DELETE RESTRICT,
    CHECK (
        (status = 'unlinked' AND linked_user_id IS NULL)
        OR status IN ('active', 'disabled', 'removed')
    ),
    CHECK (
        (member_type IN ('primary', 'linked_adult') AND linked_user_id IS NOT NULL)
        OR (member_type IN ('unlinked_adult', 'dependent') AND linked_user_id IS NULL)
    )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_members_active_linked_user
    ON {HOUSEHOLD_MEMBERS_TABLE} (linked_user_id)
    WHERE linked_user_id IS NOT NULL AND status = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_household_members_active_owner
    ON {HOUSEHOLD_MEMBERS_TABLE} (household_id)
    WHERE role = 'owner' AND status = 'active';
"""


class HouseholdError(Exception):
    pass


class HouseholdNotFoundError(HouseholdError):
    pass


class HouseholdValidationError(ValueError):
    pass


class HouseholdIntegrityError(HouseholdError):
    pass


class HouseholdAccessError(HouseholdError):
    pass


@dataclass(slots=True, frozen=True)
class Household:
    id: str
    owner_user_id: int
    name: str | None
    status: HouseholdStatus
    default_timezone: str | None
    created_at: str
    updated_at: str
    version: int


@dataclass(slots=True, frozen=True)
class HouseholdMember:
    id: str
    household_id: str
    linked_user_id: int | None
    display_name: str | None
    member_type: HouseholdMemberType
    role: HouseholdRole
    status: HouseholdMemberStatus
    age_band: str | None
    created_at: str
    updated_at: str
    version: int


@dataclass(slots=True, frozen=True)
class PersonalHousehold:
    household: Household
    member: HouseholdMember
    created: bool = False


@dataclass(slots=True, frozen=True)
class HouseholdContext:
    actor_user_id: int
    household_id: str
    household_member_id: str
    role: HouseholdRole
    member_status: HouseholdMemberStatus
    household_status: HouseholdStatus


@dataclass(slots=True, frozen=True)
class HouseholdFeatureConfig:
    enabled: bool = False
    allowlist: frozenset[int] = frozenset()
    allowlist_valid: bool = True


def _sqlite_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.strftime(_SQLITE_TS_FORMAT)


def _normalize_actor_user_id(value: int) -> int:
    if isinstance(value, bool):
        raise HouseholdValidationError("invalid actor")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HouseholdValidationError("invalid actor") from exc
    if parsed <= 0:
        raise HouseholdValidationError("invalid actor")
    return parsed


def _validated_version(raw: object) -> int:
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise HouseholdIntegrityError("invalid household row") from exc
    if version < 1:
        raise HouseholdIntegrityError("invalid household row")
    return version


def _row_to_household(row: sqlite3.Row) -> Household:
    try:
        household_id = require_canonical_uuid4(str(row["id"]))
        status = HouseholdStatus(str(row["status"]))
    except (TypeError, ValueError) as exc:
        raise HouseholdIntegrityError("invalid household row") from exc
    return Household(
        id=household_id,
        owner_user_id=int(row["owner_user_id"]),
        name=str(row["name"]) if row["name"] is not None else None,
        status=status,
        default_timezone=str(row["default_timezone"]) if row["default_timezone"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        version=_validated_version(row["version"]),
    )


def _row_to_member(row: sqlite3.Row) -> HouseholdMember:
    try:
        member_id = require_canonical_uuid4(str(row["id"]))
        household_id = require_canonical_uuid4(str(row["household_id"]))
        member_type = HouseholdMemberType(str(row["member_type"]))
        role = HouseholdRole(str(row["role"]))
        status = HouseholdMemberStatus(str(row["status"]))
    except (TypeError, ValueError) as exc:
        raise HouseholdIntegrityError("invalid household member row") from exc
    return HouseholdMember(
        id=member_id,
        household_id=household_id,
        linked_user_id=int(row["linked_user_id"]) if row["linked_user_id"] is not None else None,
        display_name=str(row["display_name"]) if row["display_name"] is not None else None,
        member_type=member_type,
        role=role,
        status=status,
        age_band=str(row["age_band"]) if row["age_band"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        version=_validated_version(row["version"]),
    )


def _parse_allowlist(value: str | None) -> tuple[frozenset[int], bool]:
    result: set[int] = set()
    for part in (value or "").replace(";", ",").split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            return frozenset(), False
        parsed = int(token)
        if parsed <= 0 or parsed > _SQLITE_MAX_INTEGER:
            return frozenset(), False
        result.add(parsed)
    return frozenset(result), True


def resolve_users_identity_column(conn: sqlite3.Connection) -> str:
    columns = HealBiteHouseholdStore._table_columns(conn, "users")
    if "user_id" in columns:
        return "user_id"
    if "telegram_id" in columns:
        return "telegram_id"
    raise HouseholdIntegrityError("unsupported users identity schema")


def load_household_feature_config(env: Mapping[str, str] | None = None) -> HouseholdFeatureConfig:
    source = env if env is not None else os.environ
    enabled_raw = str(source.get("HEALBITE_HOUSEHOLDS_ENABLED", "")).strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "on"}
    allowlist, valid = _parse_allowlist(source.get("HEALBITE_HOUSEHOLDS_ALLOWLIST", ""))
    if not valid:
        return HouseholdFeatureConfig(enabled=False, allowlist=frozenset(), allowlist_valid=False)
    return HouseholdFeatureConfig(enabled=enabled, allowlist=allowlist, allowlist_valid=True)


class HealBiteHouseholdStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        household_id_factory: Callable[[], str] = new_household_id,
        member_id_factory: Callable[[], str] = new_household_member_id,
        ensure_schema_on_init: bool = True,
    ) -> None:
        self.db_path = resolve_healbite_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._household_id_factory = household_id_factory
        self._member_id_factory = member_id_factory
        if ensure_schema_on_init:
            self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _owned_connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        except BaseException:
            try:
                conn.close()
            except Exception:
                pass
            raise
        else:
            conn.close()

    @staticmethod
    def _rollback_preserving_error(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except Exception:
            pass

    def ensure_schema(self) -> None:
        with self._owned_connection() as conn:
            conn.executescript(_SCHEMA_SQL)

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            if len(row) > 1
        }

    def _users_identity_column(self, conn: sqlite3.Connection) -> str:
        return resolve_users_identity_column(conn)

    def _actor_exists(self, conn: sqlite3.Connection, actor_user_id: int) -> bool:
        identity_column = self._users_identity_column(conn)
        row = conn.execute(
            f"SELECT 1 FROM users WHERE {identity_column} = ? LIMIT 1",
            (int(actor_user_id),),
        ).fetchone()
        return row is not None

    def _next_household_id(self) -> str:
        return require_canonical_uuid4(self._household_id_factory())

    def _next_member_id(self) -> str:
        return require_canonical_uuid4(self._member_id_factory())

    def _read_household_by_id_internal(self, household_id: str) -> Household | None:
        """Read by opaque ID only after the caller has established authorization."""
        require_canonical_uuid4(household_id)
        with self._owned_connection() as conn:
            household = self._get_household_by_id(conn, household_id)
            if household is not None:
                self._validate_owner_invariant(conn, household)
            return household

    def _get_household_by_id(self, conn: sqlite3.Connection, household_id: str) -> Household | None:
        row = conn.execute(
            f"SELECT * FROM {HOUSEHOLDS_TABLE} WHERE id = ? LIMIT 1",
            (household_id,),
        ).fetchone()
        return _row_to_household(row) if row is not None else None

    def _read_household_for_linked_user_internal(self, linked_user_id: int) -> Household | None:
        """Resolve a linked user for trusted bootstrap and service-layer callers."""
        actor = _normalize_actor_user_id(linked_user_id)
        with self._owned_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT h.* FROM {HOUSEHOLDS_TABLE} h
                JOIN {HOUSEHOLD_MEMBERS_TABLE} m ON m.household_id = h.id
                WHERE m.linked_user_id = ? AND m.status = ?
                ORDER BY h.created_at ASC, h.id ASC
                LIMIT 2
                """,
                (actor, HouseholdMemberStatus.ACTIVE.value),
            ).fetchall()
            if len(rows) > 1:
                raise HouseholdIntegrityError("duplicate active household membership")
            if not rows:
                return None
            household = _row_to_household(rows[0])
            self._validate_owner_invariant(conn, household)
            return household

    def _read_primary_member_for_user_internal(self, linked_user_id: int) -> HouseholdMember | None:
        """Read a primary membership for trusted bootstrap and integrity checks."""
        actor = _normalize_actor_user_id(linked_user_id)
        with self._owned_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {HOUSEHOLD_MEMBERS_TABLE}
                WHERE linked_user_id = ? AND member_type = ? AND status = ?
                ORDER BY created_at ASC, id ASC
                LIMIT 2
                """,
                (actor, HouseholdMemberType.PRIMARY.value, HouseholdMemberStatus.ACTIVE.value),
            ).fetchall()
            if len(rows) > 1:
                raise HouseholdIntegrityError("duplicate active primary membership")
            if not rows:
                return None
            member = _row_to_member(rows[0])
            household = self._get_household_by_id(conn, member.household_id)
            if household is None:
                raise HouseholdIntegrityError("member references missing household")
            self._validate_owner_invariant(conn, household)
            if member.role is not HouseholdRole.OWNER:
                raise HouseholdIntegrityError("actor membership is not primary owner")
            return member

    def _list_household_members_internal(self, household_id: str) -> list[HouseholdMember]:
        """List by opaque ID only after the caller has established authorization."""
        require_canonical_uuid4(household_id)
        with self._owned_connection() as conn:
            household = self._get_household_by_id(conn, household_id)
            if household is None:
                return []
            self._validate_owner_invariant(conn, household)
            rows = conn.execute(
                f"""
                SELECT * FROM {HOUSEHOLD_MEMBERS_TABLE}
                WHERE household_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (household_id,),
            ).fetchall()
        return [_row_to_member(row) for row in rows]

    def get_or_create_personal_household(self, actor_user_id: int) -> PersonalHousehold:
        actor = _normalize_actor_user_id(actor_user_id)
        for attempt in range(_MAX_ID_REGENERATION_ATTEMPTS):
            with self._owned_connection() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if not self._actor_exists(conn, actor):
                        raise HouseholdValidationError("unknown actor")
                    existing = self._load_personal_household_for_actor(conn, actor)
                    if existing is not None:
                        conn.commit()
                        return PersonalHousehold(existing[0], existing[1], created=False)
                    if self._load_any_membership_for_actor(conn, actor) is not None:
                        raise HouseholdIntegrityError("actor membership is not active")
                    now = _sqlite_timestamp()
                    household_id = self._next_household_id()
                    member_id = self._next_member_id()
                    conn.execute(
                        f"""
                        INSERT INTO {HOUSEHOLDS_TABLE}
                            (id, owner_user_id, name, status, default_timezone, created_at, updated_at, version)
                        VALUES (?, ?, NULL, ?, NULL, ?, ?, 1)
                        """,
                        (household_id, actor, HouseholdStatus.ACTIVE.value, now, now),
                    )
                    conn.execute(
                        f"""
                        INSERT INTO {HOUSEHOLD_MEMBERS_TABLE}
                            (id, household_id, linked_user_id, display_name, member_type, role, status,
                             age_band, created_at, updated_at, version)
                        VALUES (?, ?, ?, NULL, ?, ?, ?, NULL, ?, ?, 1)
                        """,
                        (
                            member_id,
                            household_id,
                            actor,
                            HouseholdMemberType.PRIMARY.value,
                            HouseholdRole.OWNER.value,
                            HouseholdMemberStatus.ACTIVE.value,
                            now,
                            now,
                        ),
                    )
                    household = self._get_household_by_id(conn, household_id)
                    if household is None:
                        raise HouseholdIntegrityError("household creation failed")
                    member = self._get_member_by_id(conn, member_id)
                    if member is None:
                        raise HouseholdIntegrityError("member creation failed")
                    self._validate_owner_invariant(conn, household)
                    conn.commit()
                    return PersonalHousehold(household, member, created=True)
                except sqlite3.IntegrityError as exc:
                    self._rollback_preserving_error(conn)
                    if "UNIQUE" not in str(exc).upper() and "PRIMARY" not in str(exc).upper():
                        raise HouseholdIntegrityError("household integrity conflict") from None
                    existing_after_conflict = self._read_existing_after_conflict(actor)
                    if existing_after_conflict is not None:
                        return PersonalHousehold(existing_after_conflict[0], existing_after_conflict[1], created=False)
                    if attempt == _MAX_ID_REGENERATION_ATTEMPTS - 1:
                        raise HouseholdIntegrityError("household id collision budget exhausted") from None
                    time.sleep(0.01)
                except sqlite3.OperationalError as exc:
                    self._rollback_preserving_error(conn)
                    message = str(exc).lower()
                    if "locked" in message or "busy" in message:
                        raise HouseholdIntegrityError("household database busy") from None
                    raise
                except Exception:
                    self._rollback_preserving_error(conn)
                    raise
        raise HouseholdIntegrityError("household creation retry budget exhausted")

    def _read_existing_after_conflict(self, actor_user_id: int) -> tuple[Household, HouseholdMember] | None:
        with self._owned_connection() as conn:
            return self._load_personal_household_for_actor(conn, actor_user_id)

    def resolve_actor_context(self, actor_user_id: int) -> HouseholdContext:
        actor = _normalize_actor_user_id(actor_user_id)
        with self._owned_connection() as conn:
            membership = self._load_any_membership_for_actor(conn, actor)
            if membership is None:
                created = self.get_or_create_personal_household(actor)
                return HouseholdContext(
                    actor_user_id=actor,
                    household_id=created.household.id,
                    household_member_id=created.member.id,
                    role=created.member.role,
                    member_status=created.member.status,
                    household_status=created.household.status,
                )
            household, member = membership
            return HouseholdContext(
                actor_user_id=actor,
                household_id=household.id,
                household_member_id=member.id,
                role=member.role,
                member_status=member.status,
                household_status=household.status,
            )

    def resolve_existing_actor_context(self, actor_user_id: int) -> HouseholdContext:
        actor = _normalize_actor_user_id(actor_user_id)
        with self._owned_connection() as conn:
            if not self._actor_exists(conn, actor):
                raise HouseholdValidationError("unknown actor")
            membership = self._load_any_membership_for_actor(conn, actor)
            if membership is None:
                raise HouseholdNotFoundError("household not found")
            household, member = membership
            return HouseholdContext(
                actor_user_id=actor,
                household_id=household.id,
                household_member_id=member.id,
                role=member.role,
                member_status=member.status,
                household_status=household.status,
            )

    def _load_any_membership_for_actor(self, conn: sqlite3.Connection, actor_user_id: int) -> tuple[Household, HouseholdMember] | None:
        rows = conn.execute(
            f"""
            SELECT * FROM {HOUSEHOLD_MEMBERS_TABLE}
            WHERE linked_user_id = ?
            ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, created_at ASC, id ASC
            LIMIT 2
            """,
            (int(actor_user_id),),
        ).fetchall()
        if len(rows) > 1:
            raise HouseholdIntegrityError("duplicate household membership")
        if not rows:
            return None
        member = _row_to_member(rows[0])
        household = self._get_household_by_id(conn, member.household_id)
        if household is None:
            raise HouseholdIntegrityError("member references missing household")
        if household.status is HouseholdStatus.ACTIVE and member.status is HouseholdMemberStatus.ACTIVE:
            self._validate_owner_invariant(conn, household)
        return household, member

    def _load_personal_household_for_actor(self, conn: sqlite3.Connection, actor_user_id: int) -> tuple[Household, HouseholdMember] | None:
        rows = conn.execute(
            f"""
            SELECT m.* FROM {HOUSEHOLD_MEMBERS_TABLE} m
            WHERE m.linked_user_id = ? AND m.status = ?
            ORDER BY m.created_at ASC, m.id ASC
            LIMIT 2
            """,
            (int(actor_user_id), HouseholdMemberStatus.ACTIVE.value),
        ).fetchall()
        if len(rows) > 1:
            raise HouseholdIntegrityError("duplicate active household membership")
        if not rows:
            return None
        member = _row_to_member(rows[0])
        household = self._get_household_by_id(conn, member.household_id)
        if household is None:
            raise HouseholdIntegrityError("member references missing household")
        self._validate_owner_invariant(conn, household)
        if member.member_type is not HouseholdMemberType.PRIMARY or member.role is not HouseholdRole.OWNER:
            raise HouseholdIntegrityError("actor membership is not primary owner")
        return household, member

    def _get_member_by_id(self, conn: sqlite3.Connection, member_id: str) -> HouseholdMember | None:
        row = conn.execute(
            f"SELECT * FROM {HOUSEHOLD_MEMBERS_TABLE} WHERE id = ? LIMIT 1",
            (member_id,),
        ).fetchone()
        return _row_to_member(row) if row is not None else None

    def _validate_owner_invariant(self, conn: sqlite3.Connection, household: Household) -> HouseholdMember | None:
        if household.status is not HouseholdStatus.ACTIVE:
            return None
        rows = conn.execute(
            f"""
            SELECT * FROM {HOUSEHOLD_MEMBERS_TABLE}
            WHERE household_id = ? AND role = ? AND status = ?
            ORDER BY created_at ASC, id ASC
            LIMIT 2
            """,
            (household.id, HouseholdRole.OWNER.value, HouseholdMemberStatus.ACTIVE.value),
        ).fetchall()
        if len(rows) != 1:
            raise HouseholdIntegrityError("invalid active owner count")
        owner = _row_to_member(rows[0])
        if owner.linked_user_id is None or int(owner.linked_user_id) != int(household.owner_user_id):
            raise HouseholdIntegrityError("owner pointer mismatch")
        return owner


class HealBiteHouseholdService:
    def __init__(self, store: HealBiteHouseholdStore) -> None:
        self.store = store

    def get_or_create_personal_household_for_actor(self, actor_user_id: int) -> PersonalHousehold:
        return self.store.get_or_create_personal_household(actor_user_id)

    def resolve_actor_household_context(self, actor_user_id: int) -> HouseholdContext:
        context = self.store.resolve_actor_context(actor_user_id)
        if context.member_status is not HouseholdMemberStatus.ACTIVE:
            raise HouseholdAccessError("household access denied")
        if context.household_status is not HouseholdStatus.ACTIVE:
            raise HouseholdAccessError("household access denied")
        return context

    def resolve_existing_actor_household_context(self, actor_user_id: int) -> HouseholdContext:
        """Resolve active membership from SQLite without creating a household."""
        context = self.store.resolve_existing_actor_context(actor_user_id)
        if context.member_status is not HouseholdMemberStatus.ACTIVE:
            raise HouseholdAccessError("household access denied")
        if context.household_status is not HouseholdStatus.ACTIVE:
            raise HouseholdAccessError("household access denied")
        return context

    def get_actor_household(self, actor_user_id: int) -> Household:
        """Return the actor's active household; SQLite membership is authoritative."""
        context = self.resolve_existing_actor_household_context(actor_user_id)
        household = self.store._read_household_by_id_internal(context.household_id)
        if household is None:
            raise HouseholdIntegrityError("actor household is missing")
        return household

    def get_household_for_actor(self, actor_user_id: int, household_id: str) -> Household:
        """Read an active household for an actor; an opaque ID is not authorization proof."""
        context = self.resolve_existing_actor_household_context(actor_user_id)
        self.assert_household_access(context, household_id)
        household = self.store._read_household_by_id_internal(context.household_id)
        if household is None:
            raise HouseholdIntegrityError("actor household is missing")
        return household

    def get_membership_for_actor(self, actor_user_id: int, household_id: str) -> HouseholdMember:
        """Return only the membership established server-side for the supplied actor."""
        context = self.resolve_existing_actor_household_context(actor_user_id)
        self.assert_household_access(context, household_id)
        members = self.store._list_household_members_internal(context.household_id)
        member = next((item for item in members if item.id == context.household_member_id), None)
        if member is None or member.linked_user_id != context.actor_user_id:
            raise HouseholdIntegrityError("actor membership is missing")
        return member

    def list_members_for_actor(self, actor_user_id: int, household_id: str) -> list[HouseholdMember]:
        """List members after actor, household, status, and read-role authorization."""
        context = self.resolve_existing_actor_household_context(actor_user_id)
        self.assert_household_access(context, household_id)
        if context.role not in (
            HouseholdRole.OWNER,
            HouseholdRole.ADULT_ADMIN,
            HouseholdRole.ADULT_MEMBER,
        ):
            raise HouseholdAccessError("household access denied")
        return self.store._list_household_members_internal(context.household_id)

    def assert_household_access(self, context: HouseholdContext, requested_household_id: str) -> None:
        require_canonical_uuid4(requested_household_id)
        if context.household_id != requested_household_id:
            raise HouseholdAccessError("household access denied")
        if context.member_status is not HouseholdMemberStatus.ACTIVE:
            raise HouseholdAccessError("household access denied")
        if context.household_status is not HouseholdStatus.ACTIVE:
            raise HouseholdAccessError("household access denied")
