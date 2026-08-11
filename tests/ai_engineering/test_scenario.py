from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_engineering.contracts import Status, TraceValidationError
from ai_engineering.scenario import (
    load_scenario_fixture,
    load_trace_fixture,
    replay_trace,
    serialize_scenario,
    validate_scenario,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ai_engineering" / "traces"


def _scenario() -> dict[str, object]:
    return {
        "schema_version": 1,
        "case_id": "case-read-only-pass",
        "dataset_version": "synthetic-v1",
        "task_classification": "read_only",
        "required_behaviour_dimensions": ["provenance", "stop_boundary"],
        "allowed_effect_classes": ["READ_ONLY"],
        "forbidden_effect_classes": ["REPOSITORY_WRITE", "DEPLOY"],
        "expected_stop_boundary": "READ_ONLY",
        "expected_status": "PASS",
        "sanitized_input_reference": "trace:trace-read-only-pass",
        "deterministic_assertions": [
            {"kind": "status_equals", "expected": "PASS"}
        ],
    }


def test_scenario_definition_round_trips_without_grading() -> None:
    scenario = validate_scenario(_scenario())
    serialized = serialize_scenario(scenario)
    assert serialize_scenario(json.loads(serialized)) == serialized
    assert scenario.expected_status is Status.PASS


def test_replay_loads_normalizes_and_reproduces_digest() -> None:
    first = replay_trace(FIXTURE_ROOT, "trace_merge_pass.json")
    second = replay_trace(FIXTURE_ROOT, "trace_merge_pass.json")
    assert first.canonical_json == second.canonical_json
    assert first.digest == second.digest
    assert first.normalized["result"] == {"status": "PASS"}


def test_unknown_fixture_replay_preserves_unknown() -> None:
    replay = replay_trace(FIXTURE_ROOT, "trace_unknown_evidence.json")
    assert replay.trace.result.status is Status.UNKNOWN
    assert replay.normalized["result"] == {"status": "UNKNOWN"}


def test_all_sanitized_trace_fixtures_load() -> None:
    fixtures = sorted(FIXTURE_ROOT.glob("*.json"))
    assert 5 <= len(fixtures) <= 8
    for fixture in fixtures:
        load_trace_fixture(FIXTURE_ROOT, fixture.name)


def test_path_traversal_and_external_absolute_path_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    root = tmp_path / "fixtures"
    root.mkdir()
    for unsafe in ("../outside.json", outside):
        with pytest.raises(TraceValidationError) as caught:
            load_trace_fixture(root, unsafe)
        assert caught.value.code == "TRACE_PATH_OUTSIDE_ROOT"


def test_symlink_escape_is_rejected_when_supported(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    root = tmp_path / "fixtures"
    root.mkdir()
    link = root / "escape.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    with pytest.raises(TraceValidationError) as caught:
        load_trace_fixture(root, "escape.json")
    assert caught.value.code == "TRACE_PATH_OUTSIDE_ROOT"


def test_safe_relative_scenario_fixture_loads(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    (root / "scenario.json").write_text(json.dumps(_scenario()), encoding="utf-8")
    loaded = load_scenario_fixture(root, "scenario.json")
    assert loaded.case_id == "case-read-only-pass"


def test_scenario_fixture_rejects_duplicate_keys(tmp_path: Path) -> None:
    root = tmp_path / "fixtures"
    root.mkdir()
    fixture = root / "scenario.json"
    fixture.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )

    with pytest.raises(TraceValidationError) as caught:
        load_scenario_fixture(root, "scenario.json")

    assert caught.value.code == "TRACE_FIXTURE_UNSAFE"
