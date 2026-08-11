"""Validation and canonical serialization for behaviour trace schema v1."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn, TypeVar

from ai_engineering.contracts import (
    BEHAVIOUR_TRACE_SCHEMA_VERSION,
    BehaviourTrace,
    DecisionEvent,
    EffectClass,
    GateResult,
    LLMEvidence,
    RepositoryEvidence,
    Status,
    StopBoundary,
    TaskEvidence,
    ToolEvent,
    TraceResult,
    TraceValidationError,
    UsageEvidence,
)
from ai_engineering.redaction import reject_forbidden_raw_fields, verify_sanitized_evidence


MAX_TRACE_BYTES = 1_048_576

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,255}$")
_REASONING_LEVELS = frozenset({"low", "medium", "high"})

_TRACE_FIELDS = frozenset(
    {
        "schema_version",
        "trace_id",
        "task_id",
        "repository",
        "task",
        "llm",
        "decisions",
        "tool_events",
        "gate_results",
        "usage",
        "result",
    }
)
_REPOSITORY_FIELDS = frozenset(
    {"canonical_remote", "base_sha", "head_sha", "branch", "worktree_clean"}
)
_TASK_FIELDS = frozenset(
    {
        "task_class",
        "behaviour_sensitive",
        "security_sensitive",
        "cost_sensitive",
        "production_sensitive",
        "allowed_effect_classes",
        "forbidden_effect_classes",
        "stop_boundary",
    }
)
_LLM_FIELDS = frozenset(
    {"policy_version", "recommended_model", "actual_model", "reasoning_level"}
)
_DECISION_FIELDS = frozenset(
    {"decision_code", "status", "reason_code", "evidence_refs"}
)
_TOOL_FIELDS = frozenset(
    {
        "event_id",
        "tool_name",
        "effect_class",
        "authorization_status",
        "outcome_status",
        "side_effect",
        "evidence_refs",
    }
)
_GATE_FIELDS = frozenset({"gate_name", "required", "status", "evidence_refs"})
_USAGE_FIELDS = frozenset(
    {"model_calls", "input_tokens", "output_tokens", "estimated_cost"}
)
_RESULT_FIELDS = frozenset({"status"})

_EnumT = TypeVar("_EnumT", Status, StopBoundary, EffectClass)


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


def _enum(value: object, enum_type: type[_EnumT], code: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        _fail(code)
    try:
        return enum_type(value)
    except ValueError:
        _fail(code)


def _identifier(value: object, code: str = "TRACE_VALUE_INVALID") -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _optional_identifier(value: object) -> str | None:
    if value is None:
        return None
    return _identifier(value)


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        _fail("TRACE_VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("TRACE_VALUE_INVALID")
    return value


def _references(value: object) -> tuple[str, ...]:
    references: list[str] = []
    for item in _items(value):
        if not isinstance(item, str) or _REFERENCE_RE.fullmatch(item) is None:
            _fail("TRACE_VALUE_INVALID")
        references.append(item)
    if len(references) != len(set(references)):
        _fail("TRACE_VALUE_INVALID")
    return tuple(references)


def _effects(value: object) -> tuple[EffectClass, ...]:
    effects = tuple(
        _enum(item, EffectClass, "TRACE_EFFECT_CLASS_INVALID") for item in _items(value)
    )
    if len(effects) != len(set(effects)):
        _fail("TRACE_EFFECT_CLASS_INVALID")
    return effects


def _status(value: object) -> Status:
    return _enum(value, Status, "TRACE_STATUS_INVALID")


def _stop_boundary(value: object) -> StopBoundary:
    return _enum(value, StopBoundary, "TRACE_STOP_BOUNDARY_INVALID")


def _optional_count(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail("TRACE_USAGE_INVALID")
    return value


def _optional_cost(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail("TRACE_USAGE_INVALID")
    if value < 0 or (isinstance(value, float) and not math.isfinite(value)):
        _fail("TRACE_USAGE_INVALID")
    return value


def _repository(value: object) -> RepositoryEvidence:
    payload = _exact_fields(value, _REPOSITORY_FIELDS)
    base_sha = payload["base_sha"]
    head_sha = payload["head_sha"]
    if (
        not isinstance(base_sha, str)
        or _SHA_RE.fullmatch(base_sha) is None
        or not isinstance(head_sha, str)
        or _SHA_RE.fullmatch(head_sha) is None
    ):
        _fail("TRACE_VALUE_INVALID")
    return RepositoryEvidence(
        canonical_remote=_identifier(payload["canonical_remote"]),
        base_sha=base_sha,
        head_sha=head_sha,
        branch=_identifier(payload["branch"]),
        worktree_clean=_boolean(payload["worktree_clean"]),
    )


def _task(value: object) -> TaskEvidence:
    payload = _exact_fields(value, _TASK_FIELDS)
    return TaskEvidence(
        task_class=_identifier(payload["task_class"]),
        behaviour_sensitive=_boolean(payload["behaviour_sensitive"]),
        security_sensitive=_boolean(payload["security_sensitive"]),
        cost_sensitive=_boolean(payload["cost_sensitive"]),
        production_sensitive=_boolean(payload["production_sensitive"]),
        allowed_effect_classes=_effects(payload["allowed_effect_classes"]),
        forbidden_effect_classes=_effects(payload["forbidden_effect_classes"]),
        stop_boundary=_stop_boundary(payload["stop_boundary"]),
    )


def _llm(value: object) -> LLMEvidence:
    payload = _exact_fields(value, _LLM_FIELDS)
    reasoning = payload["reasoning_level"]
    if reasoning is not None and (
        not isinstance(reasoning, str) or reasoning not in _REASONING_LEVELS
    ):
        _fail("TRACE_VALUE_INVALID")
    return LLMEvidence(
        policy_version=_optional_identifier(payload["policy_version"]),
        recommended_model=_optional_identifier(payload["recommended_model"]),
        actual_model=_optional_identifier(payload["actual_model"]),
        reasoning_level=reasoning,
    )


def _decisions(value: object) -> tuple[DecisionEvent, ...]:
    results: list[DecisionEvent] = []
    for item in _items(value):
        payload = _exact_fields(item, _DECISION_FIELDS)
        results.append(
            DecisionEvent(
                decision_code=_identifier(payload["decision_code"]),
                status=_status(payload["status"]),
                reason_code=_identifier(payload["reason_code"]),
                evidence_refs=_references(payload["evidence_refs"]),
            )
        )
    return tuple(results)


def _tool_events(value: object) -> tuple[ToolEvent, ...]:
    results: list[ToolEvent] = []
    event_ids: set[str] = set()
    for item in _items(value):
        payload = _exact_fields(item, _TOOL_FIELDS)
        event_id = _identifier(payload["event_id"])
        if event_id in event_ids:
            _fail("TRACE_VALUE_INVALID")
        event_ids.add(event_id)
        results.append(
            ToolEvent(
                event_id=event_id,
                tool_name=_identifier(payload["tool_name"]),
                effect_class=_enum(
                    payload["effect_class"],
                    EffectClass,
                    "TRACE_EFFECT_CLASS_INVALID",
                ),
                authorization_status=_status(payload["authorization_status"]),
                outcome_status=_status(payload["outcome_status"]),
                side_effect=_boolean(payload["side_effect"]),
                evidence_refs=_references(payload["evidence_refs"]),
            )
        )
    return tuple(results)


def _gate_results(value: object) -> tuple[GateResult, ...]:
    results: list[GateResult] = []
    for item in _items(value):
        payload = _exact_fields(item, _GATE_FIELDS)
        results.append(
            GateResult(
                gate_name=_identifier(payload["gate_name"]),
                required=_boolean(payload["required"]),
                status=_status(payload["status"]),
                evidence_refs=_references(payload["evidence_refs"]),
            )
        )
    return tuple(results)


def _usage(value: object) -> UsageEvidence:
    payload = _exact_fields(value, _USAGE_FIELDS)
    return UsageEvidence(
        model_calls=_optional_count(payload["model_calls"]),
        input_tokens=_optional_count(payload["input_tokens"]),
        output_tokens=_optional_count(payload["output_tokens"]),
        estimated_cost=_optional_cost(payload["estimated_cost"]),
    )


def _result(value: object) -> TraceResult:
    payload = _exact_fields(value, _RESULT_FIELDS)
    return TraceResult(status=_status(payload["status"]))


def _trace_from_mapping(value: Mapping[str, object]) -> BehaviourTrace:
    reject_forbidden_raw_fields(value)
    payload = _exact_fields(value, _TRACE_FIELDS)
    schema_version = payload["schema_version"]
    if schema_version != BEHAVIOUR_TRACE_SCHEMA_VERSION or isinstance(
        schema_version, bool
    ):
        _fail("TRACE_SCHEMA_VERSION_UNSUPPORTED")
    trace = BehaviourTrace(
        schema_version=BEHAVIOUR_TRACE_SCHEMA_VERSION,
        trace_id=_identifier(payload["trace_id"]),
        task_id=_identifier(payload["task_id"]),
        repository=_repository(payload["repository"]),
        task=_task(payload["task"]),
        llm=_llm(payload["llm"]),
        decisions=_decisions(payload["decisions"]),
        tool_events=_tool_events(payload["tool_events"]),
        gate_results=_gate_results(payload["gate_results"]),
        usage=_usage(payload["usage"]),
        result=_result(payload["result"]),
    )
    verify_sanitized_evidence(_trace_to_dict(trace))
    return trace


def _trace_to_dict(trace: BehaviourTrace) -> dict[str, object]:
    return {
        "schema_version": trace.schema_version,
        "trace_id": trace.trace_id,
        "task_id": trace.task_id,
        "repository": {
            "canonical_remote": trace.repository.canonical_remote,
            "base_sha": trace.repository.base_sha,
            "head_sha": trace.repository.head_sha,
            "branch": trace.repository.branch,
            "worktree_clean": trace.repository.worktree_clean,
        },
        "task": {
            "task_class": trace.task.task_class,
            "behaviour_sensitive": trace.task.behaviour_sensitive,
            "security_sensitive": trace.task.security_sensitive,
            "cost_sensitive": trace.task.cost_sensitive,
            "production_sensitive": trace.task.production_sensitive,
            "allowed_effect_classes": [item.value for item in trace.task.allowed_effect_classes],
            "forbidden_effect_classes": [
                item.value for item in trace.task.forbidden_effect_classes
            ],
            "stop_boundary": trace.task.stop_boundary.value,
        },
        "llm": {
            "policy_version": trace.llm.policy_version,
            "recommended_model": trace.llm.recommended_model,
            "actual_model": trace.llm.actual_model,
            "reasoning_level": trace.llm.reasoning_level,
        },
        "decisions": [
            {
                "decision_code": item.decision_code,
                "status": item.status.value,
                "reason_code": item.reason_code,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in trace.decisions
        ],
        "tool_events": [
            {
                "event_id": item.event_id,
                "tool_name": item.tool_name,
                "effect_class": item.effect_class.value,
                "authorization_status": item.authorization_status.value,
                "outcome_status": item.outcome_status.value,
                "side_effect": item.side_effect,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in trace.tool_events
        ],
        "gate_results": [
            {
                "gate_name": item.gate_name,
                "required": item.required,
                "status": item.status.value,
                "evidence_refs": list(item.evidence_refs),
            }
            for item in trace.gate_results
        ],
        "usage": {
            "model_calls": trace.usage.model_calls,
            "input_tokens": trace.usage.input_tokens,
            "output_tokens": trace.usage.output_tokens,
            "estimated_cost": trace.usage.estimated_cost,
        },
        "result": {"status": trace.result.status.value},
    }


def validate_trace(value: BehaviourTrace | Mapping[str, object]) -> BehaviourTrace:
    """Validate and return an immutable schema-v1 trace."""

    if isinstance(value, BehaviourTrace):
        return _trace_from_mapping(_trace_to_dict(value))
    return _trace_from_mapping(_mapping(value))


def normalize_trace(value: BehaviourTrace | Mapping[str, object]) -> dict[str, object]:
    """Return a deterministic JSON-ready trace without mutating input."""

    return _trace_to_dict(validate_trace(value))


def serialize_trace(value: BehaviourTrace | Mapping[str, object]) -> str:
    """Serialize a validated trace as canonical JSON without a trailing newline."""

    try:
        return json.dumps(
            normalize_trace(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise TraceValidationError("TRACE_VALUE_INVALID") from exc


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


def deserialize_trace(value: str | bytes) -> BehaviourTrace:
    """Load canonical or non-canonical JSON and validate its closed schema."""

    if isinstance(value, bytes):
        raw = value
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TraceValidationError("TRACE_JSON_INVALID") from exc
    elif isinstance(value, str):
        text = value
        raw = value.encode("utf-8")
    else:
        _fail("TRACE_JSON_INVALID")
    if not raw or len(raw) > MAX_TRACE_BYTES or b"\x00" in raw:
        _fail("TRACE_JSON_INVALID")
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, _DuplicateJsonKey, ValueError, RecursionError) as exc:
        raise TraceValidationError("TRACE_JSON_INVALID") from exc
    return _trace_from_mapping(_mapping(payload))


def trace_digest(value: BehaviourTrace | Mapping[str, object]) -> str:
    """Return SHA-256 of the canonical serialized trace."""

    return hashlib.sha256(serialize_trace(value).encode("utf-8")).hexdigest()
