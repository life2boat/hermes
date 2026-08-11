from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from ai_engineering.contracts import ScenarioAssertion, Status
from ai_engineering.graders import (
    ASSERTION_REGISTRY,
    aggregate_observed_status,
    run_assertions,
    run_graders,
)
from ai_engineering.scenario import load_trace_fixture, validate_scenario


EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals" / "agent_behaviour"


def _case(category: str, case_id: str):
    dataset = EVAL_ROOT / "datasets" / f"{category}.jsonl"
    for line in dataset.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if value["scenario"]["case_id"] == case_id:
            scenario = validate_scenario(value["scenario"])
            trace = load_trace_fixture(EVAL_ROOT, value["trace_reference"])
            return trace, scenario
    raise AssertionError(case_id)


def test_authorized_effect_passes_and_unauthorized_side_effect_fails() -> None:
    trace, scenario = _case("authority", "authority-repository-write")
    assert aggregate_observed_status(run_graders(trace, scenario)) is Status.PASS

    trace, scenario = _case("authority", "authority-merge-denied")
    assert aggregate_observed_status(run_graders(trace, scenario)) is Status.FAIL


def test_forbidden_and_outside_allow_list_effects_fail() -> None:
    trace, scenario = _case("adversarial", "adversarial-forbidden-deploy")
    assert aggregate_observed_status(run_graders(trace, scenario)) is Status.FAIL

    trace, scenario = _case("authority", "authority-repository-write")
    narrowed = replace(scenario, allowed_effect_classes=())
    assert aggregate_observed_status(run_graders(trace, narrowed)) is Status.FAIL


def test_stop_boundary_respected_and_exceeded() -> None:
    trace, scenario = _case("stop_boundaries", "stop-draft-pr")
    assert aggregate_observed_status(run_graders(trace, scenario)) is Status.PASS

    trace, scenario = _case("stop_boundaries", "stop-ready-pr-merge")
    assert aggregate_observed_status(run_graders(trace, scenario)) is Status.FAIL


def test_truthful_pass_and_fabricated_pass() -> None:
    trace, scenario = _case("truthfulness", "truth-supported-pass")
    assert aggregate_observed_status(run_graders(trace, scenario)) is Status.PASS

    for case_id in (
        "truth-unknown-pass",
        "truth-not-run-pass",
        "truth-inconclusive-pass",
        "truth-fail-pass",
    ):
        trace, scenario = _case("truthfulness", case_id)
        assert aggregate_observed_status(run_graders(trace, scenario)) is Status.FAIL


def test_unknown_required_evidence_never_becomes_pass() -> None:
    for case_id in (
        "unknown-required-unknown",
        "unknown-required-not-run",
        "unknown-required-inconclusive",
    ):
        trace, scenario = _case("unknown_handling", case_id)
        assert aggregate_observed_status(run_graders(trace, scenario)) is Status.BLOCKED


def test_denied_attempt_without_side_effect_is_safe() -> None:
    trace, scenario = _case("tool_safety", "tool-denied-attempt")
    assert aggregate_observed_status(run_graders(trace, scenario)) is Status.PASS


def test_assertion_registry_is_closed() -> None:
    assert "eval" not in ASSERTION_REGISTRY
    trace, scenario = _case("provenance", "provenance-fresh")
    unknown = replace(
        scenario,
        deterministic_assertions=(
            ScenarioAssertion(kind="dynamic_expression", expected=True),
        ),
    )
    result = run_assertions(trace, unknown)
    assert result[0].status is Status.BLOCKED
    assert result[0].reason_code == "EVAL_ASSERTION_KIND_UNKNOWN"
