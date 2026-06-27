from __future__ import annotations

import html
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_time import local_day_window_utc

WATER_INTAKE_TABLE = "water_intake_events"
WATER_PENDING_TABLE = "water_pending_inputs"
WATER_CUSTOM_AMOUNT_STATE = "water_custom_amount"
WATER_PENDING_TTL = timedelta(minutes=10)
MAX_WATER_ENTRY_ML = 3000
WATER_TARGET_MISSING_HINT = "Цель по воде ещё не настроена. Заполните /profile, чтобы я показывал прогресс."

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {WATER_INTAKE_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount_ml INTEGER NOT NULL CHECK (amount_ml > 0),
    consumed_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'telegram',
    idempotency_key TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_water_intake_user_consumed_at
    ON {WATER_INTAKE_TABLE} (user_id, consumed_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_water_intake_user_idempotency
    ON {WATER_INTAKE_TABLE} (user_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE TABLE IF NOT EXISTS {WATER_PENDING_TABLE} (
    user_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_water_pending_expires_at
    ON {WATER_PENDING_TABLE} (expires_at);
"""


@dataclass(slots=True)
class WaterIntakeEntry:
    id: int
    user_id: int
    amount_ml: int
    consumed_at: str
    source: str = "telegram"
    idempotency_key: str | None = None
    created_at: str = ""


@dataclass(slots=True)
class AddWaterResult:
    added: bool
    duplicate: bool
    entry: WaterIntakeEntry


@dataclass(slots=True)
class UndoWaterResult:
    deleted: bool
    entry: WaterIntakeEntry | None = None


@dataclass(slots=True)
class WaterSummary:
    user_id: int
    consumed_ml: int
    target_ml: int | None
    remaining_ml: int | None
    progress_percent: int | None
    entry_count: int
    entries: list[WaterIntakeEntry]


def _sqlite_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.strftime("%Y-%m-%d %H:%M:%S")


def _parse_sqlite_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)



def _format_ml(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}".replace(",", " ") + " мл"


def _row_to_entry(row: sqlite3.Row) -> WaterIntakeEntry:
    return WaterIntakeEntry(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        amount_ml=int(row["amount_ml"]),
        consumed_at=str(row["consumed_at"]),
        source=str(row["source"] or "telegram"),
        idempotency_key=str(row["idempotency_key"]) if row["idempotency_key"] is not None else None,
        created_at=str(row["created_at"] or ""),
    )


def parse_water_amount(text: str) -> int | None:
    normalized = " ".join((text or "").strip().lower().replace(",", ".").split())
    if not normalized:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(мл|ml)?", normalized)
    multiplier = 1.0
    if match is None:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(л|l|литр|литра|литров)", normalized)
        multiplier = 1000.0
    if match is None:
        return None
    try:
        value = float(match.group(1)) * multiplier
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    amount_ml = int(round(value))
    if amount_ml <= 0 or amount_ml > MAX_WATER_ENTRY_ML:
        return None
    return amount_ml


class HealBiteWaterTracker:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        water_target_resolver: Callable[[int], int | None] | None = None,
    ) -> None:
        self.db_path = resolve_healbite_db_path(db_path)
        self._water_target_resolver = water_target_resolver
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    def add_water_intake(
        self,
        user_id: int,
        amount_ml: int,
        *,
        consumed_at: datetime | None = None,
        source: str = "telegram",
        idempotency_key: str | None = None,
    ) -> AddWaterResult:
        if isinstance(amount_ml, bool) or int(amount_ml) <= 0 or int(amount_ml) > MAX_WATER_ENTRY_ML:
            raise ValueError("amount_ml must be between 1 and 3000")
        normalized_key = (idempotency_key or "").strip() or None
        timestamp = _sqlite_timestamp(consumed_at)
        created_at = _sqlite_timestamp()
        with self._connect() as conn:
            if normalized_key is not None:
                existing = self._entry_by_idempotency(conn, int(user_id), normalized_key)
                if existing is not None:
                    return AddWaterResult(added=False, duplicate=True, entry=existing)
            cursor = conn.execute(
                f"""
                INSERT INTO {WATER_INTAKE_TABLE}
                    (user_id, amount_ml, consumed_at, source, idempotency_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(user_id), int(amount_ml), timestamp, source, normalized_key, created_at),
            )
            entry = self._entry_by_id(conn, int(cursor.lastrowid))
            if entry is None:
                raise RuntimeError("Could not load saved water intake entry.")
            return AddWaterResult(added=True, duplicate=False, entry=entry)

    def _entry_by_idempotency(self, conn: sqlite3.Connection, user_id: int, key: str) -> WaterIntakeEntry | None:
        row = conn.execute(
            f"""
            SELECT * FROM {WATER_INTAKE_TABLE}
            WHERE user_id = ? AND idempotency_key = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (int(user_id), key),
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def _entry_by_id(self, conn: sqlite3.Connection, entry_id: int) -> WaterIntakeEntry | None:
        row = conn.execute(
            f"SELECT * FROM {WATER_INTAKE_TABLE} WHERE id = ? LIMIT 1",
            (int(entry_id),),
        ).fetchone()
        return _row_to_entry(row) if row is not None else None

    def list_water_intake_today(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
        timezone_name: str | None = None,
    ) -> list[WaterIntakeEntry]:
        start_utc, end_utc = local_day_window_utc(now=now, timezone_name=timezone_name)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {WATER_INTAKE_TABLE}
                WHERE user_id = ? AND consumed_at >= ? AND consumed_at < ?
                ORDER BY consumed_at ASC, id ASC
                """,
                (int(user_id), _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def get_water_intake_today(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
        timezone_name: str | None = None,
    ) -> int:
        return sum(entry.amount_ml for entry in self.list_water_intake_today(user_id, now=now, timezone_name=timezone_name))

    def undo_last_water_intake_today(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
        timezone_name: str | None = None,
    ) -> UndoWaterResult:
        start_utc, end_utc = local_day_window_utc(now=now, timezone_name=timezone_name)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {WATER_INTAKE_TABLE}
                WHERE user_id = ? AND consumed_at >= ? AND consumed_at < ?
                ORDER BY consumed_at DESC, id DESC
                LIMIT 1
                """,
                (int(user_id), _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc)),
            ).fetchone()
            if row is None:
                return UndoWaterResult(deleted=False)
            entry = _row_to_entry(row)
            conn.execute(f"DELETE FROM {WATER_INTAKE_TABLE} WHERE id = ?", (entry.id,))
            return UndoWaterResult(deleted=True, entry=entry)

    def get_water_target_ml(self, user_id: int) -> int | None:
        if self._water_target_resolver is None:
            return None
        try:
            target = self._water_target_resolver(int(user_id))
        except Exception:
            return None
        if target in (None, ""):
            return None
        try:
            normalized = int(target)
        except (TypeError, ValueError):
            return None
        return normalized if normalized > 0 else None

    def get_water_summary(
        self,
        user_id: int,
        *,
        now: datetime | None = None,
        timezone_name: str | None = None,
    ) -> WaterSummary:
        entries = self.list_water_intake_today(user_id, now=now, timezone_name=timezone_name)
        consumed = sum(entry.amount_ml for entry in entries)
        target = self.get_water_target_ml(user_id)
        remaining = max(target - consumed, 0) if target is not None else None
        progress = int(round((consumed / target) * 100)) if target else None
        return WaterSummary(
            user_id=int(user_id),
            consumed_ml=consumed,
            target_ml=target,
            remaining_ml=remaining,
            progress_percent=progress,
            entry_count=len(entries),
            entries=entries,
        )

    def stage_custom_amount(self, user_id: int, *, now: datetime | None = None) -> None:
        created = now or datetime.now(timezone.utc)
        expires = created + WATER_PENDING_TTL
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {WATER_PENDING_TABLE}(user_id, state, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    state = excluded.state,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    int(user_id),
                    WATER_CUSTOM_AMOUNT_STATE,
                    _sqlite_timestamp(created),
                    _sqlite_timestamp(expires),
                ),
            )

    def get_pending_state(self, user_id: int, *, now: datetime | None = None) -> str | None:
        current = now or datetime.now(timezone.utc)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT state, expires_at FROM {WATER_PENDING_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
            if row is None:
                return None
            if _parse_sqlite_timestamp(str(row["expires_at"])) <= current.astimezone(timezone.utc):
                conn.execute(f"DELETE FROM {WATER_PENDING_TABLE} WHERE user_id = ?", (int(user_id),))
                return None
            return str(row["state"])

    def clear_pending_state(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {WATER_PENDING_TABLE} WHERE user_id = ?", (int(user_id),))


def format_water_tracker_report(summary: WaterSummary, *, notice: str | None = None) -> str:
    lines = [
        "💧 <b>Вода сегодня</b>",
        "",
        f"Выпито: {_format_ml(summary.consumed_ml)}",
    ]
    if summary.target_ml is None:
        lines.extend(
            [
                "Цель: —",
                WATER_TARGET_MISSING_HINT,
            ]
        )
    else:
        lines.extend(
            [
                f"Цель: {_format_ml(summary.target_ml)}",
                f"Осталось: {_format_ml(summary.remaining_ml)}",
                f"Прогресс: {summary.progress_percent or 0}%",
            ]
        )
    if notice:
        lines.extend(["", html.escape(notice)])
    if summary.entries:
        lines.extend(["", f"Записей сегодня: {summary.entry_count}"])
    return "\n".join(lines)


def format_water_custom_prompt() -> str:
    return "Введите объём воды: например 300 мл или 0,5 л.\n/cancel — отменить."


def _default_water_target_resolver(user_id: int) -> int | None:
    from gateway.healbite_user_profile import get_default_healbite_user_profile

    return get_default_healbite_user_profile().get_water_target_ml(int(user_id))


def get_default_water_tracker() -> HealBiteWaterTracker:
    return HealBiteWaterTracker(water_target_resolver=_default_water_target_resolver)
