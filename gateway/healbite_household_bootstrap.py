
from __future__ import annotations

import json
import sqlite3
import stat
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from gateway.healbite_household_schema import (
    HOUSEHOLD_MEMBERS_TABLE,
    HOUSEHOLDS_TABLE,
    HOUSEHOLD_MEMBER_STATUSES,
    HOUSEHOLD_MEMBER_TYPES,
    HOUSEHOLD_ROLES,
    HOUSEHOLD_STATUSES,
    is_canonical_uuid4,
)
from gateway.healbite_households import (
    HealBiteHouseholdStore,
    HouseholdError,
    HouseholdIntegrityError,
    resolve_users_identity_column,
)

SQLITE_MAX_INTEGER = 9223372036854775807
PRODUCTION_DB_PATH = Path("/home/hermes/healbite.db")
EXPECTED_HOUSEHOLDS_COLUMNS = {
    "id", "owner_user_id", "name", "status", "default_timezone", "created_at", "updated_at", "version",
}
EXPECTED_MEMBERS_COLUMNS = {
    "id", "household_id", "linked_user_id", "display_name", "member_type", "role", "status",
    "age_band", "created_at", "updated_at", "version",
}
EXPECTED_INDEXES = {"idx_household_members_active_linked_user", "idx_household_members_active_owner"}
NON_HOUSEHOLD_TABLES = (
    "users", "profiles", "nutrition_log", "weight_entries", "water_intake",
    "weight_reminder_settings", "weight_reminder_deliveries",
)

EXIT_SUCCESS = 0
EXIT_INVALID_ARGUMENTS = 2
EXIT_DB_UNAVAILABLE = 3
EXIT_SCHEMA_NOT_CANONICAL = 4
EXIT_INTEGRITY_FAILURE = 5
EXIT_ELIGIBILITY_REQUIRED = 6
EXIT_HOUSEHOLD_CONFLICT = 7
EXIT_APPLY_FAILURE = 8


def _sqlite_read_only_uri(db_path: Path) -> str:
    return f"file:{quote(str(db_path.resolve()), safe='/')}?mode=ro"


def _read_only_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(_sqlite_read_only_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()}


def _count(conn: sqlite3.Connection, table: str) -> int:
    escaped = table.replace('"', '""')
    return int(conn.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0])


def _identity_column_state(conn: sqlite3.Connection) -> tuple[str | None, str]:
    if "users" not in _table_names(conn):
        return None, "missing_users_table"
    try:
        return resolve_users_identity_column(conn), "supported"
    except HouseholdIntegrityError:
        return None, "unsupported"


def _normalize_candidate(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
    else:
        return None
    if value <= 0 or value > SQLITE_MAX_INTEGER:
        return None
    return value


def discover_authoritative_users(conn: sqlite3.Connection) -> dict[str, Any]:
    identity_column, identity_state = _identity_column_state(conn)
    result: dict[str, Any] = {
        "identity_schema_state": identity_state,
        "identity_column": identity_column,
        "source_rows": 0,
        "distinct_candidates": 0,
        "invalid_candidates": 0,
        "duplicate_source_rows": 0,
        "eligible_candidates": 0,
        "excluded_candidates": 0,
        "candidate_ids": [],
    }
    if identity_column is None:
        return result
    escaped = identity_column.replace('"', '""')
    rows = conn.execute(f'SELECT "{escaped}" AS actor_id FROM users').fetchall()
    result["source_rows"] = len(rows)
    seen: set[int] = set()
    invalid = 0
    duplicates = 0
    for row in rows:
        candidate = _normalize_candidate(row["actor_id"])
        if candidate is None:
            invalid += 1
            continue
        if candidate in seen:
            duplicates += 1
            continue
        seen.add(candidate)
    result["invalid_candidates"] = invalid
    result["duplicate_source_rows"] = duplicates
    result["distinct_candidates"] = len(seen)
    result["eligible_candidates"] = len(seen)
    result["candidate_ids"] = sorted(seen)
    return result


def _metadata_eligibility_available(conn: sqlite3.Connection) -> bool:
    columns = _table_columns(conn, "users") if "users" in _table_names(conn) else set()
    return {"is_bot", "is_system", "is_test", "status"}.issubset(columns)


def _eligible_file_is_protected(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(file_stat.st_mode):
        return False
    if not stat.S_ISREG(file_stat.st_mode):
        return False
    if file_stat.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return False
    return True


def _load_eligible_file(path: str | Path | None, authoritative: set[int]) -> tuple[set[int], dict[str, Any]]:
    result = {
        "eligible_file_used": path is not None,
        "eligible_file_valid": True,
        "eligible_file_duplicates": 0,
        "eligible_file_invalid": 0,
        "eligible_file_unknown": 0,
    }
    if path is None:
        result["eligible_file_security"] = None
        return set(authoritative), result
    selected: set[int] = set()
    seen: set[int] = set()
    eligible_path = Path(path)
    if not _eligible_file_is_protected(eligible_path):
        result.update({"eligible_file_valid": False, "eligible_file_invalid": 1, "eligible_file_security": "invalid"})
        return set(), result
    result["eligible_file_security"] = "valid"
    try:
        lines = eligible_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        result.update({"eligible_file_valid": False, "eligible_file_invalid": 1})
        return set(), result
    for raw in lines:
        token = raw.strip()
        if not token:
            continue
        candidate = _normalize_candidate(token)
        if candidate is None:
            result["eligible_file_invalid"] += 1
            result["eligible_file_valid"] = False
            continue
        if candidate in seen:
            result["eligible_file_duplicates"] += 1
            result["eligible_file_valid"] = False
            continue
        seen.add(candidate)
        if candidate not in authoritative:
            result["eligible_file_unknown"] += 1
            result["eligible_file_valid"] = False
            continue
        selected.add(candidate)
    return selected, result


def detect_schema_state(conn: sqlite3.Connection) -> str:
    tables = _table_names(conn)
    present_household = HOUSEHOLDS_TABLE in tables
    present_members = HOUSEHOLD_MEMBERS_TABLE in tables
    if not present_household and not present_members:
        return "not_initialized"
    if present_household != present_members:
        return "partial"
    if not EXPECTED_HOUSEHOLDS_COLUMNS.issubset(_table_columns(conn, HOUSEHOLDS_TABLE)):
        return "unexpected"
    if not EXPECTED_MEMBERS_COLUMNS.issubset(_table_columns(conn, HOUSEHOLD_MEMBERS_TABLE)):
        return "unexpected"
    if not EXPECTED_INDEXES.issubset(_index_names(conn)):
        return "partial"
    return "canonical"


def _safe_quick_check(conn: sqlite3.Connection) -> str:
    try:
        value = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    except sqlite3.Error:
        return "failed"
    return "ok" if value.lower() == "ok" else "failed"


def _household_consistency(conn: sqlite3.Connection, schema_state: str) -> dict[str, int]:
    result = {
        "households_total": 0,
        "members_total": 0,
        "owner_pointer_mismatches": 0,
        "duplicate_active_linked_users": 0,
        "households_without_owner": 0,
        "multiple_active_owners": 0,
        "orphan_members": 0,
        "invalid_uuid": 0,
        "invalid_version": 0,
        "invalid_enum": 0,
        "active_member_in_non_active_household": 0,
    }
    tables = _table_names(conn)
    if HOUSEHOLDS_TABLE not in tables or HOUSEHOLD_MEMBERS_TABLE not in tables:
        return result
    if not EXPECTED_HOUSEHOLDS_COLUMNS.issubset(_table_columns(conn, HOUSEHOLDS_TABLE)):
        return result
    if not EXPECTED_MEMBERS_COLUMNS.issubset(_table_columns(conn, HOUSEHOLD_MEMBERS_TABLE)):
        return result
    result["households_total"] = _count(conn, HOUSEHOLDS_TABLE)
    result["members_total"] = _count(conn, HOUSEHOLD_MEMBERS_TABLE)
    result["duplicate_active_linked_users"] = int(conn.execute(
        f"""
        SELECT COUNT(*) FROM (
            SELECT linked_user_id FROM {HOUSEHOLD_MEMBERS_TABLE}
            WHERE linked_user_id IS NOT NULL AND status='active'
            GROUP BY linked_user_id HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0])
    owner_counts = conn.execute(
        f"""
        SELECT h.id, COUNT(m.id) AS owner_count
        FROM {HOUSEHOLDS_TABLE} h
        LEFT JOIN {HOUSEHOLD_MEMBERS_TABLE} m
          ON m.household_id=h.id AND m.role='owner' AND m.status='active'
        WHERE h.status='active'
        GROUP BY h.id
        """
    ).fetchall()
    result["households_without_owner"] = sum(1 for row in owner_counts if int(row["owner_count"]) == 0)
    result["multiple_active_owners"] = sum(1 for row in owner_counts if int(row["owner_count"]) > 1)
    result["owner_pointer_mismatches"] = int(conn.execute(
        f"""
        SELECT COUNT(*) FROM {HOUSEHOLDS_TABLE} h
        JOIN {HOUSEHOLD_MEMBERS_TABLE} m
          ON m.household_id=h.id AND m.role='owner' AND m.status='active'
        WHERE h.status='active' AND (m.linked_user_id IS NULL OR m.linked_user_id != h.owner_user_id)
        """
    ).fetchone()[0])
    result["orphan_members"] = int(conn.execute(
        f"""
        SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} m
        LEFT JOIN {HOUSEHOLDS_TABLE} h ON h.id=m.household_id
        WHERE h.id IS NULL
        """
    ).fetchone()[0])
    result["active_member_in_non_active_household"] = int(conn.execute(
        f"""
        SELECT COUNT(*) FROM {HOUSEHOLD_MEMBERS_TABLE} m
        JOIN {HOUSEHOLDS_TABLE} h ON h.id=m.household_id
        WHERE m.status='active' AND h.status != 'active'
        """
    ).fetchone()[0])
    for row in conn.execute(f"SELECT id, status, version FROM {HOUSEHOLDS_TABLE}").fetchall():
        if not is_canonical_uuid4(str(row["id"])):
            result["invalid_uuid"] += 1
        if str(row["status"]) not in HOUSEHOLD_STATUSES:
            result["invalid_enum"] += 1
        try:
            if int(row["version"]) < 1:
                result["invalid_version"] += 1
        except (TypeError, ValueError):
            result["invalid_version"] += 1
    for row in conn.execute(
        f"SELECT id, household_id, member_type, role, status, version FROM {HOUSEHOLD_MEMBERS_TABLE}"
    ).fetchall():
        if not is_canonical_uuid4(str(row["id"])) or not is_canonical_uuid4(str(row["household_id"])):
            result["invalid_uuid"] += 1
        if str(row["member_type"]) not in HOUSEHOLD_MEMBER_TYPES or str(row["role"]) not in HOUSEHOLD_ROLES:
            result["invalid_enum"] += 1
        if str(row["status"]) not in HOUSEHOLD_MEMBER_STATUSES:
            result["invalid_enum"] += 1
        try:
            if int(row["version"]) < 1:
                result["invalid_version"] += 1
        except (TypeError, ValueError):
            result["invalid_version"] += 1
    return result


def _covered_eligible_users(conn: sqlite3.Connection, eligible_ids: set[int]) -> int:
    covered = 0
    for actor_id in eligible_ids:
        rows = conn.execute(
            f"""
            SELECT h.owner_user_id, h.status AS household_status, m.linked_user_id, m.role, m.status AS member_status
            FROM {HOUSEHOLD_MEMBERS_TABLE} m
            JOIN {HOUSEHOLDS_TABLE} h ON h.id=m.household_id
            WHERE m.linked_user_id=? AND m.member_type='primary'
            LIMIT 2
            """,
            (actor_id,),
        ).fetchall()
        if len(rows) != 1:
            continue
        row = rows[0]
        if (
            int(row["owner_user_id"]) == actor_id
            and int(row["linked_user_id"]) == actor_id
            and row["role"] == "owner"
            and row["household_status"] == "active"
            and row["member_status"] == "active"
        ):
            covered += 1
    return covered


def _has_conflicts(result: Mapping[str, Any]) -> bool:
    keys = (
        "owner_pointer_mismatches", "duplicate_active_linked_users", "households_without_owner",
        "multiple_active_owners", "orphan_members", "invalid_uuid", "invalid_version", "invalid_enum",
        "active_member_in_non_active_household",
    )
    return any(int(result.get(key, 0) or 0) > 0 for key in keys)


def _non_household_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = _table_names(conn)
    return {f"{table}_count": _count(conn, table) for table in NON_HOUSEHOLD_TABLES if table in tables}


def _is_production_db_path(path: Path) -> bool:
    try:
        if path.resolve(strict=False) == PRODUCTION_DB_PATH.resolve(strict=False):
            return True
    except OSError:
        pass
    try:
        if path.exists() and PRODUCTION_DB_PATH.exists() and path.samefile(PRODUCTION_DB_PATH):
            return True
    except OSError:
        pass
    return False


def audit_household_db(db_path: str | Path, eligible_users_file: str | Path | None = None) -> dict[str, Any]:
    path = Path(db_path)
    result: dict[str, Any] = {
        "mode": "audit",
        "result": "failed",
        "db_exists": path.exists(),
        "schema_state": "unknown",
        "integrity": "failed",
    }
    if not path.exists():
        result.update({"error_type": "DB_NOT_FOUND"})
        return result
    try:
        with _read_only_connect(path) as conn:
            integrity = _safe_quick_check(conn)
            result["integrity"] = integrity
            if integrity != "ok":
                result.update({"error_type": "SQLITE_INTEGRITY_CHECK_FAILED"})
                return result
            schema_state = detect_schema_state(conn)
            result["schema_state"] = schema_state
            result.update(_household_consistency(conn, schema_state))
            discovery = discover_authoritative_users(conn)
            candidate_ids = set(discovery.pop("candidate_ids", []))
            result.update(discovery)
            metadata_available = _metadata_eligibility_available(conn)
            eligible_ids, file_state = _load_eligible_file(eligible_users_file, candidate_ids)
            result.update(file_state)
            if metadata_available or eligible_users_file is not None:
                result["eligibility_state"] = "verified" if file_state["eligible_file_valid"] else "invalid"
                result["eligible_users_total"] = len(eligible_ids) if file_state["eligible_file_valid"] else 0
                if schema_state == "canonical":
                    covered = _covered_eligible_users(conn, eligible_ids)
                    result["eligible_users_covered"] = covered
                    result["eligible_users_missing"] = max(0, len(eligible_ids) - covered)
                else:
                    result["eligible_users_covered"] = 0
                    result["eligible_users_missing"] = len(eligible_ids)
            else:
                result["eligibility_state"] = "unverified"
                result["eligible_users_total"] = None
                result["eligible_users_covered"] = None
                result["eligible_users_missing"] = None
            result["coverage_state"] = "verified" if result["eligible_users_total"] is not None else "unverified"
            result["result"] = "success" if schema_state in {"not_initialized", "canonical"} else "failed"
            if not file_state["eligible_file_valid"]:
                result.update({"result": "failed", "error_type": "ELIGIBILITY_INVALID"})
            elif _has_conflicts(result):
                result.update({"result": "failed", "error_type": "HOUSEHOLD_CONFLICT"})
            return result
    except sqlite3.Error:
        result.update({"error_type": "SQLITE_DATABASE_ERROR", "integrity": "failed"})
        return result
    except OSError:
        result.update({"error_type": "DB_UNAVAILABLE"})
        return result


def bootstrap_households(
    db_path: str | Path,
    *,
    apply: bool = False,
    initialize_schema: bool = False,
    eligible_users_file: str | Path | None = None,
    batch_size: int = 100,
) -> dict[str, Any]:
    started = time.monotonic()
    path = Path(db_path)
    mode = "apply" if apply else "dry-run"
    result: dict[str, Any] = {
        "mode": mode,
        "result": "failed",
        "apply_ready": False,
        "created_count": 0,
        "already_existing_count": 0,
        "would_create_count": 0,
        "conflict_count": 0,
        "partial_count": 0,
        "batch_count": 0,
        "initializer_called": False,
    }
    if batch_size <= 0 or batch_size > 1000:
        result.update({"error_type": "INVALID_BATCH_SIZE", "exit_code": EXIT_INVALID_ARGUMENTS})
        return result
    if (apply or initialize_schema) and _is_production_db_path(path):
        result.update({"error_type": "PRODUCTION_PATH_APPLY_REFUSED", "exit_code": EXIT_APPLY_FAILURE})
        return result
    if not path.exists():
        result.update({"error_type": "DB_NOT_FOUND", "db_exists": False, "exit_code": EXIT_DB_UNAVAILABLE})
        return result
    if initialize_schema:
        HealBiteHouseholdStore(db_path=path).ensure_schema()
        result["initializer_called"] = True
    audit = audit_household_db(path, eligible_users_file)
    result.update({k: v for k, v in audit.items() if k not in {"mode", "result"}})
    if audit.get("integrity") != "ok":
        result.update({"error_type": audit.get("error_type", "SQLITE_INTEGRITY_CHECK_FAILED"), "exit_code": EXIT_INTEGRITY_FAILURE})
        return result
    if audit.get("schema_state") == "not_initialized":
        result.update({"error_type": "SCHEMA_NOT_INITIALIZED", "verdict": "SCHEMA NOT INITIALIZED ? APPLY REFUSED", "exit_code": EXIT_SCHEMA_NOT_CANONICAL})
        return result
    if audit.get("schema_state") != "canonical":
        result.update({"error_type": "SCHEMA_NOT_CANONICAL", "exit_code": EXIT_SCHEMA_NOT_CANONICAL})
        return result
    if _has_conflicts(audit):
        result.update({"verdict": "HOUSEHOLD STATE CONFLICT ? APPLY REFUSED", "exit_code": EXIT_HOUSEHOLD_CONFLICT})
        return result
    try:
        with _read_only_connect(path) as conn:
            discovery = discover_authoritative_users(conn)
            authoritative = set(discovery.get("candidate_ids", []))
            metadata_available = _metadata_eligibility_available(conn)
            eligible_ids, file_state = _load_eligible_file(eligible_users_file, authoritative)
    except sqlite3.Error:
        result.update({"error_type": "SQLITE_DATABASE_ERROR", "exit_code": EXIT_DB_UNAVAILABLE})
        return result
    result.update(file_state)
    if not file_state["eligible_file_valid"]:
        result.update({"eligibility_state": "invalid", "exit_code": EXIT_ELIGIBILITY_REQUIRED})
        return result
    if not metadata_available and eligible_users_file is None:
        result.update({
            "eligibility_state": "unverified",
            "apply_ready": False,
            "verdict": "ELIGIBILITY POLICY REQUIRED ? APPLY REFUSED",
            "result": "failed" if apply else "success",
            "exit_code": EXIT_ELIGIBILITY_REQUIRED if apply else EXIT_SUCCESS,
        })
        return result
    result["eligibility_state"] = "verified"
    result["eligible_count"] = len(eligible_ids)
    result["apply_ready"] = True
    with _read_only_connect(path) as conn:
        before_counts = _non_household_counts(conn)
        covered = _covered_eligible_users(conn, eligible_ids)
        result["already_existing_count"] = covered
        result["would_create_count"] = max(0, len(eligible_ids) - covered)
    if not apply:
        result.update({"result": "success", "exit_code": EXIT_SUCCESS, "duration_ms": int((time.monotonic() - started) * 1000)})
        return result
    store = HealBiteHouseholdStore(db_path=path)
    created = 0
    already = 0
    try:
        for offset in range(0, len(eligible_ids), batch_size):
            result["batch_count"] += 1
            for actor_id in sorted(eligible_ids)[offset:offset + batch_size]:
                personal = store.get_or_create_personal_household(actor_id)
                if personal.created:
                    created += 1
                else:
                    already += 1
    except (HouseholdError, sqlite3.Error):
        result.update({"error_type": "APPLY_EXECUTION_FAILED", "exit_code": EXIT_APPLY_FAILURE})
        return result
    result["created_count"] = created
    result["already_existing_count"] = already
    post = audit_household_db(path, eligible_users_file)
    result["post_integrity"] = post.get("integrity")
    result["post_schema_state"] = post.get("schema_state")
    result["post_owner_pointer_mismatches"] = post.get("owner_pointer_mismatches")
    result["post_duplicate_active_linked_users"] = post.get("duplicate_active_linked_users")
    result["post_households_without_owner"] = post.get("households_without_owner")
    result["post_orphan_members"] = post.get("orphan_members")
    with _read_only_connect(path) as conn:
        result["non_household_counts_preserved"] = before_counts == _non_household_counts(conn)
    success = post.get("integrity") == "ok" and post.get("schema_state") == "canonical" and not _has_conflicts(post)
    result.update({
        "result": "success" if success else "failed",
        "exit_code": EXIT_SUCCESS if success else EXIT_APPLY_FAILURE,
        "duration_ms": int((time.monotonic() - started) * 1000),
    })
    return result


def safe_json(result: Mapping[str, Any]) -> str:
    safe = {k: v for k, v in result.items() if k != "candidate_ids"}
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


def print_text(result: Mapping[str, Any]) -> None:
    for key in sorted(k for k in result if k != "candidate_ids"):
        value = result[key]
        if isinstance(value, (list, tuple, set, frozenset)):
            value = len(value)
        print(f"{key}={value}")
