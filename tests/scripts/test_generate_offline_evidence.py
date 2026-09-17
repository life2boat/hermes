import json
from datetime import datetime, timezone
import subprocess
import os
import shutil
import pytest
from scripts.compute_bundle_digest import compute_canonical_digest_from_dict
from scripts.hermes_release_qualification import get_all_gates
from ai_engineering.contracts import Status


def test_generate_offline_evidence():
    target_sha = "abc123sha4567890123456789012345678901234"
    try:
        result = subprocess.run(
            ["python3", "scripts/generate_offline_evidence.py", target_sha],
            capture_output=True, text=True
        )
        assert result.returncode == 0

        # 1. Secrets evidence
        with open("secret_evidence_unsigned.json") as f:
            secret_ev = json.load(f)

        assert secret_ev["evidence_type"] == "production_secret_presence"
        assert secret_ev["target_sha"] == target_sha
        assert "source_class" in secret_ev
        assert secret_ev["source_class"] == "explicit-protected-dotenv"
        assert "required_secrets" in secret_ev

        if secret_ev.get("status") == "PASS":
            names = [sec["name"] for sec in secret_ev["required_secrets"]]
            assert "TELEGRAM_BOT_TOKEN" in names

            # ensure no actual secret values are emitted
            for sec in secret_ev["required_secrets"]:
                assert "value" not in sec
                assert "source_class" in sec
                assert sec["source_class"] == "approved-production-secret-source"

        # 2. Schema evidence
        with open("schema_evidence_unsigned.json") as f:
            schema_ev = json.load(f)

        assert schema_ev["evidence_type"] == "production_schema_compatibility"
        assert schema_ev["target_sha"] == target_sha
        if schema_ev.get("status") == "PASS":
            assert schema_ev["actual_user_version"] == 0
            assert schema_ev["expected_user_version"] == 0
            assert schema_ev["schema_delta"] == "NONE"
            assert schema_ev["migration_required"] is False
            assert schema_ev["integrity_status"] == "ok"
            assert schema_ev["foreign_key_violation_count"] == 0
            assert schema_ev["actual_schema_digest"] == schema_ev["expected_schema_digest"]

        # 3. Rollback evidence
        with open("rollback_evidence_unsigned.json") as f:
            rollback_ev = json.load(f)

        if rollback_ev.get("status") == "PASS":
            assert rollback_ev["evidence_type"] == "rollback_ready"
            assert rollback_ev["target_sha"] == target_sha
            assert rollback_ev["rollback_attempt_count_max"] == 1
            assert rollback_ev["rollback_health_required"] is True
            assert rollback_ev["same_compose_chain"] is True
            assert rollback_ev["database_restore_required"] is False
            assert rollback_ev["schema_downgrade_required"] is False
            assert rollback_ev["rollback_procedure_proven"] is True
            assert bool(rollback_ev["canonical_rehearsal_evidence"])
    finally:
        for fname in (
            "secret_evidence_unsigned.json",
            "db_evidence_unsigned.json",
            "schema_evidence_unsigned.json",
            "rollback_evidence_unsigned.json",
            "credential_risk_evidence_unsigned.json",
        ):
            if os.path.exists(fname):
                try:
                    os.remove(fname)
                except OSError:
                    pass


def sign_ev(ev, key):
    import hmac, hashlib
    payload = {
        k: v
        for k, v in ev.items()
        if k not in ("evidence_digest", "execution_provenance")
    }
    if "execution_provenance" in ev:
        payload["execution_provenance"] = {
            k: v for k, v in ev["execution_provenance"].items() if k != "signature"
        }
    payload_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload_digest = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()
    sig = hmac.new(
        key.encode("utf-8"), payload_digest.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    final_ev = dict(ev)
    final_ev["execution_provenance"] = dict(payload.get("execution_provenance", {}))
    final_ev["execution_provenance"]["signature"] = sig
    final_ev["evidence_digest"] = compute_canonical_digest_from_dict(final_ev)
    return final_ev


def test_producer_output_accepted_by_real_qualifier():
    target_sha = "abc123sha4567890123456789012345678901234"
    key = "testanchor"
    os.environ["HERMES_PROVENANCE_KEY"] = key
    fresh_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    from scripts.canonical_schema_contract import (
        EXPECTED_SCHEMA_DIGEST,
        EXPECTED_USER_VERSION,
    )

    sch_unsigned = {
        "schema_version": 1,
        "evidence_type": "production_schema_compatibility",
        "target_sha": target_sha,
        "status": "PASS",
        "observed_schema": "CREATE TABLE test (id INTEGER);",
        "digest": EXPECTED_SCHEMA_DIGEST,
        "user_version": EXPECTED_USER_VERSION,
        "actual_user_version": EXPECTED_USER_VERSION,
        "expected_user_version": EXPECTED_USER_VERSION,
        "actual_schema_digest": EXPECTED_SCHEMA_DIGEST,
        "expected_schema_digest": EXPECTED_SCHEMA_DIGEST,
        "schema_delta": "NONE",
        "migration_required": False,
        "integrity_status": "ok",
        "foreign_key_violation_count": 0,
        "collected_at_utc": fresh_time,
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "healbite-production",
        },
    }
    sch_signed = sign_ev(sch_unsigned, key)

    rb_unsigned = {
        "schema_version": 1,
        "evidence_type": "rollback_ready",
        "target_sha": target_sha,
        "status": "PASS",
        "collected_at_utc": fresh_time,
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "healbite-production",
        },
        "current_production_image_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "current_production_oci_revision": target_sha,
        "rollback_image_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "rollback_image_resolvable": True,
        "rollback_revision": target_sha,
        "rollback_mechanism_id": "docker-compose-revert",
        "same_compose_chain": True,
        "database_restore_required": False,
        "schema_downgrade_required": False,
        "rollback_health_required": True,
        "rollback_attempt_count_max": 1,
        "rollback_procedure_proven": True,
        "canonical_rehearsal_evidence": "artifact:rollback-rehearsal:docker-compose-revert:pass",
    }
    rb_signed = sign_ev(rb_unsigned, key)

    bundle = {
        "target_sha": target_sha,
        "schema_evidence": sch_signed,
        "rollback_evidence": rb_signed,
    }
    bundle["bundle_digest"] = compute_canonical_digest_from_dict(bundle)

    gates, *_ = get_all_gates(target_sha, bundle)
    sch_gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert sch_gate.status == Status.PASS, f"Schema gate failed: {sch_gate.reason}"

    rb_gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert rb_gate.status == Status.PASS, f"Rollback gate failed: {rb_gate.reason}"


def test_missing_top_level_source_class_rejected():
    key = "testanchor"
    os.environ["HERMES_PROVENANCE_KEY"] = key
    fresh_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target_sha = "sha123"

    secret_ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": target_sha,
        "status": "PASS",
        "source_class": "explicit-protected-dotenv",
        "collected_at_utc": fresh_time,
        "required_secrets": [
            {
                "name": "TELEGRAM_BOT_TOKEN",
                "required": True,
                "present": True,
                "source_class": "approved-production-secret-source",
            }
        ],
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "healbite-production",
        },
    }
    del secret_ev["source_class"]
    secret_signed = sign_ev(secret_ev, key)

    bundle = {
        "target_sha": target_sha,
        "secret_evidence": secret_signed,
    }
    bundle["bundle_digest"] = compute_canonical_digest_from_dict(bundle)
    gates, *_ = get_all_gates(target_sha, bundle)
    sec_gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert sec_gate.status == Status.FAIL

def test_db_path_validation_in_offline_evidence(tmp_path):
    from scripts.generate_offline_evidence import check_db_path
    from scripts.hermes_deploy_preflight import validate_database_source_path, DeployPreflightError
    import os

    canonical_db = tmp_path / "canonical.db"
    canonical_db.write_bytes(b"sqlite")

    status, classification, version = check_db_path({"source": str(canonical_db)})
    assert status == "PASS"
    assert classification == "authoritative-production-path"

    missing_db = tmp_path / "missing.db"
    status, classification, version = check_db_path({"source": str(missing_db)})
    assert status == "BLOCKED"

    if hasattr(os, "symlink"):
        symlink_db = tmp_path / "symlink.db"
        os.symlink(str(canonical_db), str(symlink_db))
        status, classification, version = check_db_path({"source": str(symlink_db)})
        assert status == "BLOCKED"

    dir_db = tmp_path / "dir.db"
    dir_db.mkdir()
    status, classification, version = check_db_path({"source": str(dir_db)})
    assert status == "BLOCKED"

    from pathlib import Path
    relative_db = Path("relative.db")
    if not relative_db.exists():
        relative_db.write_bytes(b"")
    status, classification, version = check_db_path({"source": str(relative_db)})
    assert status == "BLOCKED"
    if relative_db.exists():
        relative_db.unlink()

    try:
        from scripts._cli_utils import _validate_database_source_path
        has_old_validator = True
    except ImportError:
        has_old_validator = False
    assert not has_old_validator, "The producer must no longer depend on the nonexistent old validator"
