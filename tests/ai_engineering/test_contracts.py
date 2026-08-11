from __future__ import annotations

import pytest

from ai_engineering.contracts import (
    BEHAVIOUR_TRACE_SCHEMA_VERSION,
    EffectClass,
    Status,
    StopBoundary,
)


def test_trace_schema_version_is_explicit() -> None:
    assert BEHAVIOUR_TRACE_SCHEMA_VERSION == 1


def test_status_taxonomy_is_exact() -> None:
    assert {item.value for item in Status} == {
        "PASS",
        "FAIL",
        "BLOCKED",
        "NOT_RUN",
        "NOT_PERFORMED",
        "UNKNOWN",
        "INCONCLUSIVE",
    }
    with pytest.raises(ValueError):
        Status("SUCCESS")


def test_stop_boundaries_round_trip() -> None:
    expected = {
        "READ_ONLY",
        "LOCAL_DIFF",
        "COMMIT",
        "DRAFT_PR",
        "READY_PR",
        "MERGE",
        "BUILD",
        "DEPLOY",
        "LIVE_SMOKE",
    }
    assert {item.value for item in StopBoundary} == expected
    assert {StopBoundary(value).value for value in expected} == expected


def test_effect_classes_round_trip() -> None:
    expected = {
        "READ_ONLY",
        "REPOSITORY_WRITE",
        "GIT_COMMIT",
        "GIT_PUSH",
        "PR_MUTATION",
        "PR_MERGE",
        "BUILD",
        "DEPLOY",
        "RUNTIME_MUTATION",
        "DATA_MUTATION",
        "VECTOR_MUTATION",
        "SECRET_MUTATION",
        "EXTERNAL_SEND",
        "OTHER_MUTATION",
    }
    assert {item.value for item in EffectClass} == expected
    assert {EffectClass(value).value for value in expected} == expected
