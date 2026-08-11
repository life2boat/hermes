"""Safe fixture loading and replay substrate for future behaviour evals."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn

from ai_engineering.contracts import (
    SCENARIO_SCHEMA_VERSION,
    EffectClass,
    ReplayResult,
    ScenarioAssertion,
    ScenarioDefinition,
    Status,
    StopBoundary,
    TraceValidationError,
)
from ai_engineering.redaction import reject_forbidden_raw_fields, verify_sanitized_evidence
from ai_engineering.trace import deserialize_trace, normalize_trace, serialize_trace, trace_digest


MAX_FIXTURE_BYTES = 1_048_576

_SCENARIO_FIELDS = frozenset(
    {
        "schema_version",
        "case_id",
        "dataset_version",
        "task_classification",
        "required_behaviour_dimensions",
        "allowed_effect_classes",
        "forbidden_effect_classes",
        "expected_stop_boundary",
        "expected_status",
        "sanitized_input_reference",
        "deterministic_assertions",
    }
)
_ASSERTION_FIELDS = frozenset({"kind", "expected"})

class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result

def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError


def _fail(code: str) -> NoReturn:
    raise TraceValidationError(code)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _fail("TRACE_REQUIRED_FIELD_MISSING")
    return value


def _exact_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    keys = frozenset(payload)
    if expected - keys:
        _fail("TRACE_REQUIRED_FIELD_MISSING")
    if keys - expected:
        _fail("TRACE_UNEXPECTED_FIELD")
    return payload


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        _fail("TRACE_VALUE_INVALID")
    if any(not (character.isalnum() or character in "._:/-") for character in value):
        _fail("TRACE_VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("TRACE_VALUE_INVALID")
    return value


def _effect(value: object) -> EffectClass:
    if not isinstance(value, str):
        _fail("TRACE_EFFECT_CLASS_INVALID")
    try:
        return EffectClass(value)
    except ValueError:
        _fail("TRACE_EFFECT_CLASS_INVALID")


def _effects(value: object) -> tuple[EffectClass, ...]:
    result = tuple(_effect(item) for item in _items(value))
    if len(result) != len(set(result)):
        _fail("TRACE_EFFECT_CLASS_INVALID")
    return result


def validate_scenario(value: ScenarioDefinition | Mapping[str, object]) -> ScenarioDefinition:
    """Validate the closed scenario-definition substrate without grading it."""

    if isinstance(value, ScenarioDefinition):
        return validate_scenario(normalize_scenario(value))
    reject_forbidden_raw_fields(value)
    payload = _exact_fields(value, _SCENARIO_FIELDS)
    version = payload["schema_version"]
    if version != SCENARIO_SCHEMA_VERSION or isinstance(version, bool):
        _fail("TRACE_SCHEMA_VERSION_UNSUPPORTED")
    try:
        stop_boundary = StopBoundary(payload["expected_stop_boundary"])
    except (ValueError, TypeError):
        _fail("TRACE_STOP_BOUNDARY_INVALID")
    try:
        expected_status = Status(payload["expected_status"])
    except (ValueError, TypeError):
        _fail("TRACE_STATUS_INVALID")
    dimensions = tuple(_identifier(item) for item in _items(payload["required_behaviour_dimensions"]))
    if len(dimensions) != len(set(dimensions)):
        _fail("TRACE_VALUE_INVALID")
    assertions: list[ScenarioAssertion] = []
    for item in _items(payload["deterministic_assertions"]):
        assertion = _exact_fields(item, _ASSERTION_FIELDS)
        expected = assertion["expected"]
        if expected is not None and not isinstance(expected, (str, int, bool)):
            _fail("TRACE_VALUE_INVALID")
        assertions.append(
            ScenarioAssertion(kind=_identifier(assertion["kind"]), expected=expected)
        )
    scenario = ScenarioDefinition(
        schema_version=SCENARIO_SCHEMA_VERSION,
        case_id=_identifier(payload["case_id"]),
        dataset_version=_identifier(payload["dataset_version"]),
        task_classification=_identifier(payload["task_classification"]),
        required_behaviour_dimensions=dimensions,
        allowed_effect_classes=_effects(payload["allowed_effect_classes"]),
        forbidden_effect_classes=_effects(payload["forbidden_effect_classes"]),
        expected_stop_boundary=stop_boundary,
        expected_status=expected_status,
        sanitized_input_reference=_identifier(payload["sanitized_input_reference"]),
        deterministic_assertions=tuple(assertions),
    )
    verify_sanitized_evidence(normalize_scenario(scenario))
    return scenario


def normalize_scenario(value: ScenarioDefinition | Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, ScenarioDefinition):
        value = validate_scenario(value)
    return {
        "schema_version": value.schema_version,
        "case_id": value.case_id,
        "dataset_version": value.dataset_version,
        "task_classification": value.task_classification,
        "required_behaviour_dimensions": list(value.required_behaviour_dimensions),
        "allowed_effect_classes": [item.value for item in value.allowed_effect_classes],
        "forbidden_effect_classes": [item.value for item in value.forbidden_effect_classes],
        "expected_stop_boundary": value.expected_stop_boundary.value,
        "expected_status": value.expected_status.value,
        "sanitized_input_reference": value.sanitized_input_reference,
        "deterministic_assertions": [
            {"kind": item.kind, "expected": item.expected}
            for item in value.deterministic_assertions
        ],
    }


def serialize_scenario(value: ScenarioDefinition | Mapping[str, object]) -> str:
    return json.dumps(
        normalize_scenario(validate_scenario(value)),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _safe_fixture_bytes(fixture_root: Path, relative_path: str | Path) -> bytes:
    if not isinstance(relative_path, (str, Path)):
        _fail("TRACE_PATH_OUTSIDE_ROOT")
    reference = os.fspath(relative_path)
    if not reference or "\\" in reference:
        _fail("TRACE_PATH_OUTSIDE_ROOT")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        _fail("TRACE_PATH_OUTSIDE_ROOT")
    try:
        if fixture_root.is_symlink():
            _fail("TRACE_FIXTURE_UNSAFE")
        root = fixture_root.resolve(strict=True)
        if not root.is_dir():
            _fail("TRACE_FIXTURE_UNSAFE")
        candidate = root.joinpath(relative)
        if candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
        else:
            resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            _fail("TRACE_PATH_OUTSIDE_ROOT")
        if not resolved.is_file():
            _fail("TRACE_FIXTURE_UNSAFE")
        data = resolved.read_bytes()
    except TraceValidationError:
        raise
    except (OSError, RuntimeError):
        _fail("TRACE_FIXTURE_UNSAFE")
    if not data or len(data) > MAX_FIXTURE_BYTES or b"\x00" in data:
        _fail("TRACE_FIXTURE_UNSAFE")
    return data


def load_trace_fixture(fixture_root: Path, relative_path: str | Path):
    """Load a sanitized trace fixture confined to an approved root."""

    return deserialize_trace(_safe_fixture_bytes(fixture_root, relative_path))


def load_scenario_fixture(
    fixture_root: Path, relative_path: str | Path
) -> ScenarioDefinition:
    """Load a scenario definition without executing assertions or graders."""

    data = _safe_fixture_bytes(fixture_root, relative_path)
    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        ValueError,
        RecursionError,
    ) as exc:
        raise TraceValidationError("TRACE_FIXTURE_UNSAFE") from exc
    return validate_scenario(_mapping(payload))


def replay_trace(fixture_root: Path, relative_path: str | Path) -> ReplayResult:
    """Reproduce normalized evidence identity; this does not judge behaviour."""

    trace = load_trace_fixture(fixture_root, relative_path)
    normalized = normalize_trace(trace)
    canonical_json = serialize_trace(trace)
    digest = trace_digest(trace)
    if hashlib.sha256(canonical_json.encode("utf-8")).hexdigest() != digest:
        _fail("TRACE_REPLAY_MISMATCH")
    return ReplayResult(
        trace=trace,
        normalized=normalized,
        canonical_json=canonical_json,
        digest=digest,
    )
