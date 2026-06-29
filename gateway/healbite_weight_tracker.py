from __future__ import annotations

import html
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gateway.healbite_nutrition_diary import resolve_healbite_db_path
from gateway.healbite_time import local_day_window_utc

logger = logging.getLogger(__name__)

WEIGHT_ENTRIES_TABLE = "weight_entries"
WEIGHT_PENDING_TABLE = "weight_pending_inputs"
WEIGHT_CUSTOM_STATE = "weight_custom_amount"
WEIGHT_PENDING_TTL = timedelta(minutes=10)
MIN_WEIGHT_KG = 35.0
MAX_WEIGHT_KG = 300.0
DEFAULT_TIMEZONE_NAME = "UTC"

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
    weight_saved: bool
    profile_weight_updated: bool
    recalculation_attempted: bool
    recalculation_completed: bool
    targets_changed: bool
    profile_incomplete: bool
    recalculation_failed: bool = False

    @property
    def profile_updated(self) -> bool:
        return self.profile_weight_updated

    @property
    def targets_recalculated(self) -> bool:
        return self.recalculation_completed

    @property
    def recalculation_error(self) -> bool:
        return self.recalculation_failed


@dataclass(slots=True)
class WeightSummary:
    user_id: int
    latest: WeightEntry | None
    latest_today: WeightEntry | None
    delta_7d_grams: int | None
    delta_30d_grams: int | None
    entries: list[WeightEntry]


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
    name = (value or DEFAULT_TIMEZONE_NAME).strip() or DEFAULT_TIMEZONE_NAME
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return DEFAULT_TIMEZONE_NAME
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


def _target_snapshot(profile: object | None) -> tuple[float | None, float | None, float | None, float | None]:
    if profile is None:
        return (None, None, None, None)
    return (
        getattr(profile, "daily_kcal_target", None),
        getattr(profile, "daily_protein_g", None),
        getattr(profile, "daily_fat_g", None),
        getattr(profile, "daily_carbs_g", None),
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


class HealBiteWeightTracker:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self._uses_default_db_path = db_path is None
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

    def _profile_store(self):
        from gateway.healbite_user_profile import HealBiteUserProfileStore, get_default_healbite_user_profile

        store = get_default_healbite_user_profile()
        store_path = Path(getattr(store, "db_path", self.db_path))
        try:
            same_db = store_path.resolve() == self.db_path.resolve()
        except OSError:
            same_db = str(store_path) == str(self.db_path)
        if same_db:
            return store
        return HealBiteUserProfileStore(db_path=self.db_path)

    def _insert_weight_entry(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: int,
        parsed_weight_kg: float,
        recorded: datetime,
        source: str,
        timezone_name: str | None,
    ) -> sqlite3.Row:
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
                _kg_to_grams(parsed_weight_kg),
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
        return row

    def _update_canonical_profile_weight(self, conn: sqlite3.Connection, *, user_id: int, weight_kg: float) -> None:
        from gateway.healbite_user_profile import PROFILES_TABLE

        store = self._profile_store()
        store._ensure_profile_row(conn, user_id=int(user_id))
        identity_column = store._profiles_identity_column(conn)
        conn.execute(
            f"UPDATE {PROFILES_TABLE} SET weight_kg = ?, updated_at = ? WHERE {identity_column} = ?",
            (_kg_to_grams(weight_kg) / 1000.0, _sqlite_timestamp(), int(user_id)),
        )

    def _commit_weight_write(self, conn: sqlite3.Connection) -> None:
        conn.commit()

    def _persist_weight_and_profile_weight(
        self,
        *,
        user_id: int,
        parsed_weight_kg: float,
        recorded: datetime,
        source: str,
        timezone_name: str | None,
    ) -> WeightEntry:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = self._insert_weight_entry(
                conn,
                user_id=int(user_id),
                parsed_weight_kg=parsed_weight_kg,
                recorded=recorded,
                source=source,
                timezone_name=timezone_name,
            )
            self._update_canonical_profile_weight(conn, user_id=int(user_id), weight_kg=parsed_weight_kg)
            self._commit_weight_write(conn)
            return _row_to_entry(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _log_weight_outcome(
        self,
        *,
        outcome: str,
        result: WeightAddResult | None = None,
        error_type: str | None = None,
    ) -> None:
        logger.info(
            "[HealBite][weight_record] route=weight action=record outcome=%s weight_saved=%s profile_weight_updated=%s recalculation_attempted=%s recalculation_completed=%s targets_changed=%s error_type=%s",
            outcome,
            str(result.weight_saved).lower() if result is not None else "false",
            str(result.profile_weight_updated).lower() if result is not None else "false",
            str(result.recalculation_attempted).lower() if result is not None else "false",
            str(result.recalculation_completed).lower() if result is not None else "false",
            str(result.targets_changed).lower() if result is not None else "false",
            error_type or "none",
        )

    def add_weight_entry(
        self,
        user_id: int,
        weight_kg: float,
        *,
        recorded_at: datetime | None = None,
        source: str = "telegram",
        timezone_name: str | None = None,
    ) -> WeightAddResult:
        from gateway.healbite_nutrition_targets import NutritionTargetValidationError
        from gateway.healbite_user_profile import profile_missing_fields

        parsed = parse_weight_kg(str(weight_kg))
        if parsed is None:
            raise ValueError("weight_kg must be between 35 and 300")
        recorded = recorded_at or datetime.now(timezone.utc)
        if recorded.tzinfo is None:
            recorded = recorded.replace(tzinfo=timezone.utc)
        else:
            recorded = recorded.astimezone(timezone.utc)

        store = self._profile_store()
        targets_before = _target_snapshot(store.get_user_profile(int(user_id)))
        try:
            entry = self._persist_weight_and_profile_weight(
                user_id=int(user_id),
                parsed_weight_kg=parsed,
                recorded=recorded,
                source=source,
                timezone_name=timezone_name,
            )
        except Exception as exc:
            self._log_weight_outcome(outcome="failed", error_type=type(exc).__name__)
            raise

        profile = store.get_user_profile(int(user_id))
        if profile is None:
            raise RuntimeError("Could not load saved HealBite profile after weight update.")

        missing_fields = profile_missing_fields(profile)
        if missing_fields:
            result = WeightAddResult(
                entry=entry,
                weight_saved=True,
                profile_weight_updated=True,
                recalculation_attempted=False,
                recalculation_completed=False,
                targets_changed=False,
                profile_incomplete=True,
                recalculation_failed=False,
            )
            self._log_weight_outcome(outcome="profile_incomplete", result=result)
            return result

        target_source = profile.target_source or ("manual" if profile.manual_kcal_target is not None else "calculated")
        try:
            recalculated = store.recalculate_profile_targets(
                user_id=int(user_id),
                username=profile.username,
                target_source=target_source,
                manual_kcal_target=profile.manual_kcal_target if target_source == "manual" else None,
            )
        except NutritionTargetValidationError:
            result = WeightAddResult(
                entry=entry,
                weight_saved=True,
                profile_weight_updated=True,
                recalculation_attempted=True,
                recalculation_completed=False,
                targets_changed=False,
                profile_incomplete=False,
                recalculation_failed=True,
            )
            self._log_weight_outcome(outcome="recalculation_failed", result=result, error_type="NutritionTargetValidationError")
            return result
        except Exception as exc:
            result = WeightAddResult(
                entry=entry,
                weight_saved=True,
                profile_weight_updated=True,
                recalculation_attempted=True,
                recalculation_completed=False,
                targets_changed=False,
                profile_incomplete=False,
                recalculation_failed=True,
            )
            self._log_weight_outcome(outcome="recalculation_failed", result=result, error_type=type(exc).__name__)
            return result

        targets_changed = _target_snapshot(recalculated) != targets_before
        result = WeightAddResult(
            entry=entry,
            weight_saved=True,
            profile_weight_updated=True,
            recalculation_attempted=True,
            recalculation_completed=True,
            targets_changed=targets_changed,
            profile_incomplete=False,
            recalculation_failed=False,
        )
        self._log_weight_outcome(outcome="success", result=result)
        return result

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


def _delta_since(entries: list[WeightEntry], latest: WeightEntry | None, *, days: int, now: datetime) -> int | None:
    if latest is None:
        return None
    threshold = now.astimezone(timezone.utc) - timedelta(days=days)
    candidates = [entry for entry in entries if _parse_sqlite_timestamp(entry.recorded_at_utc) >= threshold]
    if len(candidates) < 2:
        return None
    return int(latest.weight_grams) - int(candidates[0].weight_grams)


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
    if result.recalculation_failed:
        return "Вес сохранён, но пересчитать нормы сейчас не удалось."
    if result.profile_incomplete:
        return "Вес записан. Для пересчёта КБЖУ заполните /profile."
    if result.recalculation_completed and result.targets_changed:
        return "Вес записан. КБЖУ пересчитаны."
    if result.recalculation_completed:
        return "Вес записан. Нормы КБЖУ не изменились."
    return "Вес записан."


def get_default_weight_tracker() -> HealBiteWeightTracker:
    return HealBiteWeightTracker()
