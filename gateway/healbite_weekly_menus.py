from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence
from urllib.parse import quote

from gateway.healbite_household_schema import require_canonical_uuid4
from gateway.healbite_households import (
    HouseholdContext,
    HouseholdMemberStatus,
    HouseholdRole,
    HouseholdStatus,
)
from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_weekly_menu_schema import (
    MEAL_SLOT_ORDER,
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_OPERATIONS,
    WEEKLY_MENU_MEAL_SLOTS,
    WEEKLY_MENU_REVISION_STATUSES,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_SCHEMA_SQL,
    WEEKLY_MENU_SERIES_TABLE,
    WeeklyMenuEntryOrigin,
    WeeklyMenuIdempotencyOperation,
    WeeklyMenuMealSlot,
    WeeklyMenuRevisionStatus,
    WeeklyMenuSchemaState,
    detect_weekly_menu_schema_state,
    is_valid_local_date,
    is_valid_week_start,
    new_weekly_menu_entry_id,
    new_weekly_menu_idempotency_id,
    new_weekly_menu_revision_id,
    new_weekly_menu_series_id,
    normalize_week_start,
    parse_iso_local_date,
    require_monday_week_start,
    require_weekly_menu_entry_id,
    require_weekly_menu_revision_id,
    require_weekly_menu_series_id,
    week_dates,
)

_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_MAX_ID_REGENERATION_ATTEMPTS = 5
_MAX_TITLE_LENGTH = 200
_MAX_DESCRIPTION_LENGTH = 2000
_MAX_IDEMPOTENCY_KEY_LENGTH = 128


class WeeklyMenuError(Exception):
    pass


class WeeklyMenuValidationError(ValueError):
    pass


class WeeklyMenuAccessError(WeeklyMenuError):
    pass


class WeeklyMenuConflictError(WeeklyMenuError):
    pass


class WeeklyMenuNotFoundError(WeeklyMenuError):
    pass


class WeeklyMenuStateError(WeeklyMenuError):
    pass


class WeeklyMenuSchemaError(WeeklyMenuError):
    pass


@dataclass(slots=True, frozen=True)
class HouseholdAuthorizationContext:
    actor_user_id: int
    household_id: str
    household_member_id: str
    role: HouseholdRole
    member_status: HouseholdMemberStatus
    household_status: HouseholdStatus

    @classmethod
    def from_household_context(
        cls,
        context: HouseholdContext | HouseholdAuthorizationContext,
    ) -> HouseholdAuthorizationContext:
        if isinstance(context, HouseholdAuthorizationContext):
            return context
        return cls(
            actor_user_id=int(context.actor_user_id),
            household_id=require_canonical_uuid4(context.household_id),
            household_member_id=require_canonical_uuid4(context.household_member_id),
            role=HouseholdRole(context.role),
            member_status=HouseholdMemberStatus(context.member_status),
            household_status=HouseholdStatus(context.household_status),
        )


@dataclass(slots=True, frozen=True)
class WeeklyMenuSeries:
    id: str
    household_id: str
    week_start: str
    created_at: str
    updated_at: str
    version: int


@dataclass(slots=True, frozen=True)
class WeeklyMenuRevision:
    id: str
    series_id: str
    household_id: str
    revision_number: int
    status: WeeklyMenuRevisionStatus
    source_revision_id: str | None
    created_by_member_id: str
    created_at: str
    updated_at: str
    published_at: str | None
    archived_at: str | None
    version: int


@dataclass(slots=True, frozen=True)
class WeeklyMenuEntry:
    id: str
    menu_id: str
    household_id: str
    local_date: str
    meal_slot: WeeklyMenuMealSlot
    position: int
    title: str
    description: str | None
    servings: str | None
    origin: WeeklyMenuEntryOrigin
    created_at: str
    updated_at: str
    version: int


@dataclass(slots=True, frozen=True)
class WeeklyMenuEntryInput:
    local_date: str
    meal_slot: WeeklyMenuMealSlot | str
    position: int
    title: str
    description: str | None = None
    servings: str | None = None
    origin: WeeklyMenuEntryOrigin | str = WeeklyMenuEntryOrigin.MANUAL


@dataclass(slots=True, frozen=True)
class WeeklyMenuRevisionView:
    series: WeeklyMenuSeries
    revision: WeeklyMenuRevision
    entries: tuple[WeeklyMenuEntry, ...]


@dataclass(slots=True, frozen=True)
class WeeklyMenuSchemaAudit:
    schema_state: WeeklyMenuSchemaState
    series_count: int
    revision_count: int
    entry_count: int
    orphan_revision_count: int
    orphan_entry_count: int
    invalid_uuid_count: int
    invalid_week_start_count: int
    invalid_status_count: int
    invalid_version_count: int
    multiple_draft_count: int
    multiple_active_published_count: int
    cross_household_inconsistency_count: int


def _sqlite_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.strftime(_SQLITE_TS_FORMAT)


def _normalize_positive_int(value: object, *, label: str) -> int:
    if isinstance(value, bool):
        raise WeeklyMenuValidationError(f"invalid {label}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WeeklyMenuValidationError(f"invalid {label}") from exc
    if parsed <= 0:
        raise WeeklyMenuValidationError(f"invalid {label}")
    return parsed


def _normalize_version(value: object, *, label: str) -> int:
    version = _normalize_positive_int(value, label=label)
    if version < 1:
        raise WeeklyMenuValidationError(f"invalid {label}")
    return version


def _normalize_idempotency_key(value: str) -> str:
    key = str(value).strip()
    if not key or len(key) > _MAX_IDEMPOTENCY_KEY_LENGTH:
        raise WeeklyMenuValidationError("invalid idempotency key")
    return key


def _sqlite_read_only_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"


def _payload_fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_version(raw: object) -> int:
    version = _normalize_positive_int(raw, label="version")
    if version < 1:
        raise WeeklyMenuStateError("invalid version")
    return version


def _coerce_meal_slot(value: WeeklyMenuMealSlot | str) -> WeeklyMenuMealSlot:
    if isinstance(value, WeeklyMenuMealSlot):
        return value
    try:
        slot = WeeklyMenuMealSlot(str(value))
    except ValueError as exc:
        raise WeeklyMenuValidationError("invalid meal slot") from exc
    if slot.value not in WEEKLY_MENU_MEAL_SLOTS:
        raise WeeklyMenuValidationError("invalid meal slot")
    return slot


def _coerce_origin(value: WeeklyMenuEntryOrigin | str) -> WeeklyMenuEntryOrigin:
    if isinstance(value, WeeklyMenuEntryOrigin):
        return value
    try:
        origin = WeeklyMenuEntryOrigin(str(value))
    except ValueError as exc:
        raise WeeklyMenuValidationError("invalid entry origin") from exc
    return origin


def _is_valid_uuid(value: object) -> bool:
    try:
        require_canonical_uuid4(str(value))
    except (TypeError, ValueError):
        return False
    return True


def _sort_entry_inputs(values: Iterable[WeeklyMenuEntryInput]) -> list[WeeklyMenuEntryInput]:
    return sorted(
        values,
        key=lambda item: (
            str(item.local_date),
            MEAL_SLOT_ORDER[_coerce_meal_slot(item.meal_slot).value],
            int(item.position),
            str(item.title),
        ),
    )


class HealBiteWeeklyMenuStore:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        series_id_factory: Callable[[], str] = new_weekly_menu_series_id,
        revision_id_factory: Callable[[], str] = new_weekly_menu_revision_id,
        entry_id_factory: Callable[[], str] = new_weekly_menu_entry_id,
        idempotency_id_factory: Callable[[], str] = new_weekly_menu_idempotency_id,
    ) -> None:
        self.db_path = resolve_healbite_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._series_id_factory = series_id_factory
        self._revision_id_factory = revision_id_factory
        self._entry_id_factory = entry_id_factory
        self._idempotency_id_factory = idempotency_id_factory

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _schema_statements() -> tuple[str, ...]:
        return tuple(statement.strip() for statement in WEEKLY_MENU_SCHEMA_SQL.split(";") if statement.strip())

    def _read_only_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(_sqlite_read_only_uri(self.db_path), uri=True, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def schema_state(self) -> WeeklyMenuSchemaState:
        if not self.db_path.exists():
            return WeeklyMenuSchemaState.NOT_INITIALIZED
        with self._connect() as conn:
            return detect_weekly_menu_schema_state(conn)

    def initialize_schema(self) -> WeeklyMenuSchemaState:
        state = self.schema_state()
        if state is WeeklyMenuSchemaState.CANONICAL:
            return state
        if state is WeeklyMenuSchemaState.PARTIAL:
            raise WeeklyMenuSchemaError("weekly menu schema is partial")
        if state is WeeklyMenuSchemaState.INCOMPATIBLE:
            raise WeeklyMenuSchemaError("weekly menu schema is incompatible")
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for statement in self._schema_statements():
                    conn.execute(statement)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        final = self.schema_state()
        if final is not WeeklyMenuSchemaState.CANONICAL:
            raise WeeklyMenuSchemaError("weekly menu schema initialization failed")
        return final

    def audit_schema(self) -> WeeklyMenuSchemaAudit:
        state = self.schema_state()
        if state is WeeklyMenuSchemaState.NOT_INITIALIZED:
            return WeeklyMenuSchemaAudit(
                schema_state=state,
                series_count=0,
                revision_count=0,
                entry_count=0,
                orphan_revision_count=0,
                orphan_entry_count=0,
                invalid_uuid_count=0,
                invalid_week_start_count=0,
                invalid_status_count=0,
                invalid_version_count=0,
                multiple_draft_count=0,
                multiple_active_published_count=0,
                cross_household_inconsistency_count=0,
            )
        with self._read_only_connect() as conn:
            if detect_weekly_menu_schema_state(conn) is not WeeklyMenuSchemaState.CANONICAL:
                raise WeeklyMenuSchemaError("weekly menu schema is not canonical")
            series_rows = conn.execute(f"SELECT * FROM {WEEKLY_MENU_SERIES_TABLE}").fetchall()
            revision_rows = conn.execute(f"SELECT * FROM {WEEKLY_MENU_REVISIONS_TABLE}").fetchall()
            entry_rows = conn.execute(f"SELECT * FROM {WEEKLY_MENU_ENTRIES_TABLE}").fetchall()
            invalid_uuid_count = 0
            invalid_week_start_count = 0
            invalid_status_count = 0
            invalid_version_count = 0
            for row in series_rows:
                if not _is_valid_uuid(row["id"]):
                    invalid_uuid_count += 1
                if not _is_valid_uuid(row["household_id"]):
                    invalid_uuid_count += 1
                if not is_valid_week_start(str(row["week_start"])):
                    invalid_week_start_count += 1
                if int(row["version"]) < 1:
                    invalid_version_count += 1
            for row in revision_rows:
                if any(
                    not _is_valid_uuid(row[column])
                    for column in ("id", "series_id", "household_id", "created_by_member_id")
                ):
                    invalid_uuid_count += 1
                if row["source_revision_id"] is not None and not _is_valid_uuid(row["source_revision_id"]):
                    invalid_uuid_count += 1
                if str(row["status"]) not in WEEKLY_MENU_REVISION_STATUSES:
                    invalid_status_count += 1
                if int(row["version"]) < 1 or int(row["revision_number"]) < 1:
                    invalid_version_count += 1
            for row in entry_rows:
                if any(
                    not _is_valid_uuid(row[column])
                    for column in ("id", "menu_id", "household_id")
                ):
                    invalid_uuid_count += 1
                if not is_valid_local_date(str(row["local_date"])):
                    invalid_week_start_count += 1
                if str(row["meal_slot"]) not in WEEKLY_MENU_MEAL_SLOTS:
                    invalid_status_count += 1
                if int(row["version"]) < 1 or int(row["position"]) < 1:
                    invalid_version_count += 1
            orphan_revision_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {WEEKLY_MENU_REVISIONS_TABLE} r
                    LEFT JOIN {WEEKLY_MENU_SERIES_TABLE} s ON s.id = r.series_id
                    WHERE s.id IS NULL
                    """
                ).fetchone()[0]
            )
            orphan_entry_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {WEEKLY_MENU_ENTRIES_TABLE} e
                    LEFT JOIN {WEEKLY_MENU_REVISIONS_TABLE} r ON r.id = e.menu_id
                    WHERE r.id IS NULL
                    """
                ).fetchone()[0]
            )
            multiple_draft_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                        SELECT series_id
                        FROM {WEEKLY_MENU_REVISIONS_TABLE}
                        WHERE status = ?
                        GROUP BY series_id
                        HAVING COUNT(*) > 1
                    )
                    """,
                    (WeeklyMenuRevisionStatus.DRAFT.value,),
                ).fetchone()[0]
            )
            multiple_active_published_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM (
                        SELECT series_id
                        FROM {WEEKLY_MENU_REVISIONS_TABLE}
                        WHERE status = ?
                        GROUP BY series_id
                        HAVING COUNT(*) > 1
                    )
                    """,
                    (WeeklyMenuRevisionStatus.PUBLISHED.value,),
                ).fetchone()[0]
            )
            cross_household_inconsistency_count = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {WEEKLY_MENU_REVISIONS_TABLE} r
                    JOIN {WEEKLY_MENU_SERIES_TABLE} s ON s.id = r.series_id
                    WHERE r.household_id != s.household_id
                    """
                ).fetchone()[0]
            ) + int(
                conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM {WEEKLY_MENU_ENTRIES_TABLE} e
                    JOIN {WEEKLY_MENU_REVISIONS_TABLE} r ON r.id = e.menu_id
                    WHERE e.household_id != r.household_id
                    """
                ).fetchone()[0]
            )
        return WeeklyMenuSchemaAudit(
            schema_state=state,
            series_count=len(series_rows),
            revision_count=len(revision_rows),
            entry_count=len(entry_rows),
            orphan_revision_count=orphan_revision_count,
            orphan_entry_count=orphan_entry_count,
            invalid_uuid_count=invalid_uuid_count,
            invalid_week_start_count=invalid_week_start_count,
            invalid_status_count=invalid_status_count,
            invalid_version_count=invalid_version_count,
            multiple_draft_count=multiple_draft_count,
            multiple_active_published_count=multiple_active_published_count,
            cross_household_inconsistency_count=cross_household_inconsistency_count,
        )

    def create_or_get_weekly_menu_series(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        household_id: str,
        week_start: str,
    ) -> WeeklyMenuSeries:
        auth = self._authorize(context, household_id=household_id, mutation=True)
        canonical_week_start = require_monday_week_start(week_start)
        self._require_canonical_schema()
        for attempt in range(_MAX_ID_REGENERATION_ATTEMPTS):
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = self._get_series_by_household_week(conn, auth.household_id, canonical_week_start)
                    if existing is not None:
                        conn.commit()
                        return existing
                    now = _sqlite_timestamp()
                    series_id = require_weekly_menu_series_id(self._series_id_factory())
                    conn.execute(
                        f"""
                        INSERT INTO {WEEKLY_MENU_SERIES_TABLE}
                            (id, household_id, week_start, created_at, updated_at, version)
                        VALUES (?, ?, ?, ?, ?, 1)
                        """,
                        (series_id, auth.household_id, canonical_week_start, now, now),
                    )
                    created = self._get_series_by_id(conn, series_id)
                    if created is None:
                        raise WeeklyMenuStateError("weekly menu series creation failed")
                    conn.commit()
                    return created
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    if "UNIQUE" not in str(exc).upper():
                        raise WeeklyMenuConflictError("weekly menu series integrity conflict") from None
                    existing = self.get_weekly_menu_series(auth, auth.household_id, canonical_week_start)
                    if existing is not None:
                        return existing
                    if attempt == _MAX_ID_REGENERATION_ATTEMPTS - 1:
                        raise WeeklyMenuConflictError("weekly menu series retry budget exhausted") from None
                    time.sleep(0.01)
                except Exception:
                    conn.rollback()
                    raise
        raise WeeklyMenuConflictError("weekly menu series retry budget exhausted")

    def get_weekly_menu_series(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        household_id: str,
        week_start: str,
    ) -> WeeklyMenuSeries | None:
        auth = self._authorize(context, household_id=household_id, mutation=False)
        self._require_canonical_schema()
        canonical_week_start = require_monday_week_start(week_start)
        with self._read_only_connect() as conn:
            return self._get_series_by_household_week(conn, auth.household_id, canonical_week_start)

    def list_weekly_menu_revisions(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        series_id: str,
    ) -> tuple[WeeklyMenuRevision, ...]:
        auth = self._authorize(context, household_id=None, mutation=False)
        self._require_canonical_schema()
        with self._read_only_connect() as conn:
            series = self._get_series_by_id(conn, require_weekly_menu_series_id(series_id))
            if series is None:
                raise WeeklyMenuNotFoundError("weekly menu series not found")
            self._assert_series_in_scope(auth, series)
            rows = conn.execute(
                f"""
                SELECT * FROM {WEEKLY_MENU_REVISIONS_TABLE}
                WHERE series_id = ?
                ORDER BY revision_number ASC, created_at ASC, id ASC
                """,
                (series.id,),
            ).fetchall()
            return tuple(self._row_to_revision(row) for row in rows)

    def get_weekly_menu_revision(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        revision_id: str,
    ) -> WeeklyMenuRevisionView:
        auth = self._authorize(context, household_id=None, mutation=False)
        self._require_canonical_schema()
        with self._read_only_connect() as conn:
            revision = self._get_revision_by_id(conn, require_weekly_menu_revision_id(revision_id))
            if revision is None:
                raise WeeklyMenuNotFoundError("weekly menu revision not found")
            series = self._get_series_by_id(conn, revision.series_id)
            if series is None:
                raise WeeklyMenuStateError("weekly menu revision references missing series")
            self._assert_series_in_scope(auth, series)
            entries = self._list_entries_for_revision(conn, revision.id)
            return WeeklyMenuRevisionView(series=series, revision=revision, entries=entries)

    def create_draft_revision(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        series_id: str,
        *,
        expected_series_version: int,
        idempotency_key: str,
    ) -> WeeklyMenuRevisionView:
        auth = self._authorize(context, household_id=None, mutation=True)
        self._require_canonical_schema()
        revision_series_id = require_weekly_menu_series_id(series_id)
        expected_series_version = _normalize_version(expected_series_version, label="expected_series_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        payload_hash = _payload_fingerprint({"series_id": revision_series_id, "expected_series_version": expected_series_version})
        for attempt in range(_MAX_ID_REGENERATION_ATTEMPTS):
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    series = self._get_series_by_id(conn, revision_series_id)
                    if series is None:
                        raise WeeklyMenuNotFoundError("weekly menu series not found")
                    self._assert_series_in_scope(auth, series)
                    existing_idempotent = self._resolve_idempotent_revision(
                        conn=conn,
                        auth=auth,
                        operation=WeeklyMenuIdempotencyOperation.CREATE_DRAFT,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                    )
                    if existing_idempotent is not None:
                        conn.commit()
                        return self._build_revision_view(conn, existing_idempotent)
                    if series.version != expected_series_version:
                        raise WeeklyMenuConflictError("weekly menu series version mismatch")
                    current_draft = self._get_revision_by_status(conn, series.id, WeeklyMenuRevisionStatus.DRAFT)
                    if current_draft is not None:
                        self._store_idempotency(
                            conn=conn,
                            auth=auth,
                            operation=WeeklyMenuIdempotencyOperation.CREATE_DRAFT,
                            idempotency_key=normalized_key,
                            payload_hash=payload_hash,
                            series_id=series.id,
                            revision_id=current_draft.id,
                        )
                        conn.commit()
                        return self._build_revision_view(conn, current_draft)
                    published = self._get_revision_by_status(conn, series.id, WeeklyMenuRevisionStatus.PUBLISHED)
                    now = _sqlite_timestamp()
                    revision_id = require_weekly_menu_revision_id(self._revision_id_factory())
                    next_revision_number = self._next_revision_number(conn, series.id)
                    conn.execute(
                        f"""
                        INSERT INTO {WEEKLY_MENU_REVISIONS_TABLE}
                            (id, series_id, household_id, revision_number, status, source_revision_id,
                             created_by_member_id, created_at, updated_at, published_at, archived_at, version)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1)
                        """,
                        (
                            revision_id,
                            series.id,
                            series.household_id,
                            next_revision_number,
                            WeeklyMenuRevisionStatus.DRAFT.value,
                            published.id if published is not None else None,
                            auth.household_member_id,
                            now,
                            now,
                        ),
                    )
                    if published is not None:
                        for entry in self._list_entries_for_revision(conn, published.id):
                            entry_id = require_weekly_menu_entry_id(self._entry_id_factory())
                            conn.execute(
                                f"""
                                INSERT INTO {WEEKLY_MENU_ENTRIES_TABLE}
                                    (id, menu_id, household_id, local_date, meal_slot, position,
                                     title, description, servings, origin, created_at, updated_at, version)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                                """,
                                (
                                    entry_id,
                                    revision_id,
                                    series.household_id,
                                    entry.local_date,
                                    entry.meal_slot.value,
                                    entry.position,
                                    entry.title,
                                    entry.description,
                                    entry.servings,
                                    entry.origin.value,
                                    now,
                                    now,
                                ),
                            )
                    self._bump_series(conn, series.id, now)
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=WeeklyMenuIdempotencyOperation.CREATE_DRAFT,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        series_id=series.id,
                        revision_id=revision_id,
                    )
                    created = self._get_revision_by_id(conn, revision_id)
                    if created is None:
                        raise WeeklyMenuStateError("weekly menu draft creation failed")
                    conn.commit()
                    return self._build_revision_view(conn, created)
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    if "UNIQUE" not in str(exc).upper():
                        raise WeeklyMenuConflictError("weekly menu draft integrity conflict") from None
                    if attempt == _MAX_ID_REGENERATION_ATTEMPTS - 1:
                        raise WeeklyMenuConflictError("weekly menu draft retry budget exhausted") from None
                    time.sleep(0.01)
                except Exception:
                    conn.rollback()
                    raise
        raise WeeklyMenuConflictError("weekly menu draft retry budget exhausted")

    def replace_draft_entries(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        revision_id: str,
        entries: Sequence[WeeklyMenuEntryInput],
        *,
        expected_revision_version: int,
        idempotency_key: str,
    ) -> WeeklyMenuRevisionView:
        auth = self._authorize(context, household_id=None, mutation=True)
        self._require_canonical_schema()
        canonical_revision_id = require_weekly_menu_revision_id(revision_id)
        expected_revision_version = _normalize_version(expected_revision_version, label="expected_revision_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        validated_entries = self._validate_entry_inputs(entries)
        payload_hash = _payload_fingerprint(
            {
                "revision_id": canonical_revision_id,
                "expected_revision_version": expected_revision_version,
                "entries": [
                    {
                        "local_date": entry.local_date,
                        "meal_slot": _coerce_meal_slot(entry.meal_slot).value,
                        "position": int(entry.position),
                        "title": entry.title.strip(),
                        "description": None if entry.description is None else entry.description.strip(),
                        "servings": None if entry.servings is None else entry.servings.strip(),
                        "origin": _coerce_origin(entry.origin).value,
                    }
                    for entry in validated_entries
                ],
            }
        )
        for attempt in range(_MAX_ID_REGENERATION_ATTEMPTS):
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    revision = self._get_revision_by_id(conn, canonical_revision_id)
                    if revision is None:
                        raise WeeklyMenuNotFoundError("weekly menu revision not found")
                    series = self._get_series_by_id(conn, revision.series_id)
                    if series is None:
                        raise WeeklyMenuStateError("weekly menu revision references missing series")
                    self._assert_series_in_scope(auth, series)
                    existing_idempotent = self._resolve_idempotent_revision(
                        conn=conn,
                        auth=auth,
                        operation=WeeklyMenuIdempotencyOperation.REPLACE_DRAFT_ENTRIES,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                    )
                    if existing_idempotent is not None:
                        conn.commit()
                        return self._build_revision_view(conn, existing_idempotent)
                    if revision.status is not WeeklyMenuRevisionStatus.DRAFT:
                        raise WeeklyMenuStateError("only draft revisions may be edited")
                    if revision.version != expected_revision_version:
                        raise WeeklyMenuConflictError("weekly menu revision version mismatch")
                    allowed_dates = set(week_dates(series.week_start))
                    for entry in validated_entries:
                        if entry.local_date not in allowed_dates:
                            raise WeeklyMenuValidationError("entry local_date is outside weekly menu scope")
                    conn.execute(f"DELETE FROM {WEEKLY_MENU_ENTRIES_TABLE} WHERE menu_id = ?", (revision.id,))
                    now = _sqlite_timestamp()
                    for entry in validated_entries:
                        entry_id = require_weekly_menu_entry_id(self._entry_id_factory())
                        conn.execute(
                            f"""
                            INSERT INTO {WEEKLY_MENU_ENTRIES_TABLE}
                                (id, menu_id, household_id, local_date, meal_slot, position,
                                 title, description, servings, origin, created_at, updated_at, version)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                entry_id,
                                revision.id,
                                revision.household_id,
                                entry.local_date,
                                _coerce_meal_slot(entry.meal_slot).value,
                                int(entry.position),
                                entry.title.strip(),
                                None if entry.description is None else entry.description.strip(),
                                None if entry.servings is None else entry.servings.strip(),
                                _coerce_origin(entry.origin).value,
                                now,
                                now,
                            ),
                        )
                    self._bump_revision(conn, revision.id, now)
                    self._bump_series(conn, series.id, now)
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=WeeklyMenuIdempotencyOperation.REPLACE_DRAFT_ENTRIES,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        series_id=series.id,
                        revision_id=revision.id,
                    )
                    updated = self._get_revision_by_id(conn, revision.id)
                    if updated is None:
                        raise WeeklyMenuStateError("weekly menu draft update failed")
                    conn.commit()
                    return self._build_revision_view(conn, updated)
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    if "UNIQUE" not in str(exc).upper():
                        raise WeeklyMenuConflictError("weekly menu entry replacement conflict") from None
                    if attempt == _MAX_ID_REGENERATION_ATTEMPTS - 1:
                        raise WeeklyMenuConflictError("weekly menu entry replacement retry budget exhausted") from None
                    time.sleep(0.01)
                except Exception:
                    conn.rollback()
                    raise
        raise WeeklyMenuConflictError("weekly menu entry replacement retry budget exhausted")

    def publish_weekly_menu_revision(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        revision_id: str,
        *,
        expected_series_version: int,
        expected_revision_version: int,
        idempotency_key: str,
    ) -> WeeklyMenuRevisionView:
        auth = self._authorize(context, household_id=None, mutation=True)
        self._require_canonical_schema()
        canonical_revision_id = require_weekly_menu_revision_id(revision_id)
        expected_series_version = _normalize_version(expected_series_version, label="expected_series_version")
        expected_revision_version = _normalize_version(expected_revision_version, label="expected_revision_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        payload_hash = _payload_fingerprint(
            {
                "revision_id": canonical_revision_id,
                "expected_series_version": expected_series_version,
                "expected_revision_version": expected_revision_version,
            }
        )
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                revision = self._get_revision_by_id(conn, canonical_revision_id)
                if revision is None:
                    raise WeeklyMenuNotFoundError("weekly menu revision not found")
                series = self._get_series_by_id(conn, revision.series_id)
                if series is None:
                    raise WeeklyMenuStateError("weekly menu revision references missing series")
                self._assert_series_in_scope(auth, series)
                existing_idempotent = self._resolve_idempotent_revision(
                    conn=conn,
                    auth=auth,
                    operation=WeeklyMenuIdempotencyOperation.PUBLISH_REVISION,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing_idempotent is not None:
                    conn.commit()
                    return self._build_revision_view(conn, existing_idempotent)
                if revision.status is not WeeklyMenuRevisionStatus.DRAFT:
                    raise WeeklyMenuStateError("only draft revisions may be published")
                if revision.version != expected_revision_version:
                    raise WeeklyMenuConflictError("weekly menu revision version mismatch")
                if series.version != expected_series_version:
                    raise WeeklyMenuConflictError("weekly menu series version mismatch")
                if not self._list_entries_for_revision(conn, revision.id):
                    raise WeeklyMenuValidationError("cannot publish an empty weekly menu")
                now = _sqlite_timestamp()
                current_published = self._get_revision_by_status(conn, series.id, WeeklyMenuRevisionStatus.PUBLISHED)
                if current_published is not None and current_published.id != revision.id:
                    conn.execute(
                        f"""
                        UPDATE {WEEKLY_MENU_REVISIONS_TABLE}
                        SET status = ?, archived_at = ?, updated_at = ?, version = version + 1
                        WHERE id = ? AND status = ?
                        """,
                        (
                            WeeklyMenuRevisionStatus.ARCHIVED.value,
                            now,
                            now,
                            current_published.id,
                            WeeklyMenuRevisionStatus.PUBLISHED.value,
                        ),
                    )
                conn.execute(
                    f"""
                    UPDATE {WEEKLY_MENU_REVISIONS_TABLE}
                    SET status = ?, published_at = ?, archived_at = NULL, updated_at = ?, version = version + 1
                    WHERE id = ? AND status = ?
                    """,
                    (
                        WeeklyMenuRevisionStatus.PUBLISHED.value,
                        now,
                        now,
                        revision.id,
                        WeeklyMenuRevisionStatus.DRAFT.value,
                    ),
                )
                self._bump_series(conn, series.id, now)
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=WeeklyMenuIdempotencyOperation.PUBLISH_REVISION,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    series_id=series.id,
                    revision_id=revision.id,
                )
                published = self._get_revision_by_id(conn, revision.id)
                if published is None:
                    raise WeeklyMenuStateError("weekly menu publish failed")
                conn.commit()
                return self._build_revision_view(conn, published)
            except Exception:
                conn.rollback()
                raise

    def archive_weekly_menu_revision(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        revision_id: str,
        *,
        expected_series_version: int,
        expected_revision_version: int,
        idempotency_key: str,
    ) -> WeeklyMenuRevisionView:
        auth = self._authorize(context, household_id=None, mutation=True)
        self._require_canonical_schema()
        canonical_revision_id = require_weekly_menu_revision_id(revision_id)
        expected_series_version = _normalize_version(expected_series_version, label="expected_series_version")
        expected_revision_version = _normalize_version(expected_revision_version, label="expected_revision_version")
        normalized_key = _normalize_idempotency_key(idempotency_key)
        payload_hash = _payload_fingerprint(
            {
                "revision_id": canonical_revision_id,
                "expected_series_version": expected_series_version,
                "expected_revision_version": expected_revision_version,
            }
        )
        with self._connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                revision = self._get_revision_by_id(conn, canonical_revision_id)
                if revision is None:
                    raise WeeklyMenuNotFoundError("weekly menu revision not found")
                series = self._get_series_by_id(conn, revision.series_id)
                if series is None:
                    raise WeeklyMenuStateError("weekly menu revision references missing series")
                self._assert_series_in_scope(auth, series)
                existing_idempotent = self._resolve_idempotent_revision(
                    conn=conn,
                    auth=auth,
                    operation=WeeklyMenuIdempotencyOperation.ARCHIVE_REVISION,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                )
                if existing_idempotent is not None:
                    conn.commit()
                    return self._build_revision_view(conn, existing_idempotent)
                if revision.version != expected_revision_version:
                    raise WeeklyMenuConflictError("weekly menu revision version mismatch")
                if series.version != expected_series_version:
                    raise WeeklyMenuConflictError("weekly menu series version mismatch")
                if revision.status is WeeklyMenuRevisionStatus.ARCHIVED:
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=WeeklyMenuIdempotencyOperation.ARCHIVE_REVISION,
                        idempotency_key=normalized_key,
                        payload_hash=payload_hash,
                        series_id=series.id,
                        revision_id=revision.id,
                    )
                    conn.commit()
                    return self._build_revision_view(conn, revision)
                now = _sqlite_timestamp()
                conn.execute(
                    f"""
                    UPDATE {WEEKLY_MENU_REVISIONS_TABLE}
                    SET status = ?, archived_at = ?, updated_at = ?, version = version + 1
                    WHERE id = ? AND status IN (?, ?)
                    """,
                    (
                        WeeklyMenuRevisionStatus.ARCHIVED.value,
                        now,
                        now,
                        revision.id,
                        WeeklyMenuRevisionStatus.DRAFT.value,
                        WeeklyMenuRevisionStatus.PUBLISHED.value,
                    ),
                )
                self._bump_series(conn, series.id, now)
                self._store_idempotency(
                    conn=conn,
                    auth=auth,
                    operation=WeeklyMenuIdempotencyOperation.ARCHIVE_REVISION,
                    idempotency_key=normalized_key,
                    payload_hash=payload_hash,
                    series_id=series.id,
                    revision_id=revision.id,
                )
                archived = self._get_revision_by_id(conn, revision.id)
                if archived is None:
                    raise WeeklyMenuStateError("weekly menu archive failed")
                conn.commit()
                return self._build_revision_view(conn, archived)
            except Exception:
                conn.rollback()
                raise

    def lookup_generated_draft_replay(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        *,
        idempotency_key: str,
        payload_hash: str,
    ) -> WeeklyMenuRevisionView | None:
        auth = self._authorize(context, household_id=None, mutation=True)
        self._require_canonical_schema()
        normalized_key = _normalize_idempotency_key(idempotency_key)
        with self._read_only_connect() as conn:
            revision = self._resolve_generated_idempotent_revision(
                conn=conn,
                auth=auth,
                idempotency_key=normalized_key,
                payload_hash=str(payload_hash),
            )
            if revision is None:
                return None
            return self._build_revision_view(conn, revision)

    def apply_generated_draft_entries(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        *,
        week_start: str,
        entries: Sequence[WeeklyMenuEntryInput],
        expected_series_version: int | None,
        expected_draft_revision_id: str | None,
        expected_draft_revision_version: int | None,
        idempotency_key: str,
        payload_hash: str,
    ) -> WeeklyMenuRevisionView:
        auth = self._authorize(context, household_id=None, mutation=True)
        self._require_canonical_schema()
        canonical_week_start = require_monday_week_start(normalize_week_start(str(week_start).strip()))
        normalized_key = _normalize_idempotency_key(idempotency_key)
        validated_entries = self._validate_entry_inputs(entries)
        if any(_coerce_origin(entry.origin) is not WeeklyMenuEntryOrigin.GENERATED for entry in validated_entries):
            raise WeeklyMenuValidationError("generated weekly menu entries must use generated origin")
        normalized_expected_series_version = (
            None
            if expected_series_version is None
            else _normalize_version(expected_series_version, label="expected_series_version")
        )
        normalized_expected_draft_revision_id = (
            None
            if expected_draft_revision_id is None
            else require_weekly_menu_revision_id(expected_draft_revision_id)
        )
        normalized_expected_draft_revision_version = (
            None
            if expected_draft_revision_version is None
            else _normalize_version(expected_draft_revision_version, label="expected_draft_revision_version")
        )
        for attempt in range(_MAX_ID_REGENERATION_ATTEMPTS):
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing_idempotent = self._resolve_generated_idempotent_revision(
                        conn=conn,
                        auth=auth,
                        idempotency_key=normalized_key,
                        payload_hash=str(payload_hash),
                    )
                    if existing_idempotent is not None:
                        conn.commit()
                        return self._build_revision_view(conn, existing_idempotent)
                    series = self._get_series_by_household_week(conn, auth.household_id, canonical_week_start)
                    now = _sqlite_timestamp()
                    if series is None:
                        if normalized_expected_series_version is not None:
                            raise WeeklyMenuConflictError("weekly menu series version mismatch")
                        series_id = require_weekly_menu_series_id(self._series_id_factory())
                        conn.execute(
                            f"""
                            INSERT INTO {WEEKLY_MENU_SERIES_TABLE}
                                (id, household_id, week_start, created_at, updated_at, version)
                            VALUES (?, ?, ?, ?, ?, 1)
                            """,
                            (series_id, auth.household_id, canonical_week_start, now, now),
                        )
                        series = self._get_series_by_id(conn, series_id)
                        if series is None:
                            raise WeeklyMenuStateError("weekly menu series creation failed")
                    else:
                        self._assert_series_in_scope(auth, series)
                        if normalized_expected_series_version is None or series.version != normalized_expected_series_version:
                            raise WeeklyMenuConflictError("weekly menu series version mismatch")
                    allowed_dates = set(week_dates(series.week_start))
                    for entry in validated_entries:
                        if entry.local_date not in allowed_dates:
                            raise WeeklyMenuValidationError("entry local_date is outside weekly menu scope")
                    current_draft = self._get_revision_by_status(conn, series.id, WeeklyMenuRevisionStatus.DRAFT)
                    if normalized_expected_draft_revision_id is None:
                        if current_draft is not None:
                            raise WeeklyMenuConflictError("weekly menu draft state changed")
                    else:
                        if current_draft is None or current_draft.id != normalized_expected_draft_revision_id:
                            raise WeeklyMenuConflictError("weekly menu draft state changed")
                        if (
                            normalized_expected_draft_revision_version is None
                            or current_draft.version != normalized_expected_draft_revision_version
                        ):
                            raise WeeklyMenuConflictError("weekly menu draft version mismatch")
                    if current_draft is None:
                        revision_id = require_weekly_menu_revision_id(self._revision_id_factory())
                        next_revision_number = self._next_revision_number(conn, series.id)
                        published = self._get_revision_by_status(conn, series.id, WeeklyMenuRevisionStatus.PUBLISHED)
                        conn.execute(
                            f"""
                            INSERT INTO {WEEKLY_MENU_REVISIONS_TABLE}
                                (id, series_id, household_id, revision_number, status, source_revision_id,
                                 created_by_member_id, created_at, updated_at, published_at, archived_at, version)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 1)
                            """,
                            (
                                revision_id,
                                series.id,
                                series.household_id,
                                next_revision_number,
                                WeeklyMenuRevisionStatus.DRAFT.value,
                                published.id if published is not None else None,
                                auth.household_member_id,
                                now,
                                now,
                            ),
                        )
                        target_revision_id = revision_id
                        idempotency_operation = WeeklyMenuIdempotencyOperation.CREATE_DRAFT
                    else:
                        conn.execute(
                            f"DELETE FROM {WEEKLY_MENU_ENTRIES_TABLE} WHERE menu_id = ?",
                            (current_draft.id,),
                        )
                        conn.execute(
                            f"""
                            UPDATE {WEEKLY_MENU_REVISIONS_TABLE}
                            SET updated_at = ?, version = version + 1
                            WHERE id = ? AND status = ?
                            """,
                            (now, current_draft.id, WeeklyMenuRevisionStatus.DRAFT.value),
                        )
                        target_revision_id = current_draft.id
                        idempotency_operation = WeeklyMenuIdempotencyOperation.REPLACE_DRAFT_ENTRIES
                    for entry in validated_entries:
                        entry_id = require_weekly_menu_entry_id(self._entry_id_factory())
                        conn.execute(
                            f"""
                            INSERT INTO {WEEKLY_MENU_ENTRIES_TABLE}
                                (id, menu_id, household_id, local_date, meal_slot, position,
                                 title, description, servings, origin, created_at, updated_at, version)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                entry_id,
                                target_revision_id,
                                series.household_id,
                                entry.local_date,
                                _coerce_meal_slot(entry.meal_slot).value,
                                int(entry.position),
                                entry.title.strip(),
                                None if entry.description is None else entry.description.strip(),
                                None if entry.servings is None else entry.servings.strip(),
                                WeeklyMenuEntryOrigin.GENERATED.value,
                                now,
                                now,
                            ),
                        )
                    self._bump_series(conn, series.id, now)
                    self._store_idempotency(
                        conn=conn,
                        auth=auth,
                        operation=idempotency_operation,
                        idempotency_key=normalized_key,
                        payload_hash=str(payload_hash),
                        series_id=series.id,
                        revision_id=target_revision_id,
                    )
                    updated = self._get_revision_by_id(conn, target_revision_id)
                    if updated is None:
                        raise WeeklyMenuStateError("weekly menu generation draft write failed")
                    conn.commit()
                    return self._build_revision_view(conn, updated)
                except sqlite3.IntegrityError as exc:
                    conn.rollback()
                    if "UNIQUE" not in str(exc).upper():
                        raise WeeklyMenuConflictError("weekly menu generation draft conflict") from None
                    if attempt == _MAX_ID_REGENERATION_ATTEMPTS - 1:
                        raise WeeklyMenuConflictError("weekly menu generation retry budget exhausted") from None
                    time.sleep(0.01)
                except Exception:
                    conn.rollback()
                    raise
        raise WeeklyMenuConflictError("weekly menu generation retry budget exhausted")

    def _require_canonical_schema(self) -> None:
        state = self.schema_state()
        if state is WeeklyMenuSchemaState.NOT_INITIALIZED:
            raise WeeklyMenuSchemaError("weekly menu schema is not initialized")
        if state is WeeklyMenuSchemaState.PARTIAL:
            raise WeeklyMenuSchemaError("weekly menu schema is partial")
        if state is WeeklyMenuSchemaState.INCOMPATIBLE:
            raise WeeklyMenuSchemaError("weekly menu schema is incompatible")

    def _authorize(
        self,
        context: HouseholdContext | HouseholdAuthorizationContext,
        *,
        household_id: str | None,
        mutation: bool,
    ) -> HouseholdAuthorizationContext:
        auth = HouseholdAuthorizationContext.from_household_context(context)
        _normalize_positive_int(auth.actor_user_id, label="actor")
        if auth.member_status is not HouseholdMemberStatus.ACTIVE:
            raise WeeklyMenuAccessError("household member is not active")
        if auth.household_status is not HouseholdStatus.ACTIVE:
            raise WeeklyMenuAccessError("household is not active")
        if household_id is not None and require_canonical_uuid4(household_id) != auth.household_id:
            raise WeeklyMenuAccessError("household scope mismatch")
        if mutation and auth.role not in (HouseholdRole.OWNER, HouseholdRole.ADULT_ADMIN):
            raise WeeklyMenuAccessError("household member may not mutate weekly menus")
        return auth

    def _assert_series_in_scope(self, auth: HouseholdAuthorizationContext, series: WeeklyMenuSeries) -> None:
        if series.household_id != auth.household_id:
            raise WeeklyMenuAccessError("weekly menu series out of household scope")

    def _validate_entry_inputs(self, entries: Sequence[WeeklyMenuEntryInput]) -> list[WeeklyMenuEntryInput]:
        if not entries:
            raise WeeklyMenuValidationError("weekly menu revision must contain at least one entry")
        normalized: list[WeeklyMenuEntryInput] = []
        seen_slots: set[tuple[str, str, int]] = set()
        for entry in _sort_entry_inputs(entries):
            local_date = str(entry.local_date).strip()
            if not is_valid_local_date(local_date):
                raise WeeklyMenuValidationError("invalid local_date")
            position = _normalize_positive_int(entry.position, label="position")
            slot = _coerce_meal_slot(entry.meal_slot)
            title = str(entry.title).strip()
            if not title or len(title) > _MAX_TITLE_LENGTH:
                raise WeeklyMenuValidationError("invalid title")
            description = None if entry.description is None else str(entry.description).strip()
            if description == "":
                description = None
            if description is not None and len(description) > _MAX_DESCRIPTION_LENGTH:
                raise WeeklyMenuValidationError("invalid description")
            servings = None if entry.servings is None else str(entry.servings).strip()
            if servings == "":
                servings = None
            if servings is not None and len(servings) > 32:
                raise WeeklyMenuValidationError("invalid servings")
            origin = _coerce_origin(entry.origin)
            dedupe_key = (local_date, slot.value, position)
            if dedupe_key in seen_slots:
                raise WeeklyMenuValidationError("duplicate weekly menu slot position")
            seen_slots.add(dedupe_key)
            normalized.append(
                WeeklyMenuEntryInput(
                    local_date=local_date,
                    meal_slot=slot,
                    position=position,
                    title=title,
                    description=description,
                    servings=servings,
                    origin=origin,
                )
            )
        return normalized

    def _resolve_idempotent_revision(
        self,
        *,
        conn: sqlite3.Connection,
        auth: HouseholdAuthorizationContext,
        operation: WeeklyMenuIdempotencyOperation,
        idempotency_key: str,
        payload_hash: str,
    ) -> WeeklyMenuRevision | None:
        row = conn.execute(
            f"""
            SELECT * FROM {WEEKLY_MENU_IDEMPOTENCY_TABLE}
            WHERE household_id = ? AND actor_member_id = ? AND operation = ? AND idempotency_key = ?
            LIMIT 1
            """,
            (auth.household_id, auth.household_member_id, operation.value, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["payload_fingerprint"]) != payload_hash:
            raise WeeklyMenuConflictError("idempotency key replayed with different payload")
        revision_id = str(row["revision_id"]) if row["revision_id"] is not None else None
        if not revision_id:
            raise WeeklyMenuStateError("weekly menu idempotency references missing revision")
        revision = self._get_revision_by_id(conn, revision_id)
        if revision is None:
            raise WeeklyMenuStateError("weekly menu idempotency references missing revision")
        return revision

    def _resolve_generated_idempotent_revision(
        self,
        *,
        conn: sqlite3.Connection,
        auth: HouseholdAuthorizationContext,
        idempotency_key: str,
        payload_hash: str,
    ) -> WeeklyMenuRevision | None:
        rows = conn.execute(
            f"""
            SELECT * FROM {WEEKLY_MENU_IDEMPOTENCY_TABLE}
            WHERE household_id = ?
              AND actor_member_id = ?
              AND operation IN (?, ?)
              AND idempotency_key = ?
            ORDER BY created_at DESC, id DESC
            """,
            (
                auth.household_id,
                auth.household_member_id,
                WeeklyMenuIdempotencyOperation.CREATE_DRAFT.value,
                WeeklyMenuIdempotencyOperation.REPLACE_DRAFT_ENTRIES.value,
                idempotency_key,
            ),
        ).fetchall()
        if not rows:
            return None
        revision_id: str | None = None
        for row in rows:
            if str(row["payload_fingerprint"]) != payload_hash:
                raise WeeklyMenuConflictError("idempotency key replayed with different payload")
            row_revision_id = str(row["revision_id"]) if row["revision_id"] is not None else None
            if not row_revision_id:
                raise WeeklyMenuStateError("weekly menu idempotency references missing revision")
            if revision_id is None:
                revision_id = row_revision_id
                continue
            if row_revision_id != revision_id:
                raise WeeklyMenuStateError("weekly menu idempotency key references multiple revisions")
        assert revision_id is not None
        revision = self._get_revision_by_id(conn, revision_id)
        if revision is None:
            raise WeeklyMenuStateError("weekly menu idempotency references missing revision")
        return revision

    def _store_idempotency(
        self,
        *,
        conn: sqlite3.Connection,
        auth: HouseholdAuthorizationContext,
        operation: WeeklyMenuIdempotencyOperation,
        idempotency_key: str,
        payload_hash: str,
        series_id: str | None,
        revision_id: str | None,
    ) -> None:
        conn.execute(
            f"""
            INSERT INTO {WEEKLY_MENU_IDEMPOTENCY_TABLE}
                (id, household_id, actor_member_id, operation, idempotency_key, payload_fingerprint,
                 series_id, revision_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                require_canonical_uuid4(self._idempotency_id_factory()),
                auth.household_id,
                auth.household_member_id,
                operation.value,
                idempotency_key,
                payload_hash,
                series_id,
                revision_id,
                _sqlite_timestamp(),
            ),
        )

    def _next_revision_number(self, conn: sqlite3.Connection, series_id: str) -> int:
        row = conn.execute(
            f"SELECT COALESCE(MAX(revision_number), 0) FROM {WEEKLY_MENU_REVISIONS_TABLE} WHERE series_id = ?",
            (series_id,),
        ).fetchone()
        return int(row[0]) + 1

    def _bump_series(self, conn: sqlite3.Connection, series_id: str, timestamp: str) -> None:
        conn.execute(
            f"UPDATE {WEEKLY_MENU_SERIES_TABLE} SET updated_at = ?, version = version + 1 WHERE id = ?",
            (timestamp, series_id),
        )

    def _bump_revision(self, conn: sqlite3.Connection, revision_id: str, timestamp: str) -> None:
        conn.execute(
            f"UPDATE {WEEKLY_MENU_REVISIONS_TABLE} SET updated_at = ?, version = version + 1 WHERE id = ?",
            (timestamp, revision_id),
        )

    def _build_revision_view(self, conn: sqlite3.Connection, revision: WeeklyMenuRevision) -> WeeklyMenuRevisionView:
        series = self._get_series_by_id(conn, revision.series_id)
        if series is None:
            raise WeeklyMenuStateError("weekly menu revision references missing series")
        return WeeklyMenuRevisionView(
            series=series,
            revision=self._get_revision_by_id(conn, revision.id) or revision,
            entries=self._list_entries_for_revision(conn, revision.id),
        )

    def _get_series_by_household_week(
        self,
        conn: sqlite3.Connection,
        household_id: str,
        week_start: str,
    ) -> WeeklyMenuSeries | None:
        row = conn.execute(
            f"""
            SELECT * FROM {WEEKLY_MENU_SERIES_TABLE}
            WHERE household_id = ? AND week_start = ?
            LIMIT 1
            """,
            (household_id, week_start),
        ).fetchone()
        return self._row_to_series(row) if row is not None else None

    def _get_series_by_id(self, conn: sqlite3.Connection, series_id: str) -> WeeklyMenuSeries | None:
        row = conn.execute(
            f"SELECT * FROM {WEEKLY_MENU_SERIES_TABLE} WHERE id = ? LIMIT 1",
            (series_id,),
        ).fetchone()
        return self._row_to_series(row) if row is not None else None

    def _get_revision_by_id(self, conn: sqlite3.Connection, revision_id: str) -> WeeklyMenuRevision | None:
        row = conn.execute(
            f"SELECT * FROM {WEEKLY_MENU_REVISIONS_TABLE} WHERE id = ? LIMIT 1",
            (revision_id,),
        ).fetchone()
        return self._row_to_revision(row) if row is not None else None

    def _get_revision_by_status(
        self,
        conn: sqlite3.Connection,
        series_id: str,
        status: WeeklyMenuRevisionStatus,
    ) -> WeeklyMenuRevision | None:
        row = conn.execute(
            f"""
            SELECT * FROM {WEEKLY_MENU_REVISIONS_TABLE}
            WHERE series_id = ? AND status = ?
            ORDER BY revision_number DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (series_id, status.value),
        ).fetchone()
        return self._row_to_revision(row) if row is not None else None

    def _list_entries_for_revision(self, conn: sqlite3.Connection, revision_id: str) -> tuple[WeeklyMenuEntry, ...]:
        rows = conn.execute(
            f"""
            SELECT * FROM {WEEKLY_MENU_ENTRIES_TABLE}
            WHERE menu_id = ?
            ORDER BY local_date ASC, meal_slot ASC, position ASC, id ASC
            """,
            (revision_id,),
        ).fetchall()
        entries = [self._row_to_entry(row) for row in rows]
        return tuple(
            sorted(
                entries,
                key=lambda item: (item.local_date, MEAL_SLOT_ORDER[item.meal_slot.value], item.position, item.id),
            )
        )

    def _row_to_series(self, row: sqlite3.Row | None) -> WeeklyMenuSeries:
        if row is None:
            raise WeeklyMenuNotFoundError("weekly menu series not found")
        return WeeklyMenuSeries(
            id=require_weekly_menu_series_id(str(row["id"])),
            household_id=require_canonical_uuid4(str(row["household_id"])),
            week_start=require_monday_week_start(str(row["week_start"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=_validated_version(row["version"]),
        )

    def _row_to_revision(self, row: sqlite3.Row | None) -> WeeklyMenuRevision:
        if row is None:
            raise WeeklyMenuNotFoundError("weekly menu revision not found")
        return WeeklyMenuRevision(
            id=require_weekly_menu_revision_id(str(row["id"])),
            series_id=require_weekly_menu_series_id(str(row["series_id"])),
            household_id=require_canonical_uuid4(str(row["household_id"])),
            revision_number=_normalize_positive_int(row["revision_number"], label="revision_number"),
            status=WeeklyMenuRevisionStatus(str(row["status"])),
            source_revision_id=None if row["source_revision_id"] is None else require_weekly_menu_revision_id(str(row["source_revision_id"])),
            created_by_member_id=require_canonical_uuid4(str(row["created_by_member_id"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            published_at=None if row["published_at"] is None else str(row["published_at"]),
            archived_at=None if row["archived_at"] is None else str(row["archived_at"]),
            version=_validated_version(row["version"]),
        )

    def _row_to_entry(self, row: sqlite3.Row | None) -> WeeklyMenuEntry:
        if row is None:
            raise WeeklyMenuNotFoundError("weekly menu entry not found")
        return WeeklyMenuEntry(
            id=require_weekly_menu_entry_id(str(row["id"])),
            menu_id=require_weekly_menu_revision_id(str(row["menu_id"])),
            household_id=require_canonical_uuid4(str(row["household_id"])),
            local_date=parse_iso_local_date(str(row["local_date"])).isoformat(),
            meal_slot=WeeklyMenuMealSlot(str(row["meal_slot"])),
            position=_normalize_positive_int(row["position"], label="position"),
            title=str(row["title"]),
            description=None if row["description"] is None else str(row["description"]),
            servings=None if row["servings"] is None else str(row["servings"]),
            origin=WeeklyMenuEntryOrigin(str(row["origin"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            version=_validated_version(row["version"]),
        )
