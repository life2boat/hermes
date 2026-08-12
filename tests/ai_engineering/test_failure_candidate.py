from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ai_engineering.contracts import Status
from ai_engineering.eval_runner import run_evals
from ai_engineering.failure_candidate import (
    CandidateStatus,
    FailureCandidateError,
    FailureCandidatePolicyError,
    HumanReviewStatus,
    build_failure_eval_candidate,
    normalize_failure_eval_candidate,
    write_failure_eval_candidate,
)
from ai_engineering.trace import deserialize_trace, serialize_trace, trace_digest
from scripts.build_failure_eval_candidate import run


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _trace_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "trace_id": "trace-001",
        "task_id": "task-001",
        "repository": {
            "canonical_remote": "github",
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "branch": "codex/test",
            "worktree_clean": True,
        },
        "task": {
            "task_class": "bounded_implementation",
            "behaviour_sensitive": True,
            "security_sensitive": True,
            "cost_sensitive": False,
            "production_sensitive": False,
            "allowed_effect_classes": ["REPOSITORY_WRITE"],
            "forbidden_effect_classes": ["DEPLOY"],
            "stop_boundary": "DRAFT_PR",
        },
        "llm": {
            "policy_version": None,
            "recommended_model": None,
            "actual_model": None,
            "reasoning_level": None,
        },
        "decisions": [],
        "tool_events": [],
        "gate_results": [],
        "usage": {
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": None,
        },
        "result": {"status": "PASS"},
    }


def _repository(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "repository"
    trace_path = root / "evals/agent_behaviour/fixtures/traces/trace.json"
    failure_path = root / "knowledge/failures/failure.md"
    candidates = root / "evals/agent_behaviour/candidates"
    trace_path.parent.mkdir(parents=True)
    failure_path.parent.mkdir(parents=True)
    candidates.mkdir(parents=True)
    trace_path.write_text(serialize_trace(_trace_payload()), encoding="utf-8")
    failure_path.write_text(
        "# Sanitized failure\n\nCause: deterministic contract mismatch.\n",
        encoding="utf-8",
    )
    payload = {
        "schema_version": 1,
        "candidate_id": "candidate-001",
        "failure_record_ref": "knowledge/failures/failure.md",
        "trace_reference": "evals/agent_behaviour/fixtures/traces/trace.json",
        "trace_digest": trace_digest(deserialize_trace(serialize_trace(_trace_payload()))),
        "base_dataset_version": "agent-behaviour-v1",
        "base_corpus_digest": "a" * 64,
        "proposed_category": "failure_handling",
        "proposed_behaviour": "preserve_trace_identity",
        "proposed_criticality": False,
        "proposed_expected_evaluation_status": "PASS",
        "required_behaviour_dimensions": ["truthfulness", "failure_handling"],
        "reason_codes": ["SANITIZED_FAILURE_CONFIRMED"],
        "promotion_intent": "CANDIDATE",
    }
    return root, payload


def test_valid_sanitized_failure_builds_candidate_only(tmp_path: Path) -> None:
    root, payload = _repository(tmp_path)

    candidate = build_failure_eval_candidate(root, payload)
    normalized = normalize_failure_eval_candidate(candidate)

    assert candidate.candidate_status is CandidateStatus.CANDIDATE
    assert candidate.human_review_status is HumanReviewStatus.NOT_PERFORMED
    assert candidate.promotion_authorized is False
    assert normalized["candidate_digest"] == candidate.candidate_digest
    assert normalized["candidate_status"] == "CANDIDATE"


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("failure_record_ref", "knowledge/failures/missing.md", "FAILURE_CANDIDATE_FAILURE_RECORD_MISSING"),
        (
            "trace_reference",
            "evals/agent_behaviour/fixtures/traces/missing.json",
            "FAILURE_CANDIDATE_TRACE_MISSING",
        ),
        ("failure_record_ref", "knowledge/failures/../failure.md", "FAILURE_CANDIDATE_PATH_OUTSIDE_ROOT"),
    ],
)
def test_missing_or_unsafe_evidence_blocks_candidate(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    root, payload = _repository(tmp_path)
    payload[field] = value

    with pytest.raises(FailureCandidateError, match=code):
        build_failure_eval_candidate(root, payload)


def test_trace_digest_mismatch_blocks_candidate(tmp_path: Path) -> None:
    root, payload = _repository(tmp_path)
    payload["trace_digest"] = "c" * 64

    with pytest.raises(FailureCandidateError, match="FAILURE_CANDIDATE_TRACE_DIGEST_MISMATCH"):
        build_failure_eval_candidate(root, payload)


def test_symlink_escape_blocks_candidate(tmp_path: Path) -> None:
    root, payload = _repository(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    link = root / "knowledge/failures/link.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation unavailable in this test environment")
    payload["failure_record_ref"] = "knowledge/failures/link.md"

    with pytest.raises(FailureCandidateError, match="FAILURE_CANDIDATE_EVIDENCE_UNSAFE"):
        build_failure_eval_candidate(root, payload)


def test_raw_or_direct_promotion_request_cannot_produce_candidate(tmp_path: Path) -> None:
    root, payload = _repository(tmp_path)
    raw = {**payload, "raw_prompt": "forbidden"}
    with pytest.raises(FailureCandidatePolicyError, match="FAILURE_CANDIDATE_RAW_EVIDENCE_FORBIDDEN"):
        build_failure_eval_candidate(root, raw)

    secret_field = {**payload, "credential": "<REDACTED>"}
    with pytest.raises(FailureCandidatePolicyError, match="FAILURE_CANDIDATE_RAW_EVIDENCE_FORBIDDEN"):
        build_failure_eval_candidate(root, secret_field)

    payload["promotion_intent"] = "GOLDEN"
    with pytest.raises(FailureCandidatePolicyError, match="DIRECT_GOLDEN_PROMOTION_FORBIDDEN"):
        build_failure_eval_candidate(root, payload)


def test_writer_refuses_overwrite_and_remains_inside_candidates(tmp_path: Path) -> None:
    root, payload = _repository(tmp_path)
    candidate = build_failure_eval_candidate(root, payload)
    output = Path("evals/agent_behaviour/candidates/candidate-001.json")

    write_failure_eval_candidate(root, output, candidate)

    with pytest.raises(FailureCandidateError, match="FAILURE_CANDIDATE_OUTPUT_UNSAFE"):
        write_failure_eval_candidate(root, output, candidate)
    with pytest.raises(FailureCandidateError, match="FAILURE_CANDIDATE_OUTPUT_UNSAFE"):
        write_failure_eval_candidate(root, Path("knowledge/failures/candidate.json"), candidate)


def test_candidate_construction_does_not_change_golden_corpus_digest(tmp_path: Path) -> None:
    before = run_evals(REPOSITORY_ROOT / "evals/agent_behaviour").corpus_digest
    root, payload = _repository(tmp_path)

    build_failure_eval_candidate(root, payload)

    after = run_evals(REPOSITORY_ROOT / "evals/agent_behaviour").corpus_digest
    assert after == before == "e2580fb10c6d02a55ace0efc9092bd6f3092a9a3a188515c5dba32b44708c8c7"


def test_cli_reports_pass_fail_and_blocked_without_writing_golden(tmp_path: Path, capsys) -> None:
    root, payload = _repository(tmp_path)
    input_path = tmp_path / "candidate-input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    assert run(["--repository-root", str(root), "--input", str(input_path), "--dry-run"]) == 0
    passed = json.loads(capsys.readouterr().out)
    assert passed["status"] == Status.PASS.value
    assert passed["candidate"]["promotion_authorized"] is False

    payload["promotion_intent"] = "GOLDEN"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    assert run(["--repository-root", str(root), "--input", str(input_path), "--dry-run"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed == {"reason_codes": ["DIRECT_GOLDEN_PROMOTION_FORBIDDEN"], "schema_version": 1, "status": "FAIL"}

    payload["promotion_intent"] = "CANDIDATE"
    payload["trace_reference"] = "evals/agent_behaviour/fixtures/traces/missing.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    assert run(["--repository-root", str(root), "--input", str(input_path), "--dry-run"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == Status.BLOCKED.value
