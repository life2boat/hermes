#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

import yaml

from gateway.config import Platform, load_gateway_config
from gateway.healbite_feature_gates import load_feature_gate_config
from gateway.healbite_nutrition_diary import NUTRITION_LOG_TABLE, resolve_healbite_db_path
from gateway.healbite_shopping_schema import (
    SHOPPING_IDEMPOTENCY_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_LISTS_TABLE,
    ShoppingSchemaState,
    detect_shopping_schema_state,
)
from gateway.healbite_user_profile import USERS_TABLE
from gateway.healbite_water_tracker import WATER_INTAKE_TABLE, WATER_PENDING_TABLE
from gateway.healbite_weight_reminder_schema import WEIGHT_REMINDER_CANONICAL_TABLES
from gateway.healbite_weight_tracker import WEIGHT_ENTRIES_TABLE, WEIGHT_PENDING_TABLE
from gateway.healbite_weekly_menu_schema import (
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_SERIES_TABLE,
    WeeklyMenuSchemaState,
    detect_weekly_menu_schema_state,
)
from gateway.slash_access import policy_from_extra
from hermes_cli.config import get_config_path, get_hermes_home

_WEEKLY_TABLES = (
    WEEKLY_MENU_SERIES_TABLE,
    WEEKLY_MENU_REVISIONS_TABLE,
    WEEKLY_MENU_ENTRIES_TABLE,
    WEEKLY_MENU_IDEMPOTENCY_TABLE,
)
_SHOPPING_TABLES = (
    SHOPPING_LISTS_TABLE,
    SHOPPING_ITEMS_TABLE,
    SHOPPING_IDEMPOTENCY_TABLE,
)
_OPTIONAL_HEALTH_TABLES = (
    USERS_TABLE,
    WATER_INTAKE_TABLE,
    WATER_PENDING_TABLE,
    WEIGHT_ENTRIES_TABLE,
    WEIGHT_PENDING_TABLE,
    *WEIGHT_REMINDER_CANONICAL_TABLES,
)
_KNOWN_VISION_MAIN_PROVIDERS = frozenset(
    {
        "anthropic",
        "copilot",
        "gemini",
        "gmi",
        "minimax",
        "minimax-cn",
        "nous",
        "openai",
        "openai-codex",
        "openrouter",
        "tencent-tokenhub",
        "xai",
        "xiaomi",
        "zai",
    }
)
_TEXT_ONLY_MAIN_PROVIDERS = frozenset({"deepseek", "deepseek-chat", "deepseek-reasoner"})
_VISION_CAPABILITY_PROVIDER_KEYS = frozenset(
    {
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "GLM_API_KEY",
        "ZAI_API_KEY",
        "NOUS_API_KEY",
    }
)


class StatusDBError(RuntimeError):
    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True, slots=True)
class ProviderMarkerSnapshot:
    provider_calls: int
    provider_call_failures: int
    provider_auth_failures: int
    provider_unavailable_without_call: int
    provider_not_configured: int
    generation_calls: int

    def as_dict(self) -> dict[str, int]:
        return {
            "provider_calls": self.provider_calls,
            "provider_call_failures": self.provider_call_failures,
            "provider_auth_failures": self.provider_auth_failures,
            "provider_unavailable_without_call": self.provider_unavailable_without_call,
            "provider_not_configured": self.provider_not_configured,
            "generation_calls": self.generation_calls,
        }


def classify_provider_markers(
    *,
    provider_call_attempted: bool,
    provider_call_failed: bool = False,
    provider_auth_failed: bool = False,
    provider_available: bool | None = None,
    provider_configured: bool | None = None,
    generation_invoked: bool = False,
) -> ProviderMarkerSnapshot:
    provider_calls = 1 if provider_call_attempted else 0
    call_failed = provider_call_attempted and provider_call_failed
    auth_failed = provider_call_attempted and provider_auth_failed
    unavailable_without_call = 0
    not_configured = 0
    if not provider_call_attempted and provider_available is False:
        if provider_configured is False:
            not_configured = 1
        else:
            unavailable_without_call = 1
    return ProviderMarkerSnapshot(
        provider_calls=provider_calls,
        provider_call_failures=1 if call_failed else 0,
        provider_auth_failures=1 if auth_failed else 0,
        provider_unavailable_without_call=unavailable_without_call,
        provider_not_configured=not_configured,
        generation_calls=1 if generation_invoked else 0,
    )


def infer_provider_configuration_state(
    *,
    model_provider: str | None,
    vision_provider: str | None,
    env: Mapping[str, str] | None = None,
) -> bool:
    source = env if env is not None else os.environ
    explicit = (vision_provider or "").strip().lower()
    if explicit not in {"", "unknown", "auto", "none"}:
        return True
    main_provider = (model_provider or "").strip().lower()
    if main_provider in _KNOWN_VISION_MAIN_PROVIDERS:
        return True
    if main_provider in _TEXT_ONLY_MAIN_PROVIDERS:
        return False
    return any(bool(source.get(key)) for key in _VISION_CAPABILITY_PROVIDER_KEYS)


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] if row else 0)


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _count_if_table(conn: sqlite3.Connection, table_name: str) -> int:
    if not _table_exists(conn, table_name):
        return 0
    escaped = table_name.replace('"', '""')
    return _safe_scalar(conn, f'SELECT COUNT(*) FROM "{escaped}"')


def validate_status_db_path(db_path: str | Path) -> Path:
    raw = str(db_path).strip()
    if not raw:
        raise StatusDBError("DB_PATH_EMPTY", "status requires a non-empty DB path")
    lowered = raw.lower()
    if lowered.startswith("file:") or "://" in raw or any(token in raw for token in ("?", "&", "#")):
        raise StatusDBError("DB_PATH_UNSAFE", "status rejected an unsafe DB path")
    path = Path(raw)
    if not path.exists():
        raise StatusDBError("DB_PATH_MISSING", "status DB path does not exist")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise StatusDBError("DB_PATH_NOT_REGULAR", "status DB path must point to a regular file")
    return resolved


def readonly_sqlite_uri(db_path: str | Path) -> str:
    resolved = validate_status_db_path(db_path)
    return f"file:{quote(str(resolved), safe='/')}?mode=ro"


def _raise_mapped_db_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        raise StatusDBError("DB_PERMISSION_DENIED", "status could not read the DB") from exc
    if isinstance(exc, sqlite3.DatabaseError):
        message = str(exc).lower()
        if "locked" in message:
            raise StatusDBError("DB_LOCKED", "status could not read the DB because it is locked") from exc
        if "not a database" in message or "file is not a database" in message:
            raise StatusDBError("DB_INVALID_SQLITE", "status DB file is not a valid SQLite database") from exc
        if "unable to open database file" in message:
            raise StatusDBError("DB_PERMISSION_DENIED", "status could not read the DB") from exc
        raise StatusDBError("DB_DATABASE_ERROR", "status could not inspect the DB safely") from exc
    if isinstance(exc, OSError):
        raise StatusDBError("DB_PERMISSION_DENIED", "status could not read the DB") from exc
    raise exc


def open_read_only_db(db_path: str | Path) -> sqlite3.Connection:
    uri = readonly_sqlite_uri(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only=ON")
        return conn
    except Exception as exc:
        _raise_mapped_db_error(exc)


def inspect_db_connection(conn: sqlite3.Connection, *, db_path: str | Path) -> dict[str, Any]:
    path = validate_status_db_path(db_path)
    conn.execute("PRAGMA query_only=ON")
    integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    foreign_key_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    weekly_state = detect_weekly_menu_schema_state(conn).value
    shopping_state = detect_shopping_schema_state(conn).value
    optional_health_table_presence = {
        table_name: _table_exists(conn, table_name) for table_name in _OPTIONAL_HEALTH_TABLES
    }
    result = {
        "db_path_exists": True,
        "db_path_regular": True,
        "db_access_mode": "read_only",
        "query_only": True,
        "integrity_check": integrity,
        "foreign_key_check_rows": len(foreign_key_rows),
        "households": _count_if_table(conn, "households"),
        "household_members": _count_if_table(conn, "household_members"),
        "nutrition_log_count": _count_if_table(conn, NUTRITION_LOG_TABLE),
        "profiles_table_present": _table_exists(conn, USERS_TABLE),
        "weekly_schema": weekly_state,
        "shopping_schema": shopping_state,
        "weekly_tables": sum(1 for name in _WEEKLY_TABLES if _table_exists(conn, name)),
        "shopping_tables": sum(1 for name in _SHOPPING_TABLES if _table_exists(conn, name)),
        "weekly_business_rows": sum(_count_if_table(conn, name) for name in _WEEKLY_TABLES),
        "shopping_business_rows": sum(_count_if_table(conn, name) for name in _SHOPPING_TABLES),
        "water_tables_present": sum(1 for name in (WATER_INTAKE_TABLE, WATER_PENDING_TABLE) if _table_exists(conn, name)),
        "weight_tables_present": sum(1 for name in (WEIGHT_ENTRIES_TABLE, WEIGHT_PENDING_TABLE) if _table_exists(conn, name)),
        "reminder_tables_present": sum(1 for name in WEIGHT_REMINDER_CANONICAL_TABLES if _table_exists(conn, name)),
        "optional_health_table_presence": optional_health_table_presence,
        "sqlite_total_changes": conn.total_changes,
        "db_file_size": path.stat().st_size,
        "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
        "user_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "application_id": int(conn.execute("PRAGMA application_id").fetchone()[0]),
    }
    return result


def inspect_db_path(db_path: str | Path) -> dict[str, Any]:
    with open_read_only_db(db_path) as conn:
        try:
            return inspect_db_connection(conn, db_path=db_path)
        except Exception as exc:
            _raise_mapped_db_error(exc)


def collect_status_snapshot(
    *,
    db_path: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    config_loader: Callable[[], Any] = load_gateway_config,
    check_vision_fn: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    source = dict(os.environ if env is None else env)
    config_path = Path(get_config_path())
    raw = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    model_cfg = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    aux_cfg = raw.get("auxiliary") if isinstance(raw.get("auxiliary"), dict) else {}
    vision_cfg = aux_cfg.get("vision") if isinstance(aux_cfg.get("vision"), dict) else {}
    model_provider = str(model_cfg.get("provider") or "unknown")
    model_default = str(model_cfg.get("default") or model_cfg.get("model") or "unknown")
    vision_provider = str(vision_cfg.get("provider") or "unknown")
    vision_model = str(vision_cfg.get("model") or "unknown")

    config = config_loader()
    telegram_cfg = config.platforms.get(Platform.TELEGRAM)
    extra = getattr(telegram_cfg, "extra", {}) if telegram_cfg else {}
    dm_policy = policy_from_extra(extra, "dm")
    group_policy = policy_from_extra(extra, "group")
    admin_ids = set(dm_policy.admin_user_ids) | set(group_policy.admin_user_ids)

    target_db_path = Path(db_path) if db_path is not None else resolve_healbite_db_path()
    db_report = inspect_db_path(target_db_path)
    weekly_gate = load_feature_gate_config("HEALBITE_WEEKLY_MENU", source)
    shopping_gate = load_feature_gate_config("HEALBITE_SHOPPING_LIST", source)
    vision_ready = bool(check_vision_fn()) if check_vision_fn is not None else False
    provider_configured = infer_provider_configuration_state(
        model_provider=model_provider,
        vision_provider=vision_provider,
        env=source,
    )
    provider_metrics = classify_provider_markers(
        provider_call_attempted=False,
        provider_available=vision_ready,
        provider_configured=provider_configured,
        generation_invoked=False,
    )
    provider_status_category = (
        "ready"
        if vision_ready
        else "provider_unavailable_without_call"
        if provider_metrics.provider_unavailable_without_call
        else "provider_not_configured"
        if provider_metrics.provider_not_configured
        else "unknown"
    )

    return {
        "runtime": {
            "hermes_home_present": bool(get_hermes_home()),
            "config_present": config_path.exists(),
            "env_present": (Path(get_hermes_home()) / ".env").exists(),
            "model_provider": model_provider,
            "model_default": model_default,
            "vision_provider": vision_provider,
            "vision_model": vision_model,
            "vision_ready": vision_ready,
            "provider_status_category": provider_status_category,
            **provider_metrics.as_dict(),
            "secret_presence": {
                "GEMINI_API_KEY": bool(source.get("GEMINI_API_KEY")),
                "DEEPSEEK_API_KEY": bool(source.get("DEEPSEEK_API_KEY")),
                "TELEGRAM_BOT_TOKEN": bool(source.get("TELEGRAM_BOT_TOKEN")),
            },
            "qdrant_presence": {
                "QDRANT_URL": bool(source.get("QDRANT_URL")),
                "QDRANT_API_KEY": bool(source.get("QDRANT_API_KEY")),
                "QDRANT_HOST": bool(source.get("QDRANT_HOST")),
                "QDRANT_PORT": bool(source.get("QDRANT_PORT")),
            },
            "admin_total_unique": len(admin_ids),
            "weekly_feature_enabled": weekly_gate.enabled,
            "shopping_feature_enabled": shopping_gate.enabled,
            "weekly_allowlist_count": len(weekly_gate.allowlist),
            "shopping_allowlist_count": len(shopping_gate.allowlist),
            "weekly_config_valid": weekly_gate.configuration_valid,
            "shopping_config_valid": shopping_gate.configuration_valid,
        },
        "db": db_report,
    }
