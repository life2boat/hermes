from __future__ import annotations

import atexit
import html
import json
import logging
import os
import sqlite3
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gateway.memory.embedding_adapter import EmbeddingAdapter
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

_VISION_PROMPT = (
    "You are a nutrition analyst for HealBite. Analyze the meal or drink in this image and "
    "return STRICT JSON only with no markdown fences and no extra text. "
    "Schema: {\"is_food\": bool, \"meal_name\": string, \"display_name\": string, \"raw_summary\": string, "
    "\"confidence\": number, \"items\": [{\"name\": string, \"estimated_weight_g\": number|null, "
    "\"calories_kcal\": number|null, \"protein_g\": number|null, \"fat_g\": number|null, "
    "\"carbs_g\": number|null}], \"totals\": {\"calories_kcal\": number|null, "
    "\"protein_g\": number|null, \"fat_g\": number|null, \"carbs_g\": number|null}}. "
    "Use natural Russian for all user-facing naming fields: meal_name, display_name, and item names. "
    "If the dish is uncertain, use cautious Russian wording like \"похоже на ...\" instead of inventing specifics. "
    "Avoid English dish names unless the image clearly shows a branded product name that cannot be translated naturally. "
    "If this is not a food or drink image, set is_food=false, use a short raw_summary, and keep items empty."
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
    local_now = now.astimezone() if now is not None else datetime.now().astimezone()
    start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


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
        record = normalize_nutrition_payload(raw_analysis)
        if record is None:
            logger.info("[HealBite][vision_parse_ok] ok=false reason=structured_json_missing")
            logger.info("[HealBite][vision_pending_staged] staged=false")
            return NutritionDiaryOutcome(available=False, raw_analysis=raw_analysis)

        logger.info(
            "[HealBite][vision_parse_ok] ok=true is_food=%s confidence_bucket=%s",
            str(record.is_food).lower(),
            _confidence_bucket(record.confidence),
        )
        outcome = NutritionDiaryOutcome(available=True, record=record, raw_analysis=raw_analysis)
        if self._should_stage_pending(record):
            self.stage_pending_meal(
                user_id=user_id,
                source=source,
                record=record,
                image_ref=image_ref,
                occurred_at=occurred_at,
            )
            outcome.pending = True
            logger.info("[HealBite][vision_pending_staged] staged=true")
        else:
            logger.info("[HealBite][vision_pending_staged] staged=false")
        return outcome

    def _should_stage_pending(self, record: NutritionRecord) -> bool:
        if not record.is_food:
            return False
        return any(
            value is not None
            for value in (record.calories_kcal, record.protein_g, record.fat_g, record.carbs_g)
        ) or bool(record.items)

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
        payload_json = json.dumps(
            {
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
                    payload.user_id,
                    payload_json,
                    _sqlite_timestamp(created_at_dt),
                    _sqlite_timestamp(expires_at_dt),
                ),
            )
        return payload

    def get_pending_meal(
        self,
        user_id: str | int,
        *,
        now: datetime | None = None,
        include_expired: bool = False,
    ) -> PendingMealPayload | None:
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
        payload = self._deserialize_pending_meal(
            normalized_user_id,
            payload_json=str(row["payload_json"] or ""),
            created_at=str(row["created_at"] or ""),
            expires_at=str(row["expires_at"] or ""),
        )
        if payload is None:
            self.clear_pending_meal(normalized_user_id)
            return None
        if not include_expired and self.is_pending_meal_expired(payload, now=now):
            self.clear_pending_meal(normalized_user_id)
            return None
        return payload

    def clear_pending_meal(self, user_id: str | int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM {PENDING_MEALS_TABLE} WHERE user_id = ?",
                (int(user_id),),
            )
        return bool(cursor.rowcount)

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
            self.clear_pending_meal(normalized_user_id)
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
        self.clear_pending_meal(normalized_user_id)
        return ConfirmPendingMealResult(
            status="duplicate" if duplicate else "saved",
            record=payload.record,
            sqlite_id=sqlite_id,
            duplicate=duplicate,
        )

    @staticmethod
    def is_pending_meal_expired(
        payload: PendingMealPayload,
        *,
        now: datetime | None = None,
    ) -> bool:
        if payload.expires_at is None:
            return False
        return _normalize_timestamp(now) > _normalize_timestamp(payload.expires_at)

    def _deserialize_pending_meal(
        self,
        user_id: int,
        *,
        payload_json: str,
        created_at: str,
        expires_at: str,
    ) -> PendingMealPayload | None:
        payload = safe_json_loads(payload_json, None)
        if not isinstance(payload, dict):
            return None
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
                record_payload.get("display_name") or record_payload.get("meal_name") or "Блюдо",
                "Блюдо",
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
    meal_name = html.escape(_record_display_name(record))
    calories = (
        _format_kcal(record.calories_kcal)
        if record.calories_kcal is not None
        else "без точной оценки калорийности"
    )
    macro_parts: list[str] = []
    if record.protein_g is not None:
        macro_parts.append(f"Б {_format_grams(record.protein_g)}")
    if record.fat_g is not None:
        macro_parts.append(f"Ж {_format_grams(record.fat_g)}")
    if record.carbs_g is not None:
        macro_parts.append(f"У {_format_grams(record.carbs_g)}")
    lines = [
        f"🍽 Я вижу: {meal_name}.",
        f"Оценка: примерно {calories}",
    ]
    if macro_parts:
        lines.append(" · ".join(macro_parts))
    lines.extend([
        "",
        "Сохранить в дневник?",
        "Ответьте: Да или Нет.",
    ])
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
