from __future__ import annotations

import copy

import pytest

from ai_engineering.contracts import TraceValidationError
from ai_engineering.redaction import (
    REDACTED,
    is_sanitized_evidence,
    sanitize_evidence,
    verify_sanitized_evidence,
)


def _synthetic_bearer() -> str:
    return "Bearer " + "runtime" + "-only-" + "credential-material-12345"


def test_forbidden_sensitive_key_is_rejected_without_echo() -> None:
    marker = "runtime-private-marker"
    with pytest.raises(TraceValidationError) as caught:
        sanitize_evidence({"raw_prompt": marker})
    assert caught.value.code == "TRACE_FORBIDDEN_RAW_FIELD"
    assert marker not in str(caught.value)


def test_bearer_value_is_redacted_and_input_is_not_mutated() -> None:
    original = {"metadata": {"authorization": _synthetic_bearer()}, "status": "PASS"}
    before = copy.deepcopy(original)
    sanitized = sanitize_evidence(original)
    assert sanitized == {
        "metadata": {"authorization": REDACTED},
        "status": "PASS",
    }
    assert original == before
    assert _synthetic_bearer() not in repr(sanitized)


def test_credential_like_structured_value_cannot_survive() -> None:
    with pytest.raises(TraceValidationError) as caught:
        sanitize_evidence({"nested": {"credential": "generated-at-runtime"}})
    assert caught.value.code == "TRACE_FORBIDDEN_RAW_FIELD"


def test_nested_unredacted_authorization_is_not_sanitized() -> None:
    value = {"outer": [{"authorization": _synthetic_bearer()}]}
    assert not is_sanitized_evidence(value)
    with pytest.raises(TraceValidationError) as caught:
        verify_sanitized_evidence(value)
    assert caught.value.code == "TRACE_VALUE_NOT_SANITIZED"
    assert _synthetic_bearer() not in str(caught.value)


def test_normal_sanitized_evidence_is_preserved() -> None:
    value = {"status": "PASS", "evidence_refs": ["receipt:synthetic"]}
    assert sanitize_evidence(value) == value
    assert is_sanitized_evidence(value)
