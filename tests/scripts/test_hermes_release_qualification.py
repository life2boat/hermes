import pytest
import json
from scripts.compute_bundle_digest import compute_canonical_digest_from_dict
from scripts.hermes_release_qualification import get_all_gates
from ai_engineering.contracts import Status


def test_exact_main_ci_pass():
    bundle = {
        "target_sha": "final_main_sha_123",
        "structured_ci_evidence": {
            "pr_merge_sha": "final_main_sha_123",
            "pr_head_tree_sha": "tree_sha_1",
            "final_main_tree_sha": "tree_sha_1",
            "pr_head_sha": "head_sha_123",
            "pr_merged_at": "2026-09-14T05:00:00Z",
            "workflow_runs": [
                {
                    "workflow_name": "Tests",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Lint (ruff + ty)",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Typecheck",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Nix",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Agent Release Gate",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Supply Chain Audit",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "History Check",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
            ],
        },
    }
    bundle["bundle_digest"] = compute_canonical_digest_from_dict(bundle)
    gates, *_ = get_all_gates("final_main_sha_123", bundle)
    ci_gate = next(g for g in gates if g.gate_name == "EXACT_MAIN_CI")
    assert ci_gate.status == Status.PASS


def test_exact_main_ci_fail_head_sha_mismatch():
    bundle = {
        "target_sha": "final_main_sha_123",
        "structured_ci_evidence": {
            "pr_merge_sha": "final_main_sha_123",
            "pr_head_tree_sha": "tree_sha_1",
            "final_main_tree_sha": "tree_sha_1",
            "pr_head_sha": "head_sha_123",
            "pr_merged_at": "2026-09-14T05:00:00Z",
            "workflow_runs": [
                {
                    "workflow_name": "Tests",
                    "head_sha": "wrong_sha",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Lint (ruff + ty)",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Typecheck",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Nix",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Agent Release Gate",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Supply Chain Audit",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "History Check",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
            ],
        },
    }
    bundle["bundle_digest"] = compute_canonical_digest_from_dict(bundle)
    gates, *_ = get_all_gates("final_main_sha_123", bundle)
    ci_gate = next(g for g in gates if g.gate_name == "EXACT_MAIN_CI")
    assert ci_gate.status == Status.FAIL


def test_exact_main_ci_fail_completed_after_merge():
    bundle = {
        "target_sha": "final_main_sha_123",
        "structured_ci_evidence": {
            "pr_merge_sha": "final_main_sha_123",
            "pr_head_tree_sha": "tree_sha_1",
            "final_main_tree_sha": "tree_sha_1",
            "pr_head_sha": "head_sha_123",
            "pr_merged_at": "2026-09-14T05:00:00Z",
            "workflow_runs": [
                {
                    "workflow_name": "Tests",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T05:01:00Z",
                },  # after merge
                {
                    "workflow_name": "Lint (ruff + ty)",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Typecheck",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Nix",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Agent Release Gate",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Supply Chain Audit",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "History Check",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
            ],
        },
    }
    bundle["bundle_digest"] = compute_canonical_digest_from_dict(bundle)
    gates, *_ = get_all_gates("final_main_sha_123", bundle)
    ci_gate = next(g for g in gates if g.gate_name == "EXACT_MAIN_CI")
    assert ci_gate.status == Status.FAIL


def test_exact_main_ci_fail_missing_workflow():
    bundle = {
        "target_sha": "final_main_sha_123",
        "structured_ci_evidence": {
            "pr_merge_sha": "final_main_sha_123",
            "pr_head_tree_sha": "tree_sha_1",
            "final_main_tree_sha": "tree_sha_1",
            "pr_head_sha": "head_sha_123",
            "pr_merged_at": "2026-09-14T05:00:00Z",
            "workflow_runs": [
                {
                    "workflow_name": "Tests",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                # Missing Lint
                {
                    "workflow_name": "Typecheck",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Nix",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Agent Release Gate",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "Supply Chain Audit",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
                {
                    "workflow_name": "History Check",
                    "head_sha": "head_sha_123",
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-09-14T04:59:00Z",
                },
            ],
        },
    }
    bundle["bundle_digest"] = compute_canonical_digest_from_dict(bundle)
    gates, *_ = get_all_gates("final_main_sha_123", bundle)
    ci_gate = next(g for g in gates if g.gate_name == "EXACT_MAIN_CI")
    assert ci_gate.status == Status.FAIL


def create_bundle(extra: dict) -> dict:
    bundle = {"target_sha": "sha123"}
    bundle.update(extra)
    bundle["bundle_digest"] = compute_canonical_digest_from_dict(bundle)
    return bundle


def valid_utc() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 1. SECRET RECORD SCHEMA MUST BE CLOSED


def test_secret_literal_blocked():
    bundle = create_bundle({"secret_evidence": "BLOCKED"})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.BLOCKED


def test_secret_typed_blocked():
    ev = {"status": "BLOCKED"}
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.BLOCKED


def test_db_literal_blocked():
    bundle = create_bundle({"db_evidence": "BLOCKED"})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.BLOCKED


def test_db_typed_blocked():
    ev = {"status": "BLOCKED"}
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.BLOCKED


def test_malformed_evidence():
    ev = ["not a dict"]
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_structurally_valid_but_unsigned_secret_evidence():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [
            {
                "name": "TELEGRAM_BOT_TOKEN",
                "required": True,
                "present": True,
                "source_class": "env",
            }
        ],
        "execution_provenance": {},
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.BLOCKED


def test_structurally_valid_db_evidence_without_trust_anchor():
    import os

    if "HERMES_PROVENANCE_KEY" in os.environ:
        del os.environ["HERMES_PROVENANCE_KEY"]
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {"signature": "dummy"},
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.BLOCKED


def sign_evidence(ev, key):
    import json, hashlib, hmac

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
    return hmac.new(
        key.encode("utf-8"), payload_digest.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def test_valid_signature_over_payload_cryptographic_pass():
    import os

    os.environ["HERMES_PROVENANCE_KEY"] = "testkey"
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {},
    }
    ev["execution_provenance"]["signature"] = sign_evidence(ev, "testkey")
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.PASS


def test_modify_path_classification_after_signature():
    import os

    os.environ["HERMES_PROVENANCE_KEY"] = "testkey"
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {},
    }
    ev["execution_provenance"]["signature"] = sign_evidence(ev, "testkey")

    # Tamper
    ev["path_classification"] = "canonical-production-path"
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)

    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_modify_validator_version_after_signature():
    import os

    os.environ["HERMES_PROVENANCE_KEY"] = "testkey"
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {},
    }
    ev["execution_provenance"]["signature"] = sign_evidence(ev, "testkey")

    # Tamper
    ev["validator_version"] = "2"
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)

    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_modify_timestamp_after_signature():
    import os

    os.environ["HERMES_PROVENANCE_KEY"] = "testkey"
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {},
    }
    ev["execution_provenance"]["signature"] = sign_evidence(ev, "testkey")

    # Tamper
    ev["collected_at_utc"] = "2026-09-14T01:00:00Z"
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)

    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_modify_target_sha_after_signature():
    import os

    os.environ["HERMES_PROVENANCE_KEY"] = "testkey"
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {},
    }
    ev["execution_provenance"]["signature"] = sign_evidence(ev, "testkey")

    # Tamper
    ev["target_sha"] = "sha456"
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)

    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates(
        "sha123", bundle
    )  # original target_sha="sha123" is expected
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_modify_payload_recompute_digest_fails_signature():
    import os

    os.environ["HERMES_PROVENANCE_KEY"] = "testkey"
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {},
    }
    ev["execution_provenance"]["signature"] = sign_evidence(ev, "testkey")

    # Tamper
    ev["validator_id"] = "_fake"
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)

    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL

# Task 8.3.3 specific tests for credential risk

def test_hardcoded_false_cannot_authorize():
    from scripts.hermes_release_qualification import check_credential_risk
    # missing evidence blocks
    bundle = {}
    has_risk, err = check_credential_risk(bundle, 'sha123')
    assert has_risk is True
    assert 'Missing' in err

def test_unknown_credential_state_blocks():
    from scripts.hermes_release_qualification import check_credential_risk
    import os
    os.environ['HERMES_PROVENANCE_KEY'] = 'testkey'
    ev = {
        'schema_version': 1,
        'evidence_type': 'credential_risk',
        'target_sha': 'sha123',
        'status': 'PASS',
        'credential_risk_status': 'UNKNOWN',
        'collected_at_utc': valid_utc(),
        'execution_provenance': {'isolation_level': 'docker', 'runtime_identity': 'healbite-production'}
    }
    ev['execution_provenance']['signature'] = sign_evidence(ev, 'testkey')
    ev['evidence_digest'] = compute_canonical_digest_from_dict(ev)
    bundle = {'credential_risk_evidence': ev}
    has_risk, err = check_credential_risk(bundle, 'sha123')
    assert has_risk is True
    assert 'Unknown credential_risk_status: UNKNOWN' in err

def test_proven_risk_blocks():
    from scripts.hermes_release_qualification import check_credential_risk
    import os
    os.environ['HERMES_PROVENANCE_KEY'] = 'testkey'
    ev = {
        'schema_version': 1,
        'evidence_type': 'credential_risk',
        'target_sha': 'sha123',
        'status': 'PASS',
        'credential_risk_status': 'PROVEN_RISK',
        'collected_at_utc': valid_utc(),
        'execution_provenance': {'isolation_level': 'docker', 'runtime_identity': 'healbite-production'}
    }
    ev['execution_provenance']['signature'] = sign_evidence(ev, 'testkey')
    ev['evidence_digest'] = compute_canonical_digest_from_dict(ev)
    bundle = {'credential_risk_evidence': ev}
    has_risk, err = check_credential_risk(bundle, 'sha123')
    assert has_risk is True
    assert err is None

def test_proven_clear_can_proceed():
    from scripts.hermes_release_qualification import check_credential_risk
    import os
    os.environ['HERMES_PROVENANCE_KEY'] = 'testkey'
    ev = {
        'schema_version': 1,
        'evidence_type': 'credential_risk',
        'target_sha': 'sha123',
        'status': 'PASS',
        'credential_risk_status': 'PROVEN_CLEAR',
        'collected_at_utc': valid_utc(),
        'execution_provenance': {'isolation_level': 'docker', 'runtime_identity': 'healbite-production'}
    }
    ev['execution_provenance']['signature'] = sign_evidence(ev, 'testkey')
    ev['evidence_digest'] = compute_canonical_digest_from_dict(ev)
    bundle = {'credential_risk_evidence': ev}
    has_risk, err = check_credential_risk(bundle, 'sha123')
    assert has_risk is False
    assert err is None

def test_malformed_credential_evidence_blocks():
    from scripts.hermes_release_qualification import check_credential_risk
    bundle = {'credential_risk_evidence': 'malformed'}
    has_risk, err = check_credential_risk(bundle, 'sha123')
    assert has_risk is True
    assert 'must be a dictionary' in err


# Task 8.3.5 specific tests for schema compatibility and rollback qualification

from scripts.canonical_schema_contract import EXPECTED_SCHEMA_DIGEST, EXPECTED_USER_VERSION


def make_valid_schema_evidence(target_sha="sha123", key="testkey", **overrides):
    ev = {
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
        "collected_at_utc": valid_utc(),
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "healbite-production",
        },
    }
    ev.update(overrides)
    if key:
        ev["execution_provenance"]["signature"] = sign_evidence(ev, key)
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    return ev


def make_valid_rollback_evidence(target_sha="sha123", key="testkey", **overrides):
    ev = {
        "schema_version": 1,
        "evidence_type": "rollback_ready",
        "target_sha": target_sha,
        "status": "PASS",
        "collected_at_utc": valid_utc(),
        "current_production_image_digest": "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "current_production_oci_revision": "rev123",
        "rollback_image_digest": "sha256:1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        "rollback_image_resolvable": True,
        "rollback_revision": "rev123",
        "rollback_mechanism_id": "docker-compose-revert",
        "same_compose_chain": True,
        "database_restore_required": False,
        "schema_downgrade_required": False,
        "rollback_health_required": True,
        "rollback_attempt_count_max": 1,
        "rollback_procedure_proven": True,
        "canonical_rehearsal_evidence": "artifact:rollback-rehearsal:docker-compose-revert:pass",
        "execution_provenance": {
            "isolation_level": "docker",
            "runtime_identity": "healbite-production",
        },
    }
    ev.update(overrides)
    if key:
        ev["execution_provenance"]["signature"] = sign_evidence(ev, key)
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    return ev


def test_real_matching_schema_accepted(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence()
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.PASS


def test_user_version_mismatch_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(actual_user_version=1, expected_user_version=0)
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "User version mismatch" in gate.reason


def test_table_delta_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(schema_delta="TABLE_DELTA")
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "Table delta detected in schema" in gate.reason


def test_index_delta_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(schema_delta="INDEX_DELTA")
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "Index delta detected in schema" in gate.reason


def test_trigger_delta_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(schema_delta="TRIGGER_DELTA")
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "Trigger delta detected in schema" in gate.reason


def test_unknown_delta_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(schema_delta="CORRUPTED_DELTA")
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "Unknown schema delta" in gate.reason


def test_migration_required_rejected_for_current_release(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(migration_required=True)
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "Migration required" in gate.reason


def test_integrity_failure_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(integrity_status="corrupted")
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "Integrity failure in production schema" in gate.reason


def test_fk_violations_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_schema_evidence(foreign_key_violation_count=2)
    bundle = create_bundle({"schema_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate.status == Status.FAIL
    assert "FK violations in production schema" in gate.reason


def test_signed_self_asserted_none_without_comparison_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev1 = make_valid_schema_evidence(actual_schema_digest="dummy_digest")
    bundle1 = create_bundle({"schema_evidence": ev1})
    gates1, *_ = get_all_gates("sha123", bundle1)
    gate1 = next(g for g in gates1 if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate1.status == Status.FAIL
    assert "Dummy digest in schema evidence" in gate1.reason

    ev2 = make_valid_schema_evidence(
        actual_schema_digest="0000000000000000000000000000000000000000000000000000000000000000"
    )
    bundle2 = create_bundle({"schema_evidence": ev2})
    gates2, *_ = get_all_gates("sha123", bundle2)
    gate2 = next(g for g in gates2 if g.gate_name == "SCHEMA_COMPATIBILITY")
    assert gate2.status == Status.FAIL
    assert "Schema digest mismatch with canonical contract" in gate2.reason


# Rollback tests


def test_mocked_digest_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(
        current_production_image_digest="sha256:mocked1234567890abcdef1234567890abcdef1234567890abcdef1234567890ab"
    )
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Mocked rollback evidence" in gate.reason


def test_empty_digest_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(current_production_image_digest="")
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Empty digest" in gate.reason


def test_invalid_sha256_digest_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(current_production_image_digest="not_sha256_prefix")
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Invalid digest" in gate.reason


def test_unresolvable_rollback_image_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(rollback_image_resolvable=False)
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Unresolvable image" in gate.reason


def test_rollback_oci_revision_mismatch_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(
        current_production_oci_revision="rev1", rollback_revision="rev2"
    )
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Revision mismatch" in gate.reason


def test_wrong_compose_chain_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(same_compose_chain=False)
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Same compose chain not proven" in gate.reason


def test_db_restore_requirement_mismatch_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(database_restore_required=True)
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "DB restore requirement mismatch" in gate.reason


def test_schema_downgrade_requirement_mismatch_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(schema_downgrade_required=True)
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Schema downgrade requirement mismatch" in gate.reason


def test_rollback_health_policy_mismatch_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(rollback_health_required=False)
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Rollback health policy mismatch" in gate.reason


def test_rollback_attempt_limit_mismatch_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(rollback_attempt_count_max=2)
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Rollback attempt limit mismatch" in gate.reason


def test_missing_rehearsal_evidence_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(canonical_rehearsal_evidence="")
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Missing rehearsal evidence" in gate.reason

    ev2 = make_valid_rollback_evidence(canonical_rehearsal_evidence="self-asserted")
    bundle2 = create_bundle({"rollback_evidence": ev2})
    gates2, *_ = get_all_gates("sha123", bundle2)
    gate2 = next(g for g in gates2 if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate2.status == Status.FAIL
    assert "Missing rehearsal evidence" in gate2.reason


def test_self_asserted_procedure_proven_rejected(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence(rollback_procedure_proven="self-asserted")
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.FAIL
    assert "Self-asserted procedure unproven" in gate.reason


def test_valid_real_rollback_contract_accepted(monkeypatch):
    monkeypatch.setenv("HERMES_PROVENANCE_KEY", "testkey")
    ev = make_valid_rollback_evidence()
    bundle = create_bundle({"rollback_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "ROLLBACK_QUALIFIED")
    assert gate.status == Status.PASS
