from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gateway.healbite_nutrition_diary import load_nutrition_targets, resolve_healbite_db_path
from gateway.healbite_nutrition_targets import (
    NUTRITION_CALCULATION_VERSION,
    NutritionProfileInputs,
    NutritionTargetValidationError,
    activity_level_label,
    calculate_nutrition_targets,
    goal_label,
    normalize_activity_level,
    normalize_goal,
    normalize_sex,
    sex_label,
)

logger = logging.getLogger(__name__)

PROFILE_CALCULATION_DISCLAIMER = "Расчёт носит справочный характер и не заменяет консультацию врача или специалиста по питанию."

USERS_TABLE = "users"
PROFILES_TABLE = "profiles"
USER_ONBOARDING_TABLE = "user_onboarding_state"

ONBOARDING_STEP_SEX = "sex"
ONBOARDING_STEP_AGE = "age"
ONBOARDING_STEP_HEIGHT = "height_cm"
ONBOARDING_STEP_WEIGHT = "weight_kg"
ONBOARDING_STEP_GOAL = "goal"
ONBOARDING_STEP_ACTIVITY = "activity_level"
ONBOARDING_STEP_MANUAL_TARGET = "manual_kcal_target"

ONBOARDING_STEPS = (
    ONBOARDING_STEP_SEX,
    ONBOARDING_STEP_AGE,
    ONBOARDING_STEP_HEIGHT,
    ONBOARDING_STEP_WEIGHT,
    ONBOARDING_STEP_GOAL,
    ONBOARDING_STEP_ACTIVITY,
    ONBOARDING_STEP_MANUAL_TARGET,
)

SEX_OPTION_ROWS = [["Мужской", "Женский"]]
GOAL_OPTION_ROWS = [["Снижение веса"], ["Поддержание веса"], ["Набор массы"]]
ACTIVITY_OPTION_ROWS = [
    ["Минимальная активность"],
    ["Лёгкая активность"],
    ["Умеренная активность"],
    ["Высокая активность"],
    ["Очень высокая активность"],
]
MANUAL_TARGET_SKIP_ROWS = [["Пропустить"]]
MANUAL_TARGET_KEEP_ROWS = [["Оставить как есть"], ["Рассчитать автоматически"]]

_GLOBAL_PROFILE_LOCK = threading.Lock()
_GLOBAL_PROFILE_STORE: HealBiteUserProfileStore | None = None
_UNSET = object()

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {USERS_TABLE} (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    daily_kcal_target REAL,
    daily_protein_target REAL,
    daily_fat_target REAL,
    daily_carbs_target REAL,
    daily_protein_g REAL,
    daily_fat_g REAL,
    daily_carbs_g REAL,
    calculated_bmr_kcal REAL,
    calculated_tdee_kcal REAL,
    nutrition_calculation_version TEXT,
    nutrition_calculated_at TEXT,
    target_source TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS {PROFILES_TABLE} (
    telegram_id INTEGER PRIMARY KEY,
    age INTEGER,
    sex TEXT,
    height_cm INTEGER,
    weight_kg REAL,
    goal TEXT,
    activity TEXT,
    calories_limit REAL,
    allergies TEXT,
    stop_products TEXT,
    preferences TEXT,
    medical_flags TEXT,
    onboarding_done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS {USER_ONBOARDING_TABLE} (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    step TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_USER_COLUMNS = {
    "daily_kcal_target": "REAL",
    "daily_protein_target": "REAL",
    "daily_fat_target": "REAL",
    "daily_carbs_target": "REAL",
    "daily_protein_g": "REAL",
    "daily_fat_g": "REAL",
    "daily_carbs_g": "REAL",
    "calculated_bmr_kcal": "REAL",
    "calculated_tdee_kcal": "REAL",
    "nutrition_calculation_version": "TEXT",
    "nutrition_calculated_at": "TEXT",
    "target_source": "TEXT",
}

_PROFILE_COLUMNS = {
    "activity_level": "TEXT",
    "water_target_ml": "INTEGER",
}


@dataclass(slots=True)
class HealBiteUserProfile:
    user_id: int
    username: str = ""
    sex: str | None = None
    age: int | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    goal: str | None = None
    activity_level: str | None = None
    manual_kcal_target: float | None = None
    daily_kcal_target: float | None = None
    daily_protein_g: float | None = None
    daily_fat_g: float | None = None
    daily_carbs_g: float | None = None
    calculated_bmr_kcal: float | None = None
    calculated_tdee_kcal: float | None = None
    nutrition_calculation_version: str | None = None
    nutrition_calculated_at: str = ""
    target_source: str | None = None
    water_target_ml: int | None = None
    created_at: str = ""

    @property
    def daily_protein_target(self) -> float | None:
        return self.daily_protein_g

    @property
    def daily_fat_target(self) -> float | None:
        return self.daily_fat_g

    @property
    def daily_carbs_target(self) -> float | None:
        return self.daily_carbs_g


@dataclass(slots=True)
class HealBiteOnboardingState:
    user_id: int
    username: str = ""
    step: str = ONBOARDING_STEP_SEX
    created_at: str = ""


@dataclass(slots=True)
class HealBiteOnboardingReply:
    status: str
    text: str
    profile: HealBiteUserProfile | None = None
    next_step: str | None = None


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


def _to_int(value: Any) -> int | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    return int(round(numeric))


def _format_target(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    numeric = float(value)
    if numeric.is_integer():
        return f"{int(numeric)} {unit}"
    return f"{numeric:.1f} {unit}"


def format_calculation_version(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if not normalized:
        return "—"
    if normalized == NUTRITION_CALCULATION_VERSION:
        return "Mifflin v1"
    return value or "—"


def _manual_target_reply_hint(profile: HealBiteUserProfile | None) -> str:
    if profile is not None and profile.manual_kcal_target is not None:
        return (
            f"Сейчас сохранена ручная цель: {_format_target(profile.manual_kcal_target, 'ккал')}.\n"
            "Отправьте новое число, нажмите «Оставить как есть» или «Рассчитать автоматически»."
        )
    return "Если хотите, отправьте свою ручную цель калорий. Или нажмите «Пропустить»."


def onboarding_keyboard_rows(step: str, profile: HealBiteUserProfile | None = None) -> list[list[str]] | None:
    if step == ONBOARDING_STEP_SEX:
        return SEX_OPTION_ROWS
    if step == ONBOARDING_STEP_GOAL:
        return GOAL_OPTION_ROWS
    if step == ONBOARDING_STEP_ACTIVITY:
        return ACTIVITY_OPTION_ROWS
    if step == ONBOARDING_STEP_MANUAL_TARGET:
        if profile is not None and profile.manual_kcal_target is not None:
            return MANUAL_TARGET_KEEP_ROWS
        return MANUAL_TARGET_SKIP_ROWS
    return None


def format_healbite_profile_report(profile: HealBiteUserProfile | None) -> str:
    if profile is None:
        return (
            "👤 Профиль\n"
            "🎯 Цель ещё не настроена.\n"
            "Нажми /start, и я помогу заполнить базовый профиль."
        )

    lines = [
        "👤 Ваш профиль",
        f"Цель: {goal_label(profile.goal)}",
        f"Пол: {sex_label(profile.sex)}",
        f"Возраст: {_format_target(profile.age, 'лет')}",
        f"Рост: {_format_target(profile.height_cm, 'см')}",
        f"Вес: {_format_target(profile.weight_kg, 'кг')}",
        f"Активность: {activity_level_label(profile.activity_level)}",
        "",
    ]

    if profile.daily_kcal_target is not None:
        lines.extend(
            [
                f"🔥 Суточная норма: {_format_target(profile.daily_kcal_target, 'ккал')}",
                f"🥩 Белки: {_format_target(profile.daily_protein_target, 'г')}",
                f"🥑 Жиры: {_format_target(profile.daily_fat_target, 'г')}",
                f"🍚 Углеводы: {_format_target(profile.daily_carbs_target, 'г')}",
            ]
        )
        if profile.nutrition_calculation_version:
            lines.extend(
                [
                    "",
                    f"Расчёт: {format_calculation_version(profile.nutrition_calculation_version)}",
                ]
            )
        if profile.target_source == "manual":
            lines.append("Источник цели: ручная калорийность сохранена.")
        lines.extend(
            [
                "",
                PROFILE_CALCULATION_DISCLAIMER,
            ]
        )
    else:
        lines.append("🔥 Суточная норма: —")

    missing = profile_missing_fields(profile)
    if missing:
        lines.extend(
            [
                "",
                "Чтобы рассчитать персональную норму, заполните:",
                "• " + "\n• ".join(missing),
            ]
        )
    return "\n".join(lines)


def format_healbite_onboarding_prompt(step: str, profile: HealBiteUserProfile | None = None, *, edit_mode: bool = False) -> str:
    prefix = "🛠 Обновим профиль HealBite.\n" if edit_mode else "👋 Давайте настроим профиль HealBite.\n"
    prompts = {
        ONBOARDING_STEP_SEX: "Укажите пол: Мужской или Женский.",
        ONBOARDING_STEP_AGE: "Сколько вам полных лет? Укажите число от 18 до 100.",
        ONBOARDING_STEP_HEIGHT: "Укажите рост в сантиметрах, например: 180.",
        ONBOARDING_STEP_WEIGHT: "Укажите текущий вес в килограммах, например: 85.",
        ONBOARDING_STEP_GOAL: "Выберите цель: Снижение веса, Поддержание веса или Набор массы.",
        ONBOARDING_STEP_ACTIVITY: "Выберите уровень активности.",
        ONBOARDING_STEP_MANUAL_TARGET: _manual_target_reply_hint(profile),
    }
    return prefix + prompts.get(step, "Продолжим настройку профиля.")


def format_healbite_onboarding_invalid_reply(step: str, profile: HealBiteUserProfile | None = None) -> str:
    messages = {
        ONBOARDING_STEP_SEX: "Не понял пол. Выберите: Мужской или Женский.",
        ONBOARDING_STEP_AGE: "Не понял возраст. Напишите число от 18 до 100.",
        ONBOARDING_STEP_HEIGHT: "Не понял рост. Напишите число в сантиметрах, например: 180.",
        ONBOARDING_STEP_WEIGHT: "Не понял вес. Напишите число в килограммах, например: 85.",
        ONBOARDING_STEP_GOAL: "Не понял цель. Выберите: Снижение веса, Поддержание веса или Набор массы.",
        ONBOARDING_STEP_ACTIVITY: "Не понял активность. Выберите один из предложенных вариантов.",
        ONBOARDING_STEP_MANUAL_TARGET: _manual_target_reply_hint(profile),
    }
    return messages.get(step, "Не понял ответ. Попробуйте ещё раз.")


def format_healbite_onboarding_completed_reply(profile: HealBiteUserProfile) -> str:
    return (
        "✅ Профиль обновлён.\n"
        f"{format_healbite_profile_report(profile)}\n\n"
        "Командой /profile можно посмотреть профиль в любой момент."
    )


def profile_missing_fields(profile: HealBiteUserProfile | None) -> list[str]:
    if profile is None:
        return ["Пол", "Возраст", "Рост", "Вес", "Цель", "Активность"]
    missing: list[str] = []
    if normalize_sex(profile.sex) is None:
        missing.append("Пол")
    if profile.age is None:
        missing.append("Возраст")
    if profile.height_cm is None:
        missing.append("Рост")
    if profile.weight_kg is None:
        missing.append("Вес")
    if normalize_goal(profile.goal) is None:
        missing.append("Цель")
    if normalize_activity_level(profile.activity_level) is None:
        missing.append("Активность")
    return missing


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
            user_columns = self._table_columns(conn, USERS_TABLE)
            for column_name, column_type in _USER_COLUMNS.items():
                if column_name not in user_columns:
                    conn.execute(f"ALTER TABLE {USERS_TABLE} ADD COLUMN {column_name} {column_type}")
            profile_columns = self._table_columns(conn, PROFILES_TABLE)
            for column_name, column_type in _PROFILE_COLUMNS.items():
                if column_name not in profile_columns:
                    conn.execute(f"ALTER TABLE {PROFILES_TABLE} ADD COLUMN {column_name} {column_type}")

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            if len(row) > 1
        }

    def _users_identity_column(self, conn: sqlite3.Connection) -> str:
        columns = self._table_columns(conn, USERS_TABLE)
        if "user_id" in columns:
            return "user_id"
        if "telegram_id" in columns:
            return "telegram_id"
        raise RuntimeError("users table has no supported identity column")

    def _profiles_identity_column(self, conn: sqlite3.Connection) -> str:
        columns = self._table_columns(conn, PROFILES_TABLE)
        if "telegram_id" in columns:
            return "telegram_id"
        if "user_id" in columns:
            return "user_id"
        raise RuntimeError("profiles table has no supported identity column")

    def _load_user_row(self, conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
        identity_column = self._users_identity_column(conn)
        return conn.execute(
            f"SELECT * FROM {USERS_TABLE} WHERE {identity_column} = ? LIMIT 1",
            (int(user_id),),
        ).fetchone()

    def _load_profile_row(self, conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
        identity_column = self._profiles_identity_column(conn)
        return conn.execute(
            f"SELECT * FROM {PROFILES_TABLE} WHERE {identity_column} = ? LIMIT 1",
            (int(user_id),),
        ).fetchone()

    def get_user_profile(self, user_id: int) -> HealBiteUserProfile | None:
        with self._connect() as conn:
            user_row = self._load_user_row(conn, int(user_id))
            profile_row = self._load_profile_row(conn, int(user_id))
        if user_row is None and profile_row is None:
            return None

        effective_targets = load_nutrition_targets(self.db_path, user_id=int(user_id))
        manual_kcal_target = None
        if profile_row is not None and "calories_limit" in profile_row.keys():
            manual_kcal_target = _to_float(profile_row["calories_limit"])
        if manual_kcal_target is None and user_row is not None:
            target_source = str(user_row["target_source"] or "").strip().lower() if "target_source" in user_row.keys() else ""
            calc_version = str(user_row["nutrition_calculation_version"] or "").strip() if "nutrition_calculation_version" in user_row.keys() else ""
            if (not target_source or target_source == "manual") and not calc_version:
                manual_kcal_target = _to_float(user_row["daily_kcal_target"])

        profile = HealBiteUserProfile(
            user_id=int(user_id),
            username=str((user_row["username"] if user_row is not None and "username" in user_row.keys() else "") or ""),
            sex=normalize_sex(profile_row["sex"]) if profile_row is not None and "sex" in profile_row.keys() else None,
            age=_to_int(profile_row["age"]) if profile_row is not None and "age" in profile_row.keys() else None,
            height_cm=_to_float(profile_row["height_cm"]) if profile_row is not None and "height_cm" in profile_row.keys() else None,
            weight_kg=_to_float(profile_row["weight_kg"]) if profile_row is not None and "weight_kg" in profile_row.keys() else None,
            goal=normalize_goal(profile_row["goal"]) if profile_row is not None and "goal" in profile_row.keys() else None,
            activity_level=(
                normalize_activity_level(profile_row["activity_level"])
                if profile_row is not None and "activity_level" in profile_row.keys() and profile_row["activity_level"] not in (None, "")
                else normalize_activity_level(profile_row["activity"]) if profile_row is not None and "activity" in profile_row.keys() else None
            ),
            manual_kcal_target=manual_kcal_target,
            daily_kcal_target=effective_targets.calories_kcal,
            daily_protein_g=effective_targets.protein_g,
            daily_fat_g=effective_targets.fat_g,
            daily_carbs_g=effective_targets.carbs_g,
            calculated_bmr_kcal=_to_float(user_row["calculated_bmr_kcal"]) if user_row is not None and "calculated_bmr_kcal" in user_row.keys() else None,
            calculated_tdee_kcal=_to_float(user_row["calculated_tdee_kcal"]) if user_row is not None and "calculated_tdee_kcal" in user_row.keys() else None,
            nutrition_calculation_version=str((user_row["nutrition_calculation_version"] if user_row is not None and "nutrition_calculation_version" in user_row.keys() else "") or "") or None,
            nutrition_calculated_at=str((user_row["nutrition_calculated_at"] if user_row is not None and "nutrition_calculated_at" in user_row.keys() else "") or ""),
            target_source=str((user_row["target_source"] if user_row is not None and "target_source" in user_row.keys() else "") or "") or None,
            water_target_ml=(
                _to_int(profile_row["water_target_ml"])
                if profile_row is not None and "water_target_ml" in profile_row.keys()
                else None
            ),
            created_at=str(
                (
                    user_row["created_at"]
                    if user_row is not None and "created_at" in user_row.keys()
                    else profile_row["created_at"]
                    if profile_row is not None and "created_at" in profile_row.keys()
                    else ""
                )
                or ""
            ),
        )
        return profile

    def get_water_target_ml(self, user_id: int) -> int | None:
        profile = self.get_user_profile(int(user_id))
        if profile is None:
            return None
        target = profile.water_target_ml
        return int(target) if target is not None and int(target) > 0 else None

    def _ensure_user_row(self, conn: sqlite3.Connection, *, user_id: int, username: str = "") -> None:
        identity_column = self._users_identity_column(conn)
        row = conn.execute(
            f"SELECT 1 FROM {USERS_TABLE} WHERE {identity_column} = ? LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if row is None:
            conn.execute(
                f"INSERT INTO {USERS_TABLE} ({identity_column}, username, created_at) VALUES (?, ?, ?)",
                (int(user_id), (username or "").strip(), _sqlite_timestamp()),
            )

    def _ensure_profile_row(self, conn: sqlite3.Connection, *, user_id: int) -> None:
        identity_column = self._profiles_identity_column(conn)
        row = conn.execute(
            f"SELECT 1 FROM {PROFILES_TABLE} WHERE {identity_column} = ? LIMIT 1",
            (int(user_id),),
        ).fetchone()
        if row is None:
            conn.execute(
                f"INSERT INTO {PROFILES_TABLE} ({identity_column}, created_at, updated_at) VALUES (?, ?, ?)",
                (int(user_id), _sqlite_timestamp(), _sqlite_timestamp()),
            )

    def upsert_user_profile(
        self,
        *,
        user_id: int,
        username: str = "",
        daily_kcal_target: float | None = None,
        daily_protein_target: float | None = None,
        daily_fat_target: float | None = None,
        daily_carbs_target: float | None = None,
        daily_protein_g: float | None | object = _UNSET,
        daily_fat_g: float | None | object = _UNSET,
        daily_carbs_g: float | None | object = _UNSET,
        sex: str | None | object = _UNSET,
        age: int | None | object = _UNSET,
        height_cm: float | None | object = _UNSET,
        weight_kg: float | None | object = _UNSET,
        goal: str | None | object = _UNSET,
        activity_level: str | None | object = _UNSET,
        manual_kcal_target: float | None | object = _UNSET,
        calculated_bmr_kcal: float | None | object = _UNSET,
        calculated_tdee_kcal: float | None | object = _UNSET,
        nutrition_calculation_version: str | None | object = _UNSET,
        nutrition_calculated_at: str | None | object = _UNSET,
        target_source: str | None | object = _UNSET,
        created_at: str | None = None,
    ) -> HealBiteUserProfile:
        timestamp = created_at or _sqlite_timestamp()
        normalized_user_id = int(user_id)
        normalized_username = (username or "").strip()
        effective_daily_protein_g = daily_protein_target if daily_protein_g is _UNSET else daily_protein_g
        effective_daily_fat_g = daily_fat_target if daily_fat_g is _UNSET else daily_fat_g
        effective_daily_carbs_g = daily_carbs_target if daily_carbs_g is _UNSET else daily_carbs_g
        with self._connect() as conn:
            self._ensure_user_row(conn, user_id=normalized_user_id, username=normalized_username)
            user_columns = self._table_columns(conn, USERS_TABLE)
            updates: list[str] = []
            values: list[Any] = []
            if "username" in user_columns and normalized_username:
                updates.append("username = ?")
                values.append(normalized_username)
            for field_name, field_value in (
                ("daily_kcal_target", daily_kcal_target),
                ("daily_protein_target", effective_daily_protein_g),
                ("daily_fat_target", effective_daily_fat_g),
                ("daily_carbs_target", effective_daily_carbs_g),
                ("daily_protein_g", effective_daily_protein_g),
                ("daily_fat_g", effective_daily_fat_g),
                ("daily_carbs_g", effective_daily_carbs_g),
                ("calculated_bmr_kcal", calculated_bmr_kcal),
                ("calculated_tdee_kcal", calculated_tdee_kcal),
                ("nutrition_calculation_version", nutrition_calculation_version),
                ("nutrition_calculated_at", nutrition_calculated_at),
                ("target_source", target_source),
            ):
                if field_name in user_columns and field_value is not _UNSET:
                    updates.append(f"{field_name} = ?")
                    values.append(field_value)
            if "updated_at" in user_columns:
                updates.append("updated_at = CURRENT_TIMESTAMP")
            if updates:
                values.append(normalized_user_id)
                conn.execute(
                    f"UPDATE {USERS_TABLE} SET {', '.join(updates)} WHERE {self._users_identity_column(conn)} = ?",
                    tuple(values),
                )

            raw_updates_present = any(value is not _UNSET for value in (sex, age, height_cm, weight_kg, goal, activity_level, manual_kcal_target))
            if raw_updates_present:
                self._ensure_profile_row(conn, user_id=normalized_user_id)
                profile_updates: list[str] = []
                profile_values: list[Any] = []
                if sex is not _UNSET:
                    profile_updates.append("sex = ?")
                    profile_values.append(normalize_sex(sex) if sex not in (None, "") else None)
                if age is not _UNSET:
                    profile_updates.append("age = ?")
                    profile_values.append(_to_int(age))
                if height_cm is not _UNSET:
                    profile_updates.append("height_cm = ?")
                    profile_values.append(_to_float(height_cm))
                if weight_kg is not _UNSET:
                    profile_updates.append("weight_kg = ?")
                    profile_values.append(_to_float(weight_kg))
                if goal is not _UNSET:
                    profile_updates.append("goal = ?")
                    profile_values.append(normalize_goal(goal) if goal not in (None, "") else None)
                if activity_level is not _UNSET:
                    normalized_activity = normalize_activity_level(activity_level) if activity_level not in (None, "") else None
                    profile_updates.append("activity = ?")
                    profile_values.append(normalized_activity)
                    if "activity_level" in self._table_columns(conn, PROFILES_TABLE):
                        profile_updates.append("activity_level = ?")
                        profile_values.append(normalized_activity)
                if manual_kcal_target is not _UNSET:
                    profile_updates.append("calories_limit = ?")
                    profile_values.append(_to_float(manual_kcal_target))
                profile_updates.append("updated_at = ?")
                profile_values.append(timestamp)
                profile_values.append(normalized_user_id)
                conn.execute(
                    f"UPDATE {PROFILES_TABLE} SET {', '.join(profile_updates)} WHERE {self._profiles_identity_column(conn)} = ?",
                    tuple(profile_values),
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
            step=str(row["step"] or ONBOARDING_STEP_SEX),
            created_at=str(row["created_at"] or ""),
        )

    def _set_onboarding_state(self, *, user_id: int, username: str = "", step: str) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {USER_ONBOARDING_TABLE}(user_id, username, step, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    step = excluded.step
                """,
                (int(user_id), (username or "").strip(), step, _sqlite_timestamp()),
            )

    def clear_onboarding_state(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {USER_ONBOARDING_TABLE} WHERE user_id = ?", (int(user_id),))

    def begin_onboarding(self, *, user_id: int, username: str = "", edit_mode: bool = False) -> str:
        profile = self.get_user_profile(int(user_id))
        existing_state = self.get_onboarding_state(int(user_id))
        if existing_state is not None:
            return format_healbite_onboarding_prompt(existing_state.step, profile, edit_mode=edit_mode)
        if not edit_mode and profile is not None and not profile_missing_fields(profile) and profile.daily_kcal_target is not None:
            return format_healbite_profile_report(profile)

        step = ONBOARDING_STEP_SEX if edit_mode else self._next_onboarding_step(profile)
        self._set_onboarding_state(user_id=int(user_id), username=(username or "").strip(), step=step)
        return format_healbite_onboarding_prompt(step, profile, edit_mode=edit_mode)

    def _next_onboarding_step(self, profile: HealBiteUserProfile | None) -> str:
        missing = profile_missing_fields(profile)
        mapping = {
            "Пол": ONBOARDING_STEP_SEX,
            "Возраст": ONBOARDING_STEP_AGE,
            "Рост": ONBOARDING_STEP_HEIGHT,
            "Вес": ONBOARDING_STEP_WEIGHT,
            "Цель": ONBOARDING_STEP_GOAL,
            "Активность": ONBOARDING_STEP_ACTIVITY,
        }
        if missing:
            return mapping[missing[0]]
        return ONBOARDING_STEP_MANUAL_TARGET

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
        current_profile = self.get_user_profile(int(user_id))
        step = state.step
        normalized_text = " ".join((text or "").strip().split())

        if step == ONBOARDING_STEP_SEX:
            sex_value = normalize_sex(normalized_text)
            if sex_value is None:
                return HealBiteOnboardingReply(status="invalid", text=format_healbite_onboarding_invalid_reply(step, current_profile), next_step=step)
            self.upsert_user_profile(user_id=int(user_id), username=(username or state.username or "").strip(), sex=sex_value)
            return self._advance_onboarding(int(user_id), username=(username or state.username or "").strip())

        if step == ONBOARDING_STEP_AGE:
            age_value = _to_int(normalized_text)
            if age_value is None or not (18 <= age_value <= 100):
                return HealBiteOnboardingReply(status="invalid", text=format_healbite_onboarding_invalid_reply(step, current_profile), next_step=step)
            self.upsert_user_profile(user_id=int(user_id), username=(username or state.username or "").strip(), age=age_value)
            return self._advance_onboarding(int(user_id), username=(username or state.username or "").strip())

        if step == ONBOARDING_STEP_HEIGHT:
            height_value = _to_float(normalized_text)
            if height_value is None or not (120 <= height_value <= 230):
                return HealBiteOnboardingReply(status="invalid", text=format_healbite_onboarding_invalid_reply(step, current_profile), next_step=step)
            self.upsert_user_profile(user_id=int(user_id), username=(username or state.username or "").strip(), height_cm=height_value)
            return self._advance_onboarding(int(user_id), username=(username or state.username or "").strip())

        if step == ONBOARDING_STEP_WEIGHT:
            weight_value = _to_float(normalized_text)
            if weight_value is None or not (35 <= weight_value <= 300):
                return HealBiteOnboardingReply(status="invalid", text=format_healbite_onboarding_invalid_reply(step, current_profile), next_step=step)
            self.upsert_user_profile(user_id=int(user_id), username=(username or state.username or "").strip(), weight_kg=weight_value)
            return self._advance_onboarding(int(user_id), username=(username or state.username or "").strip())

        if step == ONBOARDING_STEP_GOAL:
            goal_value = normalize_goal(normalized_text)
            if goal_value is None:
                return HealBiteOnboardingReply(status="invalid", text=format_healbite_onboarding_invalid_reply(step, current_profile), next_step=step)
            self.upsert_user_profile(user_id=int(user_id), username=(username or state.username or "").strip(), goal=goal_value)
            return self._advance_onboarding(int(user_id), username=(username or state.username or "").strip())

        if step == ONBOARDING_STEP_ACTIVITY:
            activity_value = normalize_activity_level(normalized_text)
            if activity_value is None:
                return HealBiteOnboardingReply(status="invalid", text=format_healbite_onboarding_invalid_reply(step, current_profile), next_step=step)
            self.upsert_user_profile(user_id=int(user_id), username=(username or state.username or "").strip(), activity_level=activity_value)
            return self._advance_onboarding(int(user_id), username=(username or state.username or "").strip())

        if step == ONBOARDING_STEP_MANUAL_TARGET:
            return self._finish_onboarding_with_target_choice(
                user_id=int(user_id),
                username=(username or state.username or "").strip(),
                text=normalized_text,
            )

        logger.warning("Unknown HealBite onboarding step %s", step)
        self.clear_onboarding_state(int(user_id))
        return HealBiteOnboardingReply(
            status="invalid",
            text="Не удалось продолжить настройку профиля. Нажмите /start ещё раз.",
            next_step=None,
        )

    def _advance_onboarding(self, user_id: int, *, username: str = "") -> HealBiteOnboardingReply:
        profile = self.get_user_profile(int(user_id))
        next_step = self._next_onboarding_step(profile)
        self._set_onboarding_state(user_id=int(user_id), username=username, step=next_step)
        return HealBiteOnboardingReply(
            status="next",
            text=format_healbite_onboarding_prompt(next_step, profile),
            profile=profile,
            next_step=next_step,
        )

    def _finish_onboarding_with_target_choice(
        self,
        *,
        user_id: int,
        username: str,
        text: str,
    ) -> HealBiteOnboardingReply:
        profile = self.get_user_profile(int(user_id))
        if profile is None:
            return HealBiteOnboardingReply(status="invalid", text="Не удалось загрузить профиль. Нажмите /start ещё раз.")

        existing_manual = profile.manual_kcal_target
        lowered = text.casefold()
        manual_value: float | None = existing_manual
        target_source = "manual" if existing_manual is not None else "calculated"

        if text in {"Пропустить", "пропустить"} and existing_manual is None:
            target_source = "calculated"
            manual_value = None
        elif text in {"Оставить как есть", "оставить как есть"} and existing_manual is not None:
            target_source = "manual"
            manual_value = existing_manual
        elif text in {"Рассчитать автоматически", "рассчитать автоматически"} and existing_manual is not None:
            target_source = "calculated"
        else:
            parsed = _to_float(text)
            if parsed is None or parsed <= 0:
                return HealBiteOnboardingReply(
                    status="invalid",
                    text=format_healbite_onboarding_invalid_reply(ONBOARDING_STEP_MANUAL_TARGET, profile),
                    profile=profile,
                    next_step=ONBOARDING_STEP_MANUAL_TARGET,
                )
            manual_value = parsed
            target_source = "manual"
            self.upsert_user_profile(user_id=int(user_id), username=username, manual_kcal_target=manual_value)

        try:
            recalculated = self.recalculate_profile_targets(
                user_id=int(user_id),
                username=username,
                target_source=target_source,
                manual_kcal_target=manual_value if target_source == "manual" else existing_manual,
            )
        except NutritionTargetValidationError:
            logger.info(
                "[HealBite][nutrition_profile_recalculated] calculation_version=%s calculation_result=validation_error goal=%s activity_level=%s target_source=%s",
                NUTRITION_CALCULATION_VERSION,
                profile.goal or "unknown",
                profile.activity_level or "unknown",
                target_source,
            )
            return HealBiteOnboardingReply(
                status="invalid",
                text="Не удалось рассчитать КБЖУ. Проверьте данные профиля и попробуйте снова через /start edit.",
                profile=profile,
            )

        self.clear_onboarding_state(int(user_id))
        return HealBiteOnboardingReply(
            status="completed",
            text=format_healbite_onboarding_completed_reply(recalculated),
            profile=recalculated,
        )

    def recalculate_profile_targets(
        self,
        *,
        user_id: int,
        username: str = "",
        target_source: str | None = None,
        manual_kcal_target: float | None = None,
    ) -> HealBiteUserProfile:
        profile = self.get_user_profile(int(user_id))
        if profile is None:
            raise NutritionTargetValidationError("profile")

        inputs = NutritionProfileInputs(
            sex=normalize_sex(profile.sex) or "",
            age=int(profile.age or 0),
            height_cm=float(profile.height_cm or 0.0),
            weight_kg=float(profile.weight_kg or 0.0),
            activity_level=normalize_activity_level(profile.activity_level) or "",
            goal=normalize_goal(profile.goal) or "",
        )
        calculation = calculate_nutrition_targets(
            inputs,
            manual_kcal_target=manual_kcal_target if target_source == "manual" else None,
            target_source=target_source,
        )
        self.upsert_user_profile(
            user_id=int(user_id),
            username=username or profile.username,
            daily_kcal_target=calculation.daily_kcal_target,
            daily_protein_target=calculation.daily_protein_g,
            daily_fat_target=calculation.daily_fat_g,
            daily_carbs_target=calculation.daily_carbs_g,
            daily_protein_g=calculation.daily_protein_g,
            daily_fat_g=calculation.daily_fat_g,
            daily_carbs_g=calculation.daily_carbs_g,
            calculated_bmr_kcal=calculation.bmr_kcal,
            calculated_tdee_kcal=calculation.tdee_kcal,
            nutrition_calculation_version=calculation.calculation_version,
            nutrition_calculated_at=_sqlite_timestamp(),
            target_source=calculation.target_source,
            manual_kcal_target=manual_kcal_target if calculation.target_source == "manual" else _UNSET,
        )
        logger.info(
            "[HealBite][nutrition_profile_recalculated] calculation_version=%s calculation_result=ok goal=%s activity_level=%s target_source=%s",
            calculation.calculation_version,
            inputs.goal,
            inputs.activity_level,
            calculation.target_source,
        )
        refreshed = self.get_user_profile(int(user_id))
        if refreshed is None:
            raise RuntimeError("Could not load recalculated HealBite profile.")
        return refreshed

    def delete_user_profile(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {USERS_TABLE} WHERE {self._users_identity_column(conn)} = ?", (int(user_id),))
            conn.execute(f"DELETE FROM {PROFILES_TABLE} WHERE {self._profiles_identity_column(conn)} = ?", (int(user_id),))
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
