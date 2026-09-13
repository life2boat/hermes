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


def test_secret_evidence_missing():
    bundle = create_bundle({})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.BLOCKED


def test_secret_evidence_empty_dict():
    bundle = create_bundle({"secret_evidence": {}})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_evidence_garbage():
    bundle = create_bundle({"secret_evidence": "garbage"})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_status_fail():
    bundle = create_bundle({"secret_evidence": {"status": "FAIL"}})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_foreign_target_sha():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "wrong_sha",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": "time",
        "required_secrets": [{"name": "A", "required": True, "present": True}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_digest_mismatch():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": "time",
        "required_secrets": [{"name": "A", "required": True, "present": True}],
    }
    ev["evidence_digest"] = "bad_digest"
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_secret_contains_value():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": "time",
        "required_secrets": [
            {"name": "A", "required": True, "present": True, "value": "secret123"}
        ],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_required_secret_present_false():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": "time",
        "required_secrets": [{"name": "A", "required": True, "present": False}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.FAIL


def test_valid_safe_secret_metadata():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_secret_presence",
        "target_sha": "sha123",
        "status": "PASS",
        "source_class": "test",
        "collected_at_utc": "time",
        "required_secrets": [{"name": "A", "required": True, "present": True}],
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"secret_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "SECRET_CONTRACT")
    assert gate.status == Status.PASS


def test_db_evidence_missing():
    bundle = create_bundle({})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.BLOCKED


def test_db_evidence_empty_dict():
    bundle = create_bundle({"db_evidence": {}})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_evidence_garbage():
    bundle = create_bundle({"db_evidence": "garbage"})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_status_fail():
    bundle = create_bundle({"db_evidence": {"status": "FAIL"}})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_foreign_target_sha():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "wrong_sha",
        "status": "PASS",
        "validator_id": "canonical-hermes-db-path-validator",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": "time",
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_digest_mismatch():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "canonical-hermes-db-path-validator",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": "time",
    }
    ev["evidence_digest"] = "bad_digest"
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_unknown_validator():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "unknown-validator",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": "time",
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_db_negative_path_classification():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "canonical-hermes-db-path-validator",
        "validator_version": "1",
        "path_classification": "unknown-path",
        "collected_at_utc": "time",
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.FAIL


def test_valid_canonical_validator_evidence():
    ev = {
        "schema_version": 1,
        "evidence_type": "production_db_path_safety",
        "target_sha": "sha123",
        "status": "PASS",
        "validator_id": "canonical-hermes-db-path-validator",
        "validator_version": "1",
        "path_classification": "authoritative-production-path",
        "collected_at_utc": "time",
    }
    ev["evidence_digest"] = compute_canonical_digest_from_dict(ev)
    bundle = create_bundle({"db_evidence": ev})
    gates, *_ = get_all_gates("sha123", bundle)
    gate = next(g for g in gates if g.gate_name == "DB_PATH_SAFETY")
    assert gate.status == Status.PASS
