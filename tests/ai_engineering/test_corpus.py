from __future__ import annotations

import json
from pathlib import Path

from ai_engineering.contracts import Status
from ai_engineering.eval_runner import run_evals
from ai_engineering.graders import ASSERTION_REGISTRY, GRADER_REGISTRY
from ai_engineering.redaction import verify_sanitized_evidence
from ai_engineering.scenario import load_trace_fixture, validate_scenario
from ai_engineering.trace import trace_digest


EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals" / "agent_behaviour"


def _records():
    manifest = json.loads((EVAL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    for dataset in manifest["datasets"]:
        for line in (EVAL_ROOT / dataset["path"]).read_text(encoding="utf-8").splitlines():
            yield dataset["category"], json.loads(line)


def test_corpus_contract_is_complete_unique_and_sanitized() -> None:
    records = list(_records())
    case_ids = [record["scenario"]["case_id"] for _category, record in records]
    assert len(records) == 49
    assert len(set(case_ids)) == len(case_ids)
    assert {record["scenario"]["dataset_version"] for _category, record in records} == {
        "agent-behaviour-v1"
    }

    references: set[str] = set()
    for category, record in records:
        scenario = validate_scenario(record["scenario"])
        assert scenario.schema_version == 2
        assert scenario.task_classification == category
        assert all(
            dimension in GRADER_REGISTRY
            for dimension in scenario.required_behaviour_dimensions
        )
        assert all(
            assertion.kind in ASSERTION_REGISTRY
            for assertion in scenario.deterministic_assertions
        )
        trace = load_trace_fixture(EVAL_ROOT, record["trace_reference"])
        assert trace_digest(trace) == record["expected_trace_digest"]
        verify_sanitized_evidence(record)
        references.add(record["trace_reference"])

    fixture_paths = {
        path.relative_to(EVAL_ROOT).as_posix()
        for path in (EVAL_ROOT / "fixtures" / "traces").glob("*.json")
    }
    assert references == fixture_paths


def test_manifest_categories_and_critical_cases_are_explicit() -> None:
    manifest = json.loads((EVAL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["corpus_status"] == "GOLDEN"
    categories = {dataset["category"] for dataset in manifest["datasets"]}
    assert categories == {
        "provenance",
        "authority",
        "stop_boundaries",
        "tool_safety",
        "truthfulness",
        "unknown_handling",
        "failure_handling",
        "self_improvement",
        "adversarial",
    }
    result = run_evals(EVAL_ROOT)
    assert result.status is Status.PASS
    assert result.critical_passed == result.critical_total
    assert result.critical_failed == 0


def test_review_file_records_bound_human_approval() -> None:
    review = (EVAL_ROOT / "CORPUS_REVIEW.md").read_text(encoding="utf-8")
    assert "GOLDEN; human review PASS" in review
    assert "Candidate reviewed head: fa77b12cb9a0f1b1e8b0eaa596cd41092fdfdb20" in review
    assert "Human reviewer: Operator" in review
    assert "Review decision: PASS - approved for GOLDEN promotion" in review
    assert "Engine version: 1" in review
    assert "promotion-only metadata commit" in review
    assert "behavioural-content change changes the digest" in review
