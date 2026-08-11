from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ai_engineering.contracts import Status, TraceValidationError
from ai_engineering.scenario import load_trace_fixture
from ai_engineering.trace import (
    deserialize_trace,
    normalize_trace,
    serialize_trace,
    trace_digest,
    validate_trace,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "ai_engineering" / "traces"


def _payload(name: str = "trace_merge_pass.json") -> dict[str, object]:
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def test_valid_trace_is_accepted() -> None:
    trace = validate_trace(_payload())
    assert trace.schema_version == 1
    assert trace.result.status is Status.PASS


def test_required_field_missing_has_stable_error() -> None:
    payload = _payload()
    del payload["repository"]
    with pytest.raises(TraceValidationError) as caught:
        validate_trace(payload)
    assert caught.value.code == "TRACE_REQUIRED_FIELD_MISSING"


def test_unsupported_schema_version_has_stable_error() -> None:
    payload = _payload()
    payload["schema_version"] = 2
    with pytest.raises(TraceValidationError) as caught:
        validate_trace(payload)
    assert caught.value.code == "TRACE_SCHEMA_VERSION_UNSUPPORTED"


def test_invalid_status_has_stable_error() -> None:
    payload = _payload()
    payload["result"] = {"status": "SUCCESS"}
    with pytest.raises(TraceValidationError) as caught:
        validate_trace(payload)
    assert caught.value.code == "TRACE_STATUS_INVALID"


def test_invalid_effect_class_has_stable_error() -> None:
    payload = _payload()
    task = payload["task"]
    assert isinstance(task, dict)
    task["allowed_effect_classes"] = ["ARBITRARY_MUTATION"]
    with pytest.raises(TraceValidationError) as caught:
        validate_trace(payload)
    assert caught.value.code == "TRACE_EFFECT_CLASS_INVALID"


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "chain_of_thought",
        "private_reasoning",
        "raw_prompt",
        "raw_user_message",
        "raw_provider_response",
        "raw_production_log_payload",
    ],
)
def test_private_reasoning_and_raw_payload_fields_are_rejected(forbidden_key: str) -> None:
    payload = _payload()
    payload["decisions"] = [{forbidden_key: "runtime-generated-private-value"}]
    with pytest.raises(TraceValidationError) as caught:
        validate_trace(payload)
    assert caught.value.code == "TRACE_FORBIDDEN_RAW_FIELD"
    assert "runtime-generated-private-value" not in str(caught.value)


def test_unknown_usage_is_preserved_as_none() -> None:
    normalized = normalize_trace(_payload("trace_unknown_evidence.json"))
    assert normalized["usage"] == {
        "model_calls": None,
        "input_tokens": None,
        "output_tokens": None,
        "estimated_cost": None,
    }
    assert normalized["result"] == {"status": "UNKNOWN"}


def test_serialization_and_digest_are_deterministic() -> None:
    payload = _payload()
    original = copy.deepcopy(payload)
    first = serialize_trace(payload)
    second = serialize_trace(payload)
    assert first == second
    assert payload == original
    assert serialize_trace(deserialize_trace(first)) == first
    assert trace_digest(payload) == trace_digest(payload)

    reordered = dict(reversed(list(payload.items())))
    assert trace_digest(reordered) == trace_digest(payload)

    changed = copy.deepcopy(payload)
    changed["trace_id"] = "trace-merge-changed"
    assert trace_digest(changed) != trace_digest(payload)


def test_duplicate_json_keys_are_rejected() -> None:
    canonical = serialize_trace(_payload())
    duplicate = canonical.replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    with pytest.raises(TraceValidationError) as caught:
        deserialize_trace(duplicate)
    assert caught.value.code == "TRACE_JSON_INVALID"


def test_fixture_loader_returns_same_canonical_identity() -> None:
    trace = load_trace_fixture(FIXTURE_ROOT, "trace_read_only_pass.json")
    assert trace_digest(trace) == trace_digest(deserialize_trace(serialize_trace(trace)))
