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
    return "2026-09-14T00:00:00Z"


# 1. SECRET RECORD SCHEMA MUST BE CLOSED


def test_secret_record_empty_dict():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [{}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_record_foo_bar():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [{"foo": "bar"}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_missing_name():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [
            {"required": True, "present": True, "source_class": "env"}
        ],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_missing_required():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [{"name": "A", "present": True, "source_class": "env"}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_missing_present():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [{"name": "A", "required": True, "source_class": "env"}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_missing_source_class():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [{"name": "A", "required": True, "present": True}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_required_not_bool():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [
            {"name": "A", "required": "true", "present": True, "source_class": "env"}
        ],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_present_not_bool():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [
            {"name": "A", "required": True, "present": "yes", "source_class": "env"}
        ],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_unknown_field():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [
            {
                "name": "A",
                "required": True,
                "present": True,
                "source_class": "env",
                "raw_secret": "xyz",
            }
        ],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_set_incomplete():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": valid_utc(),
        "required_secrets": [],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_set_substituted():
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
            },
            {
                "name": "FOO_BAR",
                "required": True,
                "present": True,
                "source_class": "env",
            },
        ],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_malformed_utc_timestamp():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": "2026-09-14 00:00:00",
        "required_secrets": [
            {
                "name": "TELEGRAM_BOT_TOKEN",
                "required": True,
                "present": True,
                "source_class": "env",
            }
        ],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


# DB TESTS


def test_db_correct_validator_id_no_trusted_execution():
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
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status in (Status.FAIL, Status.BLOCKED)


def test_db_caller_generated_classification_only():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
    }
    # Missing execution_provenance entirely
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status in (Status.FAIL, Status.BLOCKED)


def test_db_foreign_validator_revision():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "2",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": {"signature": "abc"},
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_malformed_validator_output():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "_validate_database_source_path",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": valid_utc(),
        "execution_provenance": "a string instead of dict",
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_trusted_canonical_validator_output():
    import hmac, hashlib, os
    
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
    os.environ["HERMES_PROVENANCE_KEY"] = "testkey"
    ev["execution_provenance"] = {
        "signature": hmac.new(
            b"testkey", ev["target_sha"].encode(), hashlib.sha256
        ).hexdigest()
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.PASS
