#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.healbite_weight_reminder_schema import (
    WEIGHT_REMINDER_CANONICAL_TABLES,
    WEIGHT_REMINDER_DELIVERIES_TABLE,
    WEIGHT_REMINDER_SETTINGS_TABLE,
    assert_canonical_reminder_tables,
)

_SETTINGS_COLUMNS = {"enabled", "delivery_state"}
_DELIVERY_COLUMNS = {"status"}


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _scalar(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    return int(row[0] if row else 0)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()}


def audit_db_path(db_path: str | Path) -> dict[str, Any]:
    assert_canonical_reminder_tables(WEIGHT_REMINDER_CANONICAL_TABLES)
    path = Path(db_path)
    result: dict[str, Any] = {
        "audit_status": "failed",
        "db_exists": path.exists(),
        "settings_table_present": False,
        "deliveries_table_present": False,
        "schema_state": "unknown",
    }
    if not path.exists():
        result.update({"error_type": "DB_NOT_FOUND"})
        return result

    try:
        with _connect_read_only(path) as conn:
            conn.execute("PRAGMA query_only=ON")
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            result["integrity"] = integrity
            if integrity.lower() != "ok":
                result.update({"error_type": "SQLITE_INTEGRITY_CHECK_FAILED"})
                return result

            reminder_tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name LIKE 'weight_reminder_%' ORDER BY name"
                ).fetchall()
            ]
            result["reminder_tables"] = reminder_tables
            canonical = set(WEIGHT_REMINDER_CANONICAL_TABLES)
            present = set(reminder_tables)
            unknown = sorted(present.difference(canonical))
            result["unknown_reminder_tables"] = unknown
            settings_present = WEIGHT_REMINDER_SETTINGS_TABLE in present
            deliveries_present = WEIGHT_REMINDER_DELIVERIES_TABLE in present
            result["settings_table_present"] = settings_present
            result["deliveries_table_present"] = deliveries_present

            if not reminder_tables:
                result.update({"schema_state": "not_initialized", "audit_status": "pass"})
                return result
            if not (settings_present and deliveries_present):
                result.update({"schema_state": "unexpected_reminder_schema", "audit_status": "failed"})
                return result

            settings_columns = _table_columns(conn, WEIGHT_REMINDER_SETTINGS_TABLE)
            delivery_columns = _table_columns(conn, WEIGHT_REMINDER_DELIVERIES_TABLE)
            if not _SETTINGS_COLUMNS.issubset(settings_columns) or not _DELIVERY_COLUMNS.issubset(delivery_columns):
                result.update({"schema_state": "unexpected_reminder_schema", "audit_status": "failed"})
                return result

            result.update(
                {
                    "schema_state": "canonical",
                    "audit_status": "pass",
                    "settings_total": _scalar(conn, f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_SETTINGS_TABLE}"),
                    "settings_enabled": _scalar(
                        conn, f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE enabled=1"
                    ),
                    "settings_disabled": _scalar(
                        conn, f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE enabled=0"
                    ),
                    "settings_active": _scalar(
                        conn,
                        f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE delivery_state='active'",
                    ),
                    "settings_suspended": _scalar(
                        conn,
                        f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_SETTINGS_TABLE} WHERE delivery_state='suspended'",
                    ),
                    "deliveries_total": _scalar(conn, f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_DELIVERIES_TABLE}"),
                    "deliveries_sent": _scalar(
                        conn, f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE status='sent'"
                    ),
                    "deliveries_retry_wait": _scalar(
                        conn, f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE status='retry_wait'"
                    ),
                    "deliveries_delivery_unknown": _scalar(
                        conn,
                        f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE status='delivery_unknown'",
                    ),
                    "deliveries_permanent_failed": _scalar(
                        conn,
                        f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_DELIVERIES_TABLE} WHERE status='permanent_failed'",
                    ),
                }
            )
            if unknown:
                result["warning"] = "unexpected_reminder_tables_present"
            return result
    except sqlite3.Error:
        result.update({"integrity": "failed", "error_type": "SQLITE_DATABASE_ERROR"})
        return result


def _emit_text(result: dict[str, Any]) -> None:
    for key in sorted(result):
        value = result[key]
        if isinstance(value, list):
            value = ",".join(str(item) for item in value)
        print(f"{key}={value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only HealBite weight reminder DB audit")
    parser.add_argument("db_path")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    result = audit_db_path(args.db_path)
    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _emit_text(result)
    return 0 if result.get("audit_status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
