from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_engineering.contracts import TraceValidationError
from ai_engineering.trace import (
    deserialize_trace,
    normalize_trace,
    serialize_trace,
    trace_digest,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "ai_engineering"
    / "traces"
    / "trace_merge_pass.json"
)


def _v2_payload() -> dict[str, object]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    payload["prompt"] = {
        "prompt_id": "repository-fix",
        "prompt_version": "1.0.0",
        "prompt_digest": "a" * 64,
        "prompt_template_version": "prompt-template-v1",
        "eval_set_version": "prompt-quality-v1",
        "model_id": "synthetic-model",
        "context_source_ids": ["repo:canonical", "fixture:review"],
        "output_schema_version": "review-output-v1",
    }
    return payload


def test_trace_v2_records_prompt_provenance_without_raw_prompt() -> None:
    payload = _v2_payload()
    normalized = normalize_trace(payload)
    assert normalized["schema_version"] == 2
    assert normalized["prompt"] == payload["prompt"]
    canonical = serialize_trace(payload)
    assert serialize_trace(deserialize_trace(canonical)) == canonical
    assert len(trace_digest(payload)) == 64
    assert "prompt_text" not in canonical


def test_trace_v1_remains_byte_compatible_and_has_no_prompt_field() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    canonical = serialize_trace(payload)
    assert normalize_trace(payload)["schema_version"] == 1
    assert "prompt" not in normalize_trace(payload)
    assert serialize_trace(deserialize_trace(canonical)) == canonical


def test_trace_v2_requires_exact_prompt_schema_and_digest() -> None:
    missing = _v2_payload()
    del missing["prompt"]
    with pytest.raises(TraceValidationError) as caught:
        normalize_trace(missing)
    assert caught.value.code == "TRACE_REQUIRED_FIELD_MISSING"

    invalid = _v2_payload()
    assert isinstance(invalid["prompt"], dict)
    invalid["prompt"]["prompt_digest"] = "not-a-digest"
    with pytest.raises(TraceValidationError) as caught:
        normalize_trace(invalid)
    assert caught.value.code == "TRACE_VALUE_INVALID"


def test_trace_rejects_raw_prompt_or_hidden_reasoning_fields() -> None:
    payload = _v2_payload()
    assert isinstance(payload["prompt"], dict)
    payload["prompt"]["raw_prompt"] = "unreviewed payload"
    with pytest.raises(TraceValidationError) as caught:
        normalize_trace(payload)
    assert caught.value.code == "TRACE_FORBIDDEN_RAW_FIELD"
