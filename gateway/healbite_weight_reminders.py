from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gateway.healbite_nutrition_diary import resolve_healbite_db_path

WEIGHT_REMINDER_SETTINGS_TABLE = "weight_reminder_settings"
WEIGHT_REMINDER_DELIVERIES_TABLE = "weight_reminder_deliveries"
DEFAULT_SCAN_INTERVAL_SECONDS = 60
DEFAULT_CLAIM_LEASE_SECONDS = 300
DEFAULT_MISSED_GRACE_HOURS = 12
DEFAULT_BATCH_SIZE = 50
_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


class ReminderDeliveryState(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class ReminderDeliveryStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SENDING = "sending"
    RETRY_WAIT = "retry_wait"
    SENT = "sent"
    DELIVERY_UNKNOWN = "delivery_unknown"
    PERMANENT_FAILED = "permanent_failed"
    SKIPPED = "skipped"
    SKIPPED_STALE = "skipped_stale"


class ReminderScheduleAction(str, Enum):
    WAIT = "wait"
    DELIVER = "deliver"
    SKIP_EXPIRED = "skip_expired"


class ReminderSchedulingError(ValueError):
    pass


@dataclass(slots=True)
class ReminderOccurrence:
    scheduled_local: datetime
    scheduled_utc: datetime
    timezone_name: str
    fold: int = 0
    shifted_for_dst_gap: bool = False


@dataclass(slots=True)
class ReminderSchedulingDecision:
    action: ReminderScheduleAction
    occurrence: ReminderOccurrence | None
    next_occurrence: ReminderOccurrence
    skip_reason: str = ""


@dataclass(slots=True)
class WeightReminderSetting:
    user_id: int
    enabled: bool
    timezone_name: str
    weekday: int
    local_time: str
    next_due_at_utc: str | None
    schedule_version: int
    delivery_state: ReminderDeliveryState
    suspended_at_utc: str | None
    suspension_reason: str | None
    last_delivered_at_utc: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class WeightReminderDelivery:
    id: int
    user_id: int
    scheduled_for_utc: str
    delivery_key: str
    status: ReminderDeliveryStatus
    attempt_count: int
    claimed_at_utc: str | None
    claim_expires_at_utc: str | None
    last_error_type: str | None
    sent_at_utc: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class WeightReminderConfig:
    enabled: bool = False
    allowlist: frozenset[int] = frozenset()
    scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS
    missed_grace_hours: int = DEFAULT_MISSED_GRACE_HOURS


_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {WEIGHT_REMINDER_SETTINGS_TABLE} (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    timezone TEXT NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    local_time TEXT NOT NULL,
    next_due_at_utc TEXT,
    schedule_version INTEGER NOT NULL DEFAULT 1 CHECK (schedule_version >= 0),
    delivery_state TEXT NOT NULL DEFAULT 'active'
        CHECK (delivery_state IN ('active', 'suspended')),
    suspended_at_utc TEXT,
    suspension_reason TEXT,
    last_delivered_at_utc TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_reminder_settings_due
    ON {WEIGHT_REMINDER_SETTINGS_TABLE}(enabled, delivery_state, next_due_at_utc);
CREATE INDEX IF NOT EXISTS idx_weight_reminder_settings_user_state
    ON {WEIGHT_REMINDER_SETTINGS_TABLE}(user_id, delivery_state);
CREATE TABLE IF NOT EXISTS {WEIGHT_REMINDER_DELIVERIES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    scheduled_for_utc TEXT NOT NULL,
    delivery_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL
        CHECK (status IN (
            'pending',
            'claimed',
            'sending',
            'retry_wait',
            'sent',
            'delivery_unknown',
            'permanent_failed',
            'skipped',
            'skipped_stale'
        )),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    claimed_at_utc TEXT,
    claim_expires_at_utc TEXT,
    last_error_type TEXT,
    sent_at_utc TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_reminder_deliveries_status_claim
    ON {WEIGHT_REMINDER_DELIVERIES_TABLE}(status, claim_expires_at_utc);
CREATE INDEX IF NOT EXISTS idx_weight_reminder_deliveries_user_scheduled
    ON {WEIGHT_REMINDER_DELIVERIES_TABLE}(user_id, scheduled_for_utc);
"""


def _sqlite_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    current = _require_aware_utc(current)
    return current.strftime(_SQLITE_TS_FORMAT)


def _parse_sqlite_timestamp(value: str) -> datetime:
    return datetime.strptime(value, _SQLITE_TS_FORMAT).replace(tzinfo=timezone.utc)


def _require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ReminderSchedulingError("now_utc must be timezone-aware UTC")
    if value.utcoffset() != timedelta(0):
        raise ReminderSchedulingError("now_utc must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def validate_timezone(timezone_name: str) -> str:
    normalized = (timezone_name or "").strip()
    if not normalized:
        raise ReminderSchedulingError("timezone must be a valid IANA name")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ReminderSchedulingError("timezone must be a valid IANA name") from exc
    return normalized


def validate_weekday(weekday: int) -> int:
    if isinstance(weekday, bool):
        raise ReminderSchedulingError("weekday must be an integer from 0 to 6")
    try:
        value = int(weekday)
    except (TypeError, ValueError) as exc:
        raise ReminderSchedulingError("weekday must be an integer from 0 to 6") from exc
    if value < 0 or value > 6:
        raise ReminderSchedulingError("weekday must be an integer from 0 to 6")
    return value


def parse_local_time(value: str) -> time:
    normalized = normalize_local_time(value)
    hour_s, minute_s = normalized.split(":", 1)
    return time(hour=int(hour_s), minute=int(minute_s))


def normalize_local_time(value: str) -> str:
    raw = (value or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if match is None:
        raise ReminderSchedulingError("local_time must be HH:MM")
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ReminderSchedulingError("local_time must be HH:MM")
    return f"{hour:02d}:{minute:02d}"


def _valid_local(naive_local: datetime, tzinfo: ZoneInfo, fold: int) -> datetime | None:
    aware = naive_local.replace(tzinfo=tzinfo, fold=fold)
    round_trip = aware.astimezone(timezone.utc).astimezone(tzinfo)
    if (
        round_trip.year,
        round_trip.month,
        round_trip.day,
        round_trip.hour,
        round_trip.minute,
        round_trip.second,
        round_trip.microsecond,
    ) != (
        naive_local.year,
        naive_local.month,
        naive_local.day,
        naive_local.hour,
        naive_local.minute,
        naive_local.second,
        naive_local.microsecond,
    ):
        return None
    return aware


def _resolve_local_candidate(naive_local: datetime, tzinfo: ZoneInfo) -> tuple[datetime, bool]:
    first_fold = _valid_local(naive_local, tzinfo, 0)
    if first_fold is not None:
        return first_fold, False
    second_fold = _valid_local(naive_local, tzinfo, 1)
    if second_fold is not None:
        return second_fold.replace(fold=0), False
    probe = naive_local
    for _ in range(240):
        probe += timedelta(minutes=1)
        resolved = _valid_local(probe, tzinfo, 0)
        if resolved is not None:
            return resolved, True
    raise ReminderSchedulingError("could not resolve local time")


def calculate_next_occurrence_utc(
    now_utc: datetime,
    timezone_name: str,
    weekday: int,
    local_time: str,
) -> ReminderOccurrence:
    current = _require_aware_utc(now_utc)
    tz_name = validate_timezone(timezone_name)
    day = validate_weekday(weekday)
    local = parse_local_time(local_time)
    tzinfo = ZoneInfo(tz_name)
    local_now = current.astimezone(tzinfo)
    days_until = (day - local_now.weekday()) % 7
    local_date = local_now.date() + timedelta(days=days_until)
    for week_offset in (0, 7):
        candidate_naive = datetime.combine(local_date + timedelta(days=week_offset), local)
        candidate_local, shifted = _resolve_local_candidate(candidate_naive, tzinfo)
        candidate_utc = candidate_local.astimezone(timezone.utc)
        if candidate_utc > current:
            return ReminderOccurrence(
                scheduled_local=candidate_local,
                scheduled_utc=candidate_utc,
                timezone_name=tz_name,
                fold=int(getattr(candidate_local, "fold", 0)),
                shifted_for_dst_gap=shifted,
            )
    raise ReminderSchedulingError("could not calculate next occurrence")


def make_delivery_key(
    *,
    user_id: int,
    occurrence: ReminderOccurrence,
    schedule_version: int,
) -> str:
    version = int(schedule_version)
    local_identity = occurrence.scheduled_local.strftime("%Y-%m-%dT%H:%M")
    raw = f"weight-reminder:v1:{int(user_id)}:{local_identity}:{occurrence.timezone_name}:{version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def classify_missed_reminder(
    *,
    now_utc: datetime,
    scheduled_utc: datetime,
    timezone_name: str,
    weekday: int,
    local_time: str,
    grace_hours: int = DEFAULT_MISSED_GRACE_HOURS,
) -> ReminderSchedulingDecision:
    current = _require_aware_utc(now_utc)
    scheduled = _require_aware_utc(scheduled_utc)
    if isinstance(grace_hours, bool) or int(grace_hours) <= 0:
        raise ReminderSchedulingError("grace_hours must be positive")
    next_reference = max(current, scheduled)
    next_occurrence = calculate_next_occurrence_utc(
        next_reference,
        timezone_name,
        weekday,
        local_time,
    )
    occurrence = ReminderOccurrence(
        scheduled_local=scheduled.astimezone(ZoneInfo(validate_timezone(timezone_name))),
        scheduled_utc=scheduled,
        timezone_name=validate_timezone(timezone_name),
        fold=int(getattr(scheduled.astimezone(ZoneInfo(validate_timezone(timezone_name))), "fold", 0)),
        shifted_for_dst_gap=False,
    )
    if scheduled > current:
        return ReminderSchedulingDecision(ReminderScheduleAction.WAIT, None, next_occurrence)
    age = current - scheduled
    if age <= timedelta(hours=int(grace_hours)):
        return ReminderSchedulingDecision(ReminderScheduleAction.DELIVER, occurrence, next_occurrence)
    return ReminderSchedulingDecision(
        ReminderScheduleAction.SKIP_EXPIRED,
        occurrence,
        next_occurrence,
        skip_reason="missed_window",
    )


def load_weight_reminder_config(env: dict[str, str] | None = None) -> WeightReminderConfig:
    source = env if env is not None else os.environ
    enabled_raw = str(source.get("WEIGHT_REMINDERS_ENABLED", "")).strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "on"}
    allowlist = _parse_allowlist(source.get("WEIGHT_REMINDERS_ALLOWLIST", ""))
    return WeightReminderConfig(
        enabled=enabled,
        allowlist=allowlist,
        scan_interval_seconds=_positive_int(
            source.get("WEIGHT_REMINDER_SCAN_INTERVAL_SECONDS"),
            DEFAULT_SCAN_INTERVAL_SECONDS,
        ),
        missed_grace_hours=_positive_int(
            source.get("WEIGHT_REMINDER_MISSED_GRACE_HOURS"),
            DEFAULT_MISSED_GRACE_HOURS,
        ),
    )


def _positive_int(value: str | None, default: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        parsed = int(str(value).strip())
    except ValueError:
        return default
    if parsed <= 0:
        return default
    return parsed


def _parse_allowlist(value: str | None) -> frozenset[int]:
    result: set[int] = set()
    for part in (value or "").replace(";", ",").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            result.add(int(token))
        except ValueError:
            continue
    return frozenset(result)


def _setting_from_row(row: sqlite3.Row) -> WeightReminderSetting:
    return WeightReminderSetting(
        user_id=int(row["user_id"]),
        enabled=bool(int(row["enabled"])),
        timezone_name=str(row["timezone"]),
        weekday=int(row["weekday"]),
        local_time=str(row["local_time"]),
        next_due_at_utc=str(row["next_due_at_utc"]) if row["next_due_at_utc"] is not None else None,
        schedule_version=int(row["schedule_version"]),
        delivery_state=ReminderDeliveryState(str(row["delivery_state"])),
        suspended_at_utc=str(row["suspended_at_utc"]) if row["suspended_at_utc"] is not None else None,
        suspension_reason=str(row["suspension_reason"]) if row["suspension_reason"] is not None else None,
        last_delivered_at_utc=str(row["last_delivered_at_utc"]) if row["last_delivered_at_utc"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _delivery_from_row(row: sqlite3.Row) -> WeightReminderDelivery:
    return WeightReminderDelivery(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        scheduled_for_utc=str(row["scheduled_for_utc"]),
        delivery_key=str(row["delivery_key"]),
        status=ReminderDeliveryStatus(str(row["status"])),
        attempt_count=int(row["attempt_count"]),
        claimed_at_utc=str(row["claimed_at_utc"]) if row["claimed_at_utc"] is not None else None,
        claim_expires_at_utc=str(row["claim_expires_at_utc"]) if row["claim_expires_at_utc"] is not None else None,
        last_error_type=str(row["last_error_type"]) if row["last_error_type"] is not None else None,
        sent_at_utc=str(row["sent_at_utc"]) if row["sent_at_utc"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class HealBiteWeightReminderStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = resolve_healbite_db_path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def get_settings(self, user_id: int) -> WeightReminderSetting | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return _setting_from_row(row) if row is not None else None

    def create_or_update_settings(
        self,
        *,
        user_id: int,
        timezone_name: str,
        weekday: int,
        local_time: str,
        enabled: bool,
        now_utc: datetime | None = None,
    ) -> WeightReminderSetting:
        current = _require_aware_utc(now_utc or datetime.now(timezone.utc))
        tz_name = validate_timezone(timezone_name)
        day = validate_weekday(weekday)
        normalized_time = normalize_local_time(local_time)
        next_due = calculate_next_occurrence_utc(current, tz_name, day, normalized_time)
        with self._connect() as conn:
            conn.execute("BEGIN")
            existing = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
            timestamp = _sqlite_timestamp(current)
            if existing is None:
                conn.execute(
                    f"""
                    INSERT INTO {WEIGHT_REMINDER_SETTINGS_TABLE}
                        (user_id, enabled, timezone, weekday, local_time, next_due_at_utc,
                         schedule_version, delivery_state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                    """,
                    (
                        int(user_id),
                        1 if enabled else 0,
                        tz_name,
                        day,
                        normalized_time,
                        _sqlite_timestamp(next_due.scheduled_utc),
                        ReminderDeliveryState.ACTIVE.value,
                        timestamp,
                        timestamp,
                    ),
                )
            else:
                schedule_changed = (
                    str(existing["timezone"]) != tz_name
                    or int(existing["weekday"]) != day
                    or str(existing["local_time"]) != normalized_time
                )
                version = int(existing["schedule_version"]) + (1 if schedule_changed else 0)
                conn.execute(
                    f"""
                    UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                    SET enabled = ?,
                        timezone = ?,
                        weekday = ?,
                        local_time = ?,
                        next_due_at_utc = ?,
                        schedule_version = ?,
                        delivery_state = ?,
                        suspended_at_utc = NULL,
                        suspension_reason = NULL,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        1 if enabled else 0,
                        tz_name,
                        day,
                        normalized_time,
                        _sqlite_timestamp(next_due.scheduled_utc),
                        version,
                        ReminderDeliveryState.ACTIVE.value,
                        timestamp,
                        int(user_id),
                    ),
                )
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError("Could not load saved reminder settings.")
        return _setting_from_row(row)

    def disable_settings(self, user_id: int, *, now_utc: datetime | None = None) -> WeightReminderSetting | None:
        current = _require_aware_utc(now_utc or datetime.now(timezone.utc))
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                SET enabled = 0,
                    next_due_at_utc = NULL,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (_sqlite_timestamp(current), int(user_id)),
            )
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return _setting_from_row(row) if row is not None else None

    def suspend_delivery(
        self,
        user_id: int,
        *,
        safe_reason: str,
        now_utc: datetime | None = None,
    ) -> WeightReminderSetting | None:
        current = _require_aware_utc(now_utc or datetime.now(timezone.utc))
        reason = _safe_reason(safe_reason)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                SET delivery_state = ?,
                    suspended_at_utc = ?,
                    suspension_reason = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    ReminderDeliveryState.SUSPENDED.value,
                    _sqlite_timestamp(current),
                    reason,
                    _sqlite_timestamp(current),
                    int(user_id),
                ),
            )
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return _setting_from_row(row) if row is not None else None

    def resume_delivery(self, user_id: int, *, now_utc: datetime | None = None) -> WeightReminderSetting | None:
        current = _require_aware_utc(now_utc or datetime.now(timezone.utc))
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if row is None:
                return None
            next_due = calculate_next_occurrence_utc(
                current,
                str(row["timezone"]),
                int(row["weekday"]),
                str(row["local_time"]),
            )
            conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                SET delivery_state = ?,
                    suspended_at_utc = NULL,
                    suspension_reason = NULL,
                    next_due_at_utc = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    ReminderDeliveryState.ACTIVE.value,
                    _sqlite_timestamp(next_due.scheduled_utc),
                    _sqlite_timestamp(current),
                    int(user_id),
                ),
            )
            updated = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return _setting_from_row(updated) if updated is not None else None

    def insert_delivery_if_absent(
        self,
        *,
        user_id: int,
        scheduled_for_utc: datetime,
        delivery_key: str,
        status: ReminderDeliveryStatus = ReminderDeliveryStatus.PENDING,
        now_utc: datetime | None = None,
    ) -> WeightReminderDelivery:
        current = _require_aware_utc(now_utc or datetime.now(timezone.utc))
        scheduled = _require_aware_utc(scheduled_for_utc)
        key = (delivery_key or "").strip()
        if not key:
            raise ValueError("delivery_key is required")
        state = ReminderDeliveryStatus(status)
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {WEIGHT_REMINDER_DELIVERIES_TABLE}
                    (user_id, scheduled_for_utc, delivery_key, status, attempt_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    int(user_id),
                    _sqlite_timestamp(scheduled),
                    key,
                    state.value,
                    _sqlite_timestamp(current),
                    _sqlite_timestamp(current),
                ),
            )
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE delivery_key = ? LIMIT 1",
                (key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Could not load reminder delivery row.")
        return _delivery_from_row(row)

    def get_delivery_by_key(self, delivery_key: str) -> WeightReminderDelivery | None:
        key = (delivery_key or "").strip()
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE delivery_key = ? LIMIT 1",
                (key,),
            ).fetchone()
        return _delivery_from_row(row) if row is not None else None

    def list_deliveries(self, user_id: int) -> list[WeightReminderDelivery]:
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {WEIGHT_REMINDER_DELIVERIES_TABLE}
                WHERE user_id = ?
                ORDER BY scheduled_for_utc ASC, id ASC
                """,
                (int(user_id),),
            ).fetchall()
        return [_delivery_from_row(row) for row in rows]


def _safe_reason(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_:-]+", "_", (value or "").strip().lower())
    normalized = normalized.strip("_")
    return normalized[:64] or "unknown"


def get_default_weight_reminder_store() -> HealBiteWeightReminderStore:
    return HealBiteWeightReminderStore()
