"""Deterministic, offline Production Readiness Evidence Bridge.

This module binds a verified ProductionRuntimeAttestation + Comparison pair
to the Release Gate's PRODUCTION_READINESS_GATE, producing an explicit,
content-bound ProductionReadinessEvidenceReceipt.

IMPORTANT BOUNDARY
==================
This module is OFFLINE and DETERMINISTIC.

It does NOT:
  - access WSL, Docker, SQLite, Qdrant, Telegram, or any network;
  - read secrets or credentials;
  - invoke live runtime collectors;
  - call datetime.now() in core decision logic;
  - grant production execution authority;
  - expand authority beyond evidence quality verification.

EVIDENCE_EXPANDS_AUTHORITY = false

A PRODUCTION_READINESS_PASS from this module proves that verified evidence
satisfies the readiness policy. It does NOT authorize deployment or any
production mutation.

PRODUCTION_READINESS_PASS != PRODUCTION_EXECUTION_AUTHORIZED

Evidence pipeline:

    ProductionRuntimeAttestation
            ↓ (canonical validator)
    ProductionRuntimeComparison
            ↓ (binding + freshness + health checks)
    ProductionReadinessEvidenceReceipt
            ↓ (deterministic adapter)
    GateEvidence(PRODUCTION_READINESS_GATE)
            ↓
    ReleaseGateReceipt
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, NoReturn, Sequence

from ai_engineering.contracts import Status
from ai_engineering.production_runtime_attestation import (
    ComparisonStatus,
    ProductionRuntimeAttestation,
    ProductionRuntimeAttestationError,
    ProductionRuntimeComparison,
    validate_attestation,
    validate_comparison,
)
from ai_engineering.release_gate import GateEvidence, GateName

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

PRODUCTION_READINESS_EVIDENCE_SCHEMA_VERSION = 1

_UTC = timezone.utc
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+ -]{0,255}$")
_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_EVIDENCE_REF_RE = re.compile(
    r"^(artifact:production-runtime-(?:attestation|comparison):[0-9a-f]{64}"
    r"|source:[0-9a-f]{40})$"
)

# Forbidden patterns in evidence refs - raw production data must not appear
_FORBIDDEN_REF_PATTERNS = (
    "password",
    "token",
    "secret",
    "credential",
    "api_key",
    "bearer ",
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PostCollectionHealthStatus(str, Enum):
    """Closed enum for post-collection health check outcome."""

    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ProductionReadinessStatus(str, Enum):
    """Production readiness final status."""

    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class ProductionReadinessEvidenceError(ValueError):
    """Fail-closed contract error with a stable, sanitized code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise ProductionReadinessEvidenceError(code)


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------


def _require_sha(value: object, code: str = "SHA_INVALID") -> str:
    if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
        _fail(code)
    return value


def _require_digest(value: object, code: str = "DIGEST_INVALID") -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        _fail(code)
    return value


def _require_utc_timestamp(value: object, code: str = "TIMESTAMP_INVALID") -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != _UTC.utcoffset(value):
            _fail(code)
        return value.astimezone(_UTC).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if not isinstance(value, str) or not _UTC_TS_RE.fullmatch(value):
        _fail(code)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC)
    except ValueError:
        _fail(code)
    return value


def _parse_utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_UTC)


def _require_identifier(value: object, code: str = "IDENTIFIER_INVALID") -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        _fail(code)
    return value


def _require_reason(value: object, code: str = "REASON_CODE_INVALID") -> str:
    if not isinstance(value, str) or not _REASON_RE.fullmatch(value):
        _fail(code)
    return value


def _require_positive_int(value: object, code: str = "MAX_AGE_INVALID") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _fail(code)
    return value


def _require_evidence_ref(value: object) -> str:
    """Validate a sanitized evidence reference. Reject raw production data."""
    if not isinstance(value, str):
        _fail("EVIDENCE_REF_INVALID")
    low = value.casefold()
    for pat in _FORBIDDEN_REF_PATTERNS:
        if pat in low:
            _fail("EVIDENCE_REF_CONTAINS_SECRET_MATERIAL")
    if not _EVIDENCE_REF_RE.fullmatch(value):
        _fail("EVIDENCE_REF_INVALID")
    return value


# ---------------------------------------------------------------------------
# Receipt dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductionReadinessEvidenceReceipt:
    """Content-bound, tamper-evident production readiness evidence receipt.

    receipt_id is a SHA-256 over canonical JSON of all other fields.
    Changing any semantic field changes receipt_id.
    Tampered receipt_ids are rejected by verify_receipt().
    """

    schema_version: int
    receipt_id: str

    repository: str
    canonical_remote: str

    candidate_sha: str
    observed_head_sha: str
    runtime_evidence_source_sha: str

    target: str

    attestation_id: str
    comparison_id: str
    intended_digest: str

    collected_at_utc: str
    evaluated_at_utc: str
    max_age_seconds: int

    comparison_status: str
    post_collection_health_status: str

    evidence_refs: tuple[str, ...]

    final_status: str
    reason_codes: tuple[str, ...]


# ---------------------------------------------------------------------------
# Canonical serialization
# ---------------------------------------------------------------------------

_RECEIPT_CONTENT_FIELDS = (
    "schema_version",
    "repository",
    "canonical_remote",
    "candidate_sha",
    "observed_head_sha",
    "runtime_evidence_source_sha",
    "target",
    "attestation_id",
    "comparison_id",
    "intended_digest",
    "collected_at_utc",
    "evaluated_at_utc",
    "max_age_seconds",
    "comparison_status",
    "post_collection_health_status",
    "evidence_refs",
    "final_status",
    "reason_codes",
)

_RECEIPT_ALL_FIELDS = frozenset(_RECEIPT_CONTENT_FIELDS) | {"receipt_id"}


def _normalize_receipt_content(r: ProductionReadinessEvidenceReceipt) -> dict[str, Any]:
    """Return a canonical JSON-serializable dict of all fields except receipt_id."""
    return {
        "attestation_id": r.attestation_id,
        "candidate_sha": r.candidate_sha,
        "canonical_remote": r.canonical_remote,
        "collected_at_utc": r.collected_at_utc,
        "comparison_id": r.comparison_id,
        "comparison_status": r.comparison_status,
        "evaluated_at_utc": r.evaluated_at_utc,
        "evidence_refs": list(r.evidence_refs),
        "final_status": r.final_status,
        "intended_digest": r.intended_digest,
        "max_age_seconds": r.max_age_seconds,
        "observed_head_sha": r.observed_head_sha,
        "post_collection_health_status": r.post_collection_health_status,
        "reason_codes": list(r.reason_codes),
        "repository": r.repository,
        "runtime_evidence_source_sha": r.runtime_evidence_source_sha,
        "schema_version": r.schema_version,
        "target": r.target,
    }


def _compute_receipt_id(content: dict[str, Any]) -> str:
    canonical = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def normalize_receipt(r: ProductionReadinessEvidenceReceipt) -> dict[str, Any]:
    """Return a canonical serializable dict including receipt_id."""
    content = _normalize_receipt_content(r)
    return {**content, "receipt_id": r.receipt_id}


def serialize_receipt(r: ProductionReadinessEvidenceReceipt) -> bytes:
    """Return canonical UTF-8 JSON bytes of the receipt."""
    _verify_receipt_id(r)
    payload = normalize_receipt(r)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# Receipt ID verification
# ---------------------------------------------------------------------------


def _verify_receipt_id(r: ProductionReadinessEvidenceReceipt) -> None:
    """Fail closed if receipt_id does not match canonical content hash."""
    content = _normalize_receipt_content(r)
    expected = _compute_receipt_id(content)
    if r.receipt_id != expected:
        _fail("TAMPERED_RECEIPT_ID")


def verify_receipt(r: ProductionReadinessEvidenceReceipt) -> ProductionReadinessEvidenceReceipt:
    """Verify structural and ID integrity of a receipt. Returns same receipt."""
    _verify_receipt_id(r)
    return r


def deserialize_receipt(value: bytes | str) -> ProductionReadinessEvidenceReceipt:
    """Deserialize and verify a receipt from JSON bytes or string."""
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProductionReadinessEvidenceError("JSON_INVALID") from exc
    elif isinstance(value, str):
        text = value
    else:
        _fail("JSON_INVALID")

    try:
        payload: Any = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProductionReadinessEvidenceError("JSON_INVALID") from exc

    if not isinstance(payload, dict):
        _fail("JSON_INVALID")

    keys = frozenset(payload)
    if keys != _RECEIPT_ALL_FIELDS:
        missing = _RECEIPT_ALL_FIELDS - keys
        extra = keys - _RECEIPT_ALL_FIELDS
        if missing:
            _fail("REQUIRED_FIELD_MISSING")
        if extra:
            _fail("UNEXPECTED_FIELD")

    schema_version = payload["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != PRODUCTION_READINESS_EVIDENCE_SCHEMA_VERSION
    ):
        _fail("SCHEMA_VERSION_INVALID")

    receipt_id = _require_digest(payload["receipt_id"])
    evidence_refs_raw = payload["evidence_refs"]
    if not isinstance(evidence_refs_raw, list):
        _fail("EVIDENCE_REFS_INVALID")
    evidence_refs = tuple(_require_evidence_ref(ref) for ref in evidence_refs_raw)

    reason_codes_raw = payload["reason_codes"]
    if not isinstance(reason_codes_raw, list):
        _fail("REASON_CODES_INVALID")
    reason_codes = tuple(_require_reason(rc) for rc in reason_codes_raw)

    r = ProductionReadinessEvidenceReceipt(
        schema_version=PRODUCTION_READINESS_EVIDENCE_SCHEMA_VERSION,
        receipt_id=receipt_id,
        repository=_require_identifier(payload["repository"]),
        canonical_remote=_require_identifier(payload["canonical_remote"]),
        candidate_sha=_require_sha(payload["candidate_sha"]),
        observed_head_sha=_require_sha(payload["observed_head_sha"]),
        runtime_evidence_source_sha=_require_sha(payload["runtime_evidence_source_sha"]),
        target=_require_identifier(payload["target"]),
        attestation_id=_require_digest(payload["attestation_id"]),
        comparison_id=_require_digest(payload["comparison_id"]),
        intended_digest=_require_digest(payload["intended_digest"]),
        collected_at_utc=_require_utc_timestamp(payload["collected_at_utc"]),
        evaluated_at_utc=_require_utc_timestamp(payload["evaluated_at_utc"]),
        max_age_seconds=_require_positive_int(payload["max_age_seconds"]),
        comparison_status=_require_reason(payload["comparison_status"]),
        post_collection_health_status=_require_reason(
            payload["post_collection_health_status"]
        ),
        evidence_refs=evidence_refs,
        final_status=_require_reason(payload["final_status"]),
        reason_codes=reason_codes,
    )

    # Verify the receipt_id matches the deserialized content
    computed = _compute_receipt_id(_normalize_receipt_content(r))
    if computed != receipt_id:
        _fail("TAMPERED_RECEIPT_ID")

    return r


# ---------------------------------------------------------------------------
# Core verifier
# ---------------------------------------------------------------------------


def verify_production_readiness(
    *,
    attestation: ProductionRuntimeAttestation,
    comparison: ProductionRuntimeComparison,
    candidate_sha: str,
    observed_head_sha: str,
    runtime_evidence_source_sha: str,
    expected_target: str,
    repository: str,
    canonical_remote: str,
    evaluated_at_utc: str | datetime,
    max_age_seconds: int,
    post_collection_health_status: PostCollectionHealthStatus | str,
    expected_runtime_evidence_source_sha: str | None = None,
) -> ProductionReadinessEvidenceReceipt:
    """Deterministic, offline production readiness verifier.

    Does NOT call datetime.now(). Requires explicit evaluated_at_utc.
    Does NOT access production, WSL, Docker, SQLite, Qdrant, Telegram, or secrets.

    Returns a content-bound ProductionReadinessEvidenceReceipt.
    EVIDENCE_EXPANDS_AUTHORITY = false
    """
    # --- Validate caller-supplied identity fields ---
    candidate = _require_sha(candidate_sha, "CANDIDATE_SHA_INVALID")
    observed = _require_sha(observed_head_sha, "OBSERVED_HEAD_SHA_INVALID")
    runtime_source = _require_sha(
        runtime_evidence_source_sha, "RUNTIME_EVIDENCE_SOURCE_SHA_INVALID"
    )
    target = _require_identifier(expected_target, "TARGET_INVALID")
    repo = _require_identifier(repository, "REPOSITORY_INVALID")
    remote = _require_identifier(canonical_remote, "CANONICAL_REMOTE_INVALID")
    evaluated_ts = _require_utc_timestamp(evaluated_at_utc, "EVALUATED_AT_INVALID")
    max_age = _require_positive_int(max_age_seconds, "MAX_AGE_INVALID")

    # --- Validate post-collection health status ---
    if isinstance(post_collection_health_status, PostCollectionHealthStatus):
        health = post_collection_health_status
    elif isinstance(post_collection_health_status, str):
        try:
            health = PostCollectionHealthStatus(post_collection_health_status)
        except ValueError:
            _fail("POST_HEALTH_STATUS_INVALID")
    else:
        _fail("POST_HEALTH_STATUS_INVALID")

    # --- Canonical validation of attestation and comparison ---
    try:
        att = validate_attestation(attestation)
        cmp = validate_comparison(comparison)
    except ProductionRuntimeAttestationError as exc:
        raise ProductionReadinessEvidenceError(exc.code) from exc

    # --- Binding: attestation_id must match ---
    if cmp.attestation_id != att.attestation_id:
        _fail("ATTESTATION_COMPARISON_ID_MISMATCH")

    # --- Binding: target must match across all three ---
    if att.target != target:
        _fail("TARGET_MISMATCH")
    if cmp.target != target:
        _fail("TARGET_MISMATCH")

    # --- Runtime evidence source identity ---
    if (
        expected_runtime_evidence_source_sha is not None
        and runtime_source != _require_sha(
            expected_runtime_evidence_source_sha,
            "EXPECTED_RUNTIME_EVIDENCE_SOURCE_SHA_INVALID",
        )
    ):
        _fail("RUNTIME_EVIDENCE_SOURCE_MISMATCH")

    # --- Freshness policy ---
    collected_ts = att.collected_at_utc
    collected_dt = _parse_utc(collected_ts)
    evaluated_dt = _parse_utc(evaluated_ts)
    age_seconds = (evaluated_dt - collected_dt).total_seconds()
    if age_seconds < 0:
        _fail("EVALUATED_BEFORE_COLLECTED")
    if age_seconds > max_age:
        _fail("PRODUCTION_RUNTIME_EVIDENCE_STALE")

    # --- Build reason codes and determine final status (fail-closed) ---
    reason_codes: list[str] = []
    status: ProductionReadinessStatus

    # Candidate/head SHA match (exact-SHA semantics)
    if candidate != observed:
        reason_codes.append("EXACT_SHA_MISMATCH")
        status = ProductionReadinessStatus.FAIL
        return _build_receipt(
            schema_version=PRODUCTION_READINESS_EVIDENCE_SCHEMA_VERSION,
            repo=repo,
            remote=remote,
            candidate=candidate,
            observed=observed,
            runtime_source=runtime_source,
            target=target,
            att=att,
            cmp=cmp,
            collected_ts=collected_ts,
            evaluated_ts=evaluated_ts,
            max_age=max_age,
            health=health,
            status=status,
            reason_codes=reason_codes,
        )

    # Comparison status semantics (fail-closed)
    if cmp.status == ComparisonStatus.DRIFT:
        reason_codes.append("PRODUCTION_RUNTIME_DRIFT")
        status = ProductionReadinessStatus.FAIL
    elif cmp.status == ComparisonStatus.INSUFFICIENT_EVIDENCE:
        reason_codes.append("PRODUCTION_RUNTIME_EVIDENCE_INSUFFICIENT")
        status = ProductionReadinessStatus.BLOCKED
    elif cmp.status == ComparisonStatus.MATCH:
        # Post-collection health (fail-closed)
        if health == PostCollectionHealthStatus.FAIL:
            reason_codes.append("POST_COLLECTION_HEALTH_FAIL")
            status = ProductionReadinessStatus.FAIL
        elif health == PostCollectionHealthStatus.INSUFFICIENT_EVIDENCE:
            reason_codes.append("POST_COLLECTION_HEALTH_INSUFFICIENT_EVIDENCE")
            status = ProductionReadinessStatus.BLOCKED
        else:
            # PASS: all checks satisfied
            reason_codes.append("ALL_CHECKS_PASS")
            status = ProductionReadinessStatus.PASS
    else:
        # Unknown comparison status - fail closed
        _fail("COMPARISON_STATUS_UNKNOWN")

    return _build_receipt(
        schema_version=PRODUCTION_READINESS_EVIDENCE_SCHEMA_VERSION,
        repo=repo,
        remote=remote,
        candidate=candidate,
        observed=observed,
        runtime_source=runtime_source,
        target=target,
        att=att,
        cmp=cmp,
        collected_ts=collected_ts,
        evaluated_ts=evaluated_ts,
        max_age=max_age,
        health=health,
        status=status,
        reason_codes=reason_codes,
    )


def _build_receipt(
    *,
    schema_version: int,
    repo: str,
    remote: str,
    candidate: str,
    observed: str,
    runtime_source: str,
    target: str,
    att: ProductionRuntimeAttestation,
    cmp: ProductionRuntimeComparison,
    collected_ts: str,
    evaluated_ts: str,
    max_age: int,
    health: PostCollectionHealthStatus,
    status: ProductionReadinessStatus,
    reason_codes: list[str],
) -> ProductionReadinessEvidenceReceipt:
    """Build and ID-bind a ProductionReadinessEvidenceReceipt."""
    evidence_refs = tuple(
        sorted(
            {
                f"artifact:production-runtime-attestation:{att.attestation_id}",
                f"artifact:production-runtime-comparison:{cmp.comparison_id}",
                f"source:{runtime_source}",
            }
        )
    )
    sorted_reasons = tuple(sorted(set(reason_codes)))

    content: dict[str, Any] = {
        "attestation_id": att.attestation_id,
        "candidate_sha": candidate,
        "canonical_remote": remote,
        "collected_at_utc": collected_ts,
        "comparison_id": cmp.comparison_id,
        "comparison_status": cmp.status.value,
        "evaluated_at_utc": evaluated_ts,
        "evidence_refs": list(evidence_refs),
        "final_status": status.value,
        "intended_digest": cmp.intended_digest,
        "max_age_seconds": max_age,
        "observed_head_sha": observed,
        "post_collection_health_status": health.value,
        "reason_codes": list(sorted_reasons),
        "repository": repo,
        "runtime_evidence_source_sha": runtime_source,
        "schema_version": schema_version,
        "target": target,
    }
    receipt_id = _compute_receipt_id(content)

    return ProductionReadinessEvidenceReceipt(
        schema_version=schema_version,
        receipt_id=receipt_id,
        repository=repo,
        canonical_remote=remote,
        candidate_sha=candidate,
        observed_head_sha=observed,
        runtime_evidence_source_sha=runtime_source,
        target=target,
        attestation_id=att.attestation_id,
        comparison_id=cmp.comparison_id,
        intended_digest=cmp.intended_digest,
        collected_at_utc=collected_ts,
        evaluated_at_utc=evaluated_ts,
        max_age_seconds=max_age,
        comparison_status=cmp.status.value,
        post_collection_health_status=health.value,
        evidence_refs=evidence_refs,
        final_status=status.value,
        reason_codes=sorted_reasons,
    )


# ---------------------------------------------------------------------------
# Release Gate adapter
# ---------------------------------------------------------------------------


def to_production_readiness_gate_evidence(
    receipt: ProductionReadinessEvidenceReceipt,
    *,
    required: bool = True,
) -> GateEvidence:
    """Adapter: translate a verified receipt into a GateEvidence.

    The gate_name is always PRODUCTION_READINESS_GATE.
    EVIDENCE_EXPANDS_AUTHORITY = false.
    """
    # Verify receipt integrity before translating
    _verify_receipt_id(receipt)

    # Map readiness status to canonical Status
    status_map = {
        ProductionReadinessStatus.PASS.value: Status.PASS,
        ProductionReadinessStatus.FAIL.value: Status.FAIL,
        ProductionReadinessStatus.BLOCKED.value: Status.BLOCKED,
    }
    gate_status = status_map.get(receipt.final_status)
    if gate_status is None:
        _fail("RECEIPT_FINAL_STATUS_UNKNOWN")

    return GateEvidence(
        gate_name=GateName.PRODUCTION_READINESS,
        required=required,
        status=gate_status,
        evidence_refs=receipt.evidence_refs,
        reason_codes=receipt.reason_codes,
        evidence_digest=receipt.receipt_id,
    )
