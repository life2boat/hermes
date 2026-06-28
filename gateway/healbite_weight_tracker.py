from __future__ import annotations

import html
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_time import local_day_window_utc

logger = logging.getLogger(__name__)

WEIGHT_ENTRIES_TABLE = "weight_entries"
WEIGHT_PENDING_TABLE = "weight_pending_inputs"
WEIGHT_REMINDER_TABLE = "weight_reminder_settings"
WEIGHT_CUSTOM_STATE = "weight_custom_amount"
WEIGHT_PENDING_TTL = timedelta(minutes=10)
MIN_WEIGHT_KG = 35.0
MAX_WEIGHT_KG = 300.0
DEFAULT_REMINDER_WEEKDAY = 0
DEFAULT_REMINDER_TIME = "09:00"
DEFAULT_REMINDER_TZ = "UTC"

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {WEIGHT_ENTRIES_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    recorded_at_utc TEXT NOT NULL,
    local_date TEXT NOT NULL,
    weight_grams INTEGER NOT NULL CHECK (weight_grams > 0),
    source TEXT NOT NULL DEFAULT 'telegram',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_entries_user_recorded_at
    ON {WEIGHT_ENTRIES_TABLE} (user_id, recorded_at_utc);
CREATE INDEX IF NOT EXISTS idx_weight_entries_user_local_date
    ON {WEIGHT_ENTRIES_TABLE} (user_id, local_date);
CREATE TABLE IF NOT EXISTS {WEIGHT_PENDING_TABLE} (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weight_pending_expires_at
    ON {WEIGHT_PENDING_TABLE} (expires_at);
CREATE TABLE IF NOT EXISTS {WEIGHT_REMINDER_TABLE} (
    user_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    weekday INTEGER NOT NULL DEFAULT 0,
    time_local TEXT NOT NULL DEFAULT '09:00',
    timezone_name TEXT NOT NULL DEFAULT 'UTC',
    last_sent_local_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(slots=True)
class WeightEntry:
    id: int
    user_id: int
    recorded_at_utc: str
    local_date: str
    weight_grams: int
    source: str = "telegram"
    created_at: str = ""


@dataclass(slots=True)
class WeightAddResult:
    entry: WeightEntry
    profile_updated: bool
    targets_recalculated: bool
    recalculation_error: bool = False


@dataclass(slots=True)
class WeightSummary:
    user_id: int
    latest: WeightEntry | None
    latest_today: WeightEntry | None
    delta_7d_grams: int | None
    delta_30d_grams: int | None
    entries: list[WeightEntry]


@dataclass(slots=True)
class WeightReminderSetting:
    user_id: int
    enabled: bool
    weekday: int
    time_local: str
    timezone_name: str
    last_sent_local_date: str | None = None


def _sqlite_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.strftime("%Y-%m-%d %H:%M:%S")


def _parse_sqlite_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _normalize_timezone_name(value: str | None) -> str:
    name = (value or DEFAULT_REMINDER_TZ).strip() or DEFAULT_REMINDER_TZ
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return DEFAULT_REMINDER_TZ
    return name


def _local_date(value: datetime, timezone_name: str | None = None) -> str:
    tz_name = _normalize_timezone_name(timezone_name)
    return value.astimezone(ZoneInfo(tz_name)).date().isoformat()


def _row_to_entry(row: sqlite3.Row) -> WeightEntry:
    return WeightEntry(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        recorded_at_utc=str(row["recorded_at_utc"]),
        local_date=str(row["local_date"]),
        weight_grams=int(row["weight_grams"]),
        source=str(row["source"] or "telegram"),
        created_at=str(row["created_at"] or ""),
    )


def _row_to_reminder(row: sqlite3.Row) -> WeightReminderSetting:
    return WeightReminderSetting(
        user_id=int(row["user_id"]),
        enabled=bool(int(row["enabled"] or 0)),
        weekday=int(row["weekday"] or 0),
        time_local=str(row["time_local"] or DEFAULT_REMINDER_TIME),
        timezone_name=str(row["timezone_name"] or DEFAULT_REMINDER_TZ),
        last_sent_local_date=str(row["last_sent_local_date"]) if row["last_sent_local_date"] else None,
    )


def parse_weight_kg(text: str) -> float | None:
    normalized = " ".join((text or "").strip().lower().replace(",", ".").split())
    if not normalized:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d{1,2})?)\s*(?:кг|kg)?", normalized)
    if match is None:
        return None
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return None
    if value < MIN_WEIGHT_KG or value > MAX_WEIGHT_KG:
        return None
    return round(value, 1)


def _kg_to_grams(value: float) -> int:
    return int(round(float(value) * 1000))


def _format_weight_grams(value: int | None) -> str:
    if value is None:
        return "—"
    kg = int(value) / 1000.0
    text = f"{kg:.1f}".replace(".", ",")
    if text.endswith(",0"):
        text = text[:-2]
    return f"{text} кг"


def _format_delta(value: int | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value > 0 else ""
    return f"{sign}{_format_weight_grams(value)}"


def _safe_source(value: str) -> str:
    token = (value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_.:-]{1,40}", token):
        return token
    return "redacted"


class HealBiteWeightTracker:
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

    def add_weight_entry(
        self,
        user_id: int,
        weight_kg: float,
        *,
        recorded_at: datetime | None = None,
        source: str = "telegram",
        timezone_name: str | None = None,
    ) -> WeightAddResult:
        parsed = parse_weight_kg(str(weight_kg))
        if parsed is None:
            raise ValueError("weight_kg must be between 35 and 300")
        recorded = recorded_at or datetime.now(timezone.utc)
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        else:
            recorded = recorded.astimezone(timezone.utc)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {WEIGHT_ENTRIES_TABLE}
                    (user_id, recorded_at_utc, local_date, weight_grams, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    int(user_id),
                    _sqlite_timestamp(recorded),
                    _local_date(recorded, timezone_name=timezone_name),
                    _kg_to_grams(parsed),
                    source,
                    _sqlite_timestamp(),
                ),
            )
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_ENTRIES_TABLE} WHERE id = ? LIMIT 1",
                (int(cursor.lastrowid),),
            ).fetchone()
        if row is None:
            raise RuntimeError("Could not load saved weight entry.")
        entry = _row_to_entry(row)
        profile_updated, targets_recalculated, recalculation_error = self._update_profile_weight(int(user_id), parsed)
        logger.info(
            "[HealBite][weight_entry_saved] result=ok source=%s profile_updated=%s targets_recalculated=%s recalculation_error=%s",
            _safe_source(source),
            str(profile_updated).lower(),
            str(targets_recalculated).lower(),
            str(recalculation_error).lower(),
        )
        return WeightAddResult(entry, profile_updated, targets_recalculated, recalculation_error)

    def _update_profile_weight(self, user_id: int, weight_kg: float) -> tuple[bool, bool, bool]:
        try:
            from gateway.healbite_nutrition_targets import NutritionTargetValidationError
            from gateway.healbite_user_profile import get_default_healbite_user_profile, profile_missing_fields

            store = get_default_healbite_user_profile()
            profile = store.upsert_user_profile(user_id=int(user_id), weight_kg=float(weight_kg))
            if profile_missing_fields(profile):
                return True, False, False
            target_source = profile.target_source or ("manual" if profile.manual_kcal_target is not None else "calculated")
            try:
                store.recalculate_profile_targets(
                    user_id=int(user_id),
                    username=profile.username,
                    target_source=target_source,
                    manual_kcal_target=profile.manual_kcal_target if target_source == "manual" else None,
                )
            except NutritionTargetValidationError:
                logger.info(
                    "[HealBite][weight_profile_recalculation] result=validation_error target_source=%s",
                    _safe_source(target_source),
                )
                return True, False, True
            return True, True, False
        except Exception as exc:
            logger.info(
                "[HealBite][weight_profile_recalculation] result=error error_type=%s",
                type(exc).__name__,
            )
            return False, False, True

    def latest_weight(self, user_id: int) -> WeightEntry | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {WEIGHT_ENTRIES_TABLE}
                WHERE user_id = ?
                ORDER BY recorded_at_utc DESC, id DESC
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def list_weight_entries(self, user_id: int, *, days: int = 30, now: datetime | None = None) -> list[WeightEntry]:
        end = now or datetime.now(timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        else:
            end = end.astimezone(timezone.utc)
        start = end - timedelta(days=max(int(days), 1))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {WEIGHT_ENTRIES_TABLE}
                WHERE user_id = ? AND recorded_at_utc >= ? AND recorded_at_utc <= ?
                ORDER BY recorded_at_utc ASC, id ASC
                """,
                (int(user_id), _sqlite_timestamp(start), _sqlite_timestamp(end)),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def get_summary(self, user_id: int, *, now: datetime | None = None, timezone_name: str | None = None) -> WeightSummary:
        current = now or datetime.now(timezone.utc)
        entries = self.list_weight_entries(user_id, days=30, now=current)
        latest = entries[-1] if entries else self.latest_weight(user_id)
        today_start, today_end = local_day_window_utc(now=current, timezone_name=timezone_name)
        latest_today = None
        for entry in reversed(entries):
            recorded = _parse_sqlite_timestamp(entry.recorded_at_utc)
            if today_start <= recorded < today_end:
                latest_today = entry
                break
        return WeightSummary(
            user_id=int(user_id),
            latest=latest,
            latest_today=latest_today,
            delta_7d_grams=_delta_since(entries, latest, days=7, now=current),
            delta_30d_grams=_delta_since(entries, latest, days=30, now=current),
            entries=entries,
        )

    def stage_custom_weight(self, user_id: int, *, now: datetime | None = None) -> None:
        created = now or datetime.now(timezone.utc)
        expires = created + WEIGHT_PENDING_TTL
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {WEIGHT_PENDING_TABLE}(user_id, state, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state = excluded.state,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (int(user_id), WEIGHT_CUSTOM_STATE, _sqlite_timestamp(created), _sqlite_timestamp(expires)),
            )

    def get_pending_state(self, user_id: int, *, now: datetime | None = None) -> str | None:
        current = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT state, expires_at FROM {WEIGHT_PENDING_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if row is None:
                return None
            if _parse_sqlite_timestamp(str(row["expires_at"])) <= current.astimezone(timezone.utc):
                conn.execute(f"DELETE FROM {WEIGHT_PENDING_TABLE} WHERE user_id = ?", (int(user_id),))
                return None
            return str(row["state"])

    def clear_pending_state(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {WEIGHT_PENDING_TABLE} WHERE user_id = ?", (int(user_id),))

    def set_weekly_reminder(
        self,
        user_id: int,
        *,
        enabled: bool,
        weekday: int = DEFAULT_REMINDER_WEEKDAY,
        time_local: str = DEFAULT_REMINDER_TIME,
        timezone_name: str = DEFAULT_REMINDER_TZ,
    ) -> WeightReminderSetting:
        normalized_weekday = int(weekday) % 7
        normalized_time = _normalize_time_local(time_local)
        normalized_tz = _normalize_timezone_name(timezone_name)
        timestamp = _sqlite_timestamp()
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {WEIGHT_REMINDER_TABLE}
                    (user_id, enabled, weekday, time_local, timezone_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    weekday = excluded.weekday,
                    time_local = excluded.time_local,
                    timezone_name = excluded.timezone_name,
                    updated_at = excluded.updated_at
                """,
                (int(user_id), 1 if enabled else 0, normalized_weekday, normalized_time, normalized_tz, timestamp, timestamp),
            )
        logger.info("[HealBite][weight_reminder_updated] enabled=%s", str(bool(enabled)).lower())
        return self.get_weekly_reminder(int(user_id)) or WeightReminderSetting(
            int(user_id), bool(enabled), normalized_weekday, normalized_time, normalized_tz
        )

    def get_weekly_reminder(self, user_id: int) -> WeightReminderSetting | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        return _row_to_reminder(row) if row is not None else None

    def due_weekly_reminders(self, *, now: datetime | None = None, limit: int = 50) -> list[WeightReminderSetting]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        due: list[WeightReminderSetting] = []
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM {WEIGHT_REMINDER_TABLE} WHERE enabled = 1 ORDER BY user_id ASC LIMIT ?",
                (max(int(limit), 1),),
            ).fetchall()
        for row in rows:
            setting = _row_to_reminder(row)
            if _is_reminder_due(setting, current):
                due.append(setting)
        return due

    def mark_reminder_sent(self, user_id: int, *, local_date: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE {WEIGHT_REMINDER_TABLE}
                SET last_sent_local_date = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (local_date, _sqlite_timestamp(), int(user_id)),
            )


def _delta_since(entries: list[WeightEntry], latest: WeightEntry | None, *, days: int, now: datetime) -> int | None:
    if latest is None:
        return None
    threshold = now.astimezone(timezone.utc) - timedelta(days=days)
    candidates = [entry for entry in entries if _parse_sqlite_timestamp(entry.recorded_at_utc) >= threshold]
    if len(candidates) < 2:
        return None
    return int(latest.weight_grams) - int(candidates[0].weight_grams)


def _normalize_time_local(value: str) -> str:
    text = (value or DEFAULT_REMINDER_TIME).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if match is None:
        return DEFAULT_REMINDER_TIME
    hour = int(match.group(1))
    minute = int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return DEFAULT_REMINDER_TIME
    return f"{hour:02d}:{minute:02d}"


def _is_reminder_due(setting: WeightReminderSetting, now: datetime) -> bool:
    tz = ZoneInfo(_normalize_timezone_name(setting.timezone_name))
    local_now = now.astimezone(tz)
    if local_now.weekday() != int(setting.weekday):
        return False
    hour, minute = [int(part) for part in _normalize_time_local(setting.time_local).split(":")]
    local_date = local_now.date().isoformat()
    if setting.last_sent_local_date == local_date:
        return False
    return local_now.time() >= time(hour=hour, minute=minute)


def format_weight_tracker_report(summary: WeightSummary, *, notice: str | None = None) -> str:
    lines = ["⚖️ <b>Вес</b>", ""]
    if summary.latest is None:
        lines.extend([
            "Пока нет записей веса.",
            "Нажмите «Записать вес» или отправьте /weight 82,4.",
        ])
    else:
        today = _format_weight_grams(summary.latest_today.weight_grams) if summary.latest_today else "—"
        lines.extend([
            f"Текущий: {_format_weight_grams(summary.latest.weight_grams)}",
            f"Сегодня: {today}",
            f"7 дней: {_format_delta(summary.delta_7d_grams)}",
            f"30 дней: {_format_delta(summary.delta_30d_grams)}",
        ])
    if notice:
        lines.extend(["", html.escape(notice)])
    lines.extend(["", "Запись веса обновляет профиль и пересчитывает КБЖУ, если профиль заполнен."])
    return "\n".join(lines)


def format_weight_custom_prompt() -> str:
    return "Введите вес в килограммах: например 82,4 или 82.4 кг.\n/cancel — отменить."


def format_weight_saved_notice(result: WeightAddResult) -> str:
    if result.targets_recalculated:
        return "Вес записан. КБЖУ пересчитаны."
    if result.recalculation_error:
        return "Вес записан. Пересчитать КБЖУ сейчас не удалось."
    return "Вес записан. Для пересчёта КБЖУ заполните /profile."


def format_weight_reminder_report(setting: WeightReminderSetting | None) -> str:
    if setting is not None and setting.enabled:
        return "🔔 Еженедельное напоминание о весе включено."
    return "🔕 Еженедельное напоминание о весе выключено."


def get_default_weight_tracker() -> HealBiteWeightTracker:
    return HealBiteWeightTracker()
