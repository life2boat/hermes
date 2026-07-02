from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from gateway.healbite_weight_reminder_schema import (
    WEIGHT_REMINDER_DELIVERIES_TABLE,
    WEIGHT_REMINDER_SETTINGS_TABLE,
    NonCanonicalReminderTableName,
    assert_canonical_reminder_tables,
)
from gateway.healbite_weight_reminders import HealBiteWeightReminderStore

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "weight_reminder_db_audit.py"
SCRIPT_SPEC = importlib.util.spec_from_file_location("weight_reminder_db_audit", SCRIPT_PATH)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
weight_reminder_db_audit = importlib.util.module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = weight_reminder_db_audit
SCRIPT_SPEC.loader.exec_module(weight_reminder_db_audit)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_setting_and_delivery(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        INSERT INTO {WEIGHT_REMINDER_SETTINGS_TABLE}
            (user_id, enabled, timezone, weekday, local_time, next_due_at_utc,
             schedule_version, delivery_state, last_delivered_at_utc, created_at, updated_at)
        VALUES (101, 0, 'CANARY_TIMEZONE_PRIVATE', 1, '09:00', '2026-07-01 09:00:00',
                1, 'active', '2026-07-01 09:00:00', '2026-07-01 08:00:00', '2026-07-01 09:00:00')
        """
    )
    conn.execute(
        f"""
        INSERT INTO {WEIGHT_REMINDER_DELIVERIES_TABLE}
            (user_id, scheduled_for_utc, delivery_key, status, attempt_count,
             claimed_at_utc, claim_expires_at_utc, last_error_type, next_attempt_at_utc,
             schedule_version, sent_at_utc, created_at, updated_at)
        VALUES (101, '2026-07-01 09:00:00', 'CANARY_DELIVERY_KEY_PRIVATE', 'sent', 1,
                NULL, NULL, 'CANARY_ERROR_PRIVATE', NULL, 1, '2026-07-01 09:00:01',
                '2026-07-01 09:00:00', '2026-07-01 09:00:01')
        """
    )
    conn.commit()


def _audit(db_path: Path) -> dict[str, object]:
    return weight_reminder_db_audit.audit_db_path(db_path)


def test_pre_rollout_db_reports_not_initialized(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")

    result = _audit(db_path)

    assert result["audit_status"] == "pass"
    assert result["schema_state"] == "not_initialized"
    assert result["settings_table_present"] is False
    assert result["deliveries_table_present"] is False
    assert "settings_total" not in result


def test_canonical_initialized_db_reports_zero_counts(tmp_path):
    db_path = tmp_path / "healbite.db"
    HealBiteWeightReminderStore(db_path=db_path)

    result = _audit(db_path)

    assert result["audit_status"] == "pass"
    assert result["schema_state"] == "canonical"
    assert result["settings_table_present"] is True
    assert result["deliveries_table_present"] is True
    assert result["settings_total"] == 0
    assert result["deliveries_total"] == 0


def test_controlled_rollout_like_state_reports_safe_aggregates(tmp_path):
    db_path = tmp_path / "healbite.db"
    HealBiteWeightReminderStore(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        _make_setting_and_delivery(conn)

    result = _audit(db_path)

    assert result["settings_total"] == 1
    assert result["settings_enabled"] == 0
    assert result["settings_disabled"] == 1
    assert result["settings_active"] == 1
    assert result["settings_suspended"] == 0
    assert result["deliveries_total"] == 1
    assert result["deliveries_sent"] == 1
    assert result["deliveries_retry_wait"] == 0
    assert result["deliveries_delivery_unknown"] == 0
    assert result["deliveries_permanent_failed"] == 0



def test_partial_canonical_schema_fails_explicitly(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"""
            CREATE TABLE {WEIGHT_REMINDER_SETTINGS_TABLE} (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL,
                delivery_state TEXT NOT NULL
            )
            """
        )

    result = _audit(db_path)

    assert result["audit_status"] == "failed"
    assert result["schema_state"] == "partial_canonical"
    assert result["settings_table_present"] is True
    assert result["deliveries_table_present"] is False


def test_permission_denied_fails_safely_without_raw_exception(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE users (user_id INTEGER PRIMARY KEY)")
    db_path.chmod(0)
    try:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            exit_code = weight_reminder_db_audit.main([str(db_path)])
    finally:
        db_path.chmod(0o600)

    if exit_code == 0:
        pytest.skip("current user can still read chmod 000 file")
    payload = output.getvalue()
    result = json.loads(payload)
    assert result["audit_status"] == "failed"
    assert result["error_type"] == "SQLITE_DATABASE_ERROR"
    assert str(db_path) not in payload
    assert "permission" not in payload.lower()

def test_legacy_names_only_fails_as_unexpected_schema(tmp_path):
    db_path = tmp_path / "healbite.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE weight_reminder_outbox (id INTEGER PRIMARY KEY)")
        conn.execute("CREATE TABLE weight_reminder_delivery_attempts (id INTEGER PRIMARY KEY)")

    result = _audit(db_path)

    assert result["audit_status"] == "failed"
    assert result["schema_state"] == "unexpected_reminder_schema"
    assert result["settings_table_present"] is False
    assert result["deliveries_table_present"] is False
    assert result["unknown_reminder_tables"] == [
        "weight_reminder_delivery_attempts",
        "weight_reminder_outbox",
    ]


def test_canonical_plus_unknown_reports_counts_and_warning(tmp_path):
    db_path = tmp_path / "healbite.db"
    HealBiteWeightReminderStore(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        _make_setting_and_delivery(conn)
        conn.execute("CREATE TABLE weight_reminder_experimental (id INTEGER PRIMARY KEY)")

    result = _audit(db_path)

    assert result["audit_status"] == "pass"
    assert result["schema_state"] == "canonical"
    assert result["settings_total"] == 1
    assert result["deliveries_total"] == 1
    assert result["unknown_reminder_tables"] == ["weight_reminder_experimental"]
    assert result["warning"] == "unexpected_reminder_tables_present"


def test_wrong_configured_constants_fail_loud():
    with pytest.raises(NonCanonicalReminderTableName, match="NON-CANONICAL REMINDER TABLE NAME IN AUDIT TOOLING"):
        assert_canonical_reminder_tables(("weight_reminder_outbox", "weight_reminder_delivery_attempts"))


def test_audit_is_read_only_for_existing_db(tmp_path):
    db_path = tmp_path / "healbite.db"
    HealBiteWeightReminderStore(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        _make_setting_and_delivery(conn)
    before_hash = _hash(db_path)
    before_mtime = db_path.stat().st_mtime_ns

    result = _audit(db_path)

    assert result["audit_status"] == "pass"
    assert _hash(db_path) == before_hash
    assert db_path.stat().st_mtime_ns == before_mtime
    assert not db_path.with_suffix(".db-wal").exists()
    assert not db_path.with_suffix(".db-shm").exists()
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_SETTINGS_TABLE}").fetchone()[0] == 1
        assert conn.execute(f"SELECT COUNT(*) FROM {WEIGHT_REMINDER_DELIVERIES_TABLE}").fetchone()[0] == 1


def test_missing_path_does_not_create_file(tmp_path):
    db_path = tmp_path / "missing.db"

    result = _audit(db_path)

    assert result["audit_status"] == "failed"
    assert result["error_type"] == "DB_NOT_FOUND"
    assert not db_path.exists()


def test_corrupt_db_fails_safely_without_raw_exception(tmp_path):
    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"not sqlite")

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = weight_reminder_db_audit.main([str(db_path)])

    payload = output.getvalue()
    result = json.loads(payload)
    assert exit_code == 1
    assert result["audit_status"] == "failed"
    assert result["integrity"] == "failed"
    assert result["error_type"] == "SQLITE_DATABASE_ERROR"
    assert "not sqlite" not in payload


def test_privacy_canaries_do_not_appear_in_json_or_text_output(tmp_path):
    db_path = tmp_path / "healbite.db"
    HealBiteWeightReminderStore(db_path=db_path)
    with sqlite3.connect(db_path) as conn:
        _make_setting_and_delivery(conn)

    json_output = io.StringIO()
    with contextlib.redirect_stdout(json_output):
        assert weight_reminder_db_audit.main([str(db_path)]) == 0
    text_output = io.StringIO()
    with contextlib.redirect_stdout(text_output):
        assert weight_reminder_db_audit.main([str(db_path), "--format", "text"]) == 0

    combined = json_output.getvalue() + text_output.getvalue()
    assert "CANARY_TIMEZONE_PRIVATE" not in combined
    assert "CANARY_DELIVERY_KEY_PRIVATE" not in combined
    assert "CANARY_ERROR_PRIVATE" not in combined


EVIDENCE_CASES = [
    ("controlled_pre", Path("/home/hermes/backups/s70c2_allowlisted_rollout/20260702T131809Z/healbite-pre-allowlist.db"), "not_initialized", None, None, None),
    ("controlled_post", Path("/home/hermes/backups/s70c2_allowlisted_rollout/20260702T131809Z/healbite-post-allowlist.db"), "canonical", 1, 1, 1),
    ("pr4_predeploy", Path("/home/hermes/backups/s70c2_pr4_predeploy/20260702T142603Z/healbite-production-copy.db"), "canonical", 1, 1, 1),
    ("pr4_deploy_pre", Path("/home/hermes/backups/s70c2_pr4_deploy_disabled/20260702T142603Z/healbite.db"), "canonical", 1, 1, 1),
    ("pr4_deploy_post", Path("/home/hermes/backups/s70c2_pr4_deploy_disabled/20260702T142603Z/healbite.db.postdeploy"), "canonical", 1, 1, 1),
]


@pytest.mark.parametrize(("name", "path", "schema_state", "settings", "deliveries", "sent"), EVIDENCE_CASES)
def test_evidence_timeline_replay(name, path, schema_state, settings, deliveries, sent):
    if not path.exists():
        pytest.skip(f"evidence DB unavailable: {name}")

    result = _audit(path)

    assert result["audit_status"] == "pass"
    assert result["schema_state"] == schema_state
    if schema_state == "canonical":
        assert result["settings_total"] == settings
        assert result["deliveries_total"] == deliveries
        assert result["deliveries_sent"] == sent
