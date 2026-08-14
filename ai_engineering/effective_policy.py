"""Hermes Intent Control Plane PR-5 — Effective Policy / Source Attribution.

Provides deterministic, offline resolution of task-level policy and source attribution
for TaskIntent invariants, required gates, and boundaries against canonical Hermes Git revisions.

Hard Architectural Invariants:
- EFFECTIVE_POLICY_EXPANDS_AUTHORITY = False
- LLM_AS_POLICY_RESOLVER = False
- PROVIDER_CALLS = 0
- NETWORK_REQUIRED_FOR_CORE = False
- POLICY_AUTO_INFERENCE = False
- TASK_INTENT_MUTATED = False
- SEMANTIC_PROSE_CONFLICT_RESOLUTION = False
- EXTERNAL_EVIDENCE_AUTHENTICITY_VERIFIED = False (M-PR4-001 boundary)
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeVar

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        def __str__(self) -> str:
            return str(self.value)


from ai_engineering.release_gate import GateName
from ai_engineering.task_intent import (
    TaskIntent,
    intent_digest,
    validate_intent,
)

EFFECTIVE_POLICY_SCHEMA_VERSION = 1
MAX_EFFECTIVE_POLICY_BYTES = 512 * 1024  # 512 KB

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_INVARIANT_HEADING_RE = re.compile(
    r"^###\s+([A-Z0-9]+)(?:\s+\(([^)]+)\))?\.\s+(.+)$", re.MULTILINE
)

CANONICAL_SOURCE_MAP_PATH = "docs/HERMES_SOURCE_MAP.md"
CANONICAL_INVARIANTS_PATH = "docs/HERMES_INVARIANTS.md"
CANONICAL_RELEASE_GATES_PATH = "docs/AGENT_RELEASE_GATES.md"
CANONICAL_RELEASE_GATE_MODULE_PATH = "ai_engineering/release_gate.py"
TASK_INTENT_PSEUDO_PATH = "TASK_INTENT"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PolicySourceKind(StrEnum):
    CANONICAL_DOCUMENT = "CANONICAL_DOCUMENT"
    CANONICAL_MODULE = "CANONICAL_MODULE"
    TASK_INTENT = "TASK_INTENT"


class ReferenceKind(StrEnum):
    INVARIANT = "INVARIANT"
    REQUIRED_GATE = "REQUIRED_GATE"


class ResolutionStatus(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"


class EffectivePolicyStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


# ---------------------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------------------


class EffectivePolicyValidationError(ValueError):
    """Fail-closed validation error exposing only a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise EffectivePolicyValidationError(code)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicySource:
    source_id: str
    source_kind: PolicySourceKind
    path: str
    subject_sha: str
    content_sha256: str


@dataclass(frozen=True)
class PolicyResolution:
    reference_kind: ReferenceKind
    requested_reference: str
    resolution_status: ResolutionStatus
    canonical_reference: str | None = None
    source_id: str | None = None
    source_path: str | None = None
    source_selector: str | None = None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskPolicyAttribution:
    task_id: str
    intent_revision: int
    intent_digest: str
    source_base_sha: str
    constraints: tuple[str, ...]
    allowed_mutations: tuple[str, ...]
    forbidden_mutations: tuple[str, ...]
    stop_boundary: str
    source_id: str


@dataclass(frozen=True)
class EffectivePolicyReport:
    schema_version: int
    effective_policy_id: str
    task_id: str
    intent_digest: str
    intent_revision: int
    source_base_sha: str
    subject_sha: str
    status: EffectivePolicyStatus
    policy_sources: tuple[PolicySource, ...]
    task_policy: TaskPolicyAttribution
    invariant_resolutions: tuple[PolicyResolution, ...]
    required_gate_resolutions: tuple[PolicyResolution, ...]
    unresolved_references: tuple[str, ...]
    precedence_source_id: str
    authority_expansion: bool = False


# ---------------------------------------------------------------------------
# Canonical ID Calculations
# ---------------------------------------------------------------------------


def compute_source_id(
    source_kind: str | PolicySourceKind,
    path: str,
    subject_sha: str,
    content_sha256: str,
) -> str:
    """Compute deterministic SHA-256 source identity over canonical fields."""
    payload = {
        "content_sha256": content_sha256,
        "path": path,
        "source_kind": str(source_kind),
        "subject_sha": subject_sha,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_effective_policy_id(payload_without_id: Mapping[str, object]) -> str:
    """Compute deterministic SHA-256 report identity over canonical payload."""
    serialized = json.dumps(payload_without_id, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Strict JSON and Field Parsing Helpers
# ---------------------------------------------------------------------------


def _parse_strict_json(
    raw: str | bytes, max_bytes: int = MAX_EFFECTIVE_POLICY_BYTES
) -> Mapping[str, object]:
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = raw

    if len(raw_bytes) > max_bytes:
        _fail("PAYLOAD_TOO_LARGE")

    if b"\x00" in raw_bytes:
        _fail("NUL_BYTE_FORBIDDEN")

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        _fail("INVALID_JSON")

    def _check_duplicates(
        ordered_pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        d = {}
        for k, v in ordered_pairs:
            if k in d:
                _fail("DUPLICATE_KEY")
            d[k] = v
        return d

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_check_duplicates,
            parse_constant=lambda x: _fail("SPECIAL_FLOAT_FORBIDDEN"),
        )
    except json.JSONDecodeError:
        _fail("INVALID_JSON")

    if not isinstance(parsed, dict):
        _fail("VALUE_INVALID")

    return parsed


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _fail("REQUIRED_FIELD_MISSING")
    return value


def _exact_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    keys = frozenset(payload)
    if expected - keys:
        _fail("REQUIRED_FIELD_MISSING")
    if keys - expected:
        _fail("UNEXPECTED_FIELD")
    return payload


_EnumT = TypeVar(
    "_EnumT",
    PolicySourceKind,
    ReferenceKind,
    ResolutionStatus,
    EffectivePolicyStatus,
)


def _enum(value: object, enum_type: type[_EnumT], code: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        _fail(code)
    try:
        return enum_type(value)
    except ValueError:
        _fail(code)


def _sha40(value: object, code: str = "VALUE_INVALID") -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _digest64(value: object, code: str = "VALUE_INVALID") -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _string(value: object, code: str = "VALUE_INVALID") -> str:
    if not isinstance(value, str):
        _fail(code)
    return value


def _string_tuple(value: object, code: str = "VALUE_INVALID") -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        _fail(code)
    return tuple(_string(x, code) for x in value)


# ---------------------------------------------------------------------------
# Validation Functions
# ---------------------------------------------------------------------------

_POLICY_SOURCE_FIELDS = frozenset({
    "source_id",
    "source_kind",
    "path",
    "subject_sha",
    "content_sha256",
})
_POLICY_RESOLUTION_FIELDS = frozenset({
    "reference_kind",
    "requested_reference",
    "resolution_status",
    "canonical_reference",
    "source_id",
    "source_path",
    "source_selector",
    "reason_codes",
})
_TASK_POLICY_FIELDS = frozenset({
    "task_id",
    "intent_revision",
    "intent_digest",
    "source_base_sha",
    "constraints",
    "allowed_mutations",
    "forbidden_mutations",
    "stop_boundary",
    "source_id",
})
_EFFECTIVE_POLICY_REPORT_FIELDS = frozenset({
    "schema_version",
    "effective_policy_id",
    "task_id",
    "intent_digest",
    "intent_revision",
    "source_base_sha",
    "subject_sha",
    "status",
    "policy_sources",
    "task_policy",
    "invariant_resolutions",
    "required_gate_resolutions",
    "unresolved_references",
    "precedence_source_id",
    "authority_expansion",
})


def validate_policy_source(
    source: PolicySource | Mapping[str, object],
) -> PolicySource:
    """Validate a PolicySource dataclass or mapping and verify its source_id binding."""
    if isinstance(source, PolicySource):
        payload: Mapping[str, object] = {
            "source_id": source.source_id,
            "source_kind": str(source.source_kind),
            "path": source.path,
            "subject_sha": source.subject_sha,
            "content_sha256": source.content_sha256,
        }
    elif isinstance(source, Mapping):
        payload = _exact_fields(source, _POLICY_SOURCE_FIELDS)
    else:
        _fail("VALUE_INVALID")

    source_kind = _enum(payload["source_kind"], PolicySourceKind, "VALUE_INVALID")
    path = _string(payload["path"])
    subject_sha = _sha40(payload["subject_sha"])
    content_sha256 = _digest64(payload["content_sha256"])
    expected_id = compute_source_id(source_kind, path, subject_sha, content_sha256)

    claimed_id = _digest64(payload["source_id"])
    if claimed_id != expected_id:
        _fail("SOURCE_ID_MISMATCH")

    return PolicySource(
        source_id=expected_id,
        source_kind=source_kind,
        path=path,
        subject_sha=subject_sha,
        content_sha256=content_sha256,
    )


def validate_policy_resolution(
    resolution: PolicyResolution | Mapping[str, object],
) -> PolicyResolution:
    """Validate a PolicyResolution dataclass or mapping."""
    if isinstance(resolution, PolicyResolution):
        payload: Mapping[str, object] = {
            "reference_kind": str(resolution.reference_kind),
            "requested_reference": resolution.requested_reference,
            "resolution_status": str(resolution.resolution_status),
            "canonical_reference": resolution.canonical_reference,
            "source_id": resolution.source_id,
            "source_path": resolution.source_path,
            "source_selector": resolution.source_selector,
            "reason_codes": list(resolution.reason_codes),
        }
    elif isinstance(resolution, Mapping):
        payload = _exact_fields(resolution, _POLICY_RESOLUTION_FIELDS)
    else:
        _fail("VALUE_INVALID")

    ref_kind = _enum(payload["reference_kind"], ReferenceKind, "VALUE_INVALID")
    requested_ref = _string(payload["requested_reference"])
    status = _enum(payload["resolution_status"], ResolutionStatus, "VALUE_INVALID")
    reasons = _string_tuple(payload["reason_codes"])

    can_ref = payload["canonical_reference"]
    s_id = payload["source_id"]
    s_path = payload["source_path"]
    s_selector = payload["source_selector"]

    if status == ResolutionStatus.RESOLVED:
        if (
            not isinstance(can_ref, str)
            or not can_ref
            or not isinstance(s_id, str)
            or _DIGEST_RE.fullmatch(s_id) is None
            or not isinstance(s_path, str)
            or not s_path
            or not isinstance(s_selector, str)
            or not s_selector
            or reasons
        ):
            _fail("VALUE_INVALID")
        canonical_ref: str | None = can_ref
        source_id: str | None = s_id
        source_path: str | None = s_path
        source_selector: str | None = s_selector
    else:  # UNRESOLVED
        if (
            can_ref is not None
            or s_id is not None
            or s_path is not None
            or s_selector is not None
            or not reasons
        ):
            _fail("VALUE_INVALID")
        canonical_ref = None
        source_id = None
        source_path = None
        source_selector = None

    return PolicyResolution(
        reference_kind=ref_kind,
        requested_reference=requested_ref,
        resolution_status=status,
        canonical_reference=canonical_ref,
        source_id=source_id,
        source_path=source_path,
        source_selector=source_selector,
        reason_codes=reasons,
    )


def validate_task_policy(
    policy: TaskPolicyAttribution | Mapping[str, object],
) -> TaskPolicyAttribution:
    """Validate a TaskPolicyAttribution dataclass or mapping."""
    if isinstance(policy, TaskPolicyAttribution):
        payload: Mapping[str, object] = {
            "task_id": policy.task_id,
            "intent_revision": policy.intent_revision,
            "intent_digest": policy.intent_digest,
            "source_base_sha": policy.source_base_sha,
            "constraints": list(policy.constraints),
            "allowed_mutations": list(policy.allowed_mutations),
            "forbidden_mutations": list(policy.forbidden_mutations),
            "stop_boundary": policy.stop_boundary,
            "source_id": policy.source_id,
        }
    elif isinstance(policy, Mapping):
        payload = _exact_fields(policy, _TASK_POLICY_FIELDS)
    else:
        _fail("VALUE_INVALID")

    task_id = _string(payload["task_id"])
    if not isinstance(payload["intent_revision"], int) or isinstance(
        payload["intent_revision"], bool
    ):
        _fail("VALUE_INVALID")
    intent_revision = payload["intent_revision"]
    if intent_revision < 1:
        _fail("VALUE_INVALID")

    digest = _digest64(payload["intent_digest"])
    base_sha = _sha40(payload["source_base_sha"])
    constraints = _string_tuple(payload["constraints"])
    allowed = _string_tuple(payload["allowed_mutations"])
    forbidden = _string_tuple(payload["forbidden_mutations"])
    stop_boundary = _string(payload["stop_boundary"])
    claimed_source_id = _digest64(payload["source_id"])

    expected_source_id = compute_source_id(
        PolicySourceKind.TASK_INTENT,
        TASK_INTENT_PSEUDO_PATH,
        base_sha,
        digest,
    )
    if claimed_source_id != expected_source_id:
        _fail("SOURCE_ID_MISMATCH")

    return TaskPolicyAttribution(
        task_id=task_id,
        intent_revision=intent_revision,
        intent_digest=digest,
        source_base_sha=base_sha,
        constraints=constraints,
        allowed_mutations=allowed,
        forbidden_mutations=forbidden,
        stop_boundary=stop_boundary,
        source_id=expected_source_id,
    )


def validate_effective_policy_report(
    report: EffectivePolicyReport | Mapping[str, object],
) -> EffectivePolicyReport:
    """Validate structural integrity and hash identity of an EffectivePolicyReport.

    Proves:
    - Schema version and field types conform to specification.
    - String formats and regexes are well-formed.
    - source_id calculations match their constituent fields.
    - effective_policy_id hash matches canonical payload hashing.
    - Internal consistency between resolutions and unresolved_references list.

    Does NOT prove:
    - Canonical policy membership against repository Git blobs.
    - Semantic origin of TaskPolicyAttribution fields from TaskIntent.

    Callers requiring authoritative semantic verification MUST use verify_effective_policy_report().
    """
    if isinstance(report, EffectivePolicyReport):
        raw_report: Mapping[str, object] = {
            "schema_version": report.schema_version,
            "effective_policy_id": report.effective_policy_id,
            "task_id": report.task_id,
            "intent_digest": report.intent_digest,
            "intent_revision": report.intent_revision,
            "source_base_sha": report.source_base_sha,
            "subject_sha": report.subject_sha,
            "status": str(report.status),
            "policy_sources": [
                {
                    "source_id": s.source_id,
                    "source_kind": str(s.source_kind),
                    "path": s.path,
                    "subject_sha": s.subject_sha,
                    "content_sha256": s.content_sha256,
                }
                for s in report.policy_sources
            ],
            "task_policy": {
                "task_id": report.task_policy.task_id,
                "intent_revision": report.task_policy.intent_revision,
                "intent_digest": report.task_policy.intent_digest,
                "source_base_sha": report.task_policy.source_base_sha,
                "constraints": list(report.task_policy.constraints),
                "allowed_mutations": list(report.task_policy.allowed_mutations),
                "forbidden_mutations": list(report.task_policy.forbidden_mutations),
                "stop_boundary": report.task_policy.stop_boundary,
                "source_id": report.task_policy.source_id,
            },
            "invariant_resolutions": [
                {
                    "reference_kind": str(r.reference_kind),
                    "requested_reference": r.requested_reference,
                    "resolution_status": str(r.resolution_status),
                    "canonical_reference": r.canonical_reference,
                    "source_id": r.source_id,
                    "source_path": r.source_path,
                    "source_selector": r.source_selector,
                    "reason_codes": list(r.reason_codes),
                }
                for r in report.invariant_resolutions
            ],
            "required_gate_resolutions": [
                {
                    "reference_kind": str(r.reference_kind),
                    "requested_reference": r.requested_reference,
                    "resolution_status": str(r.resolution_status),
                    "canonical_reference": r.canonical_reference,
                    "source_id": r.source_id,
                    "source_path": r.source_path,
                    "source_selector": r.source_selector,
                    "reason_codes": list(r.reason_codes),
                }
                for r in report.required_gate_resolutions
            ],
            "unresolved_references": list(report.unresolved_references),
            "precedence_source_id": report.precedence_source_id,
            "authority_expansion": report.authority_expansion,
        }
    elif isinstance(report, Mapping):
        raw_report = _exact_fields(report, _EFFECTIVE_POLICY_REPORT_FIELDS)
    else:
        _fail("VALUE_INVALID")

    raw_version = raw_report["schema_version"]
    if (
        raw_version != EFFECTIVE_POLICY_SCHEMA_VERSION
        or isinstance(raw_version, bool)
        or not isinstance(raw_version, int)
    ):
        _fail("SCHEMA_VERSION_UNSUPPORTED")

    task_id = _string(raw_report["task_id"])
    intent_digest_str = _digest64(raw_report["intent_digest"])
    if not isinstance(raw_report["intent_revision"], int) or isinstance(
        raw_report["intent_revision"], bool
    ):
        _fail("VALUE_INVALID")
    intent_revision = raw_report["intent_revision"]
    if intent_revision < 1:
        _fail("VALUE_INVALID")

    source_base_sha = _sha40(raw_report["source_base_sha"])
    subject_sha = _sha40(raw_report["subject_sha"])
    status = _enum(raw_report["status"], EffectivePolicyStatus, "STATUS_INVALID")

    if not isinstance(raw_report["authority_expansion"], bool):
        _fail("VALUE_INVALID")
    if raw_report["authority_expansion"] is not False:
        _fail("AUTHORITY_EXPANSION_FORBIDDEN")

    # Policy sources
    raw_sources = raw_report["policy_sources"]
    if isinstance(raw_sources, (str, bytes)) or not isinstance(raw_sources, Sequence):
        _fail("VALUE_INVALID")
    validated_sources = tuple(validate_policy_source(s) for s in raw_sources)
    source_id_map = {s.source_id: s for s in validated_sources}

    # Precedence source ID check
    precedence_id = _digest64(raw_report["precedence_source_id"])
    source_map_source = next(
        (s for s in validated_sources if s.path == CANONICAL_SOURCE_MAP_PATH),
        None,
    )
    if source_map_source is None or source_map_source.source_id != precedence_id:
        _fail("PRECEDENCE_SOURCE_MISMATCH")

    # Task policy attribution
    validated_task_policy = validate_task_policy(raw_report["task_policy"])
    if (
        validated_task_policy.task_id != task_id
        or validated_task_policy.intent_revision != intent_revision
        or validated_task_policy.intent_digest != intent_digest_str
        or validated_task_policy.source_base_sha != source_base_sha
    ):
        _fail("INTENT_DIGEST_MISMATCH")

    # Invariant resolutions
    raw_inv = raw_report["invariant_resolutions"]
    if isinstance(raw_inv, (str, bytes)) or not isinstance(raw_inv, Sequence):
        _fail("VALUE_INVALID")
    validated_inv = tuple(validate_policy_resolution(r) for r in raw_inv)
    for inv in validated_inv:
        if inv.reference_kind != ReferenceKind.INVARIANT:
            _fail("VALUE_INVALID")
        if inv.resolution_status == ResolutionStatus.RESOLVED:
            if (
                inv.source_id not in source_id_map
                or inv.source_path != source_id_map[inv.source_id].path
            ):
                _fail("VALUE_INVALID")

    # Required gate resolutions
    raw_gates = raw_report["required_gate_resolutions"]
    if isinstance(raw_gates, (str, bytes)) or not isinstance(raw_gates, Sequence):
        _fail("VALUE_INVALID")
    validated_gates = tuple(validate_policy_resolution(r) for r in raw_gates)
    for gate in validated_gates:
        if gate.reference_kind != ReferenceKind.REQUIRED_GATE:
            _fail("VALUE_INVALID")
        if gate.resolution_status == ResolutionStatus.RESOLVED:
            if (
                gate.source_id not in source_id_map
                or gate.source_path != source_id_map[gate.source_id].path
            ):
                _fail("VALUE_INVALID")

    # Unresolved references
    unresolved = _string_tuple(raw_report["unresolved_references"])
    actual_unresolved = tuple(
        r.requested_reference
        for r in (*validated_inv, *validated_gates)
        if r.resolution_status == ResolutionStatus.UNRESOLVED
    )
    if unresolved != actual_unresolved:
        _fail("UNRESOLVED_REFERENCES_MISMATCH")

    # Status consistency
    if not actual_unresolved and status != EffectivePolicyStatus.COMPLETE:
        _fail("STATUS_INVALID")
    if actual_unresolved and status != EffectivePolicyStatus.INCOMPLETE:
        _fail("STATUS_INVALID")

    # Effective Policy ID verification
    canonical_payload_without_id = {
        "authority_expansion": False,
        "intent_digest": intent_digest_str,
        "intent_revision": intent_revision,
        "invariant_resolutions": [
            {
                "canonical_reference": r.canonical_reference,
                "reason_codes": list(r.reason_codes),
                "reference_kind": str(r.reference_kind),
                "requested_reference": r.requested_reference,
                "resolution_status": str(r.resolution_status),
                "source_id": r.source_id,
                "source_path": r.source_path,
                "source_selector": r.source_selector,
            }
            for r in validated_inv
        ],
        "policy_sources": [
            {
                "content_sha256": s.content_sha256,
                "path": s.path,
                "source_id": s.source_id,
                "source_kind": str(s.source_kind),
                "subject_sha": s.subject_sha,
            }
            for s in validated_sources
        ],
        "precedence_source_id": precedence_id,
        "required_gate_resolutions": [
            {
                "canonical_reference": r.canonical_reference,
                "reason_codes": list(r.reason_codes),
                "reference_kind": str(r.reference_kind),
                "requested_reference": r.requested_reference,
                "resolution_status": str(r.resolution_status),
                "source_id": r.source_id,
                "source_path": r.source_path,
                "source_selector": r.source_selector,
            }
            for r in validated_gates
        ],
        "schema_version": EFFECTIVE_POLICY_SCHEMA_VERSION,
        "source_base_sha": source_base_sha,
        "status": str(status),
        "subject_sha": subject_sha,
        "task_id": task_id,
        "task_policy": {
            "allowed_mutations": list(validated_task_policy.allowed_mutations),
            "constraints": list(validated_task_policy.constraints),
            "forbidden_mutations": list(validated_task_policy.forbidden_mutations),
            "intent_digest": validated_task_policy.intent_digest,
            "intent_revision": validated_task_policy.intent_revision,
            "source_base_sha": validated_task_policy.source_base_sha,
            "source_id": validated_task_policy.source_id,
            "stop_boundary": validated_task_policy.stop_boundary,
            "task_id": validated_task_policy.task_id,
        },
        "unresolved_references": list(unresolved),
    }

    expected_policy_id = compute_effective_policy_id(canonical_payload_without_id)
    claimed_policy_id = _digest64(raw_report["effective_policy_id"])
    if claimed_policy_id != expected_policy_id:
        _fail("POLICY_ID_MISMATCH")

    return EffectivePolicyReport(
        schema_version=EFFECTIVE_POLICY_SCHEMA_VERSION,
        effective_policy_id=expected_policy_id,
        task_id=task_id,
        intent_digest=intent_digest_str,
        intent_revision=intent_revision,
        source_base_sha=source_base_sha,
        subject_sha=subject_sha,
        status=status,
        policy_sources=validated_sources,
        task_policy=validated_task_policy,
        invariant_resolutions=validated_inv,
        required_gate_resolutions=validated_gates,
        unresolved_references=unresolved,
        precedence_source_id=precedence_id,
        authority_expansion=False,
    )


def validate_effective_policy_report_structure(
    report: EffectivePolicyReport | Mapping[str, object],
) -> EffectivePolicyReport:
    """Explicit alias for structural validation of an EffectivePolicyReport."""
    return validate_effective_policy_report(report)


# ---------------------------------------------------------------------------
# Serialization & Deserialization
# ---------------------------------------------------------------------------


def serialize_effective_policy_report(
    report: EffectivePolicyReport, indent: int = 2
) -> str:
    """Serialize an EffectivePolicyReport to canonical JSON formatting."""
    validated = validate_effective_policy_report(report)
    payload = {
        "schema_version": validated.schema_version,
        "effective_policy_id": validated.effective_policy_id,
        "task_id": validated.task_id,
        "intent_digest": validated.intent_digest,
        "intent_revision": validated.intent_revision,
        "source_base_sha": validated.source_base_sha,
        "subject_sha": validated.subject_sha,
        "status": str(validated.status),
        "policy_sources": [
            {
                "source_id": s.source_id,
                "source_kind": str(s.source_kind),
                "path": s.path,
                "subject_sha": s.subject_sha,
                "content_sha256": s.content_sha256,
            }
            for s in validated.policy_sources
        ],
        "task_policy": {
            "task_id": validated.task_policy.task_id,
            "intent_revision": validated.task_policy.intent_revision,
            "intent_digest": validated.task_policy.intent_digest,
            "source_base_sha": validated.task_policy.source_base_sha,
            "constraints": list(validated.task_policy.constraints),
            "allowed_mutations": list(validated.task_policy.allowed_mutations),
            "forbidden_mutations": list(validated.task_policy.forbidden_mutations),
            "stop_boundary": validated.task_policy.stop_boundary,
            "source_id": validated.task_policy.source_id,
        },
        "invariant_resolutions": [
            {
                "reference_kind": str(r.reference_kind),
                "requested_reference": r.requested_reference,
                "resolution_status": str(r.resolution_status),
                "canonical_reference": r.canonical_reference,
                "source_id": r.source_id,
                "source_path": r.source_path,
                "source_selector": r.source_selector,
                "reason_codes": list(r.reason_codes),
            }
            for r in validated.invariant_resolutions
        ],
        "required_gate_resolutions": [
            {
                "reference_kind": str(r.reference_kind),
                "requested_reference": r.requested_reference,
                "resolution_status": str(r.resolution_status),
                "canonical_reference": r.canonical_reference,
                "source_id": r.source_id,
                "source_path": r.source_path,
                "source_selector": r.source_selector,
                "reason_codes": list(r.reason_codes),
            }
            for r in validated.required_gate_resolutions
        ],
        "unresolved_references": list(validated.unresolved_references),
        "precedence_source_id": validated.precedence_source_id,
        "authority_expansion": False,
    }
    return json.dumps(payload, indent=indent)


def deserialize_effective_policy_report(
    raw: str | bytes,
) -> EffectivePolicyReport:
    """Deserialize and validate an EffectivePolicyReport from raw JSON bytes or string."""
    parsed = _parse_strict_json(raw)
    return validate_effective_policy_report(parsed)


# ---------------------------------------------------------------------------
# Exact Git Source Reading
# ---------------------------------------------------------------------------


def read_git_blob(repo_root: Path | str, subject_sha: str, rel_path: str) -> bytes:
    """Read a tracked blob at the exact committed subject_sha from git repository."""
    _sha40(subject_sha)
    norm_path = rel_path.replace("\\", "/")
    if not norm_path or norm_path.startswith("/"):
        _fail("VALUE_INVALID")

    root = Path(repo_root).resolve()
    if root.is_symlink() or not root.is_dir():
        _fail("GIT_SOURCE_UNSAFE")

    # Check git tree object properties (mode, blob type)
    ls_cmd = ["git", "ls-tree", subject_sha, norm_path]
    try:
        ls_proc = subprocess.run(
            ls_cmd,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        _fail("GIT_SOURCE_UNAVAILABLE")

    if ls_proc.returncode != 0 or not ls_proc.stdout.strip():
        _fail("SOURCE_NOT_FOUND")

    lines = [ln.strip() for ln in ls_proc.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        _fail("SOURCE_NOT_FOUND")

    line = lines[0]
    parts = line.split()
    if len(parts) < 3:
        _fail("GIT_SOURCE_UNSAFE")
    mode = parts[0]
    obj_type = parts[1]

    if mode.startswith("120"):  # Symlink
        _fail("GIT_SOURCE_UNSAFE")
    if obj_type != "blob":
        _fail("GIT_SOURCE_UNSAFE")

    # Read exact blob content
    cat_cmd = ["git", "cat-file", "-p", f"{subject_sha}:{norm_path}"]
    try:
        cat_proc = subprocess.run(
            cat_cmd,
            cwd=root,
            capture_output=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        _fail("GIT_SOURCE_UNAVAILABLE")

    if cat_proc.returncode != 0:
        _fail("SOURCE_NOT_FOUND")

    raw_bytes = cat_proc.stdout
    if len(raw_bytes) > MAX_EFFECTIVE_POLICY_BYTES * 4:  # Bounded to 2MB
        _fail("GIT_SOURCE_UNSAFE")

    try:
        raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        _fail("GIT_SOURCE_UNSAFE")

    return raw_bytes


# ---------------------------------------------------------------------------
# Resolution Engine
# ---------------------------------------------------------------------------


def _parse_invariants_document(
    doc_text: str,
) -> dict[str, tuple[str, str]]:
    """Parse HERMES_INVARIANTS.md text and index all canonical and alternative heading identifiers."""
    invariants: dict[str, tuple[str, str]] = {}
    for line in doc_text.splitlines():
        line_clean = line.strip()
        m = _INVARIANT_HEADING_RE.match(line_clean)
        if m:
            canonical_id = m.group(1).strip()
            alt_id = (m.group(2) or "").strip()
            # Map canonical ID (e.g. "AI1", "R1", "S3")
            invariants[canonical_id] = (line_clean, canonical_id)
            # Map alt ID if present (e.g. "INV-AI-V2-001")
            if alt_id:
                invariants[alt_id] = (line_clean, alt_id)
    return invariants


def resolve_effective_policy(
    intent: TaskIntent | Mapping[str, object],
    repository_root: Path | str = ".",
    subject_sha: str | None = None,
    git_reader: Callable[[str, str], bytes] | None = None,
) -> EffectivePolicyReport:
    """Resolve effective policy and source attribution for an explicit TaskIntent."""
    validated_intent = validate_intent(intent)

    if subject_sha is None:
        # Default to intent's source_base_sha
        resolved_subject_sha = validated_intent.source_base_sha
    else:
        resolved_subject_sha = _sha40(subject_sha)

    reader: Callable[[str], bytes]
    if git_reader is not None:
        reader = lambda path: git_reader(resolved_subject_sha, path)
    else:
        repo_path = Path(repository_root)
        reader = lambda path: read_git_blob(repo_path, resolved_subject_sha, path)

    # 1. Read and snapshot canonical policy documents
    source_map_bytes = reader(CANONICAL_SOURCE_MAP_PATH)
    source_map_sha256 = hashlib.sha256(source_map_bytes).hexdigest()
    source_map_source = PolicySource(
        source_id=compute_source_id(
            PolicySourceKind.CANONICAL_DOCUMENT,
            CANONICAL_SOURCE_MAP_PATH,
            resolved_subject_sha,
            source_map_sha256,
        ),
        source_kind=PolicySourceKind.CANONICAL_DOCUMENT,
        path=CANONICAL_SOURCE_MAP_PATH,
        subject_sha=resolved_subject_sha,
        content_sha256=source_map_sha256,
    )

    invariants_bytes = reader(CANONICAL_INVARIANTS_PATH)
    invariants_sha256 = hashlib.sha256(invariants_bytes).hexdigest()
    invariants_source = PolicySource(
        source_id=compute_source_id(
            PolicySourceKind.CANONICAL_DOCUMENT,
            CANONICAL_INVARIANTS_PATH,
            resolved_subject_sha,
            invariants_sha256,
        ),
        source_kind=PolicySourceKind.CANONICAL_DOCUMENT,
        path=CANONICAL_INVARIANTS_PATH,
        subject_sha=resolved_subject_sha,
        content_sha256=invariants_sha256,
    )

    gates_doc_bytes = reader(CANONICAL_RELEASE_GATES_PATH)
    gates_doc_sha256 = hashlib.sha256(gates_doc_bytes).hexdigest()
    gates_doc_source = PolicySource(
        source_id=compute_source_id(
            PolicySourceKind.CANONICAL_DOCUMENT,
            CANONICAL_RELEASE_GATES_PATH,
            resolved_subject_sha,
            gates_doc_sha256,
        ),
        source_kind=PolicySourceKind.CANONICAL_DOCUMENT,
        path=CANONICAL_RELEASE_GATES_PATH,
        subject_sha=resolved_subject_sha,
        content_sha256=gates_doc_sha256,
    )

    gates_module_bytes = reader(CANONICAL_RELEASE_GATE_MODULE_PATH)
    gates_module_sha256 = hashlib.sha256(gates_module_bytes).hexdigest()
    gates_module_source = PolicySource(
        source_id=compute_source_id(
            PolicySourceKind.CANONICAL_MODULE,
            CANONICAL_RELEASE_GATE_MODULE_PATH,
            resolved_subject_sha,
            gates_module_sha256,
        ),
        source_kind=PolicySourceKind.CANONICAL_MODULE,
        path=CANONICAL_RELEASE_GATE_MODULE_PATH,
        subject_sha=resolved_subject_sha,
        content_sha256=gates_module_sha256,
    )

    intent_content_digest = intent_digest(validated_intent)
    task_intent_source = PolicySource(
        source_id=compute_source_id(
            PolicySourceKind.TASK_INTENT,
            TASK_INTENT_PSEUDO_PATH,
            validated_intent.source_base_sha,
            intent_content_digest,
        ),
        source_kind=PolicySourceKind.TASK_INTENT,
        path=TASK_INTENT_PSEUDO_PATH,
        subject_sha=validated_intent.source_base_sha,
        content_sha256=intent_content_digest,
    )

    policy_sources = (
        source_map_source,
        invariants_source,
        gates_doc_source,
        gates_module_source,
        task_intent_source,
    )

    # 2. Parse invariants from HERMES_INVARIANTS.md
    parsed_invariants = _parse_invariants_document(invariants_bytes.decode("utf-8"))

    # 3. Resolve applicable invariants
    invariant_resolutions: list[PolicyResolution] = []
    for inv_req in validated_intent.applicable_invariants:
        if inv_req in parsed_invariants:
            selector, canonical_id = parsed_invariants[inv_req]
            invariant_resolutions.append(
                PolicyResolution(
                    reference_kind=ReferenceKind.INVARIANT,
                    requested_reference=inv_req,
                    resolution_status=ResolutionStatus.RESOLVED,
                    canonical_reference=canonical_id,
                    source_id=invariants_source.source_id,
                    source_path=CANONICAL_INVARIANTS_PATH,
                    source_selector=selector,
                    reason_codes=(),
                )
            )
        else:
            invariant_resolutions.append(
                PolicyResolution(
                    reference_kind=ReferenceKind.INVARIANT,
                    requested_reference=inv_req,
                    resolution_status=ResolutionStatus.UNRESOLVED,
                    canonical_reference=None,
                    source_id=None,
                    source_path=None,
                    source_selector=None,
                    reason_codes=("INVARIANT_NOT_FOUND",),
                )
            )

    # 4. Resolve required gates against GateName
    known_gates = {g.value for g in GateName}
    gate_resolutions: list[PolicyResolution] = []
    for gate_req in validated_intent.required_gates:
        if gate_req in known_gates:
            gate_resolutions.append(
                PolicyResolution(
                    reference_kind=ReferenceKind.REQUIRED_GATE,
                    requested_reference=gate_req,
                    resolution_status=ResolutionStatus.RESOLVED,
                    canonical_reference=gate_req,
                    source_id=gates_doc_source.source_id,
                    source_path=CANONICAL_RELEASE_GATES_PATH,
                    source_selector=f"## Gate types ({gate_req})",
                    reason_codes=(),
                )
            )
        else:
            gate_resolutions.append(
                PolicyResolution(
                    reference_kind=ReferenceKind.REQUIRED_GATE,
                    requested_reference=gate_req,
                    resolution_status=ResolutionStatus.UNRESOLVED,
                    canonical_reference=None,
                    source_id=None,
                    source_path=None,
                    source_selector=None,
                    reason_codes=("GATE_NOT_FOUND",),
                )
            )

    # 5. Determine unresolved references and overall status
    unresolved = tuple(
        r.requested_reference
        for r in (*invariant_resolutions, *gate_resolutions)
        if r.resolution_status == ResolutionStatus.UNRESOLVED
    )
    status = (
        EffectivePolicyStatus.COMPLETE
        if not unresolved
        else EffectivePolicyStatus.INCOMPLETE
    )

    # 6. Build TaskPolicyAttribution
    stop_boundary_val = (
        validated_intent.stop_boundary.value
        if hasattr(validated_intent.stop_boundary, "value")
        else str(validated_intent.stop_boundary)
    )
    task_policy = TaskPolicyAttribution(
        task_id=validated_intent.task_id,
        intent_revision=validated_intent.intent_revision,
        intent_digest=intent_content_digest,
        source_base_sha=validated_intent.source_base_sha,
        constraints=tuple(validated_intent.constraints),
        allowed_mutations=tuple(validated_intent.allowed_mutations),
        forbidden_mutations=tuple(validated_intent.forbidden_mutations),
        stop_boundary=stop_boundary_val,
        source_id=task_intent_source.source_id,
    )

    # 7. Compute deterministic effective_policy_id
    payload_without_id = {
        "authority_expansion": False,
        "intent_digest": intent_content_digest,
        "intent_revision": validated_intent.intent_revision,
        "invariant_resolutions": [
            {
                "canonical_reference": r.canonical_reference,
                "reason_codes": list(r.reason_codes),
                "reference_kind": str(r.reference_kind),
                "requested_reference": r.requested_reference,
                "resolution_status": str(r.resolution_status),
                "source_id": r.source_id,
                "source_path": r.source_path,
                "source_selector": r.source_selector,
            }
            for r in invariant_resolutions
        ],
        "policy_sources": [
            {
                "content_sha256": s.content_sha256,
                "path": s.path,
                "source_id": s.source_id,
                "source_kind": str(s.source_kind),
                "subject_sha": s.subject_sha,
            }
            for s in policy_sources
        ],
        "precedence_source_id": source_map_source.source_id,
        "required_gate_resolutions": [
            {
                "canonical_reference": r.canonical_reference,
                "reason_codes": list(r.reason_codes),
                "reference_kind": str(r.reference_kind),
                "requested_reference": r.requested_reference,
                "resolution_status": str(r.resolution_status),
                "source_id": r.source_id,
                "source_path": r.source_path,
                "source_selector": r.source_selector,
            }
            for r in gate_resolutions
        ],
        "schema_version": EFFECTIVE_POLICY_SCHEMA_VERSION,
        "source_base_sha": validated_intent.source_base_sha,
        "status": str(status),
        "subject_sha": resolved_subject_sha,
        "task_id": validated_intent.task_id,
        "task_policy": {
            "allowed_mutations": list(task_policy.allowed_mutations),
            "constraints": list(task_policy.constraints),
            "forbidden_mutations": list(task_policy.forbidden_mutations),
            "intent_digest": task_policy.intent_digest,
            "intent_revision": task_policy.intent_revision,
            "source_base_sha": task_policy.source_base_sha,
            "source_id": task_policy.source_id,
            "stop_boundary": task_policy.stop_boundary,
            "task_id": task_policy.task_id,
        },
        "unresolved_references": list(unresolved),
    }

    effective_policy_id = compute_effective_policy_id(payload_without_id)

    report = EffectivePolicyReport(
        schema_version=EFFECTIVE_POLICY_SCHEMA_VERSION,
        effective_policy_id=effective_policy_id,
        task_id=validated_intent.task_id,
        intent_digest=intent_content_digest,
        intent_revision=validated_intent.intent_revision,
        source_base_sha=validated_intent.source_base_sha,
        subject_sha=resolved_subject_sha,
        status=status,
        policy_sources=policy_sources,
        task_policy=task_policy,
        invariant_resolutions=tuple(invariant_resolutions),
        required_gate_resolutions=tuple(gate_resolutions),
        unresolved_references=unresolved,
        precedence_source_id=source_map_source.source_id,
        authority_expansion=False,
    )

    return validate_effective_policy_report(report)


# ---------------------------------------------------------------------------
# Authoritative Semantic Verification
# ---------------------------------------------------------------------------


def verify_effective_policy_report(
    report: EffectivePolicyReport | Mapping[str, object] | str | bytes,
    intent: TaskIntent | Mapping[str, object],
    repository_root: Path | str = ".",
    subject_sha: str | None = None,
    git_reader: Callable[[str, str], bytes] | None = None,
) -> EffectivePolicyReport:
    """Authoritatively verify an EffectivePolicyReport against trusted TaskIntent and Git sources.

    Re-resolves the canonical expected policy from the trusted TaskIntent and canonical Git
    blobs at subject_sha, then verifies that the supplied report matches canonical semantic truth.

    Fails closed if the report contains forged resolutions (e.g. unknown invariants or gates
    marked RESOLVED), forged task policy claims (constraints, mutations, stop boundary),
    mismatched subject_sha, or altered policy sources.
    """
    if isinstance(report, (str, bytes)):
        validated_report = deserialize_effective_policy_report(report)
    elif isinstance(report, (EffectivePolicyReport, Mapping)):
        validated_report = validate_effective_policy_report(report)
    else:
        _fail("VALUE_INVALID")

    validated_intent = validate_intent(intent)

    if subject_sha is not None:
        target_subject_sha = _sha40(subject_sha)
    else:
        target_subject_sha = validated_intent.source_base_sha

    expected_report = resolve_effective_policy(
        intent=validated_intent,
        repository_root=repository_root,
        subject_sha=target_subject_sha,
        git_reader=git_reader,
    )

    if validated_report.task_id != expected_report.task_id:
        _fail("TASK_ID_MISMATCH")
    if validated_report.intent_digest != expected_report.intent_digest:
        _fail("INTENT_DIGEST_MISMATCH")
    if validated_report.intent_revision != expected_report.intent_revision:
        _fail("INTENT_REVISION_MISMATCH")
    if validated_report.source_base_sha != expected_report.source_base_sha:
        _fail("SOURCE_BASE_SHA_MISMATCH")
    if validated_report.subject_sha != expected_report.subject_sha:
        _fail("SUBJECT_SHA_MISMATCH")
    if validated_report.status != expected_report.status:
        _fail("STATUS_MISMATCH")
    if validated_report.precedence_source_id != expected_report.precedence_source_id:
        _fail("PRECEDENCE_SOURCE_MISMATCH")
    if validated_report.task_policy != expected_report.task_policy:
        _fail("TASK_POLICY_MISMATCH")
    if validated_report.policy_sources != expected_report.policy_sources:
        _fail("POLICY_SOURCE_MISMATCH")
    if validated_report.invariant_resolutions != expected_report.invariant_resolutions:
        _fail("INVARIANT_RESOLUTION_MISMATCH")
    if (
        validated_report.required_gate_resolutions
        != expected_report.required_gate_resolutions
    ):
        _fail("REQUIRED_GATE_RESOLUTION_MISMATCH")
    if validated_report.unresolved_references != expected_report.unresolved_references:
        _fail("UNRESOLVED_REFERENCES_MISMATCH")
    if validated_report.effective_policy_id != expected_report.effective_policy_id:
        _fail("POLICY_ID_MISMATCH")
    if validated_report.authority_expansion is not False:
        _fail("VALUE_INVALID")

    return validated_report
