"""Build reviewed-only Failure-to-Eval candidates from sanitized evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from ai_engineering.contracts import Status, TraceValidationError
from ai_engineering.redaction import reject_forbidden_raw_fields, verify_sanitized_evidence
from ai_engineering.trace import deserialize_trace, trace_digest


FAILURE_CANDIDATE_SCHEMA_VERSION = 1
MAX_FAILURE_EVIDENCE_BYTES = 1_048_576

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_INPUT_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "failure_record_ref",
        "trace_reference",
        "trace_digest",
        "base_dataset_version",
        "base_corpus_digest",
        "proposed_category",
        "proposed_behaviour",
        "proposed_criticality",
        "proposed_expected_evaluation_status",
        "required_behaviour_dimensions",
        "reason_codes",
        "promotion_intent",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "failure_record_ref",
        "trace_reference",
        "trace_digest",
        "base_dataset_version",
        "base_corpus_digest",
        "proposed_category",
        "proposed_behaviour",
        "proposed_criticality",
        "proposed_expected_evaluation_status",
        "required_behaviour_dimensions",
        "candidate_status",
        "human_review_status",
        "promotion_authorized",
        "reason_codes",
        "candidate_digest",
    }
)


class CandidateStatus(StrEnum):
    """Lifecycle state that this builder is allowed to produce."""

    CANDIDATE = "CANDIDATE"


class HumanReviewStatus(StrEnum):
    """Review state that remains human-owned outside this builder."""

    NOT_PERFORMED = "NOT_PERFORMED"


class FailureCandidateError(ValueError):
    """Fail-closed candidate construction error with a public stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class FailureCandidatePolicyError(FailureCandidateError):
    """A proved request to cross the candidate-only policy boundary."""


@dataclass(frozen=True, slots=True)
class FailureEvidence:
    """Sanitized, repository-bound input evidence for a proposed improvement."""

    failure_record_ref: str
    trace_reference: str
    trace_digest: str
    base_dataset_version: str
    base_corpus_digest: str
    proposed_category: str
    proposed_behaviour: str
    proposed_criticality: bool
    proposed_expected_evaluation_status: Status
    required_behaviour_dimensions: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FailureEvalCandidate:
    """A deterministic proposal that cannot itself join the Golden corpus."""

    schema_version: int
    candidate_id: str
    evidence: FailureEvidence
    candidate_status: CandidateStatus
    human_review_status: HumanReviewStatus
    promotion_authorized: bool
    candidate_digest: str


@dataclass(frozen=True, slots=True)
class FailureCandidateReceipt:
    """Sanitized result for a candidate build attempt."""

    schema_version: int
    status: Status
    candidate: FailureEvalCandidate | None
    reason_codes: tuple[str, ...]


def _fail(code: str) -> NoReturn:
    raise FailureCandidateError(code)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        _fail("FAILURE_CANDIDATE_REQUIRED_FIELD_MISSING")
    return value


def _exact_fields(value: object, expected: frozenset[str]) -> Mapping[str, object]:
    payload = _mapping(value)
    keys = frozenset(payload)
    if expected - keys:
        _fail("FAILURE_CANDIDATE_REQUIRED_FIELD_MISSING")
    if keys - expected:
        _fail("FAILURE_CANDIDATE_UNEXPECTED_FIELD")
    return payload


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("FAILURE_CANDIDATE_VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("FAILURE_CANDIDATE_VALUE_INVALID")
    return value


def _identifiers(value: object) -> tuple[str, ...]:
    results = tuple(_identifier(item) for item in _items(value))
    if len(results) != len(set(results)):
        _fail("FAILURE_CANDIDATE_VALUE_INVALID")
    return results


def _status(value: object) -> Status:
    if not isinstance(value, str):
        _fail("FAILURE_CANDIDATE_STATUS_INVALID")
    try:
        return Status(value)
    except ValueError:
        _fail("FAILURE_CANDIDATE_STATUS_INVALID")


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        _fail("FAILURE_CANDIDATE_VALUE_INVALID")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail("FAILURE_CANDIDATE_DIGEST_INVALID")
    return value


def _safe_reference(
    repository_root: Path,
    reference: str,
    allowed_root: Path,
    *,
    missing_code: str,
) -> bytes:
    """Read one UTF-8 evidence file through a non-symlink confined path."""

    if not reference or "\\" in reference:
        _fail("FAILURE_CANDIDATE_PATH_OUTSIDE_ROOT")
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        _fail("FAILURE_CANDIDATE_PATH_OUTSIDE_ROOT")
    try:
        root = repository_root.resolve(strict=True)
        expected_root = (root / allowed_root).resolve(strict=True)
        if expected_root.is_symlink() or not expected_root.is_dir():
            _fail("FAILURE_CANDIDATE_EVIDENCE_UNSAFE")
        candidate = root / relative
        if candidate.is_symlink():
            _fail("FAILURE_CANDIDATE_EVIDENCE_UNSAFE")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(expected_root) or not resolved.is_file():
            _fail("FAILURE_CANDIDATE_PATH_OUTSIDE_ROOT")
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                _fail("FAILURE_CANDIDATE_EVIDENCE_UNSAFE")
        data = resolved.read_bytes()
    except FailureCandidateError:
        raise
    except FileNotFoundError:
        _fail(missing_code)
    except (OSError, RuntimeError):
        _fail("FAILURE_CANDIDATE_EVIDENCE_UNSAFE")
    if not data or len(data) > MAX_FAILURE_EVIDENCE_BYTES or b"\x00" in data:
        _fail("FAILURE_CANDIDATE_EVIDENCE_UNSAFE")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("FAILURE_CANDIDATE_EVIDENCE_UNSAFE")
    try:
        verify_sanitized_evidence(text)
    except TraceValidationError as exc:
        raise FailureCandidateError("FAILURE_CANDIDATE_EVIDENCE_NOT_SANITIZED") from exc
    return data


def _evidence_from_input(repository_root: Path, value: Mapping[str, object]) -> FailureEvidence:
    try:
        reject_forbidden_raw_fields(value)
        verify_sanitized_evidence(value)
    except TraceValidationError as exc:
        raise FailureCandidatePolicyError("FAILURE_CANDIDATE_RAW_EVIDENCE_FORBIDDEN") from exc
    payload = _exact_fields(value, _INPUT_FIELDS)
    if payload["schema_version"] != FAILURE_CANDIDATE_SCHEMA_VERSION:
        _fail("FAILURE_CANDIDATE_SCHEMA_VERSION_UNSUPPORTED")
    if payload["promotion_intent"] != CandidateStatus.CANDIDATE.value:
        raise FailureCandidatePolicyError("DIRECT_GOLDEN_PROMOTION_FORBIDDEN")
    failure_ref = _identifier(payload["failure_record_ref"])
    trace_ref = _identifier(payload["trace_reference"])
    _safe_reference(
        repository_root,
        failure_ref,
        Path("knowledge/failures"),
        missing_code="FAILURE_CANDIDATE_FAILURE_RECORD_MISSING",
    )
    trace_data = _safe_reference(
        repository_root,
        trace_ref,
        Path("evals/agent_behaviour/fixtures/traces"),
        missing_code="FAILURE_CANDIDATE_TRACE_MISSING",
    )
    declared_trace_digest = _digest(payload["trace_digest"])
    try:
        observed_trace_digest = trace_digest(deserialize_trace(trace_data))
    except TraceValidationError as exc:
        raise FailureCandidateError("FAILURE_CANDIDATE_TRACE_INVALID") from exc
    if observed_trace_digest != declared_trace_digest:
        _fail("FAILURE_CANDIDATE_TRACE_DIGEST_MISMATCH")
    if not isinstance(payload["proposed_criticality"], bool):
        _fail("FAILURE_CANDIDATE_VALUE_INVALID")
    return FailureEvidence(
        failure_record_ref=failure_ref,
        trace_reference=trace_ref,
        trace_digest=declared_trace_digest,
        base_dataset_version=_identifier(payload["base_dataset_version"]),
        base_corpus_digest=_digest(payload["base_corpus_digest"]),
        proposed_category=_identifier(payload["proposed_category"]),
        proposed_behaviour=_identifier(payload["proposed_behaviour"]),
        proposed_criticality=_boolean(payload["proposed_criticality"]),
        proposed_expected_evaluation_status=_status(
            payload["proposed_expected_evaluation_status"]
        ),
        required_behaviour_dimensions=_identifiers(payload["required_behaviour_dimensions"]),
        reason_codes=_identifiers(payload["reason_codes"]),
    )


def _candidate_projection(
    candidate_id: str,
    evidence: FailureEvidence,
) -> dict[str, object]:
    return {
        "schema_version": FAILURE_CANDIDATE_SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "failure_record_ref": evidence.failure_record_ref,
        "trace_reference": evidence.trace_reference,
        "trace_digest": evidence.trace_digest,
        "base_dataset_version": evidence.base_dataset_version,
        "base_corpus_digest": evidence.base_corpus_digest,
        "proposed_category": evidence.proposed_category,
        "proposed_behaviour": evidence.proposed_behaviour,
        "proposed_criticality": evidence.proposed_criticality,
        "proposed_expected_evaluation_status": evidence.proposed_expected_evaluation_status.value,
        "required_behaviour_dimensions": list(evidence.required_behaviour_dimensions),
        "candidate_status": CandidateStatus.CANDIDATE.value,
        "human_review_status": HumanReviewStatus.NOT_PERFORMED.value,
        "promotion_authorized": False,
        "reason_codes": list(evidence.reason_codes),
    }


def candidate_digest(value: FailureEvalCandidate | Mapping[str, object]) -> str:
    """Return the canonical identity of semantic candidate content."""

    if isinstance(value, FailureEvalCandidate):
        projection = _candidate_projection(value.candidate_id, value.evidence)
    else:
        payload = _exact_fields(value, _CANDIDATE_FIELDS)
        projection = {key: payload[key] for key in payload if key != "candidate_digest"}
    encoded = json.dumps(
        projection,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_failure_eval_candidate(
    repository_root: Path,
    value: Mapping[str, object],
) -> FailureEvalCandidate:
    """Build a deterministic candidate without mutating a dataset or policy."""

    evidence = _evidence_from_input(repository_root, value)
    candidate_id = _identifier(value["candidate_id"])
    provisional = FailureEvalCandidate(
        schema_version=FAILURE_CANDIDATE_SCHEMA_VERSION,
        candidate_id=candidate_id,
        evidence=evidence,
        candidate_status=CandidateStatus.CANDIDATE,
        human_review_status=HumanReviewStatus.NOT_PERFORMED,
        promotion_authorized=False,
        candidate_digest="",
    )
    return FailureEvalCandidate(
        schema_version=provisional.schema_version,
        candidate_id=provisional.candidate_id,
        evidence=provisional.evidence,
        candidate_status=provisional.candidate_status,
        human_review_status=provisional.human_review_status,
        promotion_authorized=provisional.promotion_authorized,
        candidate_digest=candidate_digest(provisional),
    )


def normalize_failure_eval_candidate(
    value: FailureEvalCandidate | Mapping[str, object],
) -> dict[str, object]:
    """Return the closed canonical JSON representation of a candidate."""

    if not isinstance(value, FailureEvalCandidate):
        _fail("FAILURE_CANDIDATE_SERIALIZATION_REQUIRES_CANDIDATE")
    if value.schema_version != FAILURE_CANDIDATE_SCHEMA_VERSION:
        _fail("FAILURE_CANDIDATE_SCHEMA_VERSION_UNSUPPORTED")
    if value.candidate_status is not CandidateStatus.CANDIDATE:
        _fail("DIRECT_GOLDEN_PROMOTION_FORBIDDEN")
    if value.human_review_status is not HumanReviewStatus.NOT_PERFORMED:
        _fail("FAILURE_CANDIDATE_HUMAN_REVIEW_FORBIDDEN")
    if value.promotion_authorized:
        _fail("DIRECT_GOLDEN_PROMOTION_FORBIDDEN")
    normalized = _candidate_projection(value.candidate_id, value.evidence)
    normalized["candidate_digest"] = candidate_digest(value)
    return normalized


def serialize_failure_eval_candidate(value: FailureEvalCandidate) -> str:
    return json.dumps(
        normalize_failure_eval_candidate(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def make_failure_candidate_receipt(candidate: FailureEvalCandidate) -> FailureCandidateReceipt:
    return FailureCandidateReceipt(
        schema_version=FAILURE_CANDIDATE_SCHEMA_VERSION,
        status=Status.PASS,
        candidate=candidate,
        reason_codes=("FAILURE_EVAL_CANDIDATE_CREATED",),
    )


def normalize_failure_candidate_receipt(value: FailureCandidateReceipt) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": value.schema_version,
        "status": value.status.value,
        "reason_codes": list(value.reason_codes),
    }
    if value.candidate is not None:
        result["candidate"] = normalize_failure_eval_candidate(value.candidate)
    return result


def write_failure_eval_candidate(
    repository_root: Path,
    output_path: Path,
    candidate: FailureEvalCandidate,
) -> None:
    """Write one new canonical candidate strictly below candidates/ only."""

    try:
        root = repository_root.resolve(strict=True)
        candidates_root = (root / "evals/agent_behaviour/candidates").resolve(strict=True)
        if candidates_root.is_symlink() or not candidates_root.is_dir():
            _fail("FAILURE_CANDIDATE_OUTPUT_UNSAFE")
        target = output_path if output_path.is_absolute() else root / output_path
        parent = target.parent
        if target.exists() or target.is_symlink() or not target.name.endswith(".json"):
            _fail("FAILURE_CANDIDATE_OUTPUT_UNSAFE")
        resolved_parent = parent.resolve(strict=True)
        resolved_target = (resolved_parent / target.name).resolve(strict=False)
        if not resolved_target.is_relative_to(candidates_root):
            _fail("FAILURE_CANDIDATE_OUTPUT_UNSAFE")
        current = candidates_root
        for part in resolved_parent.relative_to(candidates_root).parts:
            current = current / part
            if current.is_symlink():
                _fail("FAILURE_CANDIDATE_OUTPUT_UNSAFE")
        payload = serialize_failure_eval_candidate(candidate) + "\n"
        with resolved_target.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        resolved_target.chmod(0o600)
    except FailureCandidateError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise FailureCandidateError("FAILURE_CANDIDATE_OUTPUT_UNSAFE") from exc
