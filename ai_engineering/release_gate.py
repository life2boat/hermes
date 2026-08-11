"""Deterministic, offline merge and production-release decisions.

The release gate aggregates explicit evidence.  It never runs repository,
provider, or production checks and never infers one gate from another.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from ai_engineering.contracts import Status, TraceValidationError
from ai_engineering.redaction import (
    reject_forbidden_raw_fields,
    verify_sanitized_evidence,
)
from ai_engineering.scenario import load_fixture_bytes


RELEASE_GATE_SCHEMA_VERSION = 1
RELEASE_GATE_POLICY_VERSION = 1
GOLDEN_CORPUS_DIGEST = (
    "e2580fb10c6d02a55ace0efc9092bd6f3092a9a3a188515c5dba32b44708c8c7"
)


class ReleaseTarget(StrEnum):
    MERGE = "MERGE"
    PRODUCTION_RELEASE = "PRODUCTION_RELEASE"


class GateName(StrEnum):
    CODE = "CODE_GATE"
    BEHAVIOUR = "BEHAVIOUR_GATE"
    SECURITY = "SECURITY_GATE"
    LIVE_BEHAVIOUR = "LIVE_BEHAVIOUR_GATE"
    COST = "COST_GATE"
    PRODUCTION_READINESS = "PRODUCTION_READINESS_GATE"


class BlockerScope(StrEnum):
    MERGE = "MERGE"
    PRODUCTION_RELEASE = "PRODUCTION_RELEASE"


class ReleaseGateError(ValueError):
    """Fail-closed input/configuration error exposing only a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    repository: str
    canonical_remote: str
    base_sha: str
    candidate_sha: str
    observed_head_sha: str
    task_id: str


@dataclass(frozen=True, slots=True)
class ReleaseTaskClassification:
    task_classification: str
    behaviour_sensitive: bool
    security_sensitive: bool
    cost_sensitive: bool
    production_sensitive: bool
    live_behaviour_required: bool


@dataclass(frozen=True, slots=True)
class GateEvidence:
    gate_name: GateName
    required: bool
    status: Status
    evidence_refs: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence_digest: str | None = None


@dataclass(frozen=True, slots=True)
class TechnicalBlocker:
    code: str
    status: Status
    evidence_refs: tuple[str, ...]
    scope: BlockerScope


@dataclass(frozen=True, slots=True)
class ReleaseGateReceipt:
    schema_version: int
    policy_version: int
    target: ReleaseTarget
    task_id: str
    task_classification: ReleaseTaskClassification
    repository: str
    canonical_remote: str
    base_sha: str
    candidate_sha: str
    observed_head_sha: str
    required_gates: tuple[GateName, ...]
    gate_results: tuple[GateEvidence, ...]
    technical_blockers: tuple[TechnicalBlocker, ...]
    governance_observations: tuple[str, ...]
    merge_eligible: Status
    production_release_eligible: Status
    status: Status
    reason_codes: tuple[str, ...]


_GATE_ORDER = tuple(GateName)
_UNRESOLVED = frozenset(
    {
        Status.BLOCKED,
        Status.NOT_RUN,
        Status.NOT_PERFORMED,
        Status.UNKNOWN,
        Status.INCONCLUSIVE,
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+ -]{0,255}$")
_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")

_SOURCE_FIELDS = frozenset(
    {
        "repository",
        "canonical_remote",
        "base_sha",
        "candidate_sha",
        "observed_head_sha",
        "task_id",
    }
)
_CLASSIFICATION_FIELDS = frozenset(
    {
        "task_classification",
        "behaviour_sensitive",
        "security_sensitive",
        "cost_sensitive",
        "production_sensitive",
        "live_behaviour_required",
    }
)
_GATE_FIELDS = frozenset(
    {
        "gate_name",
        "required",
        "status",
        "evidence_refs",
        "reason_codes",
        "evidence_digest",
    }
)
_BLOCKER_FIELDS = frozenset({"code", "status", "evidence_refs", "scope"})
_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "policy_version",
        "target",
        "source_identity",
        "task_classification",
        "gate_results",
        "technical_blockers",
        "governance_observations",
    }
)


class _DuplicateJsonKey(ValueError):
    pass


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return value


def _exact(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    if frozenset(payload) != fields:
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return payload


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return value


def _reason_code(value: object) -> str:
    if not isinstance(value, str) or not _REASON_RE.fullmatch(value):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        raise ReleaseGateError("RELEASE_GATE_SOURCE_IDENTITY_MISSING")
    return value


def _unique_strings(value: object, validator) -> tuple[str, ...]:
    result = tuple(validator(item) for item in _sequence(value))
    if len(result) != len(set(result)):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return tuple(sorted(result))


def derive_gate_requirements(
    target: ReleaseTarget,
    classification: ReleaseTaskClassification,
) -> dict[GateName, bool]:
    """Derive requirements; callers cannot weaken these flags."""

    production = target is ReleaseTarget.PRODUCTION_RELEASE
    return {
        GateName.CODE: True,
        GateName.BEHAVIOUR: (
            classification.behaviour_sensitive or classification.security_sensitive
        ),
        GateName.SECURITY: (
            classification.security_sensitive or classification.production_sensitive
        ),
        GateName.LIVE_BEHAVIOUR: (
            production and classification.live_behaviour_required
        ),
        GateName.COST: production and classification.cost_sensitive,
        GateName.PRODUCTION_READINESS: production,
    }


def _aggregate(statuses: Sequence[Status]) -> Status:
    if Status.FAIL in statuses:
        return Status.FAIL
    if any(status in _UNRESOLVED for status in statuses):
        return Status.BLOCKED
    return Status.PASS


def _normalize_gate(value: GateEvidence) -> dict[str, object]:
    return {
        "evidence_digest": value.evidence_digest,
        "evidence_refs": list(value.evidence_refs),
        "gate_name": value.gate_name.value,
        "reason_codes": list(value.reason_codes),
        "required": value.required,
        "status": value.status.value,
    }


def _normalize_blocker(value: TechnicalBlocker) -> dict[str, object]:
    return {
        "code": value.code,
        "evidence_refs": list(value.evidence_refs),
        "scope": value.scope.value,
        "status": value.status.value,
    }


def _classification_payload(value: ReleaseTaskClassification) -> dict[str, object]:
    return {
        "behaviour_sensitive": value.behaviour_sensitive,
        "cost_sensitive": value.cost_sensitive,
        "live_behaviour_required": value.live_behaviour_required,
        "production_sensitive": value.production_sensitive,
        "security_sensitive": value.security_sensitive,
        "task_classification": value.task_classification,
    }


def evaluate_release(
    *,
    target: ReleaseTarget,
    source: SourceIdentity,
    classification: ReleaseTaskClassification,
    gate_results: Sequence[GateEvidence],
    technical_blockers: Sequence[TechnicalBlocker] = (),
    governance_observations: Sequence[str] = (),
) -> ReleaseGateReceipt:
    """Evaluate explicit evidence without performing external work."""

    try:
        target = ReleaseTarget(target)
        source = _parse_source(
            {
                "repository": source.repository,
                "canonical_remote": source.canonical_remote,
                "base_sha": source.base_sha,
                "candidate_sha": source.candidate_sha,
                "observed_head_sha": source.observed_head_sha,
                "task_id": source.task_id,
            }
        )
        classification = _parse_classification(
            _classification_payload(classification)
        )
        validated_gates = tuple(
            _parse_gate(_normalize_gate(gate)) for gate in gate_results
        )
        validated_blockers = tuple(
            _parse_blocker(_normalize_blocker(blocker))
            for blocker in technical_blockers
        )
        observations = _unique_strings(governance_observations, _reason_code)
    except ReleaseGateError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID") from exc

    requirements = derive_gate_requirements(target, classification)
    by_name = {gate.gate_name: gate for gate in validated_gates}
    if len(by_name) != len(validated_gates) or set(by_name) != set(_GATE_ORDER):
        raise ReleaseGateError("RELEASE_GATE_GATE_SET_INVALID")
    for name, required in requirements.items():
        if by_name[name].required is not required:
            raise ReleaseGateError("RELEASE_GATE_REQUIREMENT_MISMATCH")
        if required and not by_name[name].evidence_refs:
            raise ReleaseGateError("RELEASE_GATE_REQUIRED_EVIDENCE_MISSING")

    blockers = list(validated_blockers)
    if source.candidate_sha != source.observed_head_sha:
        blockers.append(
            TechnicalBlocker(
                code="EXACT_SHA_MISMATCH",
                status=Status.FAIL,
                evidence_refs=("source:candidate_head",),
                scope=BlockerScope.MERGE,
            )
        )
    blocker_keys = {
        (item.code, item.status, item.evidence_refs, item.scope) for item in blockers
    }
    if len(blocker_keys) != len(blockers):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    if any(item.status not in {Status.FAIL, Status.BLOCKED} for item in blockers):
        raise ReleaseGateError("RELEASE_GATE_BLOCKER_STATUS_INVALID")
    if any(not item.evidence_refs for item in blockers):
        raise ReleaseGateError("RELEASE_GATE_BLOCKER_EVIDENCE_MISSING")

    merge_names = (
        GateName.CODE,
        GateName.BEHAVIOUR,
        GateName.SECURITY,
    )
    merge_statuses = [
        by_name[name].status for name in merge_names if requirements[name]
    ]
    merge_statuses.extend(
        blocker.status for blocker in blockers if blocker.scope is BlockerScope.MERGE
    )
    merge_eligible = _aggregate(merge_statuses)

    if target is ReleaseTarget.MERGE:
        production_eligible = Status.NOT_PERFORMED
    else:
        production_statuses = [merge_eligible]
        production_statuses.extend(
            by_name[name].status
            for name in (
                GateName.LIVE_BEHAVIOUR,
                GateName.COST,
                GateName.PRODUCTION_READINESS,
            )
            if requirements[name]
        )
        production_statuses.extend(
            blocker.status
            for blocker in blockers
            if blocker.scope is BlockerScope.PRODUCTION_RELEASE
        )
        production_eligible = _aggregate(production_statuses)

    target_status = (
        merge_eligible
        if target is ReleaseTarget.MERGE
        else production_eligible
    )
    reasons = [f"MERGE_ELIGIBLE_{merge_eligible.value}"]
    if target is ReleaseTarget.PRODUCTION_RELEASE:
        reasons.append(f"PRODUCTION_RELEASE_ELIGIBLE_{production_eligible.value}")
    for name in _GATE_ORDER:
        gate = by_name[name]
        if gate.required and gate.status is not Status.PASS:
            reasons.append(f"REQUIRED_{name.value}_{gate.status.value}")
    reasons.extend(blocker.code for blocker in blockers)

    receipt = ReleaseGateReceipt(
        schema_version=RELEASE_GATE_SCHEMA_VERSION,
        policy_version=RELEASE_GATE_POLICY_VERSION,
        target=target,
        task_id=source.task_id,
        task_classification=classification,
        repository=source.repository,
        canonical_remote=source.canonical_remote,
        base_sha=source.base_sha,
        candidate_sha=source.candidate_sha,
        observed_head_sha=source.observed_head_sha,
        required_gates=tuple(name for name in _GATE_ORDER if requirements[name]),
        gate_results=tuple(by_name[name] for name in _GATE_ORDER),
        technical_blockers=tuple(
            sorted(
                blockers,
                key=lambda item: (
                    item.scope.value,
                    item.code,
                    item.status.value,
                    item.evidence_refs,
                ),
            )
        ),
        governance_observations=observations,
        merge_eligible=merge_eligible,
        production_release_eligible=production_eligible,
        status=target_status,
        reason_codes=tuple(sorted(set(reasons))),
    )
    verify_sanitized_evidence(normalize_release_receipt(receipt))
    return receipt


def normalize_release_receipt(value: ReleaseGateReceipt) -> dict[str, object]:
    payload: dict[str, object] = {
        "base_sha": value.base_sha,
        "candidate_sha": value.candidate_sha,
        "canonical_remote": value.canonical_remote,
        "gate_results": [_normalize_gate(item) for item in value.gate_results],
        "governance_observations": list(value.governance_observations),
        "merge_eligible": value.merge_eligible.value,
        "observed_head_sha": value.observed_head_sha,
        "policy_version": value.policy_version,
        "production_release_eligible": value.production_release_eligible.value,
        "reason_codes": list(value.reason_codes),
        "repository": value.repository,
        "required_gates": [item.value for item in value.required_gates],
        "schema_version": value.schema_version,
        "status": value.status.value,
        "target": value.target.value,
        "task_classification": _classification_payload(value.task_classification),
        "task_id": value.task_id,
        "technical_blockers": [
            _normalize_blocker(item) for item in value.technical_blockers
        ],
    }
    verify_sanitized_evidence(payload)
    return payload


def serialize_release_receipt(value: ReleaseGateReceipt) -> str:
    return json.dumps(
        normalize_release_receipt(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def release_receipt_digest(value: ReleaseGateReceipt) -> str:
    return hashlib.sha256(serialize_release_receipt(value).encode("utf-8")).hexdigest()


def _parse_source(value: object) -> SourceIdentity:
    payload = _exact(value, _SOURCE_FIELDS)
    return SourceIdentity(
        repository=_identifier(payload["repository"]),
        canonical_remote=_identifier(payload["canonical_remote"]),
        base_sha=_sha(payload["base_sha"]),
        candidate_sha=_sha(payload["candidate_sha"]),
        observed_head_sha=_sha(payload["observed_head_sha"]),
        task_id=_identifier(payload["task_id"]),
    )


def _parse_classification(value: object) -> ReleaseTaskClassification:
    payload = _exact(value, _CLASSIFICATION_FIELDS)
    return ReleaseTaskClassification(
        task_classification=_identifier(payload["task_classification"]),
        behaviour_sensitive=_boolean(payload["behaviour_sensitive"]),
        security_sensitive=_boolean(payload["security_sensitive"]),
        cost_sensitive=_boolean(payload["cost_sensitive"]),
        production_sensitive=_boolean(payload["production_sensitive"]),
        live_behaviour_required=_boolean(payload["live_behaviour_required"]),
    )


def _parse_gate(value: object) -> GateEvidence:
    payload = _exact(value, _GATE_FIELDS)
    try:
        name = GateName(payload["gate_name"])
        status = Status(payload["status"])
    except (TypeError, ValueError) as exc:
        raise ReleaseGateError("RELEASE_GATE_GATE_UNKNOWN") from exc
    digest = payload["evidence_digest"]
    if digest is not None and (
        not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest)
    ):
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID")
    return GateEvidence(
        gate_name=name,
        required=_boolean(payload["required"]),
        status=status,
        evidence_refs=_unique_strings(payload["evidence_refs"], _identifier),
        reason_codes=_unique_strings(payload["reason_codes"], _reason_code),
        evidence_digest=digest,
    )


def _parse_blocker(value: object) -> TechnicalBlocker:
    payload = _exact(value, _BLOCKER_FIELDS)
    try:
        status = Status(payload["status"])
        scope = BlockerScope(payload["scope"])
    except (TypeError, ValueError) as exc:
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID") from exc
    return TechnicalBlocker(
        code=_reason_code(payload["code"]),
        status=status,
        evidence_refs=_unique_strings(payload["evidence_refs"], _identifier),
        scope=scope,
    )


def evaluate_release_mapping(value: Mapping[str, object]) -> ReleaseGateReceipt:
    """Validate a closed request mapping and evaluate it."""

    try:
        reject_forbidden_raw_fields(value)
    except TraceValidationError as exc:
        raise ReleaseGateError("RELEASE_GATE_EVIDENCE_UNSAFE") from exc
    payload = _exact(value, _REQUEST_FIELDS)
    if (
        payload["schema_version"] != RELEASE_GATE_SCHEMA_VERSION
        or payload["policy_version"] != RELEASE_GATE_POLICY_VERSION
    ):
        raise ReleaseGateError("RELEASE_GATE_VERSION_UNSUPPORTED")
    try:
        target = ReleaseTarget(payload["target"])
    except (TypeError, ValueError) as exc:
        raise ReleaseGateError("RELEASE_GATE_TARGET_UNKNOWN") from exc
    source = _parse_source(payload["source_identity"])
    classification = _parse_classification(payload["task_classification"])
    gates = tuple(_parse_gate(item) for item in _sequence(payload["gate_results"]))
    blockers = tuple(
        _parse_blocker(item) for item in _sequence(payload["technical_blockers"])
    )
    observations = _unique_strings(
        payload["governance_observations"], _reason_code
    )
    return evaluate_release(
        target=target,
        source=source,
        classification=classification,
        gate_results=gates,
        technical_blockers=blockers,
        governance_observations=observations,
    )


def load_release_request(root: Path, relative_path: str | Path) -> Mapping[str, object]:
    """Load a confined, duplicate-free, sanitized release request."""

    try:
        data = load_fixture_bytes(root, relative_path)
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        return _mapping(payload)
    except ReleaseGateError:
        raise
    except Exception as exc:
        raise ReleaseGateError("RELEASE_GATE_INPUT_INVALID") from exc
