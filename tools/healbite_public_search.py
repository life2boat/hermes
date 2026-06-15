#!/usr/bin/env python3
"""Privacy-safe public nutrition search for HealBite."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from agent.web_search_registry import get_active_search_provider
from tools.registry import registry

logger = logging.getLogger(__name__)

Category = Literal["nutrition", "activity", "harvard_plate", "food_calories"]
DEFAULT_LANGUAGE = "ru"
CACHE_TTL_SECONDS = 24 * 60 * 60
MAX_TOPIC_LENGTH = 160
TOOL_NAME = "healbite_public_search"

ALLOWED_DOMAINS: dict[str, tuple[str, ...]] = {
    "food_calories": (
        "fdc.nal.usda.gov",
        "usda.gov",
        "nutrition.gov",
    ),
    "harvard_plate": (
        "nutritionsource.hsph.harvard.edu",
        "hsph.harvard.edu",
    ),
    "activity": (
        "nhs.uk",
        "cdc.gov",
        "nih.gov",
        "who.int",
    ),
    "nutrition": (
        "fdc.nal.usda.gov",
        "nutrition.gov",
        "nutritionsource.hsph.harvard.edu",
        "nhs.uk",
        "cdc.gov",
        "nih.gov",
        "who.int",
    ),
}

LOCAL_REFERENCE_KNOWLEDGE: dict[str, dict[str, Any]] = {
    "harvard_plate": {
        "summary": (
            "Healthy Eating Plate в публичной версии Harvard Plate предлагает "
            "заполнять половину тарелки овощами и фруктами, четверть — цельными "
            "злаками, четверть — источниками белка; в приоритете вода и полезные масла."
        ),
        "uncertainty": (
            "Низкая: это стабильная публичная рекомендация, "
            "но она не заменяет персональный план питания."
        ),
        "sources": [
            {
                "title": "Harvard T.H. Chan - Healthy Eating Plate",
                "url": "https://nutritionsource.hsph.harvard.edu/healthy-eating-plate/",
                "domain": "nutritionsource.hsph.harvard.edu",
            }
        ],
    },
    "activity": {
        "summary": (
            "Для расчёта суточной потребности в калориях обычно сначала оценивают "
            "BMR/TDEE, а затем применяют коэффициент активности: низкая активность "
            "около 1.2, лёгкая — 1.375, средняя — 1.55, высокая — 1.725. "
            "Это ориентир, а не медицинская норма."
        ),
        "uncertainty": (
            "Средняя: формулы дают оценку и требуют корректировки "
            "по динамике веса, самочувствию и нагрузке."
        ),
        "sources": [
            {
                "title": "NHS - Understanding calorie needs",
                "url": "https://www.nhs.uk/live-well/healthy-weight/managing-your-weight/understanding-calories/",
                "domain": "nhs.uk",
            },
            {
                "title": "CDC - Physical Activity Basics",
                "url": "https://www.cdc.gov/physical-activity-basics/index.html",
                "domain": "cdc.gov",
            },
        ],
    },
}

USER_ID_RE = re.compile(r"\b\d{6,}\b")
HANDLE_RE = re.compile(r"@\w+")
METRIC_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:kg|кг|lb|lbs|см|cm|рост|height|вес|weight|age|возраст|лет|years?|yo)\b",
    re.IGNORECASE,
)
DIAGNOSIS_RE = re.compile(
    r"\b(?:диабет(?:а|ом|е)?|diabetes|гастрит(?:а|ом|е)?|gastritis|язва|ulcer|гэрб|gerd|ибс|ibs|целиакия|celiac|панкреатит|pancreatitis)\b",
    re.IGNORECASE,
)
NAME_CLAUSE_RE = re.compile(
    r"\b(?:меня\s+зовут|my\s+name\s+is|i\s+am|i'm|я)\s+[A-Za-zА-Яа-яЁё-]+\b",
    re.IGNORECASE,
)
ID_CLAUSE_RE = re.compile(
    r"\b(?:telegram|tg|телеграм)\s*(?:id|айди)?\s*[:#-]?\s*\d+\b",
    re.IGNORECASE,
)
PERSONAL_CLAUSE_RE = re.compile(
    r"\b(?:мой|моя|моё|my|у\s+меня|i\s+have)\b[^,;.!?\n]{0,60}",
    re.IGNORECASE,
)
FOOD_FILLER_RE = re.compile(
    r"\b(?:калорийность|калории|ккал|кбжу|сколько\s+калорий(?:\s+в)?|nutrition|calories?|macros?|protein|fat|carbs?)\b",
    re.IGNORECASE,
)
CONCEPT_FILLER_RE = re.compile(
    r"\b(?:что\s+такое|как\s+считать|как\s+рассчитать|объясни|помоги|расскажи|please|tell\s+me|show\s+me)\b",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"\s+")


class PublicSearchUnavailable(RuntimeError):
    """Raised when the public search backend cannot serve the request."""


@dataclass(frozen=True)
class PublicNutritionSearchQuery:
    topic: str
    language: str = DEFAULT_LANGUAGE
    category: Category = "nutrition"

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> "PublicNutritionSearchQuery":
        raw_topic = str((args or {}).get("topic") or "").strip()
        raw_language = str((args or {}).get("language") or DEFAULT_LANGUAGE).strip().lower()
        raw_category = str((args or {}).get("category") or "nutrition").strip().lower()
        if raw_category not in ALLOWED_DOMAINS:
            raise ValueError(f"Unsupported category: {raw_category}")
        if not raw_topic:
            raise ValueError("topic is required")
        return cls(
            topic=raw_topic,
            language=raw_language or DEFAULT_LANGUAGE,
            category=raw_category,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class SanitizedQuery:
    topic: str
    normalized_query: str
    language: str
    category: Category
    removed_sensitive_data: bool


class PublicNutritionSearchCache:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS healbite_public_search_cache (
                    normalized_query TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    language TEXT NOT NULL,
                    result_summary TEXT NOT NULL,
                    sources_json TEXT NOT NULL,
                    uncertainty TEXT,
                    user_facing_text TEXT NOT NULL,
                    source_mode TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    ttl_seconds INTEGER NOT NULL
                )
                """
            )

    def get(self, normalized_query: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM healbite_public_search_cache
                WHERE normalized_query = ?
                """,
                (normalized_query,),
            ).fetchone()
            if row is None:
                return None
            expires_at = int(row["created_at"]) + int(row["ttl_seconds"])
            if expires_at <= now:
                conn.execute(
                    "DELETE FROM healbite_public_search_cache WHERE normalized_query = ?",
                    (normalized_query,),
                )
                return None
        payload = dict(row)
        payload["sources"] = json.loads(payload.pop("sources_json"))
        return payload

    def put(
        self,
        *,
        normalized_query: str,
        category: str,
        language: str,
        result_summary: str,
        sources: list[dict[str, str]],
        uncertainty: str,
        user_facing_text: str,
        source_mode: str,
        ttl_seconds: int = CACHE_TTL_SECONDS,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO healbite_public_search_cache(
                    normalized_query, category, language, result_summary, sources_json,
                    uncertainty, user_facing_text, source_mode, created_at, ttl_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_query) DO UPDATE SET
                    category = excluded.category,
                    language = excluded.language,
                    result_summary = excluded.result_summary,
                    sources_json = excluded.sources_json,
                    uncertainty = excluded.uncertainty,
                    user_facing_text = excluded.user_facing_text,
                    source_mode = excluded.source_mode,
                    created_at = excluded.created_at,
                    ttl_seconds = excluded.ttl_seconds
                """,
                (
                    normalized_query,
                    category,
                    language,
                    result_summary,
                    json.dumps(sources, ensure_ascii=False),
                    uncertainty,
                    user_facing_text,
                    source_mode,
                    int(time.time()),
                    ttl_seconds,
                ),
            )


def cache_path() -> Path:
    base = Path(os.getenv("HERMES_HOME") or "/opt/data")
    return base / "healbite_public_search.sqlite"


def log_event(event: str, **fields: Any) -> None:
    payload = {"event": event, **fields}
    logger.info("[healbite_public_search] %s", json.dumps(payload, ensure_ascii=False, sort_keys=True))


def safe_fallback_text(category: str) -> str:
    if category == "food_calories":
        return (
            "Не удалось получить публичную справку по продукту сейчас. "
            "Попробуйте уточнить название блюда или используйте внутренние данные дневника питания."
        )
    return (
        "Не удалось получить публичную справку сейчас. "
        "Попробуйте сформулировать запрос короче или вернуться к внутренним данным HealBite."
    )


def normalize_topic(topic: str, *, category: str) -> tuple[str, bool]:
    text = topic.strip()
    removed_sensitive = False
    for pattern in (
        HANDLE_RE,
        ID_CLAUSE_RE,
        USER_ID_RE,
        METRIC_RE,
        DIAGNOSIS_RE,
        NAME_CLAUSE_RE,
        PERSONAL_CLAUSE_RE,
    ):
        text, count = pattern.subn(" ", text)
        removed_sensitive = removed_sensitive or count > 0

    if category == "food_calories":
        text = FOOD_FILLER_RE.sub(" ", text)
    else:
        text = CONCEPT_FILLER_RE.sub(" ", text)

    text = text.replace("\n", " ")
    text = re.sub(r"[^0-9A-Za-zА-Яа-яЁё%+\-/(),. ]", " ", text)
    text = WHITESPACE_RE.sub(" ", text).strip(" ,.;:-")
    text = text[:MAX_TOPIC_LENGTH].strip()
    return text, removed_sensitive


def sanitize_public_query(query: PublicNutritionSearchQuery) -> SanitizedQuery:
    if len(query.topic.strip()) > MAX_TOPIC_LENGTH * 4:
        log_event("search_blocked_privacy", reason="raw_text_too_long", category=query.category)
        raise ValueError("raw conversation text is not allowed")

    normalized_topic, removed_sensitive = normalize_topic(query.topic, category=query.category)
    if not normalized_topic:
        log_event("search_blocked_privacy", reason="empty_after_sanitize", category=query.category)
        raise ValueError("query became empty after sanitization")

    normalized_query = f"{query.category}:{query.language}:{normalized_topic.lower()}"
    log_event(
        "search_sanitized",
        category=query.category,
        language=query.language,
        removed_sensitive_data=removed_sensitive,
    )
    return SanitizedQuery(
        topic=normalized_topic,
        normalized_query=normalized_query,
        language=query.language,
        category=query.category,
        removed_sensitive_data=removed_sensitive,
    )


def local_reference_result(query: SanitizedQuery) -> dict[str, Any] | None:
    if query.category == "harvard_plate":
        return dict(LOCAL_REFERENCE_KNOWLEDGE["harvard_plate"])
    if query.category == "activity":
        return dict(LOCAL_REFERENCE_KNOWLEDGE["activity"])
    if query.category == "nutrition":
        lowered = query.topic.lower()
        if "harvard" in lowered or "тарел" in lowered:
            return dict(LOCAL_REFERENCE_KNOWLEDGE["harvard_plate"])
        if any(token in lowered for token in ("актив", "tdee", "bmr", "ккал", "калори")):
            return dict(LOCAL_REFERENCE_KNOWLEDGE["activity"])
    return None


def source_domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().strip(".")


def is_allowed_domain(url: str, *, category: str) -> bool:
    domain = source_domain(url)
    for allowed in ALLOWED_DOMAINS.get(category, ()):
        if domain == allowed or domain.endswith(f".{allowed}"):
            return True
    return False


def build_search_query(query: SanitizedQuery, *, prefer_allowed: bool) -> str:
    topic = query.topic
    if query.category == "food_calories":
        if prefer_allowed:
            return f'site:fdc.nal.usda.gov "{topic}" nutrition calories'
        return f'"{topic}" nutrition calories'
    if query.category == "harvard_plate":
        if prefer_allowed:
            return 'site:nutritionsource.hsph.harvard.edu "healthy eating plate"'
        return '"healthy eating plate"'
    if query.category == "activity":
        if prefer_allowed:
            return f'"{topic}" calorie activity site:nhs.uk'
        return f'"{topic}" calorie activity reference'
    if prefer_allowed:
        return f'"{topic}" nutrition site:nutritionsource.hsph.harvard.edu'
    return f'"{topic}" nutrition reference'


def normalize_provider_results(payload: dict[str, Any]) -> list[dict[str, str]]:
    web_results = (((payload or {}).get("data") or {}).get("web") or [])
    normalized: list[dict[str, str]] = []
    for item in web_results:
        title = str((item or {}).get("title") or "").strip()
        url = str((item or {}).get("url") or "").strip()
        description = str((item or {}).get("description") or "").strip()
        if not url:
            continue
        normalized.append(
            {
                "title": title or url,
                "url": url,
                "description": description,
                "domain": source_domain(url),
            }
        )
    return normalized


def classify_provider_error(exc: Exception | str) -> str:
    message = str(exc).lower()
    if any(token in message for token in ("401", "403", "auth", "unauthorized", "api key", "forbidden")):
        return "auth"
    if any(token in message for token in ("quota", "rate", "limit", "429", "billing", "credit", "balance")):
        return "quota"
    if any(token in message for token in ("timeout", "timed out", "deadline")):
        return "timeout"
    if any(token in message for token in ("dns", "connection", "network", "unreachable", "refused")):
        return "network"
    return "provider_unavailable"


def search_public_sources(query: SanitizedQuery) -> list[dict[str, str]]:
    provider = get_active_search_provider()
    if provider is None:
        raise PublicSearchUnavailable("no_provider")

    attempts = [
        build_search_query(query, prefer_allowed=True),
        build_search_query(query, prefer_allowed=False),
    ]
    last_error: Exception | None = None
    for index, search_query in enumerate(attempts):
        try:
            result = provider.search(search_query, limit=5)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
        if not result or not result.get("success"):
            last_error = PublicSearchUnavailable((result or {}).get("error") or "provider_failed")
            continue
        normalized = normalize_provider_results(result)
        if not normalized:
            continue
        preferred = [
            item for item in normalized if is_allowed_domain(item["url"], category=query.category)
        ]
        if preferred:
            return preferred[:3]
        if index == 1:
            return normalized[:3]
    if last_error is not None:
        raise PublicSearchUnavailable(str(last_error))
    raise PublicSearchUnavailable("no_results")


def format_sources(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {
            "title": item.get("title", "Источник"),
            "url": item.get("url", ""),
            "domain": item.get("domain", ""),
        }
        for item in items
    ]


def compose_external_summary(query: SanitizedQuery, items: list[dict[str, str]]) -> tuple[str, str]:
    descriptions = [item.get("description", "").strip() for item in items if item.get("description")]
    summary = " ".join(descriptions[:2]) if descriptions else f"Найдены публичные материалы по теме '{query.topic}'."
    if query.category == "food_calories":
        user_text = (
            f"Это примерная публичная справка по '{query.topic}': {summary} "
            "Точная калорийность зависит от рецепта, бренда и порции."
        )
    else:
        user_text = (
            f"Нашёл публичную справку по теме '{query.topic}': {summary} "
            "Публичные рекомендации полезны как ориентир, но не заменяют персональный план."
        )
    return summary, user_text


def build_payload(
    *,
    ok: bool,
    query: SanitizedQuery,
    summary: str,
    user_facing_text: str,
    uncertainty: str,
    sources: list[dict[str, str]],
    source_mode: str,
    error_type: str | None = None,
) -> str:
    payload = {
        "ok": ok,
        "tool": TOOL_NAME,
        "category": query.category,
        "language": query.language,
        "sanitized_topic": query.topic,
        "privacy_status": "sanitized",
        "removed_sensitive_data": query.removed_sensitive_data,
        "source_mode": source_mode,
        "summary": summary,
        "uncertainty": uncertainty,
        "sources": sources,
        "user_facing_text": user_facing_text,
    }
    if error_type:
        payload["error_type"] = error_type
    return json.dumps(payload, ensure_ascii=False)


def check_healbite_public_search_requirements() -> bool:
    return True


def healbite_public_search_tool(
    topic: str,
    *,
    language: str = DEFAULT_LANGUAGE,
    category: Category = "nutrition",
) -> str:
    started_at = time.perf_counter()
    raw_query = PublicNutritionSearchQuery(topic=topic, language=language, category=category)
    log_event("search_requested", category=category, language=language)
    query = sanitize_public_query(raw_query)
    cache = PublicNutritionSearchCache(cache_path())

    cached = cache.get(query.normalized_query)
    if cached is not None:
        log_event(
            "search_cache_hit",
            category=query.category,
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
            source_count=len(cached.get("sources") or []),
        )
        return build_payload(
            ok=True,
            query=query,
            summary=str(cached.get("result_summary") or ""),
            user_facing_text=str(cached.get("user_facing_text") or ""),
            uncertainty=str(cached.get("uncertainty") or ""),
            sources=list(cached.get("sources") or []),
            source_mode=str(cached.get("source_mode") or "cache"),
        )

    local_result = local_reference_result(query)
    if local_result is not None:
        user_text = (
            f"Краткая публичная справка по теме '{query.topic}': {local_result['summary']} "
            f"{local_result['uncertainty']}"
        )
        cache.put(
            normalized_query=query.normalized_query,
            category=query.category,
            language=query.language,
            result_summary=local_result["summary"],
            sources=local_result["sources"],
            uncertainty=local_result["uncertainty"],
            user_facing_text=user_text,
            source_mode="internal",
        )
        log_event(
            "search_provider_success",
            category=query.category,
            source_mode="internal",
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
            source_count=len(local_result["sources"]),
        )
        return build_payload(
            ok=True,
            query=query,
            summary=local_result["summary"],
            user_facing_text=user_text,
            uncertainty=local_result["uncertainty"],
            sources=local_result["sources"],
            source_mode="internal",
        )

    try:
        items = search_public_sources(query)
        sources = format_sources(items)
        summary, user_text = compose_external_summary(query, items)
        uncertainty = (
            "Высокая: это примерная публичная оценка."
            if query.category == "food_calories"
            else "Средняя: это публичная справка, а не персональная рекомендация."
        )
        cache.put(
            normalized_query=query.normalized_query,
            category=query.category,
            language=query.language,
            result_summary=summary,
            sources=sources,
            uncertainty=uncertainty,
            user_facing_text=user_text,
            source_mode="external",
        )
        log_event(
            "search_provider_success",
            category=query.category,
            source_mode="external",
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
            source_count=len(sources),
        )
        return build_payload(
            ok=True,
            query=query,
            summary=summary,
            user_facing_text=user_text,
            uncertainty=uncertainty,
            sources=sources,
            source_mode="external",
        )
    except Exception as exc:  # noqa: BLE001
        error_type = classify_provider_error(exc)
        log_event(
            "search_provider_failed",
            category=query.category,
            error_type=error_type,
            latency_ms=round((time.perf_counter() - started_at) * 1000, 2),
        )
        return build_payload(
            ok=False,
            query=query,
            summary="",
            user_facing_text=safe_fallback_text(query.category),
            uncertainty="Недостаточно публичных данных для уверенного ответа.",
            sources=[],
            source_mode="unavailable",
            error_type=error_type,
        )


HEALBITE_PUBLIC_SEARCH_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Privacy-safe public nutrition search for generic facts only. "
        "Use it for rare food calories, Harvard Plate, activity/TDEE concepts, "
        "and public nutrition references when internal HealBite data is insufficient. "
        "Never include names, Telegram IDs, diagnoses, weight, height, age, "
        "location, or raw chat history in the topic."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Generic public topic only, such as a food name, dish name, method, or nutrition concept.",
            },
            "language": {
                "type": "string",
                "description": "Answer language hint. Defaults to 'ru'.",
                "default": DEFAULT_LANGUAGE,
            },
            "category": {
                "type": "string",
                "enum": ["nutrition", "activity", "harvard_plate", "food_calories"],
                "description": "Choose the narrowest category for the public lookup.",
                "default": "nutrition",
            },
        },
        "required": ["topic", "category"],
    },
}

registry.register(
    name=TOOL_NAME,
    toolset="nutrition_search",
    schema=HEALBITE_PUBLIC_SEARCH_SCHEMA,
    handler=lambda args, **kw: healbite_public_search_tool(
        str((args or {}).get("topic") or ""),
        language=str((args or {}).get("language") or DEFAULT_LANGUAGE),
        category=str((args or {}).get("category") or "nutrition"),
    ),
    check_fn=check_healbite_public_search_requirements,
    emoji="🥗",
    max_result_size_chars=20_000,
)
