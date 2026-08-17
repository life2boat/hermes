"""Deterministic offline ProductionRuntimeAttestation v1 contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone, datetime
UTC = timezone.utc
from enum import Enum
from typing import Any, NoReturn, Protocol

from scripts.secret_scanner import SecretScanError, scan_secret_text


SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 1_048_576
REDACTED = "<REDACTED>"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_OBSERVATION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_INLINE_CREDENTIAL_RE = re.compile(
    r"(?i)(?:\bbearer\s+[A-Za-z0-9._~+/=-]{8,}|"
    r"\b(?:api[_-]?key|token|password|credential|secret)\s*[:=]\s*[^\s,;]{4,}|"
    r"\b(?:sk|gh[opusr]|xox[baprs])-[_A-Za-z0-9-]{8,})"
)
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_token",
    "_password",
    "_credential",
    "_credentials",
    "_authorization",
    "_cookie",
    "_secret",
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "token",
        "password",
        "credential",
        "credentials",
        "authorization",
        "cookie",
        "secret",
    }
)


class ProductionRuntimeAttestationError(ValueError):
    """Fail-closed contract error with a stable, sanitized code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise ProductionRuntimeAttestationError(code)


class CollectorStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class ComparisonStatus(str, Enum):
    MATCH = "MATCH"
    DRIFT = "DRIFT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class CollectorResult:
    collector_id: str
    status: CollectorStatus
    observations: Mapping[str, Any]


class ProductionRuntimeCollector(Protocol):
    """Interface only; B1 deliberately provides no live implementation."""

    collector_id: str

    def collect(self) -> CollectorResult: ...


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAttestation:
    schema_version: int
    attestation_id: str
    target: str
    collected_at_utc: str
    collectors: tuple[CollectorResult, ...]


@dataclass(frozen=True, slots=True)
class CollectorExpectation:
    collector_id: str
    observations: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IntendedProductionState:
    schema_version: int
    target: str
    expected_collectors: tuple[CollectorExpectation, ...]


@dataclass(frozen=True, slots=True)
class ProductionRuntimeComparison:
    schema_version: int
    comparison_id: str
    target: str
    attestation_id: str
    intended_digest: str
    status: ComparisonStatus
    drifted_observations: tuple[str, ...]
    missing_observations: tuple[str, ...]


_COLLECTOR_FIELDS = frozenset({"collector_id", "status", "observations"})
_ATTESTATION_FIELDS = frozenset(
    {"schema_version", "attestation_id", "target", "collected_at_utc", "collectors"}
)
_EXPECTATION_FIELDS = frozenset({"collector_id", "observations"})
_INTENDED_FIELDS = frozenset({"schema_version", "target", "expected_collectors"})
_COMPARISON_FIELDS = frozenset(
    {
        "schema_version",
        "comparison_id",
        "target",
        "attestation_id",
        "intended_digest",
        "status",
        "drifted_observations",
        "missing_observations",
    }
)


def _normalized_key(value: str) -> str:
    return value.strip().casefold().replace("-", "_")


def _sensitive_key(value: str) -> bool:
    normalized = _normalized_key(value)
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        _SENSITIVE_KEY_SUFFIXES
    )


def _string_has_secret(value: str) -> bool:
    if _INLINE_CREDENTIAL_RE.search(value):
        return True
    try:
        return bool(scan_secret_text(value))
    except SecretScanError:
        return True


def sanitize_evidence(value: object) -> object:
    """Return a sanitized JSON-compatible copy without deriving secret metadata."""

    def clean(item: object, *, key: str | None = None) -> object:
        if key is not None and _sensitive_key(key):
            return REDACTED
        if isinstance(item, Mapping):
            result: dict[str, object] = {}
            for raw_key, nested in item.items():
                if not isinstance(raw_key, str):
                    _fail("JSON_OBJECT_KEY_INVALID")
                result[raw_key] = clean(nested, key=raw_key)
            return result
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            return [clean(nested) for nested in item]
        if isinstance(item, str):
            return REDACTED if _string_has_secret(item) else item
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                _fail("NON_FINITE_NUMBER")
            return item
        _fail("JSON_VALUE_INVALID")

    sanitized = clean(value)
    verify_sanitized_evidence(sanitized)
    return sanitized


def verify_sanitized_evidence(value: object) -> None:
    """Reject values that could persist recognizable secret material."""

    def verify(item: object, *, key: str | None = None) -> None:
        if key is not None and _sensitive_key(key):
            if item != REDACTED:
                _fail("UNSANITIZED_SECRET_MATERIAL")
            return
        if isinstance(item, Mapping):
            for raw_key, nested in item.items():
                if not isinstance(raw_key, str):
                    _fail("JSON_OBJECT_KEY_INVALID")
                verify(nested, key=raw_key)
            return
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for nested in item:
                verify(nested)
            return
        if isinstance(item, str):
            if item != REDACTED and _string_has_secret(item):
                _fail("UNSANITIZED_SECRET_MATERIAL")
            return
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float) and math.isfinite(item):
            return
        _fail("JSON_VALUE_INVALID")

    verify(value)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("IDENTIFIER_INVALID")
    return value


def _observation_key(value: object) -> str:
    if not isinstance(value, str) or _OBSERVATION_KEY_RE.fullmatch(value) is None:
        _fail("OBSERVATION_KEY_INVALID")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        _fail("MAPPING_REQUIRED")
    return value


def _exact_fields(value: object, fields: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    keys = frozenset(payload)
    if fields - keys:
        _fail("REQUIRED_FIELD_MISSING")
    if keys - fields:
        _fail("UNEXPECTED_FIELD")
    return payload


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("SEQUENCE_REQUIRED")
    return value


def _enum(value: object, enum_type: type[Enum]) -> Enum:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        _fail("ENUM_INVALID")
    try:
        return enum_type(value)
    except ValueError:
        _fail("ENUM_INVALID")


def _canonical_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            _fail("UTC_TIMESTAMP_REQUIRED")
        normalized = value.astimezone(UTC).replace(microsecond=0)
        return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str) or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        _fail("UTC_TIMESTAMP_REQUIRED")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        _fail("UTC_TIMESTAMP_REQUIRED")
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_bytes(value: object) -> bytes:
    verify_sanitized_evidence(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProductionRuntimeAttestationError("JSON_VALUE_INVALID") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _digest_value(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail("DIGEST_INVALID")
    return value


def _duplicate_free_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def parse_json_document(value: bytes | str) -> object:
    if isinstance(value, bytes):
        if len(value) > MAX_DOCUMENT_BYTES:
            _fail("DOCUMENT_TOO_LARGE")
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProductionRuntimeAttestationError("JSON_INVALID") from exc
    elif isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_DOCUMENT_BYTES:
            _fail("DOCUMENT_TOO_LARGE")
        text = value
    else:
        _fail("JSON_INVALID")
    try:
        return json.loads(
            text,
            object_pairs_hook=_duplicate_free_object,
            parse_constant=lambda _value: _fail("NON_FINITE_NUMBER"),
        )
    except ProductionRuntimeAttestationError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProductionRuntimeAttestationError("JSON_INVALID") from exc


def create_collector_result(
    collector_id: str,
    status: CollectorStatus | str,
    observations: Mapping[str, object],
) -> CollectorResult:
    cid = _identifier(collector_id)
    collector_status = _enum(status, CollectorStatus)
    sanitized = sanitize_evidence(_mapping(observations))
    assert isinstance(sanitized, Mapping)
    normalized = {_observation_key(k): v for k, v in sanitized.items()}
    if collector_status == CollectorStatus.UNAVAILABLE and normalized:
        _fail("UNAVAILABLE_COLLECTOR_HAS_OBSERVATIONS")
    return CollectorResult(cid, collector_status, normalized)


def _collector_payload(result: CollectorResult) -> dict[str, object]:
    validated = create_collector_result(
        result.collector_id, result.status, result.observations
    )
    return {
        "collector_id": validated.collector_id,
        "observations": dict(validated.observations),
        "status": validated.status.value,
    }


def _collector_from_mapping(value: object) -> CollectorResult:
    payload = _exact_fields(value, _COLLECTOR_FIELDS)
    return create_collector_result(
        _identifier(payload["collector_id"]),
        _enum(payload["status"], CollectorStatus),
        _mapping(payload["observations"]),
    )


def _attestation_content(
    target: str, collected_at_utc: str, collectors: Sequence[CollectorResult]
) -> dict[str, object]:
    return {
        "collected_at_utc": collected_at_utc,
        "collectors": [_collector_payload(item) for item in collectors],
        "schema_version": SCHEMA_VERSION,
        "target": target,
    }


def create_attestation(
    *,
    target: str,
    collected_at_utc: str | datetime,
    collectors: Sequence[CollectorResult],
) -> ProductionRuntimeAttestation:
    normalized_target = _identifier(target)
    timestamp = _canonical_timestamp(collected_at_utc)
    ordered = tuple(sorted(collectors, key=lambda item: item.collector_id))
    if len({item.collector_id for item in ordered}) != len(ordered):
        _fail("DUPLICATE_COLLECTOR")
    content = _attestation_content(normalized_target, timestamp, ordered)
    attestation = ProductionRuntimeAttestation(
        SCHEMA_VERSION,
        _digest(content),
        normalized_target,
        timestamp,
        ordered,
    )
    return validate_attestation(attestation)


def normalize_attestation(value: ProductionRuntimeAttestation) -> dict[str, object]:
    return {
        **_attestation_content(value.target, value.collected_at_utc, value.collectors),
        "attestation_id": value.attestation_id,
    }


def validate_attestation(
    value: ProductionRuntimeAttestation | Mapping[str, object],
) -> ProductionRuntimeAttestation:
    if isinstance(value, ProductionRuntimeAttestation):
        payload = normalize_attestation(value)
    else:
        payload = dict(_exact_fields(value, _ATTESTATION_FIELDS))
        verify_sanitized_evidence(payload)
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(
        payload.get("schema_version"), bool
    ):
        _fail("SCHEMA_VERSION_INVALID")
    collectors = tuple(_collector_from_mapping(item) for item in _items(payload["collectors"]))
    if tuple(item.collector_id for item in collectors) != tuple(
        sorted(item.collector_id for item in collectors)
    ):
        _fail("COLLECTOR_ORDER_INVALID")
    if len({item.collector_id for item in collectors}) != len(collectors):
        _fail("DUPLICATE_COLLECTOR")
    target = _identifier(payload["target"])
    timestamp = _canonical_timestamp(payload["collected_at_utc"])
    expected_id = _digest(_attestation_content(target, timestamp, collectors))
    if _digest_value(payload["attestation_id"]) != expected_id:
        _fail("TAMPERED_ATTESTATION_ID")
    result = ProductionRuntimeAttestation(
        SCHEMA_VERSION, expected_id, target, timestamp, collectors
    )
    verify_sanitized_evidence(normalize_attestation(result))
    return result


def serialize_attestation(value: ProductionRuntimeAttestation) -> bytes:
    return _canonical_bytes(normalize_attestation(validate_attestation(value)))


def deserialize_attestation(value: bytes | str) -> ProductionRuntimeAttestation:
    return validate_attestation(_mapping(parse_json_document(value)))


def create_intended_state(
    *, target: str, expected_observations: Mapping[str, Mapping[str, object]]
) -> IntendedProductionState:
    expectations: list[CollectorExpectation] = []
    for collector_id, observations in _mapping(expected_observations).items():
        cid = _identifier(collector_id)
        sanitized = sanitize_evidence(_mapping(observations))
        assert isinstance(sanitized, Mapping)
        normalized = {_observation_key(k): v for k, v in sanitized.items()}
        if not normalized:
            _fail("EXPECTED_OBSERVATIONS_EMPTY")
        expectations.append(CollectorExpectation(cid, normalized))
    if not expectations:
        _fail("EXPECTED_COLLECTORS_EMPTY")
    expectations.sort(key=lambda item: item.collector_id)
    return IntendedProductionState(SCHEMA_VERSION, _identifier(target), tuple(expectations))


def normalize_intended_state(value: IntendedProductionState) -> dict[str, object]:
    return {
        "expected_collectors": [
            {
                "collector_id": item.collector_id,
                "observations": dict(item.observations),
            }
            for item in value.expected_collectors
        ],
        "schema_version": value.schema_version,
        "target": value.target,
    }


def validate_intended_state(
    value: IntendedProductionState | Mapping[str, object],
) -> IntendedProductionState:
    if isinstance(value, IntendedProductionState):
        payload = normalize_intended_state(value)
    else:
        payload = dict(_exact_fields(value, _INTENDED_FIELDS))
        verify_sanitized_evidence(payload)
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(
        payload.get("schema_version"), bool
    ):
        _fail("SCHEMA_VERSION_INVALID")
    expected: dict[str, Mapping[str, object]] = {}
    ordered_ids: list[str] = []
    for item in _items(payload["expected_collectors"]):
        expectation = _exact_fields(item, _EXPECTATION_FIELDS)
        collector_id = _identifier(expectation["collector_id"])
        if collector_id in expected:
            _fail("DUPLICATE_COLLECTOR")
        expected[collector_id] = _mapping(expectation["observations"])
        ordered_ids.append(collector_id)
    if ordered_ids != sorted(ordered_ids):
        _fail("COLLECTOR_ORDER_INVALID")
    result = create_intended_state(
        target=_identifier(payload["target"]), expected_observations=expected
    )
    verify_sanitized_evidence(normalize_intended_state(result))
    return result


def serialize_intended_state(value: IntendedProductionState) -> bytes:
    return _canonical_bytes(normalize_intended_state(validate_intended_state(value)))


def deserialize_intended_state(value: bytes | str) -> IntendedProductionState:
    return validate_intended_state(_mapping(parse_json_document(value)))


def _comparison_content(
    *,
    target: str,
    attestation_id: str,
    intended_digest: str,
    status: ComparisonStatus,
    drifted: Sequence[str],
    missing: Sequence[str],
) -> dict[str, object]:
    return {
        "attestation_id": attestation_id,
        "drifted_observations": list(drifted),
        "intended_digest": intended_digest,
        "missing_observations": list(missing),
        "schema_version": SCHEMA_VERSION,
        "status": status.value,
        "target": target,
    }


def compare_production_runtime(
    intended: IntendedProductionState, attestation: ProductionRuntimeAttestation
) -> ProductionRuntimeComparison:
    intended = validate_intended_state(intended)
    attestation = validate_attestation(attestation)
    if intended.target != attestation.target:
        _fail("TARGET_MISMATCH")
    available = {item.collector_id: item for item in attestation.collectors}
    drifted: list[str] = []
    missing: list[str] = []
    for expected in intended.expected_collectors:
        observed = available.get(expected.collector_id)
        for key, expected_value in sorted(expected.observations.items()):
            path = f"{expected.collector_id}.{key}"
            if observed is None or observed.status != CollectorStatus.AVAILABLE:
                missing.append(path)
            elif key not in observed.observations:
                missing.append(path)
            elif observed.observations[key] != expected_value:
                drifted.append(path)
    status = (
        ComparisonStatus.DRIFT
        if drifted
        else ComparisonStatus.INSUFFICIENT_EVIDENCE
        if missing
        else ComparisonStatus.MATCH
    )
    intended_digest = _digest(normalize_intended_state(intended))
    content = _comparison_content(
        target=intended.target,
        attestation_id=attestation.attestation_id,
        intended_digest=intended_digest,
        status=status,
        drifted=drifted,
        missing=missing,
    )
    return ProductionRuntimeComparison(
        SCHEMA_VERSION,
        _digest(content),
        intended.target,
        attestation.attestation_id,
        intended_digest,
        status,
        tuple(drifted),
        tuple(missing),
    )


def normalize_comparison(value: ProductionRuntimeComparison) -> dict[str, object]:
    return {
        **_comparison_content(
            target=value.target,
            attestation_id=value.attestation_id,
            intended_digest=value.intended_digest,
            status=value.status,
            drifted=value.drifted_observations,
            missing=value.missing_observations,
        ),
        "comparison_id": value.comparison_id,
    }


def validate_comparison(
    value: ProductionRuntimeComparison | Mapping[str, object],
) -> ProductionRuntimeComparison:
    if isinstance(value, ProductionRuntimeComparison):
        payload = normalize_comparison(value)
    else:
        payload = dict(_exact_fields(value, _COMPARISON_FIELDS))
        verify_sanitized_evidence(payload)
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(
        payload.get("schema_version"), bool
    ):
        _fail("SCHEMA_VERSION_INVALID")
    status = _enum(payload["status"], ComparisonStatus)
    assert isinstance(status, ComparisonStatus)
    drifted = tuple(_identifier(item) for item in _items(payload["drifted_observations"]))
    missing = tuple(_identifier(item) for item in _items(payload["missing_observations"]))
    if drifted != tuple(sorted(set(drifted))) or missing != tuple(sorted(set(missing))):
        _fail("COMPARISON_ORDER_INVALID")
    target = _identifier(payload["target"])
    attestation_id = _digest_value(payload["attestation_id"])
    intended_digest = _digest_value(payload["intended_digest"])
    content = _comparison_content(
        target=target,
        attestation_id=attestation_id,
        intended_digest=intended_digest,
        status=status,
        drifted=drifted,
        missing=missing,
    )
    expected_id = _digest(content)
    if _digest_value(payload["comparison_id"]) != expected_id:
        _fail("TAMPERED_COMPARISON_ID")
    result = ProductionRuntimeComparison(
        SCHEMA_VERSION,
        expected_id,
        target,
        attestation_id,
        intended_digest,
        status,
        drifted,
        missing,
    )
    verify_sanitized_evidence(normalize_comparison(result))
    return result


def serialize_comparison(value: ProductionRuntimeComparison) -> bytes:
    return _canonical_bytes(normalize_comparison(validate_comparison(value)))


def deserialize_comparison(value: bytes | str) -> ProductionRuntimeComparison:
    return validate_comparison(_mapping(parse_json_document(value)))


def sanitize_json_document(value: bytes | str) -> bytes:
    return _canonical_bytes(sanitize_evidence(parse_json_document(value)))
