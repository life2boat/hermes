from __future__ import annotations

import atexit
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.memory.embedding_adapter import EmbeddingAdapter
from gateway.healbite_time import local_day_window_utc
from gateway.memory.qdrant_adapter import QdrantMemoryAdapter
from utils import safe_json_loads

logger = logging.getLogger(__name__)

NUTRITION_LOG_TABLE = "nutrition_log"
PENDING_MEALS_TABLE = "pending_meals"
_PROFILES_TABLE = "profiles"
_USERS_TABLE = "users"
_STRUCTURED_FACTS_TABLE = "structured_user_facts"
_MEMORY_FACTS_TABLE = "memory_os_facts"
PENDING_MEAL_TTL = timedelta(hours=2)

_DEFAULT_DB_PATH = Path("/home/hermes/healbite.db")
_GLOBAL_DIARY_LOCK = threading.Lock()
_GLOBAL_DIARY: HealBiteNutritionDiary | None = None

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {NUTRITION_LOG_TABLE} (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    meal_name TEXT NOT NULL,
    items_json TEXT NOT NULL,
    calories_kcal REAL,
    protein_g REAL,
    fat_g REAL,
    carbs_g REAL,
    confidence REAL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    raw_summary TEXT NOT NULL,
    image_ref TEXT,
    qdrant_indexed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_{NUTRITION_LOG_TABLE}_user_occurred_at
    ON {NUTRITION_LOG_TABLE}(user_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_{NUTRITION_LOG_TABLE}_user_image_ref
    ON {NUTRITION_LOG_TABLE}(user_id, image_ref);
CREATE TABLE IF NOT EXISTS {PENDING_MEALS_TABLE} (
    user_id INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{PENDING_MEALS_TABLE}_expires_at
    ON {PENDING_MEALS_TABLE}(expires_at);
"""

FOOD_VISION_SCHEMA_VERSION = "food_vision_inventory_v1"
MIN_OVERALL_CONFIDENCE = 0.80
MIN_ITEM_CONFIDENCE = 0.75
PENDING_MEAL_KIND = "meal_save_confirmation"
PENDING_INVENTORY_KIND = "vision_inventory_confirmation"
PENDING_INVENTORY_STATE = "awaiting_inventory_confirmation"
READY_FOR_NUTRITION_CALCULATION = "READY_FOR_NUTRITION_CALCULATION"
NEEDS_WEIGHT_CONFIRMATION = "NEEDS_WEIGHT_CONFIRMATION"
INVALID_INVENTORY_STATE = "INVALID_INVENTORY_STATE"
UNKNOWN_COMPONENT_BLOCKED = "UNKNOWN_COMPONENT_BLOCKED"
_MAX_FOOD_VISION_ITEMS = 12
_MAX_FOOD_VISION_WARNINGS = 6
_MAX_FOOD_VISION_NAME_LENGTH = 120
_MAX_FOOD_VISION_PREPARATION_LENGTH = 80
_MAX_FOOD_VISION_UNCERTAINTY_LENGTH = 160
_MAX_FOOD_VISION_WARNING_LENGTH = 160
_MAX_FOOD_VISION_GRAMS = 2000.0
_MAX_PENDING_INVENTORY_NAME_LENGTH = 80
_MAX_PENDING_INVENTORY_ITEMS = 16
_AUTO_PROPOSE_WEIGHT_SPAN_G = 25.0
_LOCAL_CONFIRMATION_MAX_WEIGHT_SPAN_G = _AUTO_PROPOSE_WEIGHT_SPAN_G
_MIN_SELECTED_GRAMS = 1.0
_MAX_SELECTED_GRAMS = 2000.0
_ALLOWED_UNKNOWN_WEIGHT_VALUES = {"", "?", "unknown", "неизвестно", "n/a", "na"}
_AMBIGUOUS_PREPARATION_TOKENS = (
    "неяс",
    "неизвест",
    "возможно",
    "или",
    "примерно",
    "похоже",
    "unknown",
    "uncertain",
    "ambiguous",
)
_GENERIC_VISIBLE_FOOD_NAMES = frozenset({
    "выпечка",
    "сладкая выпечка",
    "мясо",
    "рыба",
    "соус",
    "желтый соус",
    "белый соус",
    "выпечка с начинкой",
    "пирожок",
    "булочка",
    "нарезка",
    "закуска",
})
_GENERIC_VISIBLE_NAME_TOKENS = frozenset({
    "выпечка",
    "соус",
    "мясо",
    "рыба",
    "нарезка",
    "закуска",
    "или",
    "возможно",
})
_NAME_SPECIFICITY_STOPWORDS = frozenset({
    "и",
    "с",
    "со",
    "или",
    "the",
    "a",
    "an",
    "of",
})
_FOOD_VISION_ALLOWED_TOP_LEVEL_FIELDS = frozenset({
    "schema_version",
    "items",
    "overall_confidence",
    "needs_user_confirmation",
    "warnings",
})
_FOOD_VISION_ALLOWED_ITEM_FIELDS = frozenset({
    "visible_name",
    "normalized_name",
    "confidence",
    "estimated_grams_min",
    "estimated_grams_max",
    "preparation",
    "is_sauce",
    "uncertainty",
})
_FOOD_VISION_REJECTED_FIELDS = frozenset({
    "meal_name",
    "display_name",
    "totals",
    "calories_kcal",
    "protein_g",
    "fat_g",
    "carbs_g",
    "foods",
    "nutrition",
    "raw_summary",
    "summary",
    "description",
})
_FOOD_VISION_REJECTED_ITEM_FIELDS = frozenset({
    "name",
    "estimated_weight_g",
    "weight_g",
    "calories_kcal",
    "protein_g",
    "fat_g",
    "carbs_g",
    "calories",
    "protein",
    "fat",
    "carbs",
})
_COMBINED_DISH_TITLE_PREFIXES = (
    "\u0437\u0430\u0432\u0442\u0440\u0430\u043a",
    "\u043e\u0431\u0435\u0434",
    "\u0443\u0436\u0438\u043d",
    "meal",
    "plate",
    "dish",
    "\u0442\u0430\u0440\u0435\u043b\u043a\u0430",
    "\u0431\u043b\u044e\u0434\u043e",
    "\u0430\u0441\u0441\u043e\u0440\u0442\u0438",
)
_VISION_PROMPT = (
    "You are HealBite Stage-1 food vision. "
    "Return strict JSON only for schema food_vision_inventory_v1, with no markdown and no extra text. "
    "Describe only separately visible food components in Russian. "
    "Use one item per visible component and keep visible sauces or condiments as separate items. "
    "Do not collapse a mixed plate into one dish title. "
    "Use a generic visual label when exact identity is uncertain, and put that uncertainty into the uncertainty field. "
    "Do not invent hidden ingredients, fillings, or unsupported preparation details. "
    "Give confidence per item and approximate gram ranges only when visually supportable; otherwise use null. "
    "Never return calories, macros, or totals. "
    "JSON fields: schema_version, items[{visible_name, normalized_name, confidence, estimated_grams_min, estimated_grams_max, preparation, is_sauce, uncertainty}], overall_confidence, needs_user_confirmation, warnings."
)


@dataclass(slots=True)
class NutritionRecord:
    is_food: bool
    meal_name: str
    items: list[dict[str, Any]]
    calories_kcal: float | None
    protein_g: float | None
    fat_g: float | None
    carbs_g: float | None
    confidence: float
    raw_summary: str
    display_name: str = ""


@dataclass(slots=True)
class NutritionDiaryOutcome:
    available: bool
    record: NutritionRecord | None = None
    pending: bool = False
    saved: bool = False
    duplicate: bool = False
    sqlite_id: int | None = None
    raw_analysis: str = ""
    clarification_text: str = ""
    validation_status: str = ""


@dataclass(slots=True)
class FoodVisionItem:
    visible_name: str
    normalized_name: str
    confidence: float
    estimated_grams_min: float | None
    estimated_grams_max: float | None
    preparation: str
    is_sauce: bool
    uncertainty: str


@dataclass(slots=True)
class FoodVisionInventory:
    schema_version: str
    items: list[FoodVisionItem]
    overall_confidence: float
    needs_user_confirmation: bool
    warnings: list[str]


@dataclass(slots=True)
class FoodVisionValidationResult:
    status: str
    inventory: FoodVisionInventory | None = None
    reason: str = ""


@dataclass(slots=True)
class FoodVisionConfirmationDecision:
    required: bool
    provider_requested: bool
    local_required: bool
    reasons: tuple[str, ...]


@dataclass(slots=True)
class PendingMealPayload:
    user_id: int
    source: str
    record: NutritionRecord
    image_ref: str | None = None
    occurred_at: datetime | None = None
    created_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(slots=True)
class ConfirmPendingMealResult:
    status: str
    record: NutritionRecord | None = None
    sqlite_id: int | None = None
    duplicate: bool = False


@dataclass(slots=True)
class PendingFoodVisionItem:
    index: int
    visible_name: str
    normalized_name: str
    confidence: float
    estimated_grams_min: float | None
    estimated_grams_max: float | None
    selected_grams: float | None
    preparation: str
    is_sauce: bool
    uncertainty: str
    user_modified: bool = False


@dataclass(slots=True)
class PendingFoodVisionInventory:
    user_id: int
    inventory_id: str
    schema_version: str
    items: list[PendingFoodVisionItem]
    overall_confidence: float
    warnings: list[str]
    created_at: datetime | None = None
    expires_at: datetime | None = None
    source: str = "vision"
    state: str = PENDING_INVENTORY_STATE
    image_ref: str | None = None
    occurred_at: datetime | None = None
    kind: str = PENDING_INVENTORY_KIND
    needs_user_confirmation: bool = False


@dataclass(slots=True)
class PendingInventoryAction:
    kind: str
    index: int | None = None
    name: str = ""
    grams: float | None = None


@dataclass(slots=True)
class PendingInventoryActionParseResult:
    ok: bool
    action: PendingInventoryAction | None = None
    error: str = ""


@dataclass(slots=True)
class NutritionComponentEstimate:
    name: str
    grams: float
    calories_kcal: float
    protein_g: float
    fat_g: float
    carbs_g: float


@dataclass(slots=True)
class InventoryNutritionCalculationResult:
    status: str
    components: list[NutritionComponentEstimate]
    missing_indexes: list[int]
    blocked_indexes: list[int]
    reason: str = ""
    record: NutritionRecord | None = None


@dataclass(slots=True)
class PendingInventoryReplyResult:
    status: str
    reply_text: str
    inventory: PendingFoodVisionInventory | None = None
    record: NutritionRecord | None = None
    missing_indexes: list[int] | None = None
    blocked_indexes: list[int] | None = None


@dataclass(slots=True)
class UndoMealResult:
    deleted: bool
    sqlite_id: int | None = None
    meal_name: str = ""
    calories_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    occurred_at: str = ""


@dataclass(slots=True)
class UpdateMealResult:
    updated: bool
    sqlite_id: int | None = None
    meal_name: str = ""
    calories_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    occurred_at: str = ""


@dataclass(slots=True)
class NutritionTargets:
    calories_kcal: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None

    def has_any(self) -> bool:
        return any(
            value is not None and float(value) > 0
            for value in (self.calories_kcal, self.protein_g, self.fat_g, self.carbs_g)
        )


_TARGET_FACT_ALIASES = {
    "calories_kcal": (
        "daily_calories",
        "target_calories",
        "target_calories_kcal",
        "calories_target",
        "calories_limit",
    ),
    "protein_g": (
        "daily_protein_g",
        "target_protein_g",
        "protein_target_g",
        "protein_g_target",
        "protein_target",
    ),
    "fat_g": (
        "daily_fat_g",
        "target_fat_g",
        "fat_target_g",
        "fat_g_target",
        "fat_target",
    ),
    "carbs_g": (
        "daily_carbs_g",
        "target_carbs_g",
        "carbs_target_g",
        "carbs_g_target",
        "carbs_target",
    ),
}

_TARGET_JSON_FIELDS = {
    "calories_kcal": ("calories_kcal", "calories", "daily_calories", "target_calories", "calories_limit"),
    "protein_g": ("protein_g", "protein", "daily_protein_g", "target_protein_g"),
    "fat_g": ("fat_g", "fat", "daily_fat_g", "target_fat_g"),
    "carbs_g": ("carbs_g", "carbs", "daily_carbs_g", "target_carbs_g"),
}

_TARGET_BLOB_KEYS = ("nutrition_targets", "nutrition_target", "macro_targets", "targets")


def resolve_healbite_db_path(db_path: str | Path | None = None) -> Path:
    if db_path is not None:
        return Path(db_path)
    env_value = os.getenv("HEALBITE_DB_PATH", "").strip()
    if env_value:
        return Path(env_value)
    return _DEFAULT_DB_PATH


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        token = []
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
        if token:
            try:
                return float("".join(token))
            except ValueError:
                return None
    return None


def _coerce_confidence(value: Any) -> float:
    numeric = _to_float(value)
    if numeric is None:
        return 0.0
    return max(0.0, min(float(numeric), 1.0))


def _normalize_timestamp(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sqlite_timestamp(value: datetime | None = None) -> str:
    return _normalize_timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _parse_sqlite_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _local_day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    return local_day_window_utc(now)


def _rolling_window(days: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    end_utc = _normalize_timestamp(now)
    start_utc = end_utc - timedelta(days=days)
    return start_utc, end_utc


def _format_kcal(value: Any) -> str:
    numeric = float(value or 0.0)
    return f"{numeric:.0f} \u043a\u043a\u0430\u043b"


def _format_grams(value: Any) -> str:
    numeric = float(value or 0.0)
    if abs(numeric - round(numeric)) < 0.05:
        return f"{int(round(numeric))} \u0433"
    return f"{numeric:.1f} \u0433"


def _format_percent(value: float) -> str:
    return f"{int(round(value))}%"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        if len(row) > 1
    }


def _users_identity_column(conn: sqlite3.Connection) -> str | None:
    columns = _table_columns(conn, _USERS_TABLE)
    if "user_id" in columns:
        return "user_id"
    if "telegram_id" in columns:
        return "telegram_id"
    return None


def _set_target_if_missing(targets: NutritionTargets, metric: str, raw_value: Any) -> None:
    current_value = getattr(targets, metric)
    if current_value is not None:
        return
    numeric = _to_float(raw_value)
    if numeric is None or numeric <= 0:
        return
    setattr(targets, metric, numeric)


def _extract_target_from_value(metric: str, fact_key: str, fact_value: str) -> float | None:
    if fact_key in _TARGET_FACT_ALIASES.get(metric, ()):
        return _to_float(fact_value)
    parsed = safe_json_loads(fact_value, None)
    if isinstance(parsed, dict):
        for field_name in _TARGET_JSON_FIELDS.get(metric, ()):
            numeric = _to_float(parsed.get(field_name))
            if numeric is not None and numeric > 0:
                return numeric
    return None


def _load_nutrition_targets(conn: sqlite3.Connection, *, user_id: int) -> NutritionTargets:
    targets = NutritionTargets()

    if _table_exists(conn, _USERS_TABLE):
        columns = _table_columns(conn, _USERS_TABLE)
        identity_column = _users_identity_column(conn)
        select_parts = []
        if "daily_kcal_target" in columns:
            select_parts.append("daily_kcal_target")
        if "daily_protein_g" in columns:
            select_parts.append("daily_protein_g")
        if "daily_fat_g" in columns:
            select_parts.append("daily_fat_g")
        if "daily_carbs_g" in columns:
            select_parts.append("daily_carbs_g")
        if "daily_protein_target" in columns:
            select_parts.append("daily_protein_target")
        if "daily_fat_target" in columns:
            select_parts.append("daily_fat_target")
        if "daily_carbs_target" in columns:
            select_parts.append("daily_carbs_target")
        if select_parts and identity_column:
            row = conn.execute(
                f"SELECT {', '.join(select_parts)} FROM {_USERS_TABLE} WHERE {identity_column} = ?",
                (int(user_id),),
            ).fetchone()
            if row is not None:
                if "daily_kcal_target" in select_parts:
                    _set_target_if_missing(targets, "calories_kcal", row["daily_kcal_target"])
                if "daily_protein_g" in select_parts:
                    _set_target_if_missing(targets, "protein_g", row["daily_protein_g"])
                if "daily_fat_g" in select_parts:
                    _set_target_if_missing(targets, "fat_g", row["daily_fat_g"])
                if "daily_carbs_g" in select_parts:
                    _set_target_if_missing(targets, "carbs_g", row["daily_carbs_g"])
                if "daily_protein_target" in select_parts:
                    _set_target_if_missing(targets, "protein_g", row["daily_protein_target"])
                if "daily_fat_target" in select_parts:
                    _set_target_if_missing(targets, "fat_g", row["daily_fat_target"])
                if "daily_carbs_target" in select_parts:
                    _set_target_if_missing(targets, "carbs_g", row["daily_carbs_target"])

    if _table_exists(conn, _PROFILES_TABLE):
        columns = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({_PROFILES_TABLE})").fetchall()
            if len(row) > 1
        }
        select_parts = []
        if "calories_limit" in columns:
            select_parts.append("calories_limit")
        if "target_calories" in columns:
            select_parts.append("target_calories")
        if "daily_calories" in columns:
            select_parts.append("daily_calories")
        if select_parts:
            row = conn.execute(
                f"SELECT {', '.join(select_parts)} FROM {_PROFILES_TABLE} WHERE telegram_id = ?",
                (int(user_id),),
            ).fetchone()
            if row is not None:
                for column_name in select_parts:
                    _set_target_if_missing(targets, "calories_kcal", row[column_name])

    fact_keys = sorted({key for keys in _TARGET_FACT_ALIASES.values() for key in keys}.union(_TARGET_BLOB_KEYS))
    if _table_exists(conn, _STRUCTURED_FACTS_TABLE):
        placeholders = ", ".join("?" for _ in fact_keys)
        rows = conn.execute(
            f"""
            SELECT fact_key, fact_value
            FROM {_STRUCTURED_FACTS_TABLE}
            WHERE user_id = ? AND fact_key IN ({placeholders})
            ORDER BY trust_score DESC, updated_at DESC
            """,
            (int(user_id), *fact_keys),
        ).fetchall()
        for row in rows:
            fact_key = str(row["fact_key"])
            fact_value = str(row["fact_value"])
            for metric_name in ("calories_kcal", "protein_g", "fat_g", "carbs_g"):
                _set_target_if_missing(
                    targets,
                    metric_name,
                    _extract_target_from_value(metric_name, fact_key, fact_value),
                )

    if _table_exists(conn, _MEMORY_FACTS_TABLE):
        placeholders = ", ".join("?" for _ in fact_keys)
        rows = conn.execute(
            f"""
            SELECT "key", value
            FROM {_MEMORY_FACTS_TABLE}
            WHERE user_id = ? AND "key" IN ({placeholders})
            ORDER BY trust_score DESC, updated_at DESC
            """,
            (int(user_id), *fact_keys),
        ).fetchall()
        for row in rows:
            fact_key = str(row["key"])
            fact_value = str(row["value"])
            for metric_name in ("calories_kcal", "protein_g", "fat_g", "carbs_g"):
                _set_target_if_missing(
                    targets,
                    metric_name,
                    _extract_target_from_value(metric_name, fact_key, fact_value),
                )

    return targets


def _build_target_progress(
    current_values: dict[str, float],
    targets: NutritionTargets,
) -> dict[str, dict[str, float]]:
    progress: dict[str, dict[str, float]] = {}
    for metric_name in ("calories_kcal", "protein_g", "fat_g", "carbs_g"):
        target_value = getattr(targets, metric_name)
        if target_value is None or float(target_value) <= 0:
            continue
        current_value = float(current_values.get(metric_name, 0.0))
        progress[metric_name] = {
            "current": current_value,
            "target": float(target_value),
            "percent": (current_value / float(target_value)) * 100.0 if float(target_value) > 0 else 0.0,
        }
    return progress


def _build_progress_hints(progress: dict[str, dict[str, float]]) -> list[str]:
    hints: list[str] = []
    calories_progress = progress.get("calories_kcal")
    protein_progress = progress.get("protein_g")
    if calories_progress is not None and calories_progress["percent"] < 70.0:
        hints.append("\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u043f\u043e\u043a\u0430 \u043c\u0430\u043b\u043e \u043a\u0430\u043b\u043e\u0440\u0438\u0439 \u2014 \u043c\u043e\u0436\u043d\u043e \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043f\u043e\u043b\u043d\u043e\u0446\u0435\u043d\u043d\u044b\u0439 \u043f\u0440\u0438\u0451\u043c \u043f\u0438\u0449\u0438.")
    if protein_progress is not None and protein_progress["percent"] < 70.0:
        hints.append("\u0411\u0435\u043b\u043a\u0430 \u043f\u043e\u043a\u0430 \u043c\u0430\u043b\u043e\u0432\u0430\u0442\u043e \u2014 \u043c\u043e\u0436\u043d\u043e \u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0440\u044b\u0431\u0443, \u044f\u0439\u0446\u0430, \u0442\u0432\u043e\u0440\u043e\u0433, \u043c\u044f\u0441\u043e \u0438\u043b\u0438 \u0431\u043e\u0431\u043e\u0432\u044b\u0435.")
    if any(item["percent"] > 120.0 for item in progress.values()):
        hints.append("\u0426\u0435\u043b\u044c \u0443\u0436\u0435 \u043f\u0440\u0435\u0432\u044b\u0448\u0435\u043d\u0430, \u0434\u0430\u043b\u044c\u0448\u0435 \u043b\u0443\u0447\u0448\u0435 \u0432\u044b\u0431\u0438\u0440\u0430\u0442\u044c \u043b\u0451\u0433\u043a\u0438\u0435 \u0431\u043b\u044e\u0434\u0430.")
    return hints


def _format_progress_line(
    *,
    emoji: str,
    label: str,
    metric_name: str,
    current_value: float,
    progress: dict[str, dict[str, float]],
) -> str:
    formatter = _format_kcal if metric_name == "calories_kcal" else _format_grams
    metric_progress = progress.get(metric_name)
    if metric_progress is None:
        return f"{emoji} {label}: {formatter(current_value)}"
    return (
        f"{emoji} {label}: {formatter(metric_progress['current'])} / "
        f"{formatter(metric_progress['target'])} ({_format_percent(metric_progress['percent'])})"
    )


def _normalize_meal_name_text(value: Any, fallback: str = "Блюдо") -> str:
    text = " ".join(str(value or "").split())
    return text or fallback


_LEGACY_MEAL_NAME_ALIASES = {
    "borscht with sour cream and dill": "\u0411\u043e\u0440\u0449 \u0441\u043e \u0441\u043c\u0435\u0442\u0430\u043d\u043e\u0439 \u0438 \u0443\u043a\u0440\u043e\u043f\u043e\u043c",
    "buckwheat with tomatoes and herbs": "\u0413\u0440\u0435\u0447\u043a\u0430 \u0441 \u043f\u043e\u043c\u0438\u0434\u043e\u0440\u0430\u043c\u0438 \u0438 \u0437\u0435\u043b\u0435\u043d\u044c\u044e",
    "buckwheat kasha with tomatoes and herbs": "\u0413\u0440\u0435\u0447\u043d\u0435\u0432\u0430\u044f \u043a\u0430\u0448\u0430 \u0441 \u043f\u043e\u043c\u0438\u0434\u043e\u0440\u0430\u043c\u0438 \u0438 \u0437\u0435\u043b\u0435\u043d\u044c\u044e",
    "dried fish and meat jerky platter": "\u0412\u044f\u043b\u0435\u043d\u0430\u044f \u0440\u044b\u0431\u0430 \u0438 \u043c\u044f\u0441\u043d\u044b\u0435 \u0441\u043d\u0435\u043a\u0438",
    "vegetable crudites platter with crackers and olives": "\u041e\u0432\u043e\u0449\u043d\u0430\u044f \u0442\u0430\u0440\u0435\u043b\u043a\u0430 \u0441 \u043a\u0440\u0435\u043a\u0435\u0440\u0430\u043c\u0438 \u0438 \u043e\u043b\u0438\u0432\u043a\u0430\u043c\u0438",
    "assorted dried meat and fish platter": "\u0410\u0441\u0441\u043e\u0440\u0442\u0438 \u0438\u0437 \u0432\u044f\u043b\u0435\u043d\u043e\u0433\u043e \u043c\u044f\u0441\u0430 \u0438 \u0440\u044b\u0431\u044b",
    "traditional yeast (100g)": "\u0422\u0440\u0430\u0434\u0438\u0446\u0438\u043e\u043d\u043d\u044b\u0435 \u0434\u0440\u043e\u0436\u0436\u0438 (100 \u0433)",
    "asian beef salad": "\u0410\u0437\u0438\u0430\u0442\u0441\u043a\u0438\u0439 \u0441\u0430\u043b\u0430\u0442 \u0441 \u0433\u043e\u0432\u044f\u0434\u0438\u043d\u043e\u0439",
}


def _legacy_meal_name_alias(value: Any) -> str | None:
    normalized = _normalize_meal_name_text(value, "")
    if not normalized:
        return None
    normalized_key = "".join(
        char
        for char in unicodedata.normalize("NFKD", normalized.casefold())
        if not unicodedata.combining(char)
    )
    return _LEGACY_MEAL_NAME_ALIASES.get(normalized_key)


def localized_meal_display_name(record_or_name: Any, fallback: str = "\u0411\u043b\u044e\u0434\u043e") -> str:
    explicit_candidates: list[Any] = []
    meal_name_candidate: Any = None

    if isinstance(record_or_name, NutritionRecord):
        explicit_candidates = [
            record_or_name.display_name,
            getattr(record_or_name, "meal_name_user", ""),
            getattr(record_or_name, "meal_name_ru", ""),
        ]
        meal_name_candidate = record_or_name.meal_name
    elif isinstance(record_or_name, dict):
        explicit_candidates = [
            record_or_name.get("display_name"),
            record_or_name.get("meal_name_user"),
            record_or_name.get("meal_name_ru"),
        ]
        meal_name_candidate = record_or_name.get("meal_name")
    elif isinstance(record_or_name, str):
        meal_name_candidate = record_or_name
    else:
        explicit_candidates = [
            getattr(record_or_name, "display_name", ""),
            getattr(record_or_name, "meal_name_user", ""),
            getattr(record_or_name, "meal_name_ru", ""),
        ]
        meal_name_candidate = getattr(record_or_name, "meal_name", record_or_name)


    for candidate in explicit_candidates:
        normalized = _normalize_meal_name_text(candidate, "")
        if normalized:
            return normalized

    alias = _legacy_meal_name_alias(meal_name_candidate)
    if alias:
        return alias
    return _normalize_meal_name_text(meal_name_candidate, fallback)


def _record_display_name(record: NutritionRecord) -> str:
    return localized_meal_display_name(record, "\u0411\u043b\u044e\u0434\u043e")

def _confidence_bucket(confidence: float | None) -> str:
    if confidence is None:
        return "unknown"
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def _bounded_text(value: Any, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > max_length:
        return None
    return normalized


def _parse_probability(value: Any) -> float | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    numeric = float(numeric)
    if numeric < 0.0 or numeric > 1.0:
        return None
    return numeric


def _parse_optional_grams(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().casefold() in _ALLOWED_UNKNOWN_WEIGHT_VALUES:
        return None
    return _to_float(value)


def _looks_like_combined_dish_title(value: str) -> bool:
    normalized = _normalize_meal_name_text(value, "").casefold()
    if not normalized:
        return False
    return any(
        normalized == prefix or normalized.startswith(f"{prefix} ")
        for prefix in _COMBINED_DISH_TITLE_PREFIXES
    )


def _validate_food_vision_item(raw_item: Any) -> tuple[FoodVisionItem | None, str]:
    if not isinstance(raw_item, dict):
        return None, "item_not_object"
    item_keys = set(raw_item)
    if item_keys & _FOOD_VISION_REJECTED_ITEM_FIELDS:
        return None, "aggregate_item_field_present"
    unknown_keys = item_keys - _FOOD_VISION_ALLOWED_ITEM_FIELDS
    if unknown_keys:
        return None, "unknown_item_fields"

    visible_name = _bounded_text(raw_item.get("visible_name"), max_length=_MAX_FOOD_VISION_NAME_LENGTH)
    normalized_name = _bounded_text(raw_item.get("normalized_name"), max_length=_MAX_FOOD_VISION_NAME_LENGTH)
    preparation = _bounded_text(raw_item.get("preparation", ""), max_length=_MAX_FOOD_VISION_PREPARATION_LENGTH) or ""
    uncertainty = _bounded_text(raw_item.get("uncertainty", ""), max_length=_MAX_FOOD_VISION_UNCERTAINTY_LENGTH) or ""
    confidence = _parse_probability(raw_item.get("confidence"))
    grams_min = _parse_optional_grams(raw_item.get("estimated_grams_min"))
    grams_max = _parse_optional_grams(raw_item.get("estimated_grams_max"))
    is_sauce = raw_item.get("is_sauce")

    if not visible_name or not normalized_name:
        return None, "empty_item_name"
    if confidence is None:
        return None, "invalid_item_confidence"
    if not isinstance(is_sauce, bool):
        return None, "invalid_is_sauce"
    if grams_min is not None and grams_min < 0:
        return None, "negative_grams"
    if grams_max is not None and grams_max < 0:
        return None, "negative_grams"
    if grams_min is not None and grams_max is not None and grams_min > grams_max:
        return None, "invalid_gram_range"
    if (
        (grams_min is not None and grams_min > _MAX_FOOD_VISION_GRAMS)
        or (grams_max is not None and grams_max > _MAX_FOOD_VISION_GRAMS)
    ):
        return None, "absurd_portion_range"
    if _looks_like_combined_dish_title(visible_name) or _looks_like_combined_dish_title(normalized_name):
        return None, "combined_dish_title"

    return FoodVisionItem(
        visible_name=visible_name,
        normalized_name=normalized_name,
        confidence=confidence,
        estimated_grams_min=grams_min,
        estimated_grams_max=grams_max,
        preparation=preparation,
        is_sauce=is_sauce,
        uncertainty=uncertainty,
    ), ""


def _food_vision_weight_range_width(item: FoodVisionItem) -> float | None:
    if item.estimated_grams_min is None or item.estimated_grams_max is None:
        return None
    return float(item.estimated_grams_max) - float(item.estimated_grams_min)


def _looks_like_generic_visible_component(name: str) -> bool:
    normalized = _normalize_meal_name_text(name, "").casefold()
    if not normalized:
        return False
    if normalized in _GENERIC_VISIBLE_FOOD_NAMES or " или " in f" {normalized} ":
        return True
    tokens = tuple(token.strip(".,;:()[]{}") for token in normalized.split())
    return any(token in _GENERIC_VISIBLE_NAME_TOKENS for token in tokens)


def _food_vision_preparation_is_ambiguous(preparation: str) -> bool:
    normalized = _normalize_meal_name_text(preparation, "").casefold()
    if not normalized:
        return False
    return any(token in normalized for token in _AMBIGUOUS_PREPARATION_TOKENS)


def _food_vision_normalized_name_exceeds_visible_evidence(item: FoodVisionItem) -> bool:
    visible_name = _normalize_meal_name_text(item.visible_name, "").casefold()
    normalized_name = _normalize_meal_name_text(item.normalized_name, "").casefold()
    if not visible_name or not normalized_name or visible_name == normalized_name:
        return False

    visible_tokens = {
        token.strip(".,;:()[]{}")
        for token in visible_name.split()
        if token.strip(".,;:()[]{}") and token not in _NAME_SPECIFICITY_STOPWORDS
    }
    normalized_tokens = {
        token.strip(".,;:()[]{}")
        for token in normalized_name.split()
        if token.strip(".,;:()[]{}") and token not in _NAME_SPECIFICITY_STOPWORDS
    }
    if not normalized_tokens:
        return False
    specificity_gap = normalized_tokens - visible_tokens
    if not specificity_gap:
        return False
    return (
        _looks_like_generic_visible_component(visible_name)
        or bool(item.uncertainty)
        or item.confidence < 0.9
    )


def derive_inventory_confirmation_requirement(inventory: FoodVisionInventory) -> FoodVisionConfirmationDecision:
    reasons: list[str] = []
    if inventory.needs_user_confirmation:
        reasons.append("provider_requested")

    major_component_count = sum(1 for item in inventory.items if not item.is_sauce)
    if major_component_count > 1:
        reasons.append("multiple_major_components")
    if any(item.is_sauce for item in inventory.items):
        reasons.append("sauce_present")
    if inventory.overall_confidence < MIN_OVERALL_CONFIDENCE:
        reasons.append("low_overall_confidence")
    if inventory.warnings:
        reasons.append("warnings_present")
    if any(item.confidence < MIN_ITEM_CONFIDENCE for item in inventory.items):
        reasons.append("low_item_confidence")
    if any(item.estimated_grams_min is None or item.estimated_grams_max is None for item in inventory.items):
        reasons.append("missing_weight_range")
    if any(
        (width is not None and width > _LOCAL_CONFIRMATION_MAX_WEIGHT_SPAN_G)
        for width in (_food_vision_weight_range_width(item) for item in inventory.items)
    ):
        reasons.append("broad_weight_range")
    if any(bool(item.uncertainty) for item in inventory.items):
        reasons.append("uncertainty_present")
    if any(_food_vision_preparation_is_ambiguous(item.preparation) for item in inventory.items):
        reasons.append("ambiguous_preparation")
    if any(_food_vision_normalized_name_exceeds_visible_evidence(item) for item in inventory.items):
        reasons.append("ambiguous_normalization")

    unique_reasons = tuple(dict.fromkeys(reasons))
    provider_requested = "provider_requested" in unique_reasons
    local_required = any(reason != "provider_requested" for reason in unique_reasons)
    return FoodVisionConfirmationDecision(
        required=provider_requested or local_required,
        provider_requested=provider_requested,
        local_required=local_required,
        reasons=unique_reasons,
    )


def validate_food_vision_inventory(payload_text: str) -> FoodVisionValidationResult:
    payload = safe_json_loads(payload_text, {})
    if not isinstance(payload, dict) or not payload:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="invalid_json")

    payload_keys = set(payload)
    if payload_keys & _FOOD_VISION_REJECTED_FIELDS:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="aggregate_nutrition_present")
    unknown_keys = payload_keys - _FOOD_VISION_ALLOWED_TOP_LEVEL_FIELDS
    if unknown_keys:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="unknown_top_level_fields")
    if payload.get("schema_version") != FOOD_VISION_SCHEMA_VERSION:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="unsupported_schema_version")

    items_payload = payload.get("items")
    if not isinstance(items_payload, list) or not items_payload:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="empty_items")
    if len(items_payload) > _MAX_FOOD_VISION_ITEMS:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="too_many_items")

    overall_confidence = _parse_probability(payload.get("overall_confidence"))
    if overall_confidence is None:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="invalid_overall_confidence")

    needs_user_confirmation = payload.get("needs_user_confirmation")
    if not isinstance(needs_user_confirmation, bool):
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="invalid_needs_user_confirmation")

    warnings_payload = payload.get("warnings")
    if not isinstance(warnings_payload, list):
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="invalid_warnings")
    if len(warnings_payload) > _MAX_FOOD_VISION_WARNINGS:
        return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="too_many_warnings")

    warnings: list[str] = []
    for warning in warnings_payload:
        normalized_warning = _bounded_text(warning, max_length=_MAX_FOOD_VISION_WARNING_LENGTH)
        if normalized_warning is None:
            return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason="invalid_warning_text")
        warnings.append(normalized_warning)

    items: list[FoodVisionItem] = []
    for raw_item in items_payload:
        item, reason = _validate_food_vision_item(raw_item)
        if item is None:
            return FoodVisionValidationResult(status="INVALID_PROVIDER_OUTPUT", reason=reason)
        items.append(item)

    inventory = FoodVisionInventory(
        schema_version=FOOD_VISION_SCHEMA_VERSION,
        items=items,
        overall_confidence=overall_confidence,
        needs_user_confirmation=needs_user_confirmation,
        warnings=warnings,
    )
    confirmation = derive_inventory_confirmation_requirement(inventory)
    normalized_inventory = replace(inventory, needs_user_confirmation=confirmation.required)
    return FoodVisionValidationResult(
        status="NEEDS_CLARIFICATION" if confirmation.required else "VALID",
        inventory=normalized_inventory,
        reason="clarification_required" if confirmation.required else "validated",
    )


def _format_food_vision_grams(item: FoodVisionItem | PendingFoodVisionItem) -> str:
    grams_min = item.estimated_grams_min
    grams_max = item.estimated_grams_max
    if grams_min is None and grams_max is None:
        return "\u0432\u0435\u0441 \u043d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u0435\u043d"
    if grams_min is not None and grams_max is not None:
        if grams_min == grams_max:
            return f"\u043f\u0440\u0438\u043c\u0435\u0440\u043d\u043e {grams_min:.0f} \u0433"
        return f"\u043f\u0440\u0438\u043c\u0435\u0440\u043d\u043e {grams_min:.0f}-{grams_max:.0f} \u0433"
    value = grams_min if grams_min is not None else grams_max
    return f"\u043f\u0440\u0438\u043c\u0435\u0440\u043d\u043e {value:.0f} \u0433"


def _round_selected_grams(value: float) -> float:
    if value >= 100:
        return float(round(value / 5.0) * 5)
    return float(round(value))


def _maybe_proposed_selected_grams(grams_min: float | None, grams_max: float | None) -> float | None:
    if grams_min is None or grams_max is None:
        return None
    if grams_max < grams_min:
        return None
    if (grams_max - grams_min) > _AUTO_PROPOSE_WEIGHT_SPAN_G:
        return None
    midpoint = (float(grams_min) + float(grams_max)) / 2.0
    return max(_MIN_SELECTED_GRAMS, min(_MAX_SELECTED_GRAMS, _round_selected_grams(midpoint)))


def _infer_inventory_sauce_flag(name: str) -> bool:
    normalized = _normalize_meal_name_text(name, "").casefold()
    return any(token in normalized for token in ("\u0441\u043e\u0443\u0441", "\u043c\u0430\u0439\u043e\u043d\u0435\u0437", "\u043a\u0435\u0442\u0447\u0443\u043f", "\u0433\u043e\u0440\u0447\u0438\u0446", "\u0437\u0430\u043f\u0440\u0430\u0432\u043a"))


def _normalize_inventory_item_name(value: Any, *, fallback: str = "") -> str:
    normalized = _normalize_meal_name_text(value, fallback)
    if not normalized:
        return fallback
    return normalized[:_MAX_PENDING_INVENTORY_NAME_LENGTH]


def _pending_inventory_from_food_vision(
    user_id: int,
    inventory: FoodVisionInventory,
    *,
    image_ref: str | None = None,
    occurred_at: datetime | None = None,
    now: datetime | None = None,
    ttl: timedelta | None = None,
    inventory_id: str | None = None,
) -> PendingFoodVisionInventory:
    created_at = _normalize_timestamp(now)
    expires_at = created_at + (ttl if ttl is not None else PENDING_MEAL_TTL)
    items: list[PendingFoodVisionItem] = []
    for index, item in enumerate(inventory.items, start=1):
        items.append(PendingFoodVisionItem(
            index=index,
            visible_name=item.visible_name,
            normalized_name=item.normalized_name,
            confidence=item.confidence,
            estimated_grams_min=item.estimated_grams_min,
            estimated_grams_max=item.estimated_grams_max,
            selected_grams=_maybe_proposed_selected_grams(item.estimated_grams_min, item.estimated_grams_max),
            preparation=item.preparation,
            is_sauce=item.is_sauce,
            uncertainty=item.uncertainty,
            user_modified=False,
        ))
    inventory_hash_payload = {
        "items": [{"name": item.normalized_name or item.visible_name, "grams_min": item.estimated_grams_min, "grams_max": item.estimated_grams_max} for item in items],
        "image_ref": image_ref or "",
        "occurred_at": _sqlite_timestamp(occurred_at),
    }
    inventory_id = inventory_id or hashlib.sha256(json.dumps(inventory_hash_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return PendingFoodVisionInventory(
        kind=PENDING_INVENTORY_KIND,
        state=PENDING_INVENTORY_STATE,
        inventory_id=inventory_id,
        schema_version=inventory.schema_version,
        user_id=int(user_id),
        source="vision",
        items=items,
        overall_confidence=inventory.overall_confidence,
        needs_user_confirmation=inventory.needs_user_confirmation,
        warnings=list(inventory.warnings),
        image_ref=image_ref,
        occurred_at=occurred_at,
        created_at=created_at,
        expires_at=expires_at,
    )


def _serialize_pending_inventory(payload: PendingFoodVisionInventory) -> str:
    return json.dumps({
        "kind": PENDING_INVENTORY_KIND,
        "state": payload.state,
        "inventory_id": payload.inventory_id,
        "schema_version": payload.schema_version,
        "user_id": payload.user_id,
        "source": payload.source,
        "items": [{
            "index": item.index,
            "visible_name": item.visible_name,
            "normalized_name": item.normalized_name,
            "confidence": item.confidence,
            "estimated_grams_min": item.estimated_grams_min,
            "estimated_grams_max": item.estimated_grams_max,
            "selected_grams": item.selected_grams,
            "preparation": item.preparation,
            "is_sauce": item.is_sauce,
            "uncertainty": item.uncertainty,
            "user_modified": item.user_modified,
        } for item in payload.items],
        "overall_confidence": payload.overall_confidence,
        "needs_user_confirmation": payload.needs_user_confirmation,
        "warnings": payload.warnings,
        "image_ref": payload.image_ref,
        "occurred_at": _sqlite_timestamp(payload.occurred_at),
    }, ensure_ascii=False, sort_keys=True)


def _deserialize_pending_inventory(payload: dict[str, Any], *, created_at: datetime | None, expires_at: datetime | None) -> PendingFoodVisionInventory | None:
    if payload.get("kind") != PENDING_INVENTORY_KIND:
        return None
    items_payload = payload.get("items")
    if not isinstance(items_payload, list) or not items_payload:
        return None
    items: list[PendingFoodVisionItem] = []
    for position, item_payload in enumerate(items_payload, start=1):
        if not isinstance(item_payload, dict):
            return None
        visible_name = _normalize_inventory_item_name(item_payload.get("visible_name"), fallback="")
        normalized_name = _normalize_inventory_item_name(item_payload.get("normalized_name"), fallback=visible_name.casefold())
        if not visible_name:
            return None
        items.append(PendingFoodVisionItem(
            index=int(item_payload.get("index") or position),
            visible_name=visible_name,
            normalized_name=normalized_name,
            confidence=_coerce_confidence(item_payload.get("confidence")),
            estimated_grams_min=_clamp_optional_food_grams(item_payload.get("estimated_grams_min")),
            estimated_grams_max=_clamp_optional_food_grams(item_payload.get("estimated_grams_max")),
            selected_grams=_parse_selected_grams(item_payload.get("selected_grams")),
            preparation=_normalize_optional_text(item_payload.get("preparation"), _MAX_FOOD_VISION_PREPARATION_LENGTH),
            is_sauce=bool(item_payload.get("is_sauce")),
            uncertainty=_normalize_optional_text(item_payload.get("uncertainty"), _MAX_FOOD_VISION_UNCERTAINTY_LENGTH),
            user_modified=bool(item_payload.get("user_modified")),
        ))
    return PendingFoodVisionInventory(
        kind=PENDING_INVENTORY_KIND,
        state=str(payload.get("state") or PENDING_INVENTORY_STATE),
        inventory_id=str(payload.get("inventory_id") or ""),
        schema_version=str(payload.get("schema_version") or FOOD_VISION_SCHEMA_VERSION),
        user_id=int(payload.get("user_id") or 0),
        source=str(payload.get("source") or "vision"),
        items=_reindex_pending_inventory_items(items),
        overall_confidence=_coerce_confidence(payload.get("overall_confidence")),
        needs_user_confirmation=bool(payload.get("needs_user_confirmation")),
        warnings=_sanitize_food_vision_warning_list(payload.get("warnings")),
        image_ref=str(payload.get("image_ref")) if payload.get("image_ref") is not None else None,
        occurred_at=_parse_sqlite_timestamp(payload.get("occurred_at")),
        created_at=created_at,
        expires_at=expires_at,
    )


def _clone_pending_inventory(payload: PendingFoodVisionInventory) -> PendingFoodVisionInventory:
    return PendingFoodVisionInventory(
        kind=payload.kind,
        state=payload.state,
        inventory_id=payload.inventory_id,
        schema_version=payload.schema_version,
        user_id=payload.user_id,
        source=payload.source,
        items=[PendingFoodVisionItem(index=item.index, visible_name=item.visible_name, normalized_name=item.normalized_name, confidence=item.confidence, estimated_grams_min=item.estimated_grams_min, estimated_grams_max=item.estimated_grams_max, selected_grams=item.selected_grams, preparation=item.preparation, is_sauce=item.is_sauce, uncertainty=item.uncertainty, user_modified=item.user_modified) for item in payload.items],
        overall_confidence=payload.overall_confidence,
        needs_user_confirmation=payload.needs_user_confirmation,
        warnings=list(payload.warnings),
        image_ref=payload.image_ref,
        occurred_at=payload.occurred_at,
        created_at=payload.created_at,
        expires_at=payload.expires_at,
    )


def _pending_inventory_notice_lines(notice: str | None) -> list[str]:
    if not notice:
        return ["\u041a\u0411\u0416\u0423 \u043d\u0435 \u0440\u0430\u0441\u0441\u0447\u0438\u0442\u0430\u043d\u044b.", "\u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0441\u043e\u0441\u0442\u0430\u0432 \u0438 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u0438\u043b\u0438 \u0438\u0441\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0435\u0433\u043e."]
    return [notice]


def format_food_vision_inventory_reply(inventory: FoodVisionInventory | PendingFoodVisionInventory, *, notice: str | None = None) -> str:
    pending_inventory = inventory if isinstance(inventory, PendingFoodVisionInventory) else _pending_inventory_from_food_vision(0, inventory)
    lines = ["\u042f \u0432\u0438\u0436\u0443:", ""]
    for item in pending_inventory.items:
        detail = f"{item.index}. {html.escape(item.visible_name)} \u2014 {_format_food_vision_grams(item)}"
        extras: list[str] = []
        if item.preparation:
            extras.append(html.escape(item.preparation))
        if item.uncertainty:
            extras.append(html.escape(item.uncertainty))
        if item.selected_grams is not None:
            extras.append(f"\u0432\u0435\u0441 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d: {_format_grams(item.selected_grams)}")
        else:
            extras.append("\u0432\u0435\u0441 \u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d")
        if extras:
            detail += f" ({'; '.join(extras)})"
        lines.append(detail)
    if pending_inventory.warnings:
        lines.extend(["", "\u0423\u0442\u043e\u0447\u043d\u0435\u043d\u0438\u044f:"])
        lines.extend(f"\u2022 {html.escape(warning)}" for warning in pending_inventory.warnings)
    lines.extend([
        "",
        *_pending_inventory_notice_lines(notice),
        "",
        "\u041c\u043e\u0436\u043d\u043e \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c \u0438\u043b\u0438 \u0438\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c, \u0435\u0441\u043b\u0438 \u0447\u0442\u043e-\u0442\u043e \u0440\u0430\u0441\u043f\u043e\u0437\u043d\u0430\u043d\u043e \u043d\u0435\u0442\u043e\u0447\u043d\u043e.",
        "",
        "\u041a\u043e\u043c\u0430\u043d\u0434\u044b:",
        "\u2022 \u0414\u0430 / \u041d\u0435\u0442",
        "\u2022 \u0418\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c 2: \u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430, 120 \u0433",
        "\u2022 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c: \u0441\u044b\u0440, 30 \u0433",
        "\u2022 \u0423\u0434\u0430\u043b\u0438\u0442\u044c 5",
        "\u2022 \u0412\u0435\u0441 3: 80 \u0433",
    ])
    return "\n".join(lines)


def format_pending_inventory_cancelled_reply() -> str:
    return "\u274c \u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e. \u0424\u043e\u0442\u043e \u043d\u0435 \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e \u0438 \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u043d\u0435 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u043e."


def format_pending_inventory_expired_reply() -> str:
    return "\u231b \u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 \u043f\u043e \u0444\u043e\u0442\u043e \u0438\u0441\u0442\u0435\u043a\u043b\u043e. \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0444\u043e\u0442\u043e \u0435\u0449\u0451 \u0440\u0430\u0437."


def format_pending_inventory_wait_reply(inventory: PendingFoodVisionInventory) -> str:
    return format_food_vision_inventory_reply(inventory, notice="\u0416\u0434\u0443 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f \u0438\u043b\u0438 \u043f\u0440\u0430\u0432\u043a\u0438: \u0414\u0430, \u041d\u0435\u0442, \u0418\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c N, \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c, \u0423\u0434\u0430\u043b\u0438\u0442\u044c N \u0438\u043b\u0438 \u0412\u0435\u0441 N.")


def _format_component_index_phrase(indexes: list[int]) -> str:
    joined = ", ".join(str(index) for index in indexes)
    noun = "\u043a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442\u0430" if len(indexes) == 1 else "\u043a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442\u043e\u0432"
    return f"{noun} {joined}"


def format_pending_inventory_missing_weight_reply(inventory: PendingFoodVisionInventory, missing_indexes: list[int]) -> str:
    return format_food_vision_inventory_reply(inventory, notice="\u042f \u043d\u0435 \u043c\u043e\u0433\u0443 \u0440\u0430\u0441\u0441\u0447\u0438\u0442\u0430\u0442\u044c \u041a\u0411\u0416\u0423: " + f"\u0443\u043a\u0430\u0436\u0438\u0442\u0435 \u0432\u0435\u0441 \u0434\u043b\u044f {_format_component_index_phrase(missing_indexes)}.")


def format_pending_inventory_unknown_component_reply(inventory: PendingFoodVisionInventory, blocked_indexes: list[int]) -> str:
    return format_food_vision_inventory_reply(inventory, notice="\u042f \u043d\u0435 \u043c\u043e\u0433\u0443 \u0440\u0430\u0441\u0441\u0447\u0438\u0442\u0430\u0442\u044c \u041a\u0411\u0416\u0423 \u0434\u043b\u044f " + f"{_format_component_index_phrase(blocked_indexes)}. " + "\u0418\u0441\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435, \u0437\u0430\u043c\u0435\u043d\u0438\u0442\u0435 \u0435\u0433\u043e \u043d\u0430 \u0437\u043d\u0430\u043a\u043e\u043c\u044b\u0439 \u043f\u0440\u043e\u0434\u0443\u043a\u0442 \u0438\u043b\u0438 \u0443\u0434\u0430\u043b\u0438\u0442\u0435 \u043a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442.")

def _parse_selected_grams(value: Any) -> float | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    numeric = float(numeric)
    if numeric < _MIN_SELECTED_GRAMS or numeric > _MAX_SELECTED_GRAMS:
        return None
    return round(numeric, 1)


def _pending_inventory_missing_weight_indexes(inventory: PendingFoodVisionInventory) -> list[int]:
    return [item.index for item in inventory.items if item.selected_grams is None]


def _pending_inventory_readiness(inventory: PendingFoodVisionInventory) -> tuple[str, list[int]]:
    if not inventory.items:
        return INVALID_INVENTORY_STATE, []
    missing_indexes = _pending_inventory_missing_weight_indexes(inventory)
    if missing_indexes:
        return NEEDS_WEIGHT_CONFIRMATION, missing_indexes
    return READY_FOR_NUTRITION_CALCULATION, []


def _normalize_food_lookup_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    ascii_folded = "".join(char for char in normalized if not unicodedata.combining(char))
    ascii_folded = re.sub(r"[^\w\s-]+", " ", ascii_folded)
    return " ".join(ascii_folded.split())


_SYNTHETIC_FOOD_REFERENCE = {
    "\u0412\u0430\u0444\u043b\u0438": {
        "aliases": ("\u0432\u0430\u0444\u043b\u0438", "\u0432\u0430\u0444\u043b\u044f", "\u0431\u0435\u043b\u044c\u0433\u0438\u0439\u0441\u043a\u0438\u0435 \u0432\u0430\u0444\u043b\u0438"),
        "display_name": "\u0412\u0430\u0444\u043b\u0438",
        "per_100g": {"calories_kcal": 312.0, "protein_g": 6.2, "fat_g": 12.1, "carbs_g": 45.6},
    },
    "\u041d\u0430\u0440\u0435\u0437\u043a\u0430 \u043c\u044f\u0441\u0430": {
        "aliases": ("\u043d\u0430\u0440\u0435\u0437\u043a\u0430 \u043c\u044f\u0441\u0430", "\u043c\u044f\u0441\u043e", "\u043b\u043e\u043c\u0442\u0438\u043a\u0438 \u043c\u044f\u0441\u0430", "\u0441\u043b\u0430\u0439\u0441\u044b \u043c\u044f\u0441\u0430"),
        "display_name": "\u041d\u0430\u0440\u0435\u0437\u043a\u0430 \u043c\u044f\u0441\u0430",
        "per_100g": {"calories_kcal": 187.0, "protein_g": 25.5, "fat_g": 8.4, "carbs_g": 0.0},
    },
    "\u041a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430": {
        "aliases": ("\u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430", "\u043a\u0443\u0440\u0438\u0446\u0430", "\u043a\u0443\u0440\u0438\u043d\u043e\u0435 \u0444\u0438\u043b\u0435"),
        "display_name": "\u041a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430",
        "per_100g": {"calories_kcal": 165.0, "protein_g": 31.0, "fat_g": 3.6, "carbs_g": 0.0},
    },
    "\u041e\u0433\u0443\u0440\u0446\u044b": {
        "aliases": ("\u043e\u0433\u0443\u0440\u0446\u044b", "\u043e\u0433\u0443\u0440\u0435\u0446", "\u043e\u0433\u0443\u0440\u0447\u0438\u043a\u0438", "\u043e\u0433\u0443\u0440\u0446\u044b \u0441\u043e\u043b\u043e\u043c\u043a\u043e\u0439"),
        "display_name": "\u041e\u0433\u0443\u0440\u0446\u044b",
        "per_100g": {"calories_kcal": 15.0, "protein_g": 0.7, "fat_g": 0.1, "carbs_g": 3.6},
    },
    "\u041c\u0430\u0439\u043e\u043d\u0435\u0437": {
        "aliases": ("\u043c\u0430\u0439\u043e\u043d\u0435\u0437",),
        "display_name": "\u041c\u0430\u0439\u043e\u043d\u0435\u0437",
        "per_100g": {"calories_kcal": 680.0, "protein_g": 1.0, "fat_g": 75.0, "carbs_g": 1.0},
    },
    "\u0413\u043e\u0440\u0447\u0438\u0446\u0430": {
        "aliases": ("\u0433\u043e\u0440\u0447\u0438\u0446\u0430", "\u0436\u0451\u043b\u0442\u044b\u0439 \u0441\u043e\u0443\u0441", "\u0436\u0435\u043b\u0442\u044b\u0439 \u0441\u043e\u0443\u0441", "\u0433\u043e\u0440\u0447\u0438\u0447\u043d\u044b\u0439 \u0441\u043e\u0443\u0441"),
        "display_name": "\u0413\u043e\u0440\u0447\u0438\u0446\u0430",
        "per_100g": {"calories_kcal": 66.0, "protein_g": 4.4, "fat_g": 4.0, "carbs_g": 5.8},
    },
    "\u0421\u044b\u0440": {
        "aliases": ("\u0441\u044b\u0440",),
        "display_name": "\u0421\u044b\u0440",
        "per_100g": {"calories_kcal": 356.0, "protein_g": 24.0, "fat_g": 27.0, "carbs_g": 2.0},
    },
    "\u042f\u0431\u043b\u043e\u043a\u043e": {
        "aliases": ("\u044f\u0431\u043b\u043e\u043a\u043e",),
        "display_name": "\u042f\u0431\u043b\u043e\u043a\u043e",
        "per_100g": {"calories_kcal": 52.0, "protein_g": 0.3, "fat_g": 0.2, "carbs_g": 14.0},
    },
    "\u0425\u043b\u0435\u0431": {
        "aliases": ("\u0445\u043b\u0435\u0431",),
        "display_name": "\u0425\u043b\u0435\u0431",
        "per_100g": {"calories_kcal": 265.0, "protein_g": 8.8, "fat_g": 3.2, "carbs_g": 49.0},
    },
    "\u0427\u0435\u0447\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u0441\u0443\u043f \u0441 \u043e\u0432\u043e\u0449\u0430\u043c\u0438": {
        "aliases": ("\u0447\u0435\u0447\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u0441\u0443\u043f \u0441 \u043e\u0432\u043e\u0449\u0430\u043c\u0438", "\u0447\u0435\u0447\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u0441\u0443\u043f"),
        "display_name": "\u0427\u0435\u0447\u0435\u0432\u0438\u0447\u043d\u044b\u0439 \u0441\u0443\u043f \u0441 \u043e\u0432\u043e\u0449\u0430\u043c\u0438",
        "per_100g": {"calories_kcal": 72.0, "protein_g": 3.8, "fat_g": 1.9, "carbs_g": 10.2},
    },
}


def _build_synthetic_food_alias_index() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for canonical_name, payload in _SYNTHETIC_FOOD_REFERENCE.items():
        aliases[_normalize_food_lookup_key(canonical_name)] = canonical_name
        for alias in payload["aliases"]:
            aliases[_normalize_food_lookup_key(alias)] = canonical_name
    return aliases


_SYNTHETIC_FOOD_ALIAS_INDEX = _build_synthetic_food_alias_index()


def _resolve_synthetic_food_reference(name: str) -> tuple[str, dict[str, float]] | None:
    normalized_key = _normalize_food_lookup_key(name)
    canonical_name = _SYNTHETIC_FOOD_ALIAS_INDEX.get(normalized_key)
    if canonical_name is None:
        return None
    payload = _SYNTHETIC_FOOD_REFERENCE[canonical_name]
    return payload["display_name"], dict(payload["per_100g"])


def _estimate_component_nutrition(item: PendingFoodVisionItem) -> NutritionComponentEstimate | None:
    if item.selected_grams is None:
        return None
    resolved = _resolve_synthetic_food_reference(item.normalized_name or item.visible_name)
    if resolved is None:
        return None
    display_name, reference = resolved
    multiplier = float(item.selected_grams) / 100.0
    return NutritionComponentEstimate(
        name=display_name,
        grams=float(item.selected_grams),
        calories_kcal=round(reference["calories_kcal"] * multiplier, 1),
        protein_g=round(reference["protein_g"] * multiplier, 1),
        fat_g=round(reference["fat_g"] * multiplier, 1),
        carbs_g=round(reference["carbs_g"] * multiplier, 1),
    )


def _build_component_meal_name(components: list[NutritionComponentEstimate]) -> str:
    names = [component.name for component in components if component.name]
    if not names:
        return "\u0411\u043b\u044e\u0434\u043e"
    return ", ".join(names[:3])


def _build_component_record(inventory: PendingFoodVisionInventory, components: list[NutritionComponentEstimate]) -> NutritionRecord:
    items = [{"name": component.name, "estimated_weight_g": component.grams, "calories_kcal": component.calories_kcal, "protein_g": component.protein_g, "fat_g": component.fat_g, "carbs_g": component.carbs_g} for component in components]
    meal_name = _build_component_meal_name(components)
    total_calories = round(sum(component.calories_kcal for component in components), 1)
    total_protein = round(sum(component.protein_g for component in components), 1)
    total_fat = round(sum(component.fat_g for component in components), 1)
    total_carbs = round(sum(component.carbs_g for component in components), 1)
    return NutritionRecord(
        is_food=True,
        meal_name=meal_name,
        display_name=meal_name,
        items=items,
        calories_kcal=total_calories,
        protein_g=total_protein,
        fat_g=total_fat,
        carbs_g=total_carbs,
        confidence=inventory.overall_confidence,
        raw_summary=f"\u041a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442\u043d\u043e\u0435 \u0444\u043e\u0442\u043e: {meal_name}",
    )


def calculate_inventory_nutrition(inventory: PendingFoodVisionInventory) -> InventoryNutritionCalculationResult:
    readiness, missing_indexes = _pending_inventory_readiness(inventory)
    if readiness != READY_FOR_NUTRITION_CALCULATION:
        return InventoryNutritionCalculationResult(status=readiness, components=[], missing_indexes=missing_indexes, blocked_indexes=[], reason="weights_missing" if missing_indexes else "invalid_inventory_state")
    components: list[NutritionComponentEstimate] = []
    blocked_indexes: list[int] = []
    for item in inventory.items:
        estimate = _estimate_component_nutrition(item)
        if estimate is None:
            blocked_indexes.append(item.index)
            continue
        components.append(estimate)
    if blocked_indexes:
        return InventoryNutritionCalculationResult(status=UNKNOWN_COMPONENT_BLOCKED, components=components, missing_indexes=[], blocked_indexes=blocked_indexes, reason="unknown_component")
    record = _build_component_record(inventory, components)
    return InventoryNutritionCalculationResult(status=READY_FOR_NUTRITION_CALCULATION, components=components, missing_indexes=[], blocked_indexes=[], record=record)


_RU_CONFIRM_WORDS = {"\u0434\u0430", "\u0430\u0433\u0430", "\u043e\u043a", "\u043e\u043a\u0435\u0439", "yes", "y", "\u0441\u043e\u0445\u0440\u0430\u043d\u0438", "\u0441\u043e\u0445\u0440\u0430\u043d\u044f\u0439"}
_RU_CANCEL_WORDS = {"\u043d\u0435\u0442", "\u043d\u0435\u0430", "\u043e\u0442\u043c\u0435\u043d\u0430", "\u043e\u0442\u043c\u0435\u043d\u0438", "cancel", "no", "n"}
_GRAM_SUFFIX_RE = r"(?:\u0433|\u0433\u0440|\u0433\u0440\u0430\u043c(?:\u043c|\u0430|\u043e\u0432|\u044b)?)"
_INVENTORY_REPLACE_RE = re.compile(rf"^(?:(?:\u0438\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c)\s+)?(?P<index>\d{{1,2}})\s*:\s*(?P<name>[^,:]{{1,80}}?)(?:\s*,\s*|\s+)(?P<grams>\d+(?:[.,]\d+)?)\s*{_GRAM_SUFFIX_RE}?\s*$", re.IGNORECASE)
_INVENTORY_ADD_RE = re.compile(rf"^(?:\u0434\u043e\u0431\u0430\u0432\u0438\u0442\u044c)\s*:\s*(?P<name>[^,:]{{1,80}}?)(?:\s*,\s*|\s+)(?P<grams>\d+(?:[.,]\d+)?)\s*{_GRAM_SUFFIX_RE}?\s*$", re.IGNORECASE)
_INVENTORY_REMOVE_RE = re.compile(r"^(?:\u0443\u0434\u0430\u043b\u0438\u0442\u044c)\s+(?P<index>\d{1,2})\s*$", re.IGNORECASE)
_INVENTORY_WEIGHT_RE = re.compile(rf"^(?:\u0432\u0435\u0441)\s+(?P<index>\d{{1,2}})\s*:\s*(?P<grams>\d+(?:[.,]\d+)?)\s*{_GRAM_SUFFIX_RE}?\s*$", re.IGNORECASE)


def parse_pending_inventory_action(text: str) -> PendingInventoryActionParseResult:
    normalized = re.sub(r"\s+", " ", (text or "").strip())
    if not normalized or normalized.startswith("/"):
        return PendingInventoryActionParseResult(ok=False, error="\u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043e\u0434\u0438\u043d \u043e\u0442\u0432\u0435\u0442 \u0438\u0437 \u0441\u043f\u0438\u0441\u043a\u0430: \u0414\u0430, \u041d\u0435\u0442, \u0418\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c N, \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c, \u0423\u0434\u0430\u043b\u0438\u0442\u044c N \u0438\u043b\u0438 \u0412\u0435\u0441 N.")
    lowered = normalized.casefold()
    if lowered in _RU_CONFIRM_WORDS:
        return PendingInventoryActionParseResult(ok=True, action=PendingInventoryAction(kind="confirm"))
    if lowered in _RU_CANCEL_WORDS:
        return PendingInventoryActionParseResult(ok=True, action=PendingInventoryAction(kind="cancel"))
    for regex, action_kind in ((_INVENTORY_REPLACE_RE, "replace"), (_INVENTORY_ADD_RE, "add"), (_INVENTORY_REMOVE_RE, "remove"), (_INVENTORY_WEIGHT_RE, "weight")):
        match = regex.match(normalized)
        if not match:
            continue
        index = int(match.group("index")) if "index" in match.groupdict() and match.group("index") else None
        name = _normalize_inventory_item_name(match.groupdict().get("name", ""), fallback="")
        grams = _parse_selected_grams(match.groupdict().get("grams"))
        if index is not None and index <= 0:
            return PendingInventoryActionParseResult(ok=False, error="\u041d\u043e\u043c\u0435\u0440 \u043a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442\u0430 \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u043f\u043e\u043b\u043e\u0436\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u043c.")
        if action_kind in {"replace", "add"} and not name:
            return PendingInventoryActionParseResult(ok=False, error="\u0423\u043a\u0430\u0436\u0438\u0442\u0435 \u043f\u043e\u043d\u044f\u0442\u043d\u043e\u0435 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0434\u043b\u044f \u0437\u0430\u043c\u0435\u043d\u044b \u0438\u043b\u0438 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0438\u044f.")
        if action_kind in {"replace", "add", "weight"} and grams is None:
            return PendingInventoryActionParseResult(ok=False, error="\u0412\u0435\u0441 \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u0431\u043e\u043b\u044c\u0448\u0435 0 \u0433 \u0438 \u043d\u0435 \u0431\u043e\u043b\u044c\u0448\u0435 2000 \u0433.")
        return PendingInventoryActionParseResult(ok=True, action=PendingInventoryAction(kind=action_kind, index=index, name=name, grams=grams))
    return PendingInventoryActionParseResult(ok=False, error="\u041d\u0435 \u043f\u043e\u043d\u044f\u043b \u043a\u043e\u043c\u0430\u043d\u0434\u0443. \u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439\u0442\u0435 \u043e\u0434\u0438\u043d \u0438\u0437 \u0432\u0430\u0440\u0438\u0430\u043d\u0442\u043e\u0432: \u0414\u0430, \u041d\u0435\u0442, \u0418\u0441\u043f\u0440\u0430\u0432\u0438\u0442\u044c 2: \u043a\u0443\u0440\u0438\u043d\u0430\u044f \u0433\u0440\u0443\u0434\u043a\u0430, 120 \u0433, \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c: \u0441\u044b\u0440, 30 \u0433, \u0423\u0434\u0430\u043b\u0438\u0442\u044c 5, \u0412\u0435\u0441 3: 80 \u0433.")


def _reindex_pending_inventory_items(items: list[PendingFoodVisionItem]) -> list[PendingFoodVisionItem]:
    for position, item in enumerate(items, start=1):
        item.index = position
    return items


def _mutate_pending_inventory(inventory: PendingFoodVisionInventory, action: PendingInventoryAction) -> tuple[PendingFoodVisionInventory | None, str | None]:
    updated = _clone_pending_inventory(inventory)
    items = updated.items
    if action.kind != "add":
        if action.index is None or action.index > len(items):
            return None, "\u0422\u0430\u043a\u043e\u0433\u043e \u043d\u043e\u043c\u0435\u0440\u0430 \u043d\u0435\u0442 \u0432 \u0442\u0435\u043a\u0443\u0449\u0435\u043c \u0441\u043f\u0438\u0441\u043a\u0435."
        target = items[action.index - 1]
    else:
        target = None
    if action.kind == "replace" and target is not None:
        target.visible_name = action.name
        target.normalized_name = action.name.casefold()
        target.confidence = 1.0
        target.estimated_grams_min = action.grams
        target.estimated_grams_max = action.grams
        target.selected_grams = action.grams
        target.preparation = ""
        target.is_sauce = target.is_sauce or _infer_inventory_sauce_flag(action.name)
        target.uncertainty = ""
        target.user_modified = True
    elif action.kind == "add":
        if len(items) >= _MAX_PENDING_INVENTORY_ITEMS:
            return None, "\u0421\u043b\u0438\u0448\u043a\u043e\u043c \u043c\u043d\u043e\u0433\u043e \u043a\u043e\u043c\u043f\u043e\u043d\u0435\u043d\u0442\u043e\u0432 \u0432 \u043e\u0434\u043d\u043e\u043c \u0444\u043e\u0442\u043e. \u0423\u0434\u0430\u043b\u0438\u0442\u0435 \u043b\u0438\u0448\u043d\u0435\u0435 \u0438 \u043f\u043e\u043f\u0440\u043e\u0431\u0443\u0439\u0442\u0435 \u0435\u0449\u0451 \u0440\u0430\u0437."
        items.append(PendingFoodVisionItem(index=len(items) + 1, visible_name=action.name, normalized_name=action.name.casefold(), confidence=1.0, estimated_grams_min=action.grams, estimated_grams_max=action.grams, selected_grams=action.grams, preparation="", is_sauce=_infer_inventory_sauce_flag(action.name), uncertainty="", user_modified=True))
    elif action.kind == "remove" and target is not None:
        del items[action.index - 1]
    elif action.kind == "weight" and target is not None:
        target.selected_grams = action.grams
        target.estimated_grams_min = target.estimated_grams_min if target.estimated_grams_min is not None else action.grams
        target.estimated_grams_max = target.estimated_grams_max if target.estimated_grams_max is not None else action.grams
        target.user_modified = True
    else:
        return None, "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u0438\u043c\u0435\u043d\u0438\u0442\u044c \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435."
    updated.items = _reindex_pending_inventory_items(items)
    return updated, None


def _format_component_estimate_line(component: NutritionComponentEstimate) -> str:
    return f"\u2022 {html.escape(component.name)}, {_format_grams(component.grams)} \u00b7 {_format_kcal(component.calories_kcal)} \u00b7 \u0411 {_format_grams(component.protein_g)} \u00b7 \u0416 {_format_grams(component.fat_g)} \u00b7 \u0423 {_format_grams(component.carbs_g)}"

def normalize_nutrition_payload(payload_text: str) -> NutritionRecord | None:
    payload = safe_json_loads(payload_text, {})
    if not isinstance(payload, dict) or not payload:
        return None

    nutrition_payload = payload.get("nutrition")
    if isinstance(nutrition_payload, dict):
        merged = dict(nutrition_payload)
        for key, value in payload.items():
            merged.setdefault(key, value)
        payload = merged

    totals_payload = payload.get("totals") if isinstance(payload.get("totals"), dict) else {}
    items_payload = payload.get("items")
    if not isinstance(items_payload, list):
        items_payload = payload.get("foods") if isinstance(payload.get("foods"), list) else []

    items: list[dict[str, Any]] = []
    total_calories = _to_float(totals_payload.get("calories_kcal") if totals_payload else payload.get("calories_kcal"))
    total_protein = _to_float(totals_payload.get("protein_g") if totals_payload else payload.get("protein_g"))
    total_fat = _to_float(totals_payload.get("fat_g") if totals_payload else payload.get("fat_g"))
    total_carbs = _to_float(totals_payload.get("carbs_g") if totals_payload else payload.get("carbs_g"))

    item_calories_sum = 0.0
    item_protein_sum = 0.0
    item_fat_sum = 0.0
    item_carbs_sum = 0.0
    has_item_calories = False
    has_item_protein = False
    has_item_fat = False
    has_item_carbs = False

    for raw_item in items_payload:
        if isinstance(raw_item, str):
            item = {"name": raw_item.strip()}
        elif isinstance(raw_item, dict):
            item = {
                "name": _normalize_meal_name_text(
                    raw_item.get("display_name")
                    or raw_item.get("meal_name_user")
                    or raw_item.get("name")
                    or raw_item.get("item")
                    or "",
                    "",
                ),
                "estimated_weight_g": _to_float(raw_item.get("estimated_weight_g") or raw_item.get("weight_g")),
                "calories_kcal": _to_float(raw_item.get("calories_kcal") or raw_item.get("calories")),
                "protein_g": _to_float(raw_item.get("protein_g") or raw_item.get("protein")),
                "fat_g": _to_float(raw_item.get("fat_g") or raw_item.get("fat")),
                "carbs_g": _to_float(raw_item.get("carbs_g") or raw_item.get("carbs")),
            }
        else:
            continue
        if not item.get("name"):
            continue
        items.append(item)
        if item.get("calories_kcal") is not None:
            item_calories_sum += float(item["calories_kcal"])
            has_item_calories = True
        if item.get("protein_g") is not None:
            item_protein_sum += float(item["protein_g"])
            has_item_protein = True
        if item.get("fat_g") is not None:
            item_fat_sum += float(item["fat_g"])
            has_item_fat = True
        if item.get("carbs_g") is not None:
            item_carbs_sum += float(item["carbs_g"])
            has_item_carbs = True

    if total_calories is None and has_item_calories:
        total_calories = item_calories_sum
    if total_protein is None and has_item_protein:
        total_protein = item_protein_sum
    if total_fat is None and has_item_fat:
        total_fat = item_fat_sum
    if total_carbs is None and has_item_carbs:
        total_carbs = item_carbs_sum

    meal_name = _normalize_meal_name_text(payload.get("meal_name") or payload.get("dish") or "", "")
    display_name = _normalize_meal_name_text(
        payload.get("display_name")
        or payload.get("meal_name_user")
        or payload.get("meal_name_ru")
        or meal_name,
        "",
    )
    if not meal_name and items:
        meal_name = ", ".join(item["name"] for item in items[:2])
    if not meal_name:
        meal_name = "Meal"
    if not display_name and items:
        display_name = ", ".join(item["name"] for item in items[:2])
    if not display_name:
        display_name = meal_name or "Блюдо"

    raw_summary = str(payload.get("raw_summary") or payload.get("summary") or payload.get("description") or "").strip()
    if not raw_summary:
        raw_summary = display_name or meal_name

    confidence = _coerce_confidence(payload.get("confidence"))
    is_food_value = payload.get("is_food")
    if isinstance(is_food_value, bool):
        is_food = is_food_value
    else:
        is_food = bool(
            items
            or total_calories is not None
            or total_protein is not None
            or total_fat is not None
            or total_carbs is not None
        )

    return NutritionRecord(
        is_food=is_food,
        meal_name=meal_name,
        items=items,
        calories_kcal=total_calories,
        protein_g=total_protein,
        fat_g=total_fat,
        carbs_g=total_carbs,
        confidence=confidence,
        raw_summary=raw_summary,
        display_name=display_name,
    )


def format_nutrition_context(record: NutritionRecord, *, saved: bool, duplicate: bool) -> str:
    if not record.is_food:
        return (
            "[HealBite image analysis: no clear food or drink detected. "
            f"Summary: {record.raw_summary}]"
        )

    meal_label = _record_display_name(record)
    items_text = ", ".join(item.get("name", "") for item in record.items[:5] if item.get("name")) or meal_label
    totals = []
    if record.calories_kcal is not None:
        totals.append(f"{record.calories_kcal:.0f} kcal")
    if record.protein_g is not None:
        totals.append(f"P {record.protein_g:.1f} g")
    if record.fat_g is not None:
        totals.append(f"F {record.fat_g:.1f} g")
    if record.carbs_g is not None:
        totals.append(f"C {record.carbs_g:.1f} g")
    save_note = "saved to the nutrition diary"
    if duplicate:
        save_note = "already present in the nutrition diary"
    elif not saved:
        save_note = "not auto-saved because confidence was too low"
    return (
        "[HealBite structured nutrition analysis from the user's image:\n"
        f"Meal: {meal_label}\n"
        f"Items: {items_text}\n"
        f"Estimated totals: {', '.join(totals) if totals else 'not enough structured macros'}\n"
        f"Confidence: {record.confidence:.2f}\n"
        f"Diary status: {save_note}\n"
        f"Vision summary: {record.raw_summary}]"
    )


class HealBiteNutritionDiary:
    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        qdrant_adapter: QdrantMemoryAdapter | None = None,
        embedding_adapter: EmbeddingAdapter | None = None,
        background_write: bool = True,
        autosave_confidence_threshold: float = 0.65,
    ) -> None:
        self.db_path = resolve_healbite_db_path(db_path)
        self.embedding_adapter = embedding_adapter or EmbeddingAdapter()
        self.qdrant_adapter = qdrant_adapter or QdrantMemoryAdapter(embedding_adapter=self.embedding_adapter)
        self.autosave_confidence_threshold = float(autosave_confidence_threshold)
        self._executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="healbite-diary-qdrant")
            if background_write and self.qdrant_adapter is not None
            else None
        )
        self._initialize_schema()

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    async def analyze_and_maybe_log(
        self,
        *,
        user_id: int,
        image_path: str,
        user_text: str = "",
        source: str = "photo",
        image_ref: str | None = None,
        occurred_at: datetime | None = None,
    ) -> NutritionDiaryOutcome:
        from tools.vision_tools import vision_analyze_tool

        prompt = _VISION_PROMPT
        if user_text.strip():
            prompt += f" User note: {user_text.strip()}"

        result_json = await vision_analyze_tool(image_url=image_path, user_prompt=prompt)
        result = safe_json_loads(result_json, {})
        if not isinstance(result, dict) or not result.get("success"):
            logger.info("[HealBite][vision_parse_ok] ok=false reason=provider_result_non_success")
            logger.info("[HealBite][vision_pending_staged] staged=false")
            return NutritionDiaryOutcome(
                available=False,
                raw_analysis=str(result.get("analysis", "") if isinstance(result, dict) else ""),
            )

        raw_analysis = str(result.get("analysis") or "").strip()
        validation = validate_food_vision_inventory(raw_analysis)
        if validation.inventory is None:
            logger.info("[HealBite][vision_parse_ok] ok=false reason=%s", validation.reason or "structured_json_missing")
            logger.info("[HealBite][vision_pending_staged] staged=false")
            return NutritionDiaryOutcome(
                available=False,
                raw_analysis=raw_analysis,
                validation_status=validation.status,
            )

        pending_inventory = self.stage_pending_inventory(
            user_id=user_id,
            source=source,
            inventory=validation.inventory,
            image_ref=image_ref,
            occurred_at=occurred_at,
        )
        logger.info(
            "[HealBite][vision_parse_ok] ok=true is_food=true confidence_bucket=%s validation_status=%s",
            _confidence_bucket(validation.inventory.overall_confidence),
            validation.status,
        )
        logger.info("[HealBite][vision_pending_staged] staged=true kind=inventory")
        return NutritionDiaryOutcome(
            available=True,
            raw_analysis=raw_analysis,
            clarification_text=format_food_vision_inventory_reply(pending_inventory),
            validation_status=validation.status,
        )

    def _should_stage_pending(self, record: NutritionRecord) -> bool:
        if not record.is_food:
            return False
        return any(
            value is not None
            for value in (record.calories_kcal, record.protein_g, record.fat_g, record.carbs_g)
        ) or bool(record.items)

    def _write_pending_state(
        self,
        *,
        user_id: int,
        payload_json: str,
        created_at: datetime,
        expires_at: datetime,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {PENDING_MEALS_TABLE}(user_id, payload_json, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (
                    int(user_id),
                    payload_json,
                    _sqlite_timestamp(created_at),
                    _sqlite_timestamp(expires_at),
                ),
            )

    def _serialize_pending_meal(self, payload: PendingMealPayload) -> str:
        return json.dumps(
            {
                "pending_kind": PENDING_MEAL_KIND,
                "source": payload.source,
                "image_ref": payload.image_ref,
                "occurred_at": _sqlite_timestamp(payload.occurred_at) if payload.occurred_at is not None else None,
                "record": {
                    "is_food": payload.record.is_food,
                    "meal_name": payload.record.meal_name,
                    "display_name": payload.record.display_name,
                    "items": payload.record.items,
                    "calories_kcal": payload.record.calories_kcal,
                    "protein_g": payload.record.protein_g,
                    "fat_g": payload.record.fat_g,
                    "carbs_g": payload.record.carbs_g,
                    "confidence": payload.record.confidence,
                    "raw_summary": payload.record.raw_summary,
                },
            },
            ensure_ascii=False,
        )

    def _serialize_pending_inventory(self, payload: PendingFoodVisionInventory) -> str:
        return json.dumps(
            {
                "pending_kind": PENDING_INVENTORY_KIND,
                "inventory_id": payload.inventory_id,
                "schema_version": payload.schema_version,
                "source": payload.source,
                "state": payload.state,
                "image_ref": payload.image_ref,
                "occurred_at": _sqlite_timestamp(payload.occurred_at) if payload.occurred_at is not None else None,
                "overall_confidence": payload.overall_confidence,
                "warnings": payload.warnings,
                "items": [
                    {
                        "index": item.index,
                        "visible_name": item.visible_name,
                        "normalized_name": item.normalized_name,
                        "confidence": item.confidence,
                        "estimated_grams_min": item.estimated_grams_min,
                        "estimated_grams_max": item.estimated_grams_max,
                        "selected_grams": item.selected_grams,
                        "preparation": item.preparation,
                        "is_sauce": item.is_sauce,
                        "uncertainty": item.uncertainty,
                        "user_modified": item.user_modified,
                    }
                    for item in payload.items
                ],
            },
            ensure_ascii=False,
        )

    def stage_pending_inventory(
        self,
        *,
        user_id: int,
        source: str,
        inventory: FoodVisionInventory,
        image_ref: str | None,
        occurred_at: datetime | None,
        now: datetime | None = None,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
        inventory_id: str | None = None,
    ) -> PendingFoodVisionInventory:
        payload = _pending_inventory_from_food_vision(
            int(user_id),
            inventory,
            image_ref=image_ref,
            occurred_at=occurred_at,
            now=now,
            ttl=ttl,
            inventory_id=inventory_id,
        )
        if expires_at is not None:
            payload.expires_at = _normalize_timestamp(expires_at)
        self._write_pending_state(
            user_id=payload.user_id,
            payload_json=self._serialize_pending_inventory(payload),
            created_at=payload.created_at or _normalize_timestamp(now),
            expires_at=payload.expires_at or (_normalize_timestamp(now) + PENDING_MEAL_TTL),
        )
        return payload

    def stage_pending_meal(
        self,
        *,
        user_id: int,
        source: str,
        record: NutritionRecord,
        image_ref: str | None,
        occurred_at: datetime | None,
        now: datetime | None = None,
        ttl: timedelta | None = None,
        expires_at: datetime | None = None,
    ) -> PendingMealPayload:
        created_at_dt = _normalize_timestamp(now)
        expires_at_dt = _normalize_timestamp(expires_at) if expires_at is not None else (
            created_at_dt + (ttl if ttl is not None else PENDING_MEAL_TTL)
        )
        payload = PendingMealPayload(
            user_id=int(user_id),
            source=source,
            record=record,
            image_ref=image_ref,
            occurred_at=occurred_at,
            created_at=created_at_dt,
            expires_at=expires_at_dt,
        )
        self._write_pending_state(
            user_id=payload.user_id,
            payload_json=self._serialize_pending_meal(payload),
            created_at=created_at_dt,
            expires_at=expires_at_dt,
        )
        return payload

    def get_pending_state(
        self,
        user_id: str | int,
        *,
        now: datetime | None = None,
        include_expired: bool = False,
    ) -> PendingMealPayload | PendingFoodVisionInventory | None:
        normalized_user_id = int(user_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT payload_json, created_at, expires_at
                FROM {PENDING_MEALS_TABLE}
                WHERE user_id = ?
                LIMIT 1
                """,
                (normalized_user_id,),
            ).fetchone()
            if row is None:
                return None
        payload = self._deserialize_pending_state(
            normalized_user_id,
            payload_json=str(row["payload_json"] or ""),
            created_at=str(row["created_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
        )
        if payload is None:
            self.clear_pending_state(normalized_user_id)
            return None
        if not include_expired and self.is_pending_meal_expired(payload, now=now):
            self.clear_pending_state(normalized_user_id)
            return None
        return payload

    def get_pending_inventory(
        self,
        user_id: str | int,
        *,
        now: datetime | None = None,
        include_expired: bool = False,
    ) -> PendingFoodVisionInventory | None:
        payload = self.get_pending_state(user_id, now=now, include_expired=include_expired)
        return payload if isinstance(payload, PendingFoodVisionInventory) else None

    def get_pending_meal(
        self,
        user_id: str | int,
        *,
        now: datetime | None = None,
        include_expired: bool = False,
    ) -> PendingMealPayload | None:
        payload = self.get_pending_state(user_id, now=now, include_expired=include_expired)
        return payload if isinstance(payload, PendingMealPayload) else None

    def clear_pending_state(self, user_id: str | int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {PENDING_MEALS_TABLE} WHERE user_id = ?",
                (int(user_id),),
            )
        return bool(cursor.rowcount)

    def clear_pending_meal(self, user_id: str | int) -> bool:
        return self.clear_pending_state(user_id)

    def _store_pending_inventory(self, payload: PendingFoodVisionInventory) -> PendingFoodVisionInventory:
        self._write_pending_state(
            user_id=payload.user_id,
            payload_json=self._serialize_pending_inventory(payload),
            created_at=payload.created_at or _normalize_timestamp(),
            expires_at=payload.expires_at or (_normalize_timestamp() + PENDING_MEAL_TTL),
        )
        return payload

    def confirm_pending_inventory(
        self,
        user_id: str | int,
        *,
        expected_inventory_id: str | None = None,
        now: datetime | None = None,
    ) -> PendingInventoryReplyResult:
        normalized_user_id = int(user_id)
        payload = self.get_pending_inventory(normalized_user_id, now=now, include_expired=True)
        if payload is None:
            return PendingInventoryReplyResult(status="missing", reply_text="\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 \u043f\u043e \u0444\u043e\u0442\u043e. \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0444\u043e\u0442\u043e \u0435\u0449\u0451 \u0440\u0430\u0437.")
        if expected_inventory_id and payload.inventory_id != expected_inventory_id:
            return PendingInventoryReplyResult(
                status="stale",
                reply_text="\u042d\u0442\u043e \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 \u0443\u0436\u0435 \u0437\u0430\u043c\u0435\u043d\u0435\u043d\u043e \u043d\u043e\u0432\u044b\u043c \u0444\u043e\u0442\u043e. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435 \u0444\u043e\u0442\u043e \u0435\u0449\u0451 \u0440\u0430\u0437.",
                inventory=payload,
            )
        if self.is_pending_meal_expired(payload, now=now):
            self.clear_pending_state(normalized_user_id)
            return PendingInventoryReplyResult(
                status="expired",
                reply_text=format_pending_inventory_expired_reply(),
            )
        calculation = calculate_inventory_nutrition(payload)
        if calculation.status == NEEDS_WEIGHT_CONFIRMATION:
            return PendingInventoryReplyResult(
                status="needs_weight_confirmation",
                reply_text=format_pending_inventory_missing_weight_reply(payload, calculation.missing_indexes),
                inventory=payload,
                missing_indexes=calculation.missing_indexes,
            )
        if calculation.status == UNKNOWN_COMPONENT_BLOCKED:
            return PendingInventoryReplyResult(
                status="unknown_component",
                reply_text=format_pending_inventory_unknown_component_reply(payload, calculation.blocked_indexes),
                inventory=payload,
                blocked_indexes=calculation.blocked_indexes,
            )
        if calculation.record is None:
            return PendingInventoryReplyResult(
                status="invalid_inventory_state",
                reply_text=format_food_vision_inventory_reply(payload, notice="\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u043e\u0434\u0433\u043e\u0442\u043e\u0432\u0438\u0442\u044c \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u0438\u0435. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0441\u043e\u0441\u0442\u0430\u0432 \u0438\u043b\u0438 \u043e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0444\u043e\u0442\u043e \u0435\u0449\u0451 \u0440\u0430\u0437."),
                inventory=payload,
            )
        self.stage_pending_meal(
            user_id=normalized_user_id,
            source=payload.source,
            record=calculation.record,
            image_ref=payload.image_ref,
            occurred_at=payload.occurred_at,
            now=now,
        )
        return PendingInventoryReplyResult(
            status="awaiting_save_confirmation",
            reply_text=format_pending_meal_prompt(calculation.record),
            record=calculation.record,
        )

    def apply_pending_inventory_action(
        self,
        user_id: str | int,
        action: PendingInventoryAction,
        *,
        expected_inventory_id: str | None = None,
        now: datetime | None = None,
    ) -> PendingInventoryReplyResult:
        normalized_user_id = int(user_id)
        payload = self.get_pending_inventory(normalized_user_id, now=now, include_expired=True)
        if payload is None:
            return PendingInventoryReplyResult(status="missing", reply_text="\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 \u043f\u043e \u0444\u043e\u0442\u043e. \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0444\u043e\u0442\u043e \u0435\u0449\u0451 \u0440\u0430\u0437.")
        if expected_inventory_id and payload.inventory_id != expected_inventory_id:
            return PendingInventoryReplyResult(
                status="stale",
                reply_text="\u042d\u0442\u043e \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 \u0443\u0436\u0435 \u0437\u0430\u043c\u0435\u043d\u0435\u043d\u043e \u043d\u043e\u0432\u044b\u043c \u0444\u043e\u0442\u043e. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0435\u0435 \u0444\u043e\u0442\u043e \u0435\u0449\u0451 \u0440\u0430\u0437.",
                inventory=payload,
            )
        if self.is_pending_meal_expired(payload, now=now):
            self.clear_pending_state(normalized_user_id)
            return PendingInventoryReplyResult(status="expired", reply_text=format_pending_inventory_expired_reply())
        updated, error = _mutate_pending_inventory(payload, action)
        if updated is None:
            return PendingInventoryReplyResult(
                status="invalid_action",
                reply_text=format_food_vision_inventory_reply(payload, notice=error or "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u0438\u043c\u0435\u043d\u0438\u0442\u044c \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0438\u0435."),
                inventory=payload,
            )
        self._store_pending_inventory(updated)
        return PendingInventoryReplyResult(
            status="updated",
            reply_text=format_food_vision_inventory_reply(updated, notice="\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u044f \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u044b. \u041f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u0441\u043e\u0441\u0442\u0430\u0432 \u0435\u0449\u0451 \u0440\u0430\u0437."),
            inventory=updated,
        )

    def handle_pending_inventory_reply(
        self,
        user_id: str | int,
        message: str,
        *,
        now: datetime | None = None,
        expected_inventory_id: str | None = None,
    ) -> PendingInventoryReplyResult:
        payload = self.get_pending_inventory(user_id, now=now, include_expired=True)
        if payload is None:
            return PendingInventoryReplyResult(status="missing", reply_text="\u041d\u0435 \u043d\u0430\u0448\u0451\u043b \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435 \u043f\u043e \u0444\u043e\u0442\u043e. \u041e\u0442\u043f\u0440\u0430\u0432\u044c\u0442\u0435 \u0444\u043e\u0442\u043e \u0435\u0449\u0451 \u0440\u0430\u0437.")
        parse_result = parse_pending_inventory_action(message)
        if not parse_result.ok or parse_result.action is None:
            return PendingInventoryReplyResult(
                status="invalid_action",
                reply_text=format_food_vision_inventory_reply(payload, notice=parse_result.error),
                inventory=payload,
            )
        action = parse_result.action
        if action.kind == "cancel":
            self.clear_pending_state(user_id)
            return PendingInventoryReplyResult(status="cancelled", reply_text=format_pending_inventory_cancelled_reply())
        if action.kind == "confirm":
            return self.confirm_pending_inventory(user_id, expected_inventory_id=expected_inventory_id, now=now)
        return self.apply_pending_inventory_action(user_id, action, expected_inventory_id=expected_inventory_id, now=now)

    def confirm_pending_meal(
        self,
        user_id: str | int,
        *,
        now: datetime | None = None,
    ) -> ConfirmPendingMealResult:
        normalized_user_id = int(user_id)
        payload = self.get_pending_meal(
            normalized_user_id,
            now=now,
            include_expired=True,
        )
        if payload is None:
            return ConfirmPendingMealResult(status="missing")
        if self.is_pending_meal_expired(payload, now=now):
            self.clear_pending_state(normalized_user_id)
            return ConfirmPendingMealResult(
                status="expired",
                record=payload.record,
            )

        sqlite_id, duplicate = self.save_record(
            user_id=normalized_user_id,
            source=payload.source,
            record=payload.record,
            image_ref=payload.image_ref,
            occurred_at=payload.occurred_at,
        )
        self.clear_pending_state(normalized_user_id)
        return ConfirmPendingMealResult(
            status="duplicate" if duplicate else "saved",
            record=payload.record,
            sqlite_id=sqlite_id,
            duplicate=duplicate,
        )

    @staticmethod
    def is_pending_meal_expired(
        payload: PendingMealPayload | PendingFoodVisionInventory,
        *,
        now: datetime | None = None,
    ) -> bool:
        if payload.expires_at is None:
            return False
        return _normalize_timestamp(now) > _normalize_timestamp(payload.expires_at)

    def _deserialize_pending_state(
        self,
        user_id: int,
        *,
        payload_json: str,
        created_at: str,
        expires_at: str,
    ) -> PendingMealPayload | PendingFoodVisionInventory | None:
        payload = safe_json_loads(payload_json, None)
        if not isinstance(payload, dict):
            return None
        pending_kind = str(payload.get("pending_kind") or "")
        if pending_kind == PENDING_INVENTORY_KIND or payload.get("state") == PENDING_INVENTORY_STATE:
            return self._deserialize_pending_inventory(
                user_id,
                payload=payload,
                created_at=created_at,
                expires_at=expires_at,
            )
        return self._deserialize_pending_meal(
            user_id,
            payload=payload,
            created_at=created_at,
            expires_at=expires_at,
        )

    def _deserialize_pending_inventory(
        self,
        user_id: int,
        *,
        payload: dict[str, Any],
        created_at: str,
        expires_at: str,
    ) -> PendingFoodVisionInventory | None:
        items_payload = payload.get("items")
        if not isinstance(items_payload, list) or not items_payload:
            return None
        items: list[PendingFoodVisionItem] = []
        for position, raw_item in enumerate(items_payload, start=1):
            if not isinstance(raw_item, dict):
                return None
            visible_name = _normalize_inventory_item_name(raw_item.get("visible_name"), fallback="")
            normalized_name = _normalize_inventory_item_name(raw_item.get("normalized_name") or visible_name.casefold(), fallback="")
            if not visible_name or not normalized_name:
                return None
            items.append(
                PendingFoodVisionItem(
                    index=int(raw_item.get("index") or position),
                    visible_name=visible_name,
                    normalized_name=normalized_name,
                    confidence=_coerce_confidence(raw_item.get("confidence")),
                    estimated_grams_min=_to_float(raw_item.get("estimated_grams_min")),
                    estimated_grams_max=_to_float(raw_item.get("estimated_grams_max")),
                    selected_grams=_parse_selected_grams(raw_item.get("selected_grams")),
                    preparation=_bounded_text(raw_item.get("preparation", ""), max_length=_MAX_FOOD_VISION_PREPARATION_LENGTH) or "",
                    is_sauce=bool(raw_item.get("is_sauce")),
                    uncertainty=_bounded_text(raw_item.get("uncertainty", ""), max_length=_MAX_FOOD_VISION_UNCERTAINTY_LENGTH) or "",
                    user_modified=bool(raw_item.get("user_modified")),
                )
            )
        return PendingFoodVisionInventory(
            user_id=int(user_id),
            inventory_id=_normalize_meal_name_text(payload.get("inventory_id") or "", "") or hashlib.sha1(f"{user_id}:{created_at}".encode("utf-8")).hexdigest()[:12],
            schema_version=str(payload.get("schema_version") or FOOD_VISION_SCHEMA_VERSION),
            items=_reindex_pending_inventory_items(items),
            overall_confidence=_coerce_confidence(payload.get("overall_confidence")),
            warnings=[_normalize_meal_name_text(warning, "") for warning in list(payload.get("warnings") or []) if _normalize_meal_name_text(warning, "")],
            created_at=_parse_sqlite_timestamp(created_at),
            expires_at=_parse_sqlite_timestamp(expires_at),
            source=str(payload.get("source") or "vision"),
            state=str(payload.get("state") or PENDING_INVENTORY_STATE),
            image_ref=str(payload.get("image_ref")) if payload.get("image_ref") is not None else None,
            occurred_at=_parse_sqlite_timestamp(payload.get("occurred_at")),
        )

    def _deserialize_pending_meal(
        self,
        user_id: int,
        *,
        payload: dict[str, Any],
        created_at: str,
        expires_at: str,
    ) -> PendingMealPayload | None:
        record_payload = payload.get("record")
        if not isinstance(record_payload, dict):
            return None
        record = NutritionRecord(
            is_food=bool(record_payload.get("is_food")),
            meal_name=_normalize_meal_name_text(record_payload.get("meal_name") or "Meal", "Meal"),
            items=list(record_payload.get("items") or []),
            calories_kcal=_to_float(record_payload.get("calories_kcal")),
            protein_g=_to_float(record_payload.get("protein_g")),
            fat_g=_to_float(record_payload.get("fat_g")),
            carbs_g=_to_float(record_payload.get("carbs_g")),
            confidence=_coerce_confidence(record_payload.get("confidence")),
            raw_summary=str(record_payload.get("raw_summary") or record_payload.get("meal_name") or "Meal"),
            display_name=_normalize_meal_name_text(
                record_payload.get("display_name") or record_payload.get("meal_name") or "\u0411\u043b\u044e\u0434\u043e",
                "\u0411\u043b\u044e\u0434\u043e",
            ),
        )
        return PendingMealPayload(
            user_id=int(user_id),
            source=str(payload.get("source") or "photo"),
            record=record,
            image_ref=str(payload.get("image_ref")) if payload.get("image_ref") is not None else None,
            occurred_at=_parse_sqlite_timestamp(payload.get("occurred_at")),
            created_at=_parse_sqlite_timestamp(created_at),
            expires_at=_parse_sqlite_timestamp(expires_at),
        )

    def save_record(
        self,
        *,
        user_id: int,
        source: str,
        record: NutritionRecord,
        image_ref: str | None,
        occurred_at: datetime | None,
    ) -> tuple[int | None, bool]:
        normalized_user_id = int(user_id)
        if image_ref:
            with self._connect() as conn:
                existing = conn.execute(
                    f"SELECT id FROM {NUTRITION_LOG_TABLE} WHERE user_id = ? AND image_ref = ? ORDER BY id DESC LIMIT 1",
                    (normalized_user_id, image_ref),
                ).fetchone()
                if existing is not None:
                    return int(existing["id"]), True

        occurred_at_sql = _sqlite_timestamp(occurred_at)
        stored_meal_name = _record_display_name(record)
        items_json = json.dumps(record.items, ensure_ascii=False)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                INSERT INTO {NUTRITION_LOG_TABLE}(
                    user_id,
                    source,
                    meal_name,
                    items_json,
                    calories_kcal,
                    protein_g,
                    fat_g,
                    carbs_g,
                    confidence,
                    occurred_at,
                    raw_summary,
                    image_ref,
                    qdrant_indexed
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    normalized_user_id,
                    source,
                    stored_meal_name,
                    items_json,
                    record.calories_kcal,
                    record.protein_g,
                    record.fat_g,
                    record.carbs_g,
                    record.confidence,
                    occurred_at_sql,
                    record.raw_summary,
                    image_ref,
                ),
            )
            sqlite_id = int(cursor.lastrowid)
        self._schedule_qdrant_index(
            sqlite_id=sqlite_id,
            user_id=normalized_user_id,
            record=record,
            occurred_at=occurred_at_sql,
        )
        return sqlite_id, False

    def delete_last_meal(
        self,
        user_id: str | int,
        *,
        now: datetime | None = None,
    ) -> UndoMealResult:
        normalized_user_id = int(user_id)
        start_utc, end_utc = _local_day_window(now)
        params = (
            normalized_user_id,
            _sqlite_timestamp(start_utc),
            _sqlite_timestamp(end_utc),
        )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    id,
                    meal_name,
                    calories_kcal,
                    protein_g,
                    fat_g,
                    carbs_g,
                    occurred_at,
                    created_at
                FROM {NUTRITION_LOG_TABLE}
                WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return UndoMealResult(deleted=False)
            sqlite_id = int(row["id"])
            conn.execute(
                f"DELETE FROM {NUTRITION_LOG_TABLE} WHERE id = ? AND user_id = ?",
                (sqlite_id, normalized_user_id),
            )
        return UndoMealResult(
            deleted=True,
            sqlite_id=sqlite_id,
            meal_name=str(row["meal_name"] or "Блюдо"),
            calories_kcal=_to_float(row["calories_kcal"]),
            protein_g=_to_float(row["protein_g"]),
            fat_g=_to_float(row["fat_g"]),
            carbs_g=_to_float(row["carbs_g"]),
            occurred_at=str(row["occurred_at"] or ""),
        )

    def update_last_meal(
        self,
        user_id: str,
        new_meal_name: str = None,
        new_calories: int = None,
        new_protein: float = None,
        new_fat: float = None,
        new_carbs: float = None,
        *,
        now: datetime | None = None,
    ) -> UpdateMealResult:
        normalized_user_id = int(user_id)
        normalized_meal_name = (new_meal_name or "").strip() or None

        def _normalize_numeric(value: Any, field_name: str) -> float | None:
            if value is None:
                return None
            numeric = _to_float(value)
            if numeric is None:
                raise ValueError(f"{field_name} must be a number.")
            return numeric

        calories_value = _normalize_numeric(new_calories, "new_calories")
        protein_value = _normalize_numeric(new_protein, "new_protein")
        fat_value = _normalize_numeric(new_fat, "new_fat")
        carbs_value = _normalize_numeric(new_carbs, "new_carbs")

        if all(
            value is None
            for value in (
                normalized_meal_name,
                calories_value,
                protein_value,
                fat_value,
                carbs_value,
            )
        ):
            raise ValueError("Provide at least one field to update.")

        start_utc, end_utc = _local_day_window(now)
        params = (
            normalized_user_id,
            _sqlite_timestamp(start_utc),
            _sqlite_timestamp(end_utc),
        )
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT
                    id,
                    meal_name,
                    items_json,
                    calories_kcal,
                    protein_g,
                    fat_g,
                    carbs_g,
                    confidence,
                    occurred_at,
                    raw_summary,
                    created_at
                FROM {NUTRITION_LOG_TABLE}
                WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                return UpdateMealResult(updated=False)

            sqlite_id = int(row["id"])
            updated_meal_name = _normalize_meal_name_text(
                normalized_meal_name or str(row["meal_name"] or "\u0411\u043b\u044e\u0434\u043e"),
                "\u0411\u043b\u044e\u0434\u043e",
            )
            updated_calories = calories_value if calories_value is not None else _to_float(row["calories_kcal"])
            updated_protein = protein_value if protein_value is not None else _to_float(row["protein_g"])
            updated_fat = fat_value if fat_value is not None else _to_float(row["fat_g"])
            updated_carbs = carbs_value if carbs_value is not None else _to_float(row["carbs_g"])
            occurred_at_sql = str(row["occurred_at"] or "")
            raw_summary = str(row["raw_summary"] or updated_meal_name)
            confidence = _coerce_confidence(row["confidence"])
            items_payload = safe_json_loads(str(row["items_json"] or "[]"), [])
            items = items_payload if isinstance(items_payload, list) else []

            conn.execute(
                f"""
                UPDATE {NUTRITION_LOG_TABLE}
                SET meal_name = ?,
                    calories_kcal = ?,
                    protein_g = ?,
                    fat_g = ?,
                    carbs_g = ?,
                    qdrant_indexed = 0
                WHERE id = ? AND user_id = ?
                """,
                (
                    updated_meal_name,
                    updated_calories,
                    updated_protein,
                    updated_fat,
                    updated_carbs,
                    sqlite_id,
                    normalized_user_id,
                ),
            )

        self._schedule_qdrant_index(
            sqlite_id=sqlite_id,
            user_id=normalized_user_id,
            record=NutritionRecord(
                is_food=True,
                meal_name=updated_meal_name,
                items=items,
                calories_kcal=updated_calories,
                protein_g=updated_protein,
                fat_g=updated_fat,
                carbs_g=updated_carbs,
                confidence=confidence,
                raw_summary=raw_summary,
                display_name=updated_meal_name,
            ),
            occurred_at=occurred_at_sql,
        )
        return UpdateMealResult(
            updated=True,
            sqlite_id=sqlite_id,
            meal_name=updated_meal_name,
            calories_kcal=updated_calories,
            protein_g=updated_protein,
            fat_g=updated_fat,
            carbs_g=updated_carbs,
            occurred_at=occurred_at_sql,
        )

    def _schedule_qdrant_index(
        self,
        *,
        sqlite_id: int,
        user_id: int,
        record: NutritionRecord,
        occurred_at: str,
    ) -> None:
        if self.qdrant_adapter is None:
            return
        if self._executor is None:
            self._push_to_qdrant(
                sqlite_id=sqlite_id,
                user_id=user_id,
                record=record,
                occurred_at=occurred_at,
            )
            return
        self._executor.submit(
            self._push_to_qdrant,
            sqlite_id=sqlite_id,
            user_id=user_id,
            record=record,
            occurred_at=occurred_at,
        )

    def _push_to_qdrant(
        self,
        *,
        sqlite_id: int,
        user_id: int,
        record: NutritionRecord,
        occurred_at: str,
    ) -> bool:
        if self.qdrant_adapter is None:
            return False
        display_name = _record_display_name(record)
        text = f"{display_name}. {record.raw_summary}".strip()
        payload = {
            "record_type": "nutrition_log",
            "occurred_at": occurred_at,
            "meal_name": display_name,
            "calories_kcal": record.calories_kcal,
        }
        indexed = self.qdrant_adapter.upsert_fact(
            sqlite_id=sqlite_id,
            user_id=user_id,
            text=text,
            payload=payload,
        )
        if indexed:
            try:
                with self._connect() as conn:
                    conn.execute(
                        f"UPDATE {NUTRITION_LOG_TABLE} SET qdrant_indexed = 1 WHERE id = ? AND user_id = ?",
                        (sqlite_id, user_id),
                    )
            except Exception as exc:
                logger.debug(
                    "Could not mark nutrition diary row %s as qdrant indexed: %s",
                    sqlite_id,
                    exc,
                )
        return indexed

    def get_summary(
        self,
        *,
        user_id: int,
        now: datetime | None = None,
        days: int = 1,
    ) -> dict[str, Any]:
        normalized_days = max(1, int(days))
        if normalized_days == 1:
            start_utc, end_utc = _local_day_window(now)
            period_key = "today"
            period_label = "\u0441\u0435\u0433\u043e\u0434\u043d\u044f"
        else:
            start_utc, end_utc = _rolling_window(normalized_days, now)
            period_key = f"{normalized_days}d"
            period_label = f"\u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0435 {normalized_days} \u0434\u043d\u0435\u0439"
        params = (int(user_id), _sqlite_timestamp(start_utc), _sqlite_timestamp(end_utc))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM {NUTRITION_LOG_TABLE}
                WHERE user_id = ? AND occurred_at >= ? AND occurred_at < ?
                ORDER BY occurred_at ASC, id ASC
                """,
                params,
            ).fetchall()
            targets = _load_nutrition_targets(conn, user_id=int(user_id))
        entries = [dict(row) for row in rows]
        calories_total = sum(float(row["calories_kcal"] or 0.0) for row in rows)
        protein_total = sum(float(row["protein_g"] or 0.0) for row in rows)
        fat_total = sum(float(row["fat_g"] or 0.0) for row in rows)
        carbs_total = sum(float(row["carbs_g"] or 0.0) for row in rows)
        average_values = {
            "calories_kcal": calories_total / normalized_days,
            "protein_g": protein_total / normalized_days,
            "fat_g": fat_total / normalized_days,
            "carbs_g": carbs_total / normalized_days,
        }
        comparison_values = average_values if normalized_days > 1 else {
            "calories_kcal": calories_total,
            "protein_g": protein_total,
            "fat_g": fat_total,
            "carbs_g": carbs_total,
        }
        progress = _build_target_progress(comparison_values, targets)
        return {
            "entries": entries,
            "entry_count": len(entries),
            "calories_kcal": calories_total,
            "protein_g": protein_total,
            "fat_g": fat_total,
            "carbs_g": carbs_total,
            "days": normalized_days,
            "period_key": period_key,
            "period_label": period_label,
            "average_calories_kcal": average_values["calories_kcal"],
            "average_protein_g": average_values["protein_g"],
            "average_fat_g": average_values["fat_g"],
            "average_carbs_g": average_values["carbs_g"],
            "targets": {
                "calories_kcal": targets.calories_kcal,
                "protein_g": targets.protein_g,
                "fat_g": targets.fat_g,
                "carbs_g": targets.carbs_g,
            },
            "targets_available": targets.has_any(),
            "progress": progress,
            "comparison_values": comparison_values,
            "hints": _build_progress_hints(progress),
        }

    def get_daily_summary(self, *, user_id: int, now: datetime | None = None) -> dict[str, Any]:
        return self.get_summary(user_id=user_id, now=now, days=1)


def format_nutrition_diary_report(summary: dict[str, Any]) -> str:
    entries = summary.get("entries") or []
    days = max(1, int(summary.get("days") or 1))
    progress = summary.get("progress") or {}
    comparison_values = summary.get("comparison_values") or {
        "calories_kcal": float(summary.get("calories_kcal") or 0.0),
        "protein_g": float(summary.get("protein_g") or 0.0),
        "fat_g": float(summary.get("fat_g") or 0.0),
        "carbs_g": float(summary.get("carbs_g") or 0.0),
    }
    no_target_hint = "\U0001f3af \u0426\u0435\u043b\u044c \u0435\u0449\u0451 \u043d\u0435 \u043d\u0430\u0441\u0442\u0440\u043e\u0435\u043d\u0430. \u0417\u0430\u043f\u043e\u043b\u043d\u0438\u0442\u0435 /profile, \u0447\u0442\u043e\u0431\u044b \u044f \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u043b \u043f\u0440\u043e\u0433\u0440\u0435\u0441\u0441."
    if not entries:
        if days == 1:
            message = "\U0001f4ca <b>\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u0437\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u043f\u043e\u043a\u0430 \u043f\u0443\u0441\u0442.</b>"
        else:
            message = f"\U0001f4ca <b>\u0422\u0432\u043e\u044f \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0437\u0430 {days} \u0434\u043d\u0435\u0439 \u043f\u043e\u043a\u0430 \u043f\u0443\u0441\u0442\u0430.</b>"
        if not summary.get("targets_available"):
            message += "\n\n" + no_target_hint
        return message

    if days == 1:
        title = "\U0001f4ca <b>\u0422\u0432\u043e\u0439 \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u0437\u0430 \u0441\u0435\u0433\u043e\u0434\u043d\u044f:</b>"
    else:
        title = f"\U0001f4ca <b>\u0422\u0432\u043e\u044f \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430 \u0437\u0430 {days} \u0434\u043d\u0435\u0439:</b>"

    lines = [title]
    if days > 1:
        lines.append("\U0001f4c8 <b>\u0412 \u0441\u0440\u0435\u0434\u043d\u0435\u043c \u0437\u0430 \u0434\u0435\u043d\u044c:</b>")
    lines.extend(
        [
            _format_progress_line(
                emoji="\U0001f525",
                label="\u041a\u0430\u043b\u043e\u0440\u0438\u0438",
                metric_name="calories_kcal",
                current_value=float(comparison_values.get("calories_kcal") or 0.0),
                progress=progress,
            ),
            _format_progress_line(
                emoji="\U0001f969",
                label="\u0411\u0435\u043b\u043a\u0438",
                metric_name="protein_g",
                current_value=float(comparison_values.get("protein_g") or 0.0),
                progress=progress,
            ),
            _format_progress_line(
                emoji="\U0001f9c8",
                label="\u0416\u0438\u0440\u044b",
                metric_name="fat_g",
                current_value=float(comparison_values.get("fat_g") or 0.0),
                progress=progress,
            ),
            _format_progress_line(
                emoji="\U0001f35e",
                label="\u0423\u0433\u043b\u0435\u0432\u043e\u0434\u044b",
                metric_name="carbs_g",
                current_value=float(comparison_values.get("carbs_g") or 0.0),
                progress=progress,
            ),
        ]
    )
    if days > 1:
        lines.extend(
            [
                "",
                f"\U0001f4e6 <b>\u0418\u0442\u043e\u0433\u043e \u0437\u0430 {days} \u0434\u043d\u0435\u0439:</b>",
                f"\U0001f525 \u041a\u0430\u043b\u043e\u0440\u0438\u0438: {_format_kcal(summary.get('calories_kcal'))}",
                f"\U0001f969 \u0411\u0435\u043b\u043a\u0438: {_format_grams(summary.get('protein_g'))}",
                f"\U0001f9c8 \u0416\u0438\u0440\u044b: {_format_grams(summary.get('fat_g'))}",
                f"\U0001f35e \u0423\u0433\u043b\u0435\u0432\u043e\u0434\u044b: {_format_grams(summary.get('carbs_g'))}",
            ]
        )
    if not summary.get("targets_available"):
        lines.extend(["", no_target_hint])
    hints = summary.get("hints") or []
    if hints:
        lines.extend(["", "\U0001f4a1 <b>\u041f\u043e\u0434\u0441\u043a\u0430\u0437\u043a\u0438:</b>"])
        lines.extend(f"\u2022 {html.escape(str(hint))}" for hint in hints)
    lines.extend(["", "\U0001f37d <b>\u041f\u0440\u0438\u0435\u043c\u044b \u043f\u0438\u0449\u0438:</b>"])
    for row in entries:
        meal_name = html.escape(localized_meal_display_name(row, "\u0411\u043b\u044e\u0434\u043e"))
        kcal = _format_kcal(row.get("calories_kcal"))
        lines.append(f"\u2022 {meal_name} (~{kcal})")
    return "\n".join(lines)


def format_undo_meal_report(result: UndoMealResult) -> str:
    if not result.deleted:
        return "\u0423\u0434\u0430\u043b\u044f\u0442\u044c \u043d\u0435\u0447\u0435\u0433\u043e \u2014 \u0441\u0435\u0433\u043e\u0434\u043d\u044f \u0434\u043d\u0435\u0432\u043d\u0438\u043a \u043f\u0443\u0441\u0442."

    meal_name = html.escape(localized_meal_display_name(result.meal_name, "\u0411\u043b\u044e\u0434\u043e"))
    return "\n".join(
        [
            f"\U0001f5d1 \u0417\u0430\u043f\u0438\u0441\u044c <b>{meal_name}</b> \u0443\u0434\u0430\u043b\u0435\u043d\u0430.",
            (
                f"\u0411\u044b\u043b\u043e: {_format_kcal(result.calories_kcal)} \u00b7 "
                f"\u0411 {_format_grams(result.protein_g)} \u00b7 "
                f"\u0416 {_format_grams(result.fat_g)} \u00b7 "
                f"\u0423 {_format_grams(result.carbs_g)}"
            ),
            "",
            "\u0412\u044b\u0437\u043e\u0432\u0438 /diary, \u0447\u0442\u043e\u0431\u044b \u043f\u043e\u0441\u043c\u043e\u0442\u0440\u0435\u0442\u044c \u043e\u0431\u043d\u043e\u0432\u043b\u0451\u043d\u043d\u044b\u0439 \u0438\u0442\u043e\u0433.",
        ]
    )


def format_pending_meal_prompt(record: NutritionRecord) -> str:
    component_items = [
        item
        for item in record.items
        if isinstance(item, dict)
        and item.get("name")
        and item.get("estimated_weight_g") is not None
        and item.get("calories_kcal") is not None
    ]
    if component_items:
        lines = ["\U0001f37d \u042f \u0432\u0438\u0436\u0443 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d\u043d\u044b\u0439 \u0441\u043e\u0441\u0442\u0430\u0432:", ""]
        for item in component_items:
            lines.append(
                f"\u2022 {html.escape(str(item.get('name')))}, {_format_grams(item.get('estimated_weight_g'))} \u00b7 {_format_kcal(item.get('calories_kcal'))} \u00b7 "
                f"\u0411 {_format_grams(item.get('protein_g'))} \u00b7 \u0416 {_format_grams(item.get('fat_g'))} \u00b7 \u0423 {_format_grams(item.get('carbs_g'))}"
            )
        lines.extend([
            "",
            f"\u0418\u0442\u043e\u0433\u043e: {_format_kcal(record.calories_kcal)} \u00b7 \u0411 {_format_grams(record.protein_g)} \u00b7 \u0416 {_format_grams(record.fat_g)} \u00b7 \u0423 {_format_grams(record.carbs_g)}",
            "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a?",
            "",
            "\u041e\u0442\u0432\u0435\u0442\u044c\u0442\u0435: \u0414\u0430 \u0438\u043b\u0438 \u041d\u0435\u0442.",
        ])
        return "\n".join(lines)

    meal_name = html.escape(_record_display_name(record))
    calories = _format_kcal(record.calories_kcal) if record.calories_kcal is not None else "\u043e\u0446\u0435\u043d\u043a\u0430 \u043a\u0430\u043b\u043e\u0440\u0438\u0439 \u043f\u043e\u043a\u0430 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430"
    macro_parts: list[str] = []
    if record.protein_g is not None:
        macro_parts.append(f"\u0411 {_format_grams(record.protein_g)}")
    if record.fat_g is not None:
        macro_parts.append(f"\u0416 {_format_grams(record.fat_g)}")
    if record.carbs_g is not None:
        macro_parts.append(f"\u0423 {_format_grams(record.carbs_g)}")
    lines = [f"\U0001f37d \u042f \u0432\u0438\u0436\u0443: {meal_name}.", f"\u041e\u0446\u0435\u043d\u043a\u0430: \u043f\u0440\u0438\u043c\u0435\u0440\u043d\u043e {calories}"]
    if macro_parts:
        lines.append(" \u00b7 ".join(macro_parts))
    lines.extend(["", "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0432 \u0434\u043d\u0435\u0432\u043d\u0438\u043a?", "\u041e\u0442\u0432\u0435\u0442\u044c\u0442\u0435: \u0414\u0430 \u0438\u043b\u0438 \u041d\u0435\u0442."])
    return "\n".join(lines)


def format_pending_meal_saved_reply(
    summary: dict[str, Any],
    *,
    duplicate: bool = False,
) -> str:
    prefix = "ℹ️ Эта запись уже есть в дневнике." if duplicate else "✅ Сохранено."
    return (
        f"{prefix} Итог за день: {_format_kcal(summary.get('calories_kcal'))} · "
        f"Б {_format_grams(summary.get('protein_g'))} · "
        f"Ж {_format_grams(summary.get('fat_g'))} · "
        f"У {_format_grams(summary.get('carbs_g'))}"
    )


def format_pending_meal_cancelled_reply() -> str:
    return "❌ Отменено. Что исправить?"


def format_pending_meal_wait_reply() -> str:
    return (
        "Жду подтверждение записи. Напиши Да или Нет. "
        "Если передумал, отправь новое фото или вызови /diary."
    )


def format_pending_meal_expired_reply() -> str:
    return "⌛ Подтверждение истекло. Отправь фото ещё раз."


def load_nutrition_targets(
    db_path: str | Path | None = None,
    *,
    user_id: int,
) -> NutritionTargets:
    resolved_db_path = resolve_healbite_db_path(db_path)
    conn = sqlite3.connect(resolved_db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        return _load_nutrition_targets(conn, user_id=int(user_id))
    finally:
        conn.close()


def compute_nutrition_diary_summary(
    db_path: str | Path | None = None,
    *,
    user_id: int,
    now: datetime | None = None,
    days: int = 1,
) -> dict[str, Any]:
    diary = HealBiteNutritionDiary(db_path=db_path, background_write=False)
    return diary.get_summary(user_id=user_id, now=now, days=days)


def get_existing_nutrition_diary() -> HealBiteNutritionDiary | None:
    with _GLOBAL_DIARY_LOCK:
        return _GLOBAL_DIARY


def get_default_nutrition_diary() -> HealBiteNutritionDiary:
    global _GLOBAL_DIARY
    with _GLOBAL_DIARY_LOCK:
        if _GLOBAL_DIARY is None:
            _GLOBAL_DIARY = HealBiteNutritionDiary()
            atexit.register(_GLOBAL_DIARY.close)
        return _GLOBAL_DIARY
