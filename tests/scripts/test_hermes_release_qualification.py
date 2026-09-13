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
