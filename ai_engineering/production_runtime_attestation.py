"""Deterministic offline ProductionRuntimeAttestation v1 contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, NoReturn, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

from scripts.secret_scanner import SecretScanError, scan_secret_text

UTC = timezone.utc

SCHEMA_VERSION = 1
MAX_DOCUMENT_BYTES = 1_048_576
REDACTED = "<REDACTED>"

DOMAIN_SEPARATOR_ATTESTATION = b"hermes-production-runtime-attestation-v1\n"
DOMAIN_SEPARATOR_SIGNATURE_V1 = b"hermes-production-runtime-signature-v1\n"
DOMAIN_SEPARATOR_SIGNATURE_V2 = b"hermes-production-runtime-signature-v2\n"
DOMAIN_SEPARATOR_SIGNATURE = DOMAIN_SEPARATOR_SIGNATURE_V2
DOMAIN_SEPARATOR_INTENDED = b"hermes-production-runtime-intended-v1\n"
DOMAIN_SEPARATOR_COMPARISON = b"hermes-production-runtime-comparison-v1\n"

SIGNATURE_CONTRACT_VERSION = 2
SIGNATURE_ALGORITHM_V2 = "ed25519-v2"
MAX_CLOCK_SKEW_SECONDS = 300
DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 86400

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
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
class EvidenceProvenance:
    signer_identity: str
    signature_algorithm: str
    signature: str
    signed_at_utc: str
    valid_until_utc: str
    signature_contract_version: int = SIGNATURE_CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class ObservedRuntimeState:
    image_digest: str
    oci_revision: str
    release_sha: str
    runtime_contract: str
    schema_version: int
    active_services: tuple[str, ...]
    configuration_contract_version: str
    raw_observations: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationMetadata:
    verified: bool
    verified_at_utc: str | None
    integrity_status: str
    authenticity_status: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class CollectorResult:
    collector_id: str
    status: CollectorStatus
    observations: Mapping[str, Any]


class ProductionRuntimeCollector(Protocol):
    """Interface only; B1 deliberately provides no live implementation."""

    collector_id: str

    def collect(self) -> CollectorResult: ...


class RuntimeCollector(Protocol):
    """Structured runtime collector interface for ProductionRuntimeAttestation v1."""

    collector_id: str
    collector_version: str

    def collect_identity(self) -> dict[str, str]: ...
    def collect_image_metadata(self) -> dict[str, str]: ...
    def collect_service_state(self) -> dict[str, Any]: ...
    def collect_runtime_contract(self) -> dict[str, str]: ...
    def collect_schema_metadata(self) -> dict[str, Any]: ...


class MockRuntimeCollector:
    """Mock offline implementation for testing and contract compliance."""

    def __init__(
        self,
        collector_id: str = "mock-collector",
        collector_version: str = "1.0.0",
        *,
        identity: dict[str, str] | None = None,
        image_metadata: dict[str, str] | None = None,
        service_state: dict[str, Any] | None = None,
        runtime_contract: dict[str, str] | None = None,
        schema_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.collector_id = collector_id
        self.collector_version = collector_version
        self._identity = identity or {"environment_id": "production"}
        self._image_metadata = image_metadata or {
            "image_digest": "sha256:" + "0" * 64,
            "oci_revision": "0" * 40,
        }
        self._service_state = service_state or {"active_services": ["gateway"]}
        self._runtime_contract = runtime_contract or {"runtime_contract": "hermes-v1"}
        self._schema_metadata = schema_metadata or {"schema_version": 1}

    def collect_identity(self) -> dict[str, str]:
        return dict(self._identity)

    def collect_image_metadata(self) -> dict[str, str]:
        return dict(self._image_metadata)

    def collect_service_state(self) -> dict[str, Any]:
        return dict(self._service_state)

    def collect_runtime_contract(self) -> dict[str, str]:
        return dict(self._runtime_contract)

    def collect_schema_metadata(self) -> dict[str, Any]:
        return dict(self._schema_metadata)


class StaticFixtureCollector:
    """In-memory fixture-backed collector for offline evaluations."""

    def __init__(self, data: Mapping[str, Any]) -> None:
        self.collector_id = str(data.get("collector_id", "static-fixture-collector"))
        self.collector_version = str(data.get("collector_version", "1.0.0"))
        self._data = dict(data)

    def collect_identity(self) -> dict[str, str]:
        return dict(self._data.get("identity", {}))

    def collect_image_metadata(self) -> dict[str, str]:
        return dict(self._data.get("image_metadata", {}))

    def collect_service_state(self) -> dict[str, Any]:
        return dict(self._data.get("service_state", {}))

    def collect_runtime_contract(self) -> dict[str, str]:
        return dict(self._data.get("runtime_contract", {}))

    def collect_schema_metadata(self) -> dict[str, Any]:
        return dict(self._data.get("schema_metadata", {}))


@dataclass(frozen=True, slots=True)
class ProductionRuntimeAttestation:
    schema_version: int
    attestation_id: str
    target: str
    collected_at_utc: str
    collectors: tuple[CollectorResult, ...]
    canonical_repository: str = "https://github.com/life2boat/hermes.git"
    canonical_main_sha: str = "0" * 40
    environment_id: str = ""
    collection_context_id: str = "default-ctx"
    observed_at: str = ""
    collector_id: str = "runtime"
    collector_version: str = "1.0.0"
    evidence_provenance: EvidenceProvenance | None = None
    observed_runtime_state: ObservedRuntimeState | None = None
    verification_metadata: VerificationMetadata | None = None

    def __post_init__(self) -> None:
        if not self.environment_id:
            object.__setattr__(self, "environment_id", self.target)
        if not self.observed_at:
            object.__setattr__(self, "observed_at", self.collected_at_utc)


@dataclass(frozen=True, slots=True)
class CollectorExpectation:
    collector_id: str
    observations: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class IntendedProductionState:
    schema_version: int
    target: str
    expected_collectors: tuple[CollectorExpectation, ...]
    canonical_repository: str = ""
    intended_main_sha: str = "0" * 40
    intended_image_digest: str = "sha256:" + "0" * 64
    intended_oci_revision: str = "0" * 40
    expected_service_set: tuple[str, ...] = ("gateway",)
    expected_runtime_contract: str = "hermes-runtime-v1"
    expected_schema_version: int = 1
    expected_configuration_contract_version: str = "v1"
    expected_release_identity: str = "hermes-release-1"

    def __post_init__(self) -> None:
        if not self.canonical_repository:
            object.__setattr__(self, "canonical_repository", self.target)


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
    reason_codes: tuple[str, ...] = ()
    evaluated_at_utc: str = ""


_COLLECTOR_FIELDS = frozenset({"collector_id", "status", "observations"})
_LEGACY_ATTESTATION_FIELDS = frozenset(
    {"schema_version", "attestation_id", "target", "collected_at_utc", "collectors"}
)
_V1_ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "attestation_id",
        "canonical_repository",
        "canonical_main_sha",
        "environment_id",
        "collection_context_id",
        "observed_at",
        "collector_id",
        "collector_version",
        "evidence_provenance",
        "observed_runtime_state",
        "verification_metadata",
    }
)
_EXPECTATION_FIELDS = frozenset({"collector_id", "observations"})
_LEGACY_INTENDED_FIELDS = frozenset({"schema_version", "target", "expected_collectors"})
_V1_INTENDED_FIELDS = frozenset(
    {
        "schema_version",
        "canonical_repository",
        "intended_main_sha",
        "intended_image_digest",
        "intended_oci_revision",
        "expected_service_set",
        "expected_runtime_contract",
        "expected_schema_version",
        "expected_configuration_contract_version",
        "expected_release_identity",
    }
)
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
            if _string_has_secret(item):
                _fail("UNSANITIZED_SECRET_MATERIAL")
            return
        if item is None or isinstance(item, (bool, int, float)):
            return
        _fail("JSON_VALUE_INVALID")

    verify(value)


def sanitize_json_document(raw_json: bytes | str) -> bytes:
    parsed = parse_json_document(raw_json)
    return _canonical_bytes(sanitize_evidence(parsed))


def parse_json_document(raw_json: bytes | str) -> object:
    if isinstance(raw_json, str):
        payload = raw_json.encode("utf-8")
    elif isinstance(raw_json, (bytes, bytearray)):
        payload = bytes(raw_json)
    else:
        _fail("JSON_INPUT_INVALID")

    if not payload:
        _fail("DOCUMENT_EMPTY")
    if len(payload) > MAX_DOCUMENT_BYTES:
        _fail("DOCUMENT_SIZE_EXCEEDED")

    def object_pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        seen: set[str] = set()
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in seen:
                _fail("DUPLICATE_JSON_KEY")
            seen.add(key)
            result[key] = value
        return result

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProductionRuntimeAttestationError("UTF8_DECODE_FAILED") from exc

    try:
        return json.loads(
            text,
            object_pairs_hook=object_pairs_hook,
            parse_constant=lambda _: _fail("NON_FINITE_NUMBER"),
        )
    except json.JSONDecodeError as exc:
        raise ProductionRuntimeAttestationError("JSON_SYNTAX_INVALID") from exc


def _canonical_bytes(value: object) -> bytes:
    def normalize(item: object) -> object:
        if isinstance(item, Mapping):
            return {str(k): normalize(v) for k, v in sorted(item.items())}
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        if isinstance(item, float):
            if not math.isfinite(item):
                _fail("NON_FINITE_NUMBER")
            return item
        return item

    text = json.dumps(
        normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _fail("MAPPING_EXPECTED")
    return value


def _items(value: object) -> list[object]:
    if not isinstance(value, (list, tuple)):
        _fail("SEQUENCE_EXPECTED")
    return list(value)


def _exact_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    mapping = _mapping(value)
    keys = frozenset(mapping.keys())
    if keys != expected:
        _fail("DOCUMENT_FIELD_MISMATCH")
    return mapping


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        _fail("IDENTIFIER_INVALID")
    return value


def _digest_or_sha(value: object) -> str:
    if not isinstance(value, str) or (
        not _DIGEST_RE.fullmatch(value)
        and not _SHA_RE.fullmatch(value)
        and not (value.startswith("sha256:") and _DIGEST_RE.fullmatch(value[7:]))
    ):
        _fail("DIGEST_INVALID")
    return value


def _canonical_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            _fail("UTC_TIMESTAMP_INVALID")
        utc_dt = value.astimezone(UTC)
        return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if not isinstance(value, str) or not _UTC_TIMESTAMP_RE.fullmatch(value):
        _fail("UTC_TIMESTAMP_INVALID")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ProductionRuntimeAttestationError("UTC_TIMESTAMP_INVALID") from exc
    return value


def _utc_timestamp(value: object) -> str:
    return _canonical_timestamp(value)


def _observation_key(value: object) -> str:
    if not isinstance(value, str) or not _OBSERVATION_KEY_RE.fullmatch(value):
        _fail("OBSERVATION_KEY_INVALID")
    return value


def _digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _collector_payload(result: CollectorResult) -> dict[str, object]:
    validated = create_collector_result(
        result.collector_id, result.status, result.observations
    )
    return {
        "collector_id": validated.collector_id,
        "observations": dict(validated.observations),
        "status": validated.status.value,
    }


def create_collector_result(
    collector_id: str,
    status: CollectorStatus | str,
    observations: Mapping[str, Any],
) -> CollectorResult:
    normalized_id = _identifier(collector_id)
    try:
        status_enum = (
            status if isinstance(status, CollectorStatus) else CollectorStatus(status)
        )
    except ValueError as exc:
        raise ProductionRuntimeAttestationError("COLLECTOR_STATUS_INVALID") from exc

    sanitized = sanitize_evidence(_mapping(observations))
    assert isinstance(sanitized, Mapping)
    cleaned: dict[str, object] = {
        _observation_key(k): v for k, v in sanitized.items()
    }
    if status_enum == CollectorStatus.UNAVAILABLE and cleaned:
        _fail("UNAVAILABLE_COLLECTOR_WITH_OBSERVATIONS")
    return CollectorResult(normalized_id, status_enum, cleaned)


# ---------------------------------------------------------------------------
# Cryptographic Key & Signature Management (Ed25519)
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[ed25519.Ed25519PrivateKey, ed25519.Ed25519PublicKey]:
    priv = ed25519.Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def export_private_key_hex(key: ed25519.Ed25519PrivateKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return raw.hex()


def export_public_key_hex(key: ed25519.Ed25519PublicKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return raw.hex()


def import_private_key_hex(hex_str: str) -> ed25519.Ed25519PrivateKey:
    try:
        raw = bytes.fromhex(hex_str.strip())
        return ed25519.Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        raise ProductionRuntimeAttestationError("PRIVATE_KEY_INVALID") from exc


def import_public_key_hex(hex_str: str) -> ed25519.Ed25519PublicKey:
    try:
        raw = bytes.fromhex(hex_str.strip())
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise ProductionRuntimeAttestationError("PUBLIC_KEY_INVALID") from exc


def compute_signature_payload(
    *,
    attestation_id: str,
    canonical_repository: str,
    environment_id: str,
    collection_context_id: str,
    signer_identity: str,
    signed_at_utc: str,
    valid_until_utc: str,
    signature_contract_version: int = SIGNATURE_CONTRACT_VERSION,
) -> bytes:
    payload = {
        "attestation_id": attestation_id,
        "canonical_repository": canonical_repository,
        "collection_context_id": collection_context_id,
        "domain": "hermes-production-runtime-signature-v2",
        "environment_id": environment_id,
        "signature_contract_version": signature_contract_version,
        "signed_at_utc": signed_at_utc,
        "signer_identity": signer_identity,
        "valid_until_utc": valid_until_utc,
    }
    return DOMAIN_SEPARATOR_SIGNATURE_V2 + _canonical_bytes(payload)


def sign_attestation(
    attestation: ProductionRuntimeAttestation,
    signing_key: ed25519.Ed25519PrivateKey | str,
    *,
    valid_duration_seconds: int = 3600,
    signed_at_utc: datetime | str | None = None,
) -> ProductionRuntimeAttestation:
    priv = (
        import_private_key_hex(signing_key)
        if isinstance(signing_key, str)
        else signing_key
    )
    signer_identity = export_public_key_hex(priv.public_key())
    if signed_at_utc is not None:
        now_dt = (
            signed_at_utc
            if isinstance(signed_at_utc, datetime)
            else datetime.strptime(signed_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        )
    else:
        now_dt = datetime.now(UTC)
    signed_at = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    from datetime import timedelta

    valid_until = (now_dt + timedelta(seconds=valid_duration_seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    env_id = attestation.environment_id or attestation.target or "synthetic-prod"
    repo = attestation.canonical_repository or "https://github.com/life2boat/hermes.git"
    ctx_id = attestation.collection_context_id or "default-ctx"

    sig_payload = compute_signature_payload(
        attestation_id=attestation.attestation_id,
        canonical_repository=repo,
        environment_id=env_id,
        collection_context_id=ctx_id,
        signer_identity=signer_identity,
        signed_at_utc=signed_at,
        valid_until_utc=valid_until,
        signature_contract_version=SIGNATURE_CONTRACT_VERSION,
    )
    sig_bytes = priv.sign(sig_payload)
    sig_hex = sig_bytes.hex()

    provenance = EvidenceProvenance(
        signer_identity=signer_identity,
        signature_algorithm=SIGNATURE_ALGORITHM_V2,
        signature=sig_hex,
        signed_at_utc=signed_at,
        valid_until_utc=valid_until,
        signature_contract_version=SIGNATURE_CONTRACT_VERSION,
    )

    return replace(attestation, evidence_provenance=provenance)


def verify_attestation_signature(
    attestation: ProductionRuntimeAttestation,
    trusted_public_keys: Sequence[str | ed25519.Ed25519PublicKey],
    *,
    current_time_utc: datetime | str | None = None,
    max_clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS,
    max_evidence_age_seconds: int = DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
) -> tuple[bool, str]:
    if attestation.evidence_provenance is None:
        return False, "MISSING_EVIDENCE_PROVENANCE"

    prov = attestation.evidence_provenance
    if (
        prov.signature_algorithm != SIGNATURE_ALGORITHM_V2
        or getattr(prov, "signature_contract_version", 0) != SIGNATURE_CONTRACT_VERSION
    ):
        return False, "UNSUPPORTED_SIGNATURE_CONTRACT"

    if not trusted_public_keys:
        return False, "MISSING_TRUST_STORE"

    trusted_hex_keys: set[str] = set()
    for item in trusted_public_keys:
        if isinstance(item, str):
            trusted_hex_keys.add(item.strip().lower())
        else:
            trusted_hex_keys.add(export_public_key_hex(item).lower())

    if prov.signer_identity.lower() not in trusted_hex_keys:
        return False, "UNTRUSTED_SIGNATURE_KEY"

    # Mandatory evidence freshness check (defaults to datetime.now(UTC))
    if current_time_utc is not None:
        curr_dt = (
            current_time_utc
            if isinstance(current_time_utc, datetime)
            else datetime.strptime(current_time_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        )
    else:
        curr_dt = datetime.now(UTC)

    try:
        signed_at_dt = datetime.strptime(
            prov.signed_at_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        valid_until_dt = datetime.strptime(
            prov.valid_until_utc, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
        observed_at_str = attestation.observed_at or attestation.collected_at_utc
        observed_at_dt = datetime.strptime(
            observed_at_str, "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=UTC)
    except Exception:
        return False, "TIMESTAMP_PARSE_ERROR"

    if valid_until_dt <= signed_at_dt:
        return False, "INVALID_VALIDITY_INTERVAL"

    from datetime import timedelta

    # Clock skew into future
    if signed_at_dt > curr_dt + timedelta(seconds=max_clock_skew_seconds):
        return False, "EVIDENCE_CLOCK_SKEW_FUTURE"

    if observed_at_dt > curr_dt + timedelta(seconds=max_clock_skew_seconds):
        return False, "EVIDENCE_CLOCK_SKEW_FUTURE"

    # Signed before observation (modulo clock skew)
    if signed_at_dt < observed_at_dt - timedelta(seconds=max_clock_skew_seconds):
        return False, "INVALID_SIGNATURE_TIMESTAMP"

    # Expiration
    if curr_dt > valid_until_dt:
        return False, "EXPIRED_EVIDENCE"

    # Max evidence age
    if curr_dt - observed_at_dt > timedelta(seconds=max_evidence_age_seconds):
        return False, "STALE_EVIDENCE"

    # Cryptographic signature verification over bound metadata
    env_id = attestation.environment_id or attestation.target or "synthetic-prod"
    repo = attestation.canonical_repository or "https://github.com/life2boat/hermes.git"
    ctx_id = attestation.collection_context_id or "default-ctx"

    sig_payload = compute_signature_payload(
        attestation_id=attestation.attestation_id,
        canonical_repository=repo,
        environment_id=env_id,
        collection_context_id=ctx_id,
        signer_identity=prov.signer_identity,
        signed_at_utc=prov.signed_at_utc,
        valid_until_utc=prov.valid_until_utc,
        signature_contract_version=prov.signature_contract_version,
    )

    try:
        pub = import_public_key_hex(prov.signer_identity)
        sig_bytes = bytes.fromhex(prov.signature)
        pub.verify(sig_bytes, sig_payload)
        return True, "SIGNATURE_VALID"
    except InvalidSignature:
        return False, "INVALID_CRYPTOGRAPHIC_SIGNATURE"
    except Exception:
        return False, "SIGNATURE_VERIFICATION_ERROR"


# ---------------------------------------------------------------------------
# Attestation Creation & Normalization
# ---------------------------------------------------------------------------


def normalize_observed_state(state: ObservedRuntimeState) -> dict[str, object]:
    res: dict[str, object] = {
        "active_services": sorted(list(state.active_services)),
        "configuration_contract_version": state.configuration_contract_version,
        "image_digest": state.image_digest,
        "oci_revision": state.oci_revision,
        "release_sha": state.release_sha,
        "runtime_contract": state.runtime_contract,
        "schema_version": state.schema_version,
    }
    if state.raw_observations:
        res["raw_observations"] = {
            k: v for k, v in sorted(state.raw_observations.items())
        }
    return res


def _attestation_content(
    target: str, collected_at_utc: str, collectors: Sequence[CollectorResult]
) -> dict[str, object]:
    return {
        "collected_at_utc": collected_at_utc,
        "collectors": [_collector_payload(item) for item in collectors],
        "schema_version": SCHEMA_VERSION,
        "target": target,
    }


def compute_v1_attestation_id(payload: Mapping[str, object]) -> str:
    id_content = {
        "canonical_main_sha": payload.get("canonical_main_sha", ""),
        "canonical_repository": payload.get("canonical_repository", ""),
        "collection_context_id": payload.get("collection_context_id", ""),
        "collector_id": payload.get("collector_id", ""),
        "collector_version": payload.get("collector_version", ""),
        "domain": "hermes-production-runtime-attestation-v1",
        "environment_id": payload.get("environment_id", payload.get("target", "")),
        "observed_at": payload.get("observed_at", payload.get("collected_at_utc", "")),
        "observed_runtime_state": payload.get("observed_runtime_state", {}),
        "schema_version": payload.get("schema_version", SCHEMA_VERSION),
    }
    canonical = _canonical_bytes(id_content)
    hasher = hashlib.sha256()
    hasher.update(DOMAIN_SEPARATOR_ATTESTATION)
    hasher.update(canonical)
    return hasher.hexdigest()


def compute_attestation_id(payload: Mapping[str, object]) -> str:
    if "target" in payload and "canonical_repository" not in payload:
        return _digest(payload)
    return compute_v1_attestation_id(payload)


def create_attestation(
    *,
    target: str | None = None,
    collected_at_utc: str | datetime | None = None,
    collectors: Sequence[CollectorResult] | None = None,
    canonical_repository: str = "https://github.com/life2boat/hermes.git",
    canonical_main_sha: str = "0" * 40,
    environment_id: str | None = None,
    collection_context_id: str = "default-ctx",
    observed_at: str | datetime | None = None,
    collector_id: str = "runtime",
    collector_version: str = "1.0.0",
    observed_runtime_state: ObservedRuntimeState | None = None,
    evidence_provenance: EvidenceProvenance | None = None,
) -> ProductionRuntimeAttestation:
    tgt = target or environment_id or "synthetic-prod"
    normalized_target = _identifier(tgt)
    ts = collected_at_utc or observed_at
    if ts is None:
        _fail("UTC_TIMESTAMP_INVALID")
    timestamp = _canonical_timestamp(ts)

    collector_seq = tuple(collectors or ())
    ordered = tuple(sorted(collector_seq, key=lambda item: item.collector_id))
    if len({item.collector_id for item in ordered}) != len(ordered):
        _fail("DUPLICATE_COLLECTOR")

    # If observed_runtime_state is supplied or no collectors, use v1 shape
    if observed_runtime_state is not None:
        v1_payload = {
            "canonical_main_sha": canonical_main_sha,
            "canonical_repository": canonical_repository,
            "collection_context_id": collection_context_id,
            "collector_id": collector_id,
            "collector_version": collector_version,
            "environment_id": normalized_target,
            "observed_at": timestamp,
            "observed_runtime_state": normalize_observed_state(
                observed_runtime_state
            ),
            "schema_version": SCHEMA_VERSION,
        }
        att_id = compute_v1_attestation_id(v1_payload)
        attestation = ProductionRuntimeAttestation(
            schema_version=SCHEMA_VERSION,
            attestation_id=att_id,
            target=normalized_target,
            collected_at_utc=timestamp,
            collectors=ordered,
            canonical_repository=canonical_repository,
            canonical_main_sha=canonical_main_sha,
            environment_id=normalized_target,
            collection_context_id=collection_context_id,
            observed_at=timestamp,
            collector_id=collector_id,
            collector_version=collector_version,
            evidence_provenance=evidence_provenance,
            observed_runtime_state=observed_runtime_state,
        )
        return validate_attestation(attestation)

    # Legacy shape:
    content = _attestation_content(normalized_target, timestamp, ordered)
    att_id = _digest(content)
    attestation = ProductionRuntimeAttestation(
        schema_version=SCHEMA_VERSION,
        attestation_id=att_id,
        target=normalized_target,
        collected_at_utc=timestamp,
        collectors=ordered,
        canonical_repository=canonical_repository,
        canonical_main_sha=canonical_main_sha,
        environment_id=normalized_target,
        collection_context_id=collection_context_id,
        observed_at=timestamp,
        collector_id=collector_id,
        collector_version=collector_version,
        evidence_provenance=evidence_provenance,
        observed_runtime_state=None,
    )
    return validate_attestation(attestation)


def normalize_attestation(value: ProductionRuntimeAttestation) -> dict[str, object]:
    if value.observed_runtime_state is not None:
        obs_state = (
            normalize_observed_state(value.observed_runtime_state)
            if value.observed_runtime_state
            else {}
        )
        res: dict[str, object] = {
            "attestation_id": value.attestation_id,
            "canonical_main_sha": value.canonical_main_sha,
            "canonical_repository": value.canonical_repository,
            "collection_context_id": value.collection_context_id,
            "collector_id": value.collector_id,
            "collector_version": value.collector_version,
            "environment_id": value.environment_id,
            "evidence_provenance": (
                {
                    "signature": value.evidence_provenance.signature,
                    "signature_algorithm": value.evidence_provenance.signature_algorithm,
                    "signature_contract_version": value.evidence_provenance.signature_contract_version,
                    "signed_at_utc": value.evidence_provenance.signed_at_utc,
                    "signer_identity": value.evidence_provenance.signer_identity,
                    "valid_until_utc": value.evidence_provenance.valid_until_utc,
                }
                if value.evidence_provenance is not None
                else None
            ),
            "observed_at": value.observed_at,
            "observed_runtime_state": obs_state,
            "schema_version": value.schema_version,
        }
        if value.verification_metadata is not None:
            res["verification_metadata"] = {
                "authenticity_status": value.verification_metadata.authenticity_status,
                "integrity_status": value.verification_metadata.integrity_status,
                "reason": value.verification_metadata.reason,
                "verified": value.verification_metadata.verified,
                "verified_at_utc": value.verification_metadata.verified_at_utc,
            }
        else:
            res["verification_metadata"] = None
        return res

    res = {
        "attestation_id": value.attestation_id,
        "collected_at_utc": value.collected_at_utc,
        "collectors": [_collector_payload(item) for item in value.collectors],
        "schema_version": value.schema_version,
        "target": value.target,
    }
    if value.evidence_provenance is not None:
        res["evidence_provenance"] = {
            "signature": value.evidence_provenance.signature,
            "signature_algorithm": value.evidence_provenance.signature_algorithm,
            "signature_contract_version": value.evidence_provenance.signature_contract_version,
            "signed_at_utc": value.evidence_provenance.signed_at_utc,
            "signer_identity": value.evidence_provenance.signer_identity,
            "valid_until_utc": value.evidence_provenance.valid_until_utc,
        }
    if value.verification_metadata is not None:
        res["verification_metadata"] = {
            "authenticity_status": value.verification_metadata.authenticity_status,
            "integrity_status": value.verification_metadata.integrity_status,
            "reason": value.verification_metadata.reason,
            "verified": value.verification_metadata.verified,
            "verified_at_utc": value.verification_metadata.verified_at_utc,
        }
    return res


def validate_attestation(
    value: ProductionRuntimeAttestation | Mapping[str, object],
) -> ProductionRuntimeAttestation:
    if isinstance(value, ProductionRuntimeAttestation):
        normalized = normalize_attestation(value)
        verify_sanitized_evidence(normalized)
        if value.observed_runtime_state is not None:
            expected_id = compute_v1_attestation_id(normalized)
        else:
            content = _attestation_content(
                value.target, value.collected_at_utc, value.collectors
            )
            expected_id = _digest(content)
        if value.attestation_id != expected_id:
            _fail("TAMPERED_ATTESTATION_ID")
        return value

    payload = _mapping(value)
    verify_sanitized_evidence(payload)

    # Check for v1 full shape
    if "canonical_repository" in payload or "observed_runtime_state" in payload:
        schema_ver = payload.get("schema_version")
        if schema_ver != SCHEMA_VERSION or isinstance(schema_ver, bool):
            _fail("SCHEMA_VERSION_INVALID")

        expected_id = compute_v1_attestation_id(payload)
        if payload.get("attestation_id") != expected_id:
            _fail("TAMPERED_ATTESTATION_ID")

        prov_dict = payload.get("evidence_provenance")
        provenance = None
        if prov_dict is not None and isinstance(prov_dict, Mapping):
            provenance = EvidenceProvenance(
                signer_identity=_identifier(prov_dict["signer_identity"]),
                signature_algorithm=str(prov_dict["signature_algorithm"]),
                signature=str(prov_dict["signature"]),
                signed_at_utc=_canonical_timestamp(prov_dict["signed_at_utc"]),
                valid_until_utc=_canonical_timestamp(prov_dict["valid_until_utc"]),
                signature_contract_version=int(
                    prov_dict.get("signature_contract_version", SIGNATURE_CONTRACT_VERSION)
                ),
            )

        obs_dict = _mapping(payload.get("observed_runtime_state", {}))
        observed_state = ObservedRuntimeState(
            image_digest=_digest_or_sha(obs_dict["image_digest"]),
            oci_revision=str(obs_dict["oci_revision"]),
            release_sha=str(obs_dict["release_sha"]),
            runtime_contract=str(obs_dict["runtime_contract"]),
            schema_version=int(obs_dict["schema_version"]),
            active_services=tuple(sorted(list(obs_dict["active_services"]))),
            configuration_contract_version=str(
                obs_dict["configuration_contract_version"]
            ),
            raw_observations=_mapping(obs_dict.get("raw_observations", {})),
        )

        env_id = _identifier(payload.get("environment_id", payload.get("target", "synthetic-prod")))
        obs_at = _canonical_timestamp(payload.get("observed_at", payload.get("collected_at_utc")))
        return ProductionRuntimeAttestation(
            schema_version=SCHEMA_VERSION,
            attestation_id=expected_id,
            target=env_id,
            collected_at_utc=obs_at,
            collectors=(),
            canonical_repository=_identifier(payload.get("canonical_repository", "https://github.com/life2boat/hermes.git")),
            canonical_main_sha=str(payload.get("canonical_main_sha", "0" * 40)),
            environment_id=env_id,
            collection_context_id=_identifier(payload.get("collection_context_id", "default-ctx")),
            observed_at=obs_at,
            collector_id=_identifier(payload.get("collector_id", "runtime")),
            collector_version=_identifier(payload.get("collector_version", "1.0.0")),
            evidence_provenance=provenance,
            observed_runtime_state=observed_state,
        )

    # Legacy shape
    allowed_keys = _LEGACY_ATTESTATION_FIELDS | {"evidence_provenance", "verification_metadata"}
    keys = frozenset(payload.keys())
    if not _LEGACY_ATTESTATION_FIELDS.issubset(keys) or not keys.issubset(allowed_keys):
        _fail("DOCUMENT_FIELD_MISMATCH")

    schema_ver = payload.get("schema_version")
    if schema_ver != SCHEMA_VERSION or isinstance(schema_ver, bool):
        _fail("SCHEMA_VERSION_INVALID")

    target = _identifier(payload["target"])
    timestamp = _canonical_timestamp(payload["collected_at_utc"])
    collectors_raw = _items(payload["collectors"])
    collectors: list[CollectorResult] = []
    ordered_ids: list[str] = []
    for item in collectors_raw:
        c_dict = _exact_fields(item, _COLLECTOR_FIELDS)
        cid = _identifier(c_dict["collector_id"])
        if cid in ordered_ids:
            _fail("DUPLICATE_COLLECTOR")
        ordered_ids.append(cid)
        collectors.append(
            create_collector_result(cid, c_dict["status"], c_dict["observations"])
        )
    if ordered_ids != sorted(ordered_ids):
        _fail("COLLECTOR_ORDER_INVALID")

    content = _attestation_content(target, timestamp, collectors)
    expected_id = _digest(content)
    if payload.get("attestation_id") != expected_id:
        _fail("TAMPERED_ATTESTATION_ID")

    prov_dict = payload.get("evidence_provenance")
    provenance = None
    if prov_dict is not None and isinstance(prov_dict, Mapping):
        provenance = EvidenceProvenance(
            signer_identity=_identifier(prov_dict["signer_identity"]),
            signature_algorithm=str(prov_dict["signature_algorithm"]),
            signature=str(prov_dict["signature"]),
            signed_at_utc=_canonical_timestamp(prov_dict["signed_at_utc"]),
            valid_until_utc=_canonical_timestamp(prov_dict["valid_until_utc"]),
            signature_contract_version=int(
                prov_dict.get("signature_contract_version", SIGNATURE_CONTRACT_VERSION)
            ),
        )

    result = ProductionRuntimeAttestation(
        schema_version=SCHEMA_VERSION,
        attestation_id=expected_id,
        target=target,
        collected_at_utc=timestamp,
        collectors=tuple(collectors),
        evidence_provenance=provenance,
    )
    verify_sanitized_evidence(normalize_attestation(result))
    return result


def serialize_attestation(value: ProductionRuntimeAttestation) -> bytes:
    return _canonical_bytes(normalize_attestation(validate_attestation(value)))


def deserialize_attestation(value: bytes | str) -> ProductionRuntimeAttestation:
    return validate_attestation(_mapping(parse_json_document(value)))


# ---------------------------------------------------------------------------
# Intended Production State
# ---------------------------------------------------------------------------


def create_intended_state(
    *,
    target: str | None = None,
    expected_observations: Mapping[str, Mapping[str, object]] | None = None,
    canonical_repository: str = "https://github.com/life2boat/hermes.git",
    intended_main_sha: str = "0" * 40,
    intended_image_digest: str = "sha256:" + "0" * 64,
    intended_oci_revision: str = "0" * 40,
    expected_service_set: Sequence[str] = ("gateway",),
    expected_runtime_contract: str = "hermes-runtime-v1",
    expected_schema_version: int = 1,
    expected_configuration_contract_version: str = "v1",
    expected_release_identity: str = "hermes-release-1",
) -> IntendedProductionState:
    tgt = target or canonical_repository
    normalized_target = _identifier(tgt)

    expectations: list[CollectorExpectation] = []
    if expected_observations is not None:
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

    return IntendedProductionState(
        schema_version=SCHEMA_VERSION,
        target=normalized_target,
        expected_collectors=tuple(expectations),
        canonical_repository=_identifier(canonical_repository),
        intended_main_sha=str(intended_main_sha),
        intended_image_digest=_digest_or_sha(intended_image_digest),
        intended_oci_revision=str(intended_oci_revision),
        expected_service_set=tuple(sorted(list(expected_service_set))),
        expected_runtime_contract=str(expected_runtime_contract),
        expected_schema_version=int(expected_schema_version),
        expected_configuration_contract_version=str(
            expected_configuration_contract_version
        ),
        expected_release_identity=str(expected_release_identity),
    )


def normalize_intended_state(value: IntendedProductionState) -> dict[str, object]:
    if value.expected_collectors:
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
    return {
        "canonical_repository": value.canonical_repository,
        "expected_configuration_contract_version": value.expected_configuration_contract_version,
        "expected_release_identity": value.expected_release_identity,
        "expected_runtime_contract": value.expected_runtime_contract,
        "expected_schema_version": value.expected_schema_version,
        "expected_service_set": sorted(list(value.expected_service_set)),
        "intended_image_digest": value.intended_image_digest,
        "intended_main_sha": value.intended_main_sha,
        "intended_oci_revision": value.intended_oci_revision,
        "schema_version": value.schema_version,
        "target": value.target,
    }


def validate_intended_state(
    value: IntendedProductionState | Mapping[str, object],
) -> IntendedProductionState:
    if isinstance(value, IntendedProductionState):
        payload = normalize_intended_state(value)
        verify_sanitized_evidence(payload)
        return value

    mapping = _mapping(value)
    if "expected_collectors" in mapping:
        payload = dict(_exact_fields(mapping, _LEGACY_INTENDED_FIELDS))
    else:
        allowed = _V1_INTENDED_FIELDS | {"target"}
        keys = frozenset(mapping.keys())
        if not _V1_INTENDED_FIELDS.issubset(keys) or not keys.issubset(allowed):
            _fail("DOCUMENT_FIELD_MISMATCH")
        payload = dict(mapping)
    verify_sanitized_evidence(payload)

    schema_ver = payload.get("schema_version")
    if schema_ver != SCHEMA_VERSION or isinstance(schema_ver, bool):
        _fail("SCHEMA_VERSION_INVALID")

    if "expected_collectors" in payload:
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

    tgt = payload.get("target") or payload["canonical_repository"]
    return create_intended_state(
        target=_identifier(tgt),
        canonical_repository=_identifier(payload["canonical_repository"]),
        intended_main_sha=str(payload["intended_main_sha"]),
        intended_image_digest=_digest_or_sha(payload["intended_image_digest"]),
        intended_oci_revision=str(payload["intended_oci_revision"]),
        expected_service_set=list(payload["expected_service_set"]),
        expected_runtime_contract=str(payload["expected_runtime_contract"]),
        expected_schema_version=int(payload["expected_schema_version"]),
        expected_configuration_contract_version=str(
            payload["expected_configuration_contract_version"]
        ),
        expected_release_identity=str(payload["expected_release_identity"]),
    )


def serialize_intended_state(value: IntendedProductionState) -> bytes:
    return _canonical_bytes(normalize_intended_state(validate_intended_state(value)))


def deserialize_intended_state(value: bytes | str) -> IntendedProductionState:
    return validate_intended_state(_mapping(parse_json_document(value)))


# ---------------------------------------------------------------------------
# Comparison Engine
# ---------------------------------------------------------------------------


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
    intended: IntendedProductionState,
    attestation: ProductionRuntimeAttestation,
    *,
    trusted_public_keys: Sequence[str | ed25519.Ed25519PublicKey] | None = None,
    current_time_utc: datetime | str | None = None,
    max_clock_skew_seconds: int = MAX_CLOCK_SKEW_SECONDS,
    max_evidence_age_seconds: int = DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
) -> ProductionRuntimeComparison:
    intended = validate_intended_state(intended)
    attestation = validate_attestation(attestation)

    if intended.target != attestation.target:
        _fail("TARGET_MISMATCH")

    drifted: list[str] = []
    missing: list[str] = []
    reasons: list[str] = []

    # Signature / provenance check
    if not trusted_public_keys:
        missing.append("trusted_public_keys")
        reasons.append("MISSING_TRUST_STORE")
    elif attestation.evidence_provenance is None:
        missing.append("evidence_provenance")
        reasons.append("MISSING_EVIDENCE_PROVENANCE")
    else:
        valid_sig, sig_reason = verify_attestation_signature(
            attestation,
            trusted_public_keys,
            current_time_utc=current_time_utc,
            max_clock_skew_seconds=max_clock_skew_seconds,
            max_evidence_age_seconds=max_evidence_age_seconds,
        )
        if not valid_sig:
            missing.append("signature_verification")
            reasons.append(sig_reason)

    # Legacy expected collectors check
    if intended.expected_collectors:
        available = {item.collector_id: item for item in attestation.collectors}
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
    elif attestation.observed_runtime_state is not None:
        obs = attestation.observed_runtime_state
        if intended.intended_main_sha != obs.release_sha:
            drifted.append(
                f"release_sha: expected {intended.intended_main_sha} but observed {obs.release_sha}"
            )
        if intended.intended_image_digest != obs.image_digest:
            drifted.append(
                f"image_digest: expected {intended.intended_image_digest} but observed {obs.image_digest}"
            )
        if intended.intended_oci_revision != obs.oci_revision:
            drifted.append(
                f"oci_revision: expected {intended.intended_oci_revision} but observed {obs.oci_revision}"
            )
        if intended.expected_runtime_contract != obs.runtime_contract:
            drifted.append(
                f"runtime_contract: expected {intended.expected_runtime_contract} but observed {obs.runtime_contract}"
            )
        if intended.expected_schema_version != obs.schema_version:
            drifted.append(
                f"schema_version: expected {intended.expected_schema_version} but observed {obs.schema_version}"
            )
        if (
            intended.expected_configuration_contract_version
            != obs.configuration_contract_version
        ):
            drifted.append(
                f"configuration_contract_version: expected {intended.expected_configuration_contract_version} but observed {obs.configuration_contract_version}"
            )
        for expected_svc in intended.expected_service_set:
            if expected_svc not in obs.active_services:
                missing.append(f"service:{expected_svc}")

    # Determine comparison status
    if reasons:
        status = ComparisonStatus.INSUFFICIENT_EVIDENCE
    elif drifted:
        status = ComparisonStatus.DRIFT
    elif missing:
        status = ComparisonStatus.INSUFFICIENT_EVIDENCE
    else:
        status = ComparisonStatus.MATCH

    intended_digest = _digest(normalize_intended_state(intended))
    content = _comparison_content(
        target=intended.target,
        attestation_id=attestation.attestation_id,
        intended_digest=intended_digest,
        status=status,
        drifted=drifted,
        missing=missing,
    )
    now_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return ProductionRuntimeComparison(
        SCHEMA_VERSION,
        _digest(content),
        intended.target,
        attestation.attestation_id,
        intended_digest,
        status,
        tuple(drifted),
        tuple(missing),
        tuple(sorted(set(reasons))),
        now_utc,
    )


def normalize_comparison(value: ProductionRuntimeComparison) -> dict[str, object]:
    return {
        "attestation_id": value.attestation_id,
        "comparison_id": value.comparison_id,
        "drifted_observations": list(value.drifted_observations),
        "intended_digest": value.intended_digest,
        "missing_observations": list(value.missing_observations),
        "schema_version": value.schema_version,
        "status": value.status.value,
        "target": value.target,
    }


def validate_comparison(
    value: ProductionRuntimeComparison | Mapping[str, object],
) -> ProductionRuntimeComparison:
    if isinstance(value, ProductionRuntimeComparison):
        payload = normalize_comparison(value)
    else:
        payload = dict(_exact_fields(value, _COMPARISON_FIELDS))

    schema_ver = payload.get("schema_version")
    if schema_ver != SCHEMA_VERSION or isinstance(schema_ver, bool):
        _fail("SCHEMA_VERSION_INVALID")

    target = _identifier(payload["target"])
    att_id = _digest_or_sha(payload["attestation_id"])
    intended_digest = _digest_or_sha(payload["intended_digest"])
    try:
        status = ComparisonStatus(payload["status"])
    except ValueError as exc:
        raise ProductionRuntimeAttestationError("COMPARISON_STATUS_INVALID") from exc

    drifted = tuple(str(x) for x in _items(payload["drifted_observations"]))
    missing = tuple(str(x) for x in _items(payload["missing_observations"]))

    content = _comparison_content(
        target=target,
        attestation_id=att_id,
        intended_digest=intended_digest,
        status=status,
        drifted=drifted,
        missing=missing,
    )
    expected_id = _digest(content)
    if payload.get("comparison_id") != expected_id:
        _fail("TAMPERED_COMPARISON_ID")

    return ProductionRuntimeComparison(
        SCHEMA_VERSION,
        expected_id,
        target,
        att_id,
        intended_digest,
        status,
        drifted,
        missing,
    )


def serialize_comparison(value: ProductionRuntimeComparison) -> bytes:
    return _canonical_bytes(normalize_comparison(validate_comparison(value)))


def deserialize_comparison(value: bytes | str) -> ProductionRuntimeComparison:
    return validate_comparison(_mapping(parse_json_document(value)))
