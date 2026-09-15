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
    from datetime import datetime
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


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
