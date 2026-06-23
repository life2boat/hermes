from __future__ import annotations
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.healbite_nutrition_diary import load_nutrition_targets, resolve_healbite_db_path

logger = logging.getLogger(__name__)

USERS_TABLE = "users"
USER_ONBOARDING_TABLE = "user_onboarding_state"

_GLOBAL_PROFILE_LOCK = threading.Lock()
_GLOBAL_PROFILE_STORE: HealBiteUserProfileStore | None = None

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    daily_kcal_target REAL,
    daily_protein_target REAL,
    daily_fat_target REAL,
    daily_carbs_target REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS {USER_ONBOARDING_TABLE} (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    step TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_USER_TARGET_COLUMNS = {
    "daily_kcal_target": "REAL",
    "daily_protein_target": "REAL",
    "daily_fat_target": "REAL",
    "daily_carbs_target": "REAL",
}


@dataclass(slots=True)
class HealBiteUserProfile:
    user_id: int
    username: str = ""
    daily_kcal_target: float | None = None
    daily_protein_target: float | None = None
    daily_fat_target: float | None = None
    daily_carbs_target: float | None = None
    created_at: str = ""


@dataclass(slots=True)
class HealBiteOnboardingState:
    user_id: int
    username: str = ""
    step: str = "daily_kcal_target"
    created_at: str = ""


@dataclass(slots=True)
class HealBiteOnboardingReply:
    status: str
    text: str
    profile: HealBiteUserProfile | None = None


def _sqlite_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.strftime("%Y-%m-%d %H:%M:%S")


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).strip().replace(",", ".")
    token: list[str] = []
    seen_digit = False
    for char in cleaned:
        if char.isdigit():
            token.append(char)
            seen_digit = True
            continue
        if char in {"-", "."} and (token or char == "-"):
            token.append(char)
            continue
        if seen_digit:
            break
    if not token:
        return None
    try:
        return float("".join(token))
    except ValueError:
        return None


def _format_target(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    numeric = float(value)
    if numeric.is_integer():
        return f"{int(numeric)} {unit}"
    return f"{numeric:.1f} {unit}"


def format_healbite_profile_report(profile: HealBiteUserProfile | None) -> str:
    if profile is None:
        return (
            "👤 Профиль\n"
            "🎯 Цель ещё не настроена.\n"
            "Нажми /start, и я помогу заполнить базовый профиль."
        )
    return "\n".join(
        [
            "👤 Профиль",
            f"🎯 Цель: {_format_target(profile.daily_kcal_target, 'ккал')}",
            (
                f"Б: {_format_target(profile.daily_protein_target, 'г')} | "
                f"Ж: {_format_target(profile.daily_fat_target, 'г')} | "
                f"У: {_format_target(profile.daily_carbs_target, 'г')}"
            ),
        ]
    )


def format_healbite_onboarding_prompt() -> str:
    return (
        "👋 Привет! Давай настроим базовый профиль HealBite.\n"
        "Напиши свою дневную норму калорий, например: 2000"
    )


def format_healbite_onboarding_invalid_reply() -> str:
    return "Не понял дневную норму калорий. Напиши число, например: 2000"


def format_healbite_onboarding_completed_reply(profile: HealBiteUserProfile) -> str:
    return (
        "✅ Базовый профиль сохранён.\n"
        f"{format_healbite_profile_report(profile)}\n\n"
        "Командой /profile можно посмотреть профиль в любой момент."
    )


class HealBiteUserProfileStore:
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
            user_columns = self._user_columns(conn)
            for column_name, column_type in _USER_TARGET_COLUMNS.items():
                if column_name not in user_columns:
                    conn.execute(
                        f"ALTER TABLE {USERS_TABLE} ADD COLUMN {column_name} {column_type}"
                    )

    @staticmethod
    def _user_columns(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({USERS_TABLE})").fetchall()
            if len(row) > 1
        }

    def _users_identity_column(self, conn: sqlite3.Connection) -> str:
        columns = self._user_columns(conn)
        if "user_id" in columns:
            return "user_id"
        if "telegram_id" in columns:
            return "telegram_id"
        raise RuntimeError("users table has no supported identity column")

    def get_user_profile(self, user_id: int) -> HealBiteUserProfile | None:
        with self._connect() as conn:
            identity_column = self._users_identity_column(conn)
            row = conn.execute(
                f"""
                SELECT
                    {identity_column} AS user_id,
                    username,
                    daily_kcal_target,
                    daily_protein_target,
                    daily_fat_target,
                    daily_carbs_target,
                    created_at
                FROM {USERS_TABLE}
                WHERE {identity_column} = ?
                LIMIT 1
                """,
                (int(user_id),),
            ).fetchone()
        if row is None:
            return None
        profile = HealBiteUserProfile(
            user_id=int(row["user_id"]),
            username=str(row["username"] or ""),
            daily_kcal_target=_to_float(row["daily_kcal_target"]),
            daily_protein_target=_to_float(row["daily_protein_target"]),
            daily_fat_target=_to_float(row["daily_fat_target"]),
            daily_carbs_target=_to_float(row["daily_carbs_target"]),
            created_at=str(row["created_at"] or ""),
        )
        effective_targets = load_nutrition_targets(self.db_path, user_id=int(user_id))
        profile.daily_kcal_target = effective_targets.calories_kcal
        profile.daily_protein_target = effective_targets.protein_g
        profile.daily_fat_target = effective_targets.fat_g
        profile.daily_carbs_target = effective_targets.carbs_g
        return profile

    def upsert_user_profile(
        self,
        *,
        user_id: int,
        username: str = "",
        daily_kcal_target: float | None = None,
        daily_protein_target: float | None = None,
        daily_fat_target: float | None = None,
        daily_carbs_target: float | None = None,
        created_at: str | None = None,
    ) -> HealBiteUserProfile:
        timestamp = created_at or _sqlite_timestamp()
        normalized_user_id = int(user_id)
        normalized_username = (username or "").strip()
        with self._connect() as conn:
            identity_column = self._users_identity_column(conn)
            user_columns = self._user_columns(conn)
            existing_row = conn.execute(
                f"SELECT 1 FROM {USERS_TABLE} WHERE {identity_column} = ? LIMIT 1",
                (normalized_user_id,),
            ).fetchone()

            if existing_row is not None:
                update_clauses: list[str] = []
                update_values: list[Any] = []
                if "username" in user_columns and normalized_username:
                    update_clauses.append("username = ?")
                    update_values.append(normalized_username)
                for field_name, field_value in (
                    ("daily_kcal_target", daily_kcal_target),
                    ("daily_protein_target", daily_protein_target),
                    ("daily_fat_target", daily_fat_target),
                    ("daily_carbs_target", daily_carbs_target),
                ):
                    if field_name in user_columns and field_value is not None:
                        update_clauses.append(f"{field_name} = ?")
                        update_values.append(field_value)
                if "updated_at" in user_columns:
                    update_clauses.append("updated_at = CURRENT_TIMESTAMP")
                if update_clauses:
                    update_values.append(normalized_user_id)
                    conn.execute(
                        f"UPDATE {USERS_TABLE} SET {', '.join(update_clauses)} WHERE {identity_column} = ?",
                        tuple(update_values),
                    )
            else:
                insert_columns = [identity_column]
                insert_values: list[Any] = [normalized_user_id]

                if "username" in user_columns:
                    insert_columns.append("username")
                    insert_values.append(normalized_username)
                for field_name, field_value in (
                    ("daily_kcal_target", daily_kcal_target),
                    ("daily_protein_target", daily_protein_target),
                    ("daily_fat_target", daily_fat_target),
                    ("daily_carbs_target", daily_carbs_target),
                ):
                    if field_name in user_columns:
                        insert_columns.append(field_name)
                        insert_values.append(field_value)
                if "created_at" in user_columns:
                    insert_columns.append("created_at")
                    insert_values.append(timestamp)
                placeholders = ", ".join("?" for _ in insert_columns)
                conn.execute(
                    f"INSERT INTO {USERS_TABLE} ({', '.join(insert_columns)}) VALUES ({placeholders})",
                    tuple(insert_values),
                )
        profile = self.get_user_profile(int(user_id))
        if profile is None:
            raise RuntimeError("Could not load saved HealBite user profile.")
        return profile

    def get_onboarding_state(self, user_id: int) -> HealBiteOnboardingState | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {USER_ONBOARDING_TABLE} WHERE user_id = ? LIMIT 1",
                (int(user_id),),
            ).fetchone()
        if row is None:
            return None
        return HealBiteOnboardingState(
            user_id=int(row["user_id"]),
            username=str(row["username"] or ""),
            step=str(row["step"] or "daily_kcal_target"),
            created_at=str(row["created_at"] or ""),
        )

    def clear_onboarding_state(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                f"DELETE FROM {USER_ONBOARDING_TABLE} WHERE user_id = ?",
                (int(user_id),),
            )

    def begin_onboarding(self, *, user_id: int, username: str = "") -> str:
        profile = self.get_user_profile(int(user_id))
        if profile is not None and profile.daily_kcal_target is not None:
            return format_healbite_profile_report(profile)
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {USER_ONBOARDING_TABLE}(user_id, username, step, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    step = excluded.step
                """,
                (
                    int(user_id),
                    (username or "").strip(),
                    "daily_kcal_target",
                    _sqlite_timestamp(),
                ),
            )
        return format_healbite_onboarding_prompt()

    def handle_onboarding_reply(
        self,
        *,
        user_id: int,
        text: str,
        username: str = "",
    ) -> HealBiteOnboardingReply | None:
        state = self.get_onboarding_state(int(user_id))
        if state is None:
            return None
        if state.step != "daily_kcal_target":
            logger.warning("Unknown HealBite onboarding step %s for user=%s", state.step, user_id)
            self.clear_onboarding_state(int(user_id))
            return HealBiteOnboardingReply(
                status="invalid",
                text=format_healbite_onboarding_invalid_reply(),
            )
        kcal_target = _to_float(text)
        if kcal_target is None or kcal_target <= 0:
            return HealBiteOnboardingReply(
                status="invalid",
                text=format_healbite_onboarding_invalid_reply(),
            )
        profile = self.upsert_user_profile(
            user_id=int(user_id),
            username=(username or state.username or "").strip(),
            daily_kcal_target=kcal_target,
        )
        self.clear_onboarding_state(int(user_id))
        return HealBiteOnboardingReply(
            status="completed",
            text=format_healbite_onboarding_completed_reply(profile),
            profile=profile,
        )

    def delete_user_profile(self, user_id: int) -> None:
        with self._connect() as conn:
            identity_column = self._users_identity_column(conn)
            conn.execute(f"DELETE FROM {USERS_TABLE} WHERE {identity_column} = ?", (int(user_id),))
            conn.execute(f"DELETE FROM {USER_ONBOARDING_TABLE} WHERE user_id = ?", (int(user_id),))


def get_existing_healbite_user_profile() -> HealBiteUserProfileStore | None:
    with _GLOBAL_PROFILE_LOCK:
        return _GLOBAL_PROFILE_STORE


def get_default_healbite_user_profile() -> HealBiteUserProfileStore:
    global _GLOBAL_PROFILE_STORE
    with _GLOBAL_PROFILE_LOCK:
        if _GLOBAL_PROFILE_STORE is None:
            _GLOBAL_PROFILE_STORE = HealBiteUserProfileStore()
        return _GLOBAL_PROFILE_STORE
