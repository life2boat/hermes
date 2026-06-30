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
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_BACKOFF_SECONDS = 30
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


class ReminderClaimOutcome(str, Enum):
    CLAIMED = "claimed"
    WAIT = "wait"
    SKIPPED_EXPIRED = "skipped_expired"
    NOT_ELIGIBLE = "not_eligible"


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
    next_attempt_at_utc: str | None
    schedule_version: int
    sent_at_utc: str | None
    created_at: str
    updated_at: str


@dataclass(slots=True)
class WeightReminderConfig:
    enabled: bool = False
    allowlist: frozenset[int] = frozenset()
    scan_interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS
    missed_grace_hours: int = DEFAULT_MISSED_GRACE_HOURS
    claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS
    batch_size: int = DEFAULT_BATCH_SIZE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS


@dataclass(slots=True)
class WeightReminderClaim:
    outcome: ReminderClaimOutcome
    setting: WeightReminderSetting | None = None
    delivery: WeightReminderDelivery | None = None
    occurrence: ReminderOccurrence | None = None
    error_type: str = ""


_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {WEIGHT_REMINDER_SETTINGS_TABLE} (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    timezone TEXT NOT NULL,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    local_time TEXT NOT NULL
        CHECK (local_time GLOB '[0-2][0-9]:[0-5][0-9]' AND substr(local_time, 1, 2) BETWEEN '00' AND '23'),
    next_due_at_utc TEXT,
    schedule_version INTEGER NOT NULL DEFAULT 1 CHECK (schedule_version >= 0),
    delivery_state TEXT NOT NULL DEFAULT 'active'
        CHECK (delivery_state IN ('active', 'suspended')),
    suspended_at_utc TEXT,
    suspension_reason TEXT
        CHECK (suspension_reason IS NULL OR suspension_reason IN (
            'blocked_user',
            'chat_not_found',
            'bot_blocked',
            'delivery_permanent_failure',
            'manual_suspend',
            'unknown'
        )),
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
    next_attempt_at_utc TEXT,
    schedule_version INTEGER NOT NULL DEFAULT 0 CHECK (schedule_version >= 0),
    sent_at_utc TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_reminder_deliveries_status_claim
    ON {WEIGHT_REMINDER_DELIVERIES_TABLE}(status, claim_expires_at_utc);
CREATE INDEX IF NOT EXISTS idx_weight_reminder_deliveries_retry
    ON {WEIGHT_REMINDER_DELIVERIES_TABLE}(status, next_attempt_at_utc);
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
        claim_lease_seconds=_positive_int(
            source.get("WEIGHT_REMINDER_CLAIM_LEASE_SECONDS"),
            DEFAULT_CLAIM_LEASE_SECONDS,
        ),
        batch_size=_positive_int(
            source.get("WEIGHT_REMINDER_BATCH_SIZE"),
            DEFAULT_BATCH_SIZE,
        ),
        max_attempts=_positive_int(
            source.get("WEIGHT_REMINDER_MAX_ATTEMPTS"),
            DEFAULT_MAX_ATTEMPTS,
        ),
        base_backoff_seconds=_positive_int(
            source.get("WEIGHT_REMINDER_BASE_BACKOFF_SECONDS"),
            DEFAULT_BASE_BACKOFF_SECONDS,
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
        next_attempt_at_utc=str(row["next_attempt_at_utc"]) if "next_attempt_at_utc" in row.keys() and row["next_attempt_at_utc"] is not None else None,
        schedule_version=int(row["schedule_version"]) if "schedule_version" in row.keys() else 0,
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
            columns = {
                str(row[1])
                for row in conn.execute(f"PRAGMA table_info({WEIGHT_REMINDER_DELIVERIES_TABLE})")
            }
            if "next_attempt_at_utc" not in columns:
                conn.execute(f"ALTER TABLE {WEIGHT_REMINDER_DELIVERIES_TABLE} ADD COLUMN next_attempt_at_utc TEXT")
            if "schedule_version" not in columns:
                conn.execute(
                    f"ALTER TABLE {WEIGHT_REMINDER_DELIVERIES_TABLE} "
                    "ADD COLUMN schedule_version INTEGER NOT NULL DEFAULT 0 CHECK (schedule_version >= 0)"
                )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_weight_reminder_deliveries_retry "
                f"ON {WEIGHT_REMINDER_DELIVERIES_TABLE}(status, next_attempt_at_utc)"
            )

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
                existing_active = str(existing["delivery_state"]) == ReminderDeliveryState.ACTIVE.value
                next_due_changed = (
                    schedule_changed
                    or not bool(int(existing["enabled"]))
                    or not existing_active
                    or existing["next_due_at_utc"] is None
                )
                next_due_value = (
                    _sqlite_timestamp(next_due.scheduled_utc)
                    if next_due_changed
                    else str(existing["next_due_at_utc"])
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
                        next_due_value,
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

    def create_or_update_settings_if_current(
        self,
        *,
        user_id: int,
        timezone_name: str,
        weekday: int,
        local_time: str,
        enabled: bool,
        source_schedule_version: int | None,
        now_utc: datetime | None = None,
    ) -> tuple[bool, WeightReminderSetting | None]:
        current = _require_aware_utc(now_utc or datetime.now(timezone.utc))
        tz_name = validate_timezone(timezone_name)
        day = validate_weekday(weekday)
        normalized_time = normalize_local_time(local_time)
        next_due = calculate_next_occurrence_utc(current, tz_name, day, normalized_time)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if existing is not None:
                current_version = int(existing["schedule_version"])
                if source_schedule_version is None or current_version != int(source_schedule_version):
                    conn.commit()
                    return False, _setting_from_row(existing)
            timestamp = _sqlite_timestamp(current)
            if existing is None:
                if source_schedule_version is not None:
                    conn.commit()
                    return False, None
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
                existing_active = str(existing["delivery_state"]) == ReminderDeliveryState.ACTIVE.value
                next_due_changed = (
                    schedule_changed
                    or not bool(int(existing["enabled"]))
                    or not existing_active
                    or existing["next_due_at_utc"] is None
                )
                next_due_value = (
                    _sqlite_timestamp(next_due.scheduled_utc)
                    if next_due_changed
                    else str(existing["next_due_at_utc"])
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
        return True, _setting_from_row(row) if row is not None else None

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
                SET enabled = 1,
                    delivery_state = ?,
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


    def list_due_settings(
        self,
        *,
        now_utc: datetime,
        allowlist: frozenset[int],
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> list[WeightReminderSetting]:
        current = _sqlite_timestamp(now_utc)
        if not allowlist:
            return []
        placeholders = ",".join("?" for _ in allowlist)
        params: list[object] = [current, *sorted(int(v) for v in allowlist), max(1, int(batch_size))]
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE}
                WHERE enabled = 1
                  AND delivery_state = ?
                  AND next_due_at_utc IS NOT NULL
                  AND next_due_at_utc <= ?
                  AND user_id IN ({placeholders})
                ORDER BY next_due_at_utc ASC, user_id ASC
                LIMIT ?
                """,
                (ReminderDeliveryState.ACTIVE.value, *params),
            ).fetchall()
        return [_setting_from_row(row) for row in rows]

    def recover_expired_claims(self, *, now_utc: datetime) -> dict[str, int]:
        current = _sqlite_timestamp(now_utc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claimed = conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_DELIVERIES_TABLE}
                SET status = ?, claimed_at_utc = NULL, claim_expires_at_utc = NULL, updated_at = ?
                WHERE status = ?
                  AND claim_expires_at_utc IS NOT NULL
                  AND claim_expires_at_utc <= ?
                """,
                (
                    ReminderDeliveryStatus.PENDING.value,
                    current,
                    ReminderDeliveryStatus.CLAIMED.value,
                    current,
                ),
            ).rowcount
            sending = conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_DELIVERIES_TABLE}
                SET status = ?, last_error_type = ?, updated_at = ?
                WHERE status = ?
                  AND claim_expires_at_utc IS NOT NULL
                  AND claim_expires_at_utc <= ?
                """,
                (
                    ReminderDeliveryStatus.DELIVERY_UNKNOWN.value,
                    "expired_sending_claim",
                    current,
                    ReminderDeliveryStatus.SENDING.value,
                    current,
                ),
            ).rowcount
            conn.commit()
        return {"claimed": int(claimed), "sending": int(sending)}

    def claim_due_setting(
        self,
        setting: WeightReminderSetting,
        *,
        now_utc: datetime,
        claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        missed_grace_hours: int = DEFAULT_MISSED_GRACE_HOURS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> WeightReminderClaim:
        current_dt = _require_aware_utc(now_utc)
        current = _sqlite_timestamp(current_dt)
        lease_until = _sqlite_timestamp(current_dt + timedelta(seconds=int(claim_lease_seconds)))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE}
                WHERE user_id = ? AND enabled = 1 AND delivery_state = ? LIMIT 1
                """,
                (int(setting.user_id), ReminderDeliveryState.ACTIVE.value),
            ).fetchone()
            if row is None or row["next_due_at_utc"] is None:
                conn.commit()
                return WeightReminderClaim(ReminderClaimOutcome.NOT_ELIGIBLE)
            fresh = _setting_from_row(row)
            scheduled = _parse_sqlite_timestamp(str(row["next_due_at_utc"]))
            decision = classify_missed_reminder(
                now_utc=current_dt,
                scheduled_utc=scheduled,
                timezone_name=fresh.timezone_name,
                weekday=fresh.weekday,
                local_time=fresh.local_time,
                grace_hours=missed_grace_hours,
            )
            if decision.action is ReminderScheduleAction.WAIT or decision.occurrence is None:
                conn.commit()
                return WeightReminderClaim(ReminderClaimOutcome.WAIT, setting=fresh)
            key = make_delivery_key(
                user_id=fresh.user_id,
                occurrence=decision.occurrence,
                schedule_version=fresh.schedule_version,
            )
            if decision.action is ReminderScheduleAction.SKIP_EXPIRED:
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {WEIGHT_REMINDER_DELIVERIES_TABLE}
                        (user_id, scheduled_for_utc, delivery_key, status, attempt_count,
                         schedule_version, last_error_type, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        fresh.user_id,
                        _sqlite_timestamp(decision.occurrence.scheduled_utc),
                        key,
                        ReminderDeliveryStatus.SKIPPED.value,
                        fresh.schedule_version,
                        "missed_window",
                        current,
                        current,
                    ),
                )
                conn.execute(
                    f"""
                    UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                    SET next_due_at_utc = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        _sqlite_timestamp(decision.next_occurrence.scheduled_utc),
                        current,
                        fresh.user_id,
                    ),
                )
                conn.commit()
                return WeightReminderClaim(
                    ReminderClaimOutcome.SKIPPED_EXPIRED,
                    setting=fresh,
                    occurrence=decision.occurrence,
                    error_type="missed_window",
                )
            conn.execute(
                f"""
                INSERT OR IGNORE INTO {WEIGHT_REMINDER_DELIVERIES_TABLE}
                    (user_id, scheduled_for_utc, delivery_key, status, attempt_count,
                     schedule_version, created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    fresh.user_id,
                    _sqlite_timestamp(decision.occurrence.scheduled_utc),
                    key,
                    ReminderDeliveryStatus.PENDING.value,
                    fresh.schedule_version,
                    current,
                    current,
                ),
            )
            delivery = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE delivery_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if delivery is None:
                conn.rollback()
                raise RuntimeError("Could not load reminder delivery row.")
            status = ReminderDeliveryStatus(str(delivery["status"]))
            attempts = int(delivery["attempt_count"])
            next_attempt_raw = delivery["next_attempt_at_utc"]
            next_attempt_due = next_attempt_raw is None or str(next_attempt_raw) <= current
            reclaim_claimed = (
                status is ReminderDeliveryStatus.CLAIMED
                and delivery["claim_expires_at_utc"] is not None
                and str(delivery["claim_expires_at_utc"]) <= current
            )
            claimable = (
                status is ReminderDeliveryStatus.PENDING
                or (status is ReminderDeliveryStatus.RETRY_WAIT and next_attempt_due)
                or reclaim_claimed
            )
            if not claimable or attempts >= int(max_attempts):
                conn.commit()
                return WeightReminderClaim(ReminderClaimOutcome.NOT_ELIGIBLE, setting=fresh)
            conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_DELIVERIES_TABLE}
                SET status = ?,
                    attempt_count = attempt_count + 1,
                    claimed_at_utc = ?,
                    claim_expires_at_utc = ?,
                    next_attempt_at_utc = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    ReminderDeliveryStatus.CLAIMED.value,
                    current,
                    lease_until,
                    current,
                    int(delivery["id"]),
                ),
            )
            claimed = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE id = ? LIMIT 1",
                (int(delivery["id"]),),
            ).fetchone()
            conn.commit()
        return WeightReminderClaim(
            ReminderClaimOutcome.CLAIMED,
            setting=fresh,
            delivery=_delivery_from_row(claimed),
            occurrence=decision.occurrence,
        )

    def mark_delivery_sending(self, delivery_id: int, *, now_utc: datetime) -> WeightReminderDelivery:
        return self._transition_delivery(
            delivery_id,
            from_status={ReminderDeliveryStatus.CLAIMED},
            to_status=ReminderDeliveryStatus.SENDING,
            now_utc=now_utc,
        )

    def mark_delivery_retry_wait(
        self,
        delivery_id: int,
        *,
        error_type: str,
        next_attempt_at_utc: datetime,
        now_utc: datetime,
    ) -> WeightReminderDelivery:
        return self._transition_delivery(
            delivery_id,
            from_status={ReminderDeliveryStatus.CLAIMED, ReminderDeliveryStatus.SENDING},
            to_status=ReminderDeliveryStatus.RETRY_WAIT,
            now_utc=now_utc,
            error_type=error_type,
            next_attempt_at_utc=next_attempt_at_utc,
            clear_claim=True,
        )

    def mark_delivery_unknown_and_advance(
        self,
        delivery_id: int,
        *,
        setting: WeightReminderSetting,
        now_utc: datetime,
        error_type: str = "ambiguous_delivery",
    ) -> WeightReminderDelivery:
        current = _require_aware_utc(now_utc)
        next_due = calculate_next_occurrence_utc(
            current,
            setting.timezone_name,
            setting.weekday,
            setting.local_time,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = self._transition_delivery_conn(
                conn,
                delivery_id,
                from_status={ReminderDeliveryStatus.SENDING},
                to_status=ReminderDeliveryStatus.DELIVERY_UNKNOWN,
                now_utc=current,
                error_type=error_type,
                clear_claim=True,
            )
            conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                SET next_due_at_utc = ?, updated_at = ?
                WHERE user_id = ? AND schedule_version = ?
                """,
                (
                    _sqlite_timestamp(next_due.scheduled_utc),
                    _sqlite_timestamp(current),
                    setting.user_id,
                    setting.schedule_version,
                ),
            )
            conn.commit()
        return updated

    def mark_delivery_sent_and_advance(
        self,
        delivery_id: int,
        *,
        setting: WeightReminderSetting,
        now_utc: datetime,
    ) -> WeightReminderDelivery:
        current = _require_aware_utc(now_utc)
        next_due = calculate_next_occurrence_utc(
            current,
            setting.timezone_name,
            setting.weekday,
            setting.local_time,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = self._transition_delivery_conn(
                conn,
                delivery_id,
                from_status={ReminderDeliveryStatus.SENDING},
                to_status=ReminderDeliveryStatus.SENT,
                now_utc=current,
                clear_claim=True,
                sent_at_utc=current,
            )
            conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                SET last_delivered_at_utc = ?, next_due_at_utc = ?, updated_at = ?
                WHERE user_id = ? AND schedule_version = ?
                """,
                (
                    _sqlite_timestamp(current),
                    _sqlite_timestamp(next_due.scheduled_utc),
                    _sqlite_timestamp(current),
                    setting.user_id,
                    setting.schedule_version,
                ),
            )
            conn.commit()
        return updated

    def mark_delivery_permanent_failed(
        self,
        delivery_id: int,
        *,
        setting: WeightReminderSetting,
        now_utc: datetime,
        error_type: str,
        suspend_user: bool,
    ) -> WeightReminderDelivery:
        current = _require_aware_utc(now_utc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = self._transition_delivery_conn(
                conn,
                delivery_id,
                from_status={ReminderDeliveryStatus.CLAIMED, ReminderDeliveryStatus.SENDING, ReminderDeliveryStatus.RETRY_WAIT},
                to_status=ReminderDeliveryStatus.PERMANENT_FAILED,
                now_utc=current,
                error_type=error_type,
                clear_claim=True,
            )
            if suspend_user:
                conn.execute(
                    f"""
                    UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                    SET delivery_state = ?, suspended_at_utc = ?, suspension_reason = ?, updated_at = ?
                    WHERE user_id = ? AND schedule_version = ?
                    """,
                    (
                        ReminderDeliveryState.SUSPENDED.value,
                        _sqlite_timestamp(current),
                        _safe_reason(error_type),
                        _sqlite_timestamp(current),
                        setting.user_id,
                        setting.schedule_version,
                    ),
                )
            conn.commit()
        return updated

    def mark_delivery_skipped_stale_and_advance(
        self,
        delivery_id: int,
        *,
        setting: WeightReminderSetting | None,
        now_utc: datetime,
        error_type: str = "stale_schedule",
    ) -> WeightReminderDelivery:
        current = _require_aware_utc(now_utc)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = self._transition_delivery_conn(
                conn,
                delivery_id,
                from_status={ReminderDeliveryStatus.CLAIMED},
                to_status=ReminderDeliveryStatus.SKIPPED_STALE,
                now_utc=current,
                error_type=error_type,
                clear_claim=True,
            )
            if setting is not None and setting.enabled and setting.delivery_state is ReminderDeliveryState.ACTIVE:
                next_due = calculate_next_occurrence_utc(
                    current,
                    setting.timezone_name,
                    setting.weekday,
                    setting.local_time,
                )
                conn.execute(
                    f"""
                    UPDATE {WEIGHT_REMINDER_SETTINGS_TABLE}
                    SET next_due_at_utc = ?, updated_at = ?
                    WHERE user_id = ? AND schedule_version = ?
                    """,
                    (
                        _sqlite_timestamp(next_due.scheduled_utc),
                        _sqlite_timestamp(current),
                        setting.user_id,
                        setting.schedule_version,
                    ),
                )
            conn.commit()
        return updated

    def _transition_delivery(
        self,
        delivery_id: int,
        *,
        from_status: set[ReminderDeliveryStatus],
        to_status: ReminderDeliveryStatus,
        now_utc: datetime,
        error_type: str | None = None,
        next_attempt_at_utc: datetime | None = None,
        clear_claim: bool = False,
    ) -> WeightReminderDelivery:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            updated = self._transition_delivery_conn(
                conn,
                delivery_id,
                from_status=from_status,
                to_status=to_status,
                now_utc=now_utc,
                error_type=error_type,
                next_attempt_at_utc=next_attempt_at_utc,
                clear_claim=clear_claim,
            )
            conn.commit()
        return updated

    def _transition_delivery_conn(
        self,
        conn: sqlite3.Connection,
        delivery_id: int,
        *,
        from_status: set[ReminderDeliveryStatus],
        to_status: ReminderDeliveryStatus,
        now_utc: datetime,
        error_type: str | None = None,
        next_attempt_at_utc: datetime | None = None,
        clear_claim: bool = False,
        sent_at_utc: datetime | None = None,
    ) -> WeightReminderDelivery:
        current = _sqlite_timestamp(now_utc)
        allowed = tuple(status.value for status in from_status)
        placeholders = ",".join("?" for _ in allowed)
        claimed_at_sql = "NULL" if clear_claim else "claimed_at_utc"
        claim_expires_sql = "NULL" if clear_claim else "claim_expires_at_utc"
        conn.execute(
            f"""
            UPDATE {WEIGHT_REMINDER_DELIVERIES_TABLE}
            SET status = ?,
                last_error_type = COALESCE(?, last_error_type),
                next_attempt_at_utc = ?,
                claimed_at_utc = {claimed_at_sql},
                claim_expires_at_utc = {claim_expires_sql},
                sent_at_utc = COALESCE(?, sent_at_utc),
                updated_at = ?
            WHERE id = ? AND status IN ({placeholders})
            """,
            (
                to_status.value,
                _safe_error_type(error_type) if error_type else None,
                _sqlite_timestamp(next_attempt_at_utc) if next_attempt_at_utc else None,
                _sqlite_timestamp(sent_at_utc) if sent_at_utc else None,
                current,
                int(delivery_id),
                *allowed,
            ),
        )
        row = conn.execute(
            f"SELECT * FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE id = ? LIMIT 1",
            (int(delivery_id),),
        ).fetchone()
        if row is None:
            raise RuntimeError("Reminder delivery row disappeared.")
        if ReminderDeliveryStatus(str(row["status"])) is not to_status:
            raise ReminderSchedulingError("illegal reminder delivery transition")
        return _delivery_from_row(row)

    def get_active_setting_if_current(
        self,
        *,
        user_id: int,
        schedule_version: int,
    ) -> WeightReminderSetting | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {WEIGHT_REMINDER_SETTINGS_TABLE}
                WHERE user_id = ? AND schedule_version = ? LIMIT 1
                """,
                (int(user_id), int(schedule_version)),
            ).fetchone()
        return _setting_from_row(row) if row is not None else None


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
    normalized = re.sub(r"[^a-z0-9_:-]+", "_", (value or "").strip().lower()).strip("_")
    allowed = {
        "blocked_user",
        "chat_not_found",
        "bot_blocked",
        "delivery_permanent_failure",
        "manual_suspend",
        "unknown",
    }
    return normalized if normalized in allowed else "unknown"


def _safe_error_type(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9_:-]+", "_", (value or "unknown").strip().lower()).strip("_")
    return normalized[:64] or "unknown"


def get_default_weight_reminder_store() -> HealBiteWeightReminderStore:
    return HealBiteWeightReminderStore()
