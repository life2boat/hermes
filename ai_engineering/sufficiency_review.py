"""Canonical EvidenceSufficiencyReview — semantic adjudication layer.

PURPOSE
-------
The EvidenceSufficiencyReview is the semantic adjudication layer of the
Intent Control Plane.  It answers: does the referenced evidence actually
support the acceptance criterion?

This is SEPARATE from Analyzer v1, which is structural-only, and
SEPARATE from the artifact byte verifier, which is byte-binding only.

KEY POLICY (AI6)
----------------
No critical production behaviour may rely solely on LLM-as-judge.
REVIEWER_CLASS=INDEPENDENT_AGENT is never equivalent to HUMAN.
A non-HUMAN sufficiency review may satisfy a criterion in Convergence v2,
but production systems that require HUMAN review must not accept
INDEPENDENT_AGENT as a substitute.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


SUFFICIENCY_REVIEW_SCHEMA_VERSION = 1

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CRITERION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class SufficiencyReviewError(ValueError):
    """Fail-closed error with a stable error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise SufficiencyReviewError(code)


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("VALUE_INVALID")
    return value


def _criterion_id(value: object) -> str:
    if not isinstance(value, str) or _CRITERION_ID_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("VALUE_INVALID")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
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


def _enum(value: object, enum_type: type, code: str = "VALUE_INVALID"):
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        _fail(code)
    try:
        return enum_type(value)
    except ValueError:
        _fail(code)


def _compute_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReviewerClass(StrEnum):
    """Class of the sufficiency reviewer.

    IMPORTANT: INDEPENDENT_AGENT is NEVER equivalent to HUMAN.
    Systems requiring HUMAN evidence must not accept INDEPENDENT_AGENT.
    """

    DETERMINISTIC = "DETERMINISTIC"
    HUMAN = "HUMAN"
    INDEPENDENT_AGENT = "INDEPENDENT_AGENT"


class SufficiencyStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriterionSufficiencyResult:
    criterion_id: str
    status: SufficiencyStatus
    reason_codes: tuple[str, ...]
    evidence_observation_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceSufficiencyReview:
    schema_version: int
    review_id: str  # content-bound digest
    task_id: str
    intent_digest: str
    analysis_id: str
    subject_sha: str
    evidence_bundle_id: str
    reviewer_id: str
    reviewer_class: ReviewerClass
    overall_status: SufficiencyStatus
    criterion_reviews: tuple[CriterionSufficiencyResult, ...]


# ---------------------------------------------------------------------------
# Schema field sets
# ---------------------------------------------------------------------------

_CRITERION_REVIEW_FIELDS = frozenset({
    "criterion_id",
    "status",
    "reason_codes",
    "evidence_observation_ids",
})

_REVIEW_FIELDS = frozenset({
    "schema_version",
    "review_id",
    "task_id",
    "intent_digest",
    "analysis_id",
    "subject_sha",
    "evidence_bundle_id",
    "reviewer_id",
    "reviewer_class",
    "overall_status",
    "criterion_reviews",
})


# ---------------------------------------------------------------------------
# review_id computation
# ---------------------------------------------------------------------------


def compute_review_id(
    task_id: str,
    intent_digest: str,
    analysis_id: str,
    subject_sha: str,
    evidence_bundle_id: str,
    reviewer_id: str,
    reviewer_class: ReviewerClass | str,
    overall_status: SufficiencyStatus | str,
    criterion_reviews: Sequence[CriterionSufficiencyResult | Mapping[str, Any]],
    schema_version: int = SUFFICIENCY_REVIEW_SCHEMA_VERSION,
) -> str:
    """Deterministic review_id from review content (excludes review_id itself)."""
    class_val = (
        reviewer_class.value
        if isinstance(reviewer_class, ReviewerClass)
        else str(reviewer_class)
    )
    status_val = (
        overall_status.value
        if isinstance(overall_status, SufficiencyStatus)
        else str(overall_status)
    )
    crit_list = []
    for cr in criterion_reviews:
        if isinstance(cr, CriterionSufficiencyResult):
            crit_list.append({
                "criterion_id": cr.criterion_id,
                "status": cr.status.value,
                "reason_codes": list(cr.reason_codes),
                "evidence_observation_ids": list(cr.evidence_observation_ids),
            })
        else:
            m = dict(cr)
            if hasattr(m.get("status"), "value"):
                m["status"] = m["status"].value
            crit_list.append(m)
    return _compute_digest({
        "analysis_id": str(analysis_id),
        "criterion_reviews": crit_list,
        "evidence_bundle_id": str(evidence_bundle_id),
        "intent_digest": str(intent_digest),
        "overall_status": status_val,
        "reviewer_class": class_val,
        "reviewer_id": str(reviewer_id),
        "schema_version": schema_version,
        "subject_sha": str(subject_sha),
        "task_id": str(task_id),
    })


# ---------------------------------------------------------------------------
# Public factories and validators
# ---------------------------------------------------------------------------


def create_criterion_sufficiency_result(
    criterion_id: str,
    status: SufficiencyStatus | str,
    reason_codes: Sequence[str] = (),
    evidence_observation_ids: Sequence[str] = (),
) -> CriterionSufficiencyResult:
    c_id = _criterion_id(criterion_id)
    st = _enum(status, SufficiencyStatus)
    rc = tuple(_string(r) for r in reason_codes)
    obs_ids = tuple(_string(o) for o in evidence_observation_ids)
    return CriterionSufficiencyResult(
        criterion_id=c_id,
        status=st,
        reason_codes=rc,
        evidence_observation_ids=obs_ids,
    )


def create_sufficiency_review(
    task_id: str,
    intent_digest: str,
    analysis_id: str,
    subject_sha: str,
    evidence_bundle_id: str,
    reviewer_id: str,
    reviewer_class: ReviewerClass | str,
    overall_status: SufficiencyStatus | str,
    criterion_reviews: Sequence[CriterionSufficiencyResult],
) -> EvidenceSufficiencyReview:
    """Public factory for EvidenceSufficiencyReview with deterministic review_id."""
    t_id = _identifier(task_id)
    i_dgst = _digest(intent_digest)
    a_id = _digest(analysis_id)
    s_sha = _sha(subject_sha)
    b_id = _digest(evidence_bundle_id)
    r_id = _string(reviewer_id)
    r_class = _enum(reviewer_class, ReviewerClass)
    o_status = _enum(overall_status, SufficiencyStatus)
    crit = tuple(criterion_reviews)

    review_id = compute_review_id(
        task_id=t_id,
        intent_digest=i_dgst,
        analysis_id=a_id,
        subject_sha=s_sha,
        evidence_bundle_id=b_id,
        reviewer_id=r_id,
        reviewer_class=r_class,
        overall_status=o_status,
        criterion_reviews=crit,
    )
    review = EvidenceSufficiencyReview(
        schema_version=SUFFICIENCY_REVIEW_SCHEMA_VERSION,
        review_id=review_id,
        task_id=t_id,
        intent_digest=i_dgst,
        analysis_id=a_id,
        subject_sha=s_sha,
        evidence_bundle_id=b_id,
        reviewer_id=r_id,
        reviewer_class=r_class,
        overall_status=o_status,
        criterion_reviews=crit,
    )
    return validate_sufficiency_review(review)


def _validate_from_mapping(payload: Mapping[str, object]) -> EvidenceSufficiencyReview:
    task_id = _identifier(payload["task_id"])
    intent_dgst = _digest(payload["intent_digest"])
    analysis_id = _digest(payload["analysis_id"])
    subject_sha = _sha(payload["subject_sha"])
    bundle_id = _digest(payload["evidence_bundle_id"])
    reviewer_id = _string(payload["reviewer_id"])
    reviewer_class = _enum(payload["reviewer_class"], ReviewerClass)
    overall_status = _enum(payload["overall_status"], SufficiencyStatus)

    criterion_reviews: list[CriterionSufficiencyResult] = []
    for item in _items(payload["criterion_reviews"]):
        cp = _exact_fields(item, _CRITERION_REVIEW_FIELDS)
        crit_id = _criterion_id(cp["criterion_id"])
        st = _enum(cp["status"], SufficiencyStatus)
        rc = tuple(_string(r) for r in _items(cp["reason_codes"]))
        obs_ids = tuple(_string(o) for o in _items(cp["evidence_observation_ids"]))
        criterion_reviews.append(CriterionSufficiencyResult(
            criterion_id=crit_id,
            status=st,
            reason_codes=rc,
            evidence_observation_ids=obs_ids,
        ))

    expected_id = compute_review_id(
        task_id=task_id,
        intent_digest=intent_dgst,
        analysis_id=analysis_id,
        subject_sha=subject_sha,
        evidence_bundle_id=bundle_id,
        reviewer_id=reviewer_id,
        reviewer_class=reviewer_class,
        overall_status=overall_status,
        criterion_reviews=criterion_reviews,
    )
    if expected_id != payload["review_id"]:
        _fail("TAMPERED_REVIEW_ID")

    return EvidenceSufficiencyReview(
        schema_version=SUFFICIENCY_REVIEW_SCHEMA_VERSION,
        review_id=expected_id,
        task_id=task_id,
        intent_digest=intent_dgst,
        analysis_id=analysis_id,
        subject_sha=subject_sha,
        evidence_bundle_id=bundle_id,
        reviewer_id=reviewer_id,
        reviewer_class=reviewer_class,
        overall_status=overall_status,
        criterion_reviews=tuple(criterion_reviews),
    )


def validate_sufficiency_review(
    value: "EvidenceSufficiencyReview | Mapping[str, object]",
) -> EvidenceSufficiencyReview:
    """Canonical validator. Recomputes review_id and rejects any tampering."""
    if isinstance(value, EvidenceSufficiencyReview):
        payload: Mapping[str, object] = {
            "task_id": value.task_id,
            "intent_digest": value.intent_digest,
            "analysis_id": value.analysis_id,
            "subject_sha": value.subject_sha,
            "evidence_bundle_id": value.evidence_bundle_id,
            "reviewer_id": value.reviewer_id,
            "reviewer_class": value.reviewer_class.value,
            "overall_status": value.overall_status.value,
            "criterion_reviews": [
                {
                    "criterion_id": cr.criterion_id,
                    "status": cr.status.value,
                    "reason_codes": list(cr.reason_codes),
                    "evidence_observation_ids": list(cr.evidence_observation_ids),
                }
                for cr in value.criterion_reviews
            ],
            "review_id": value.review_id,
        }
    else:
        if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
            _fail("REQUIRED_FIELD_MISSING")
        raw_version = value.get("schema_version")
        if raw_version != SUFFICIENCY_REVIEW_SCHEMA_VERSION or isinstance(raw_version, bool):
            _fail("UNKNOWN_REVIEW_SCHEMA_VERSION")
        payload = _exact_fields(value, _REVIEW_FIELDS)
    return _validate_from_mapping(payload)


def deserialize_sufficiency_review(
    value: "Mapping[str, object] | str | bytes",
) -> EvidenceSufficiencyReview:
    """Deserialize and validate from JSON string, bytes, or mapping."""
    import json as _json
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SufficiencyReviewError("JSON_INVALID") from exc
    if isinstance(value, str):
        try:
            data = _json.loads(value)
        except Exception as exc:
            raise SufficiencyReviewError("JSON_INVALID") from exc
        if not isinstance(data, Mapping):
            _fail("REQUIRED_FIELD_MISSING")
        return validate_sufficiency_review(data)
    if isinstance(value, Mapping):
        return validate_sufficiency_review(value)
    _fail("REQUIRED_FIELD_MISSING")


def serialize_sufficiency_review(review: EvidenceSufficiencyReview) -> dict[str, Any]:
    return {
        "schema_version": review.schema_version,
        "review_id": review.review_id,
        "task_id": review.task_id,
        "intent_digest": review.intent_digest,
        "analysis_id": review.analysis_id,
        "subject_sha": review.subject_sha,
        "evidence_bundle_id": review.evidence_bundle_id,
        "reviewer_id": review.reviewer_id,
        "reviewer_class": review.reviewer_class.value,
        "overall_status": review.overall_status.value,
        "criterion_reviews": [
            {
                "criterion_id": cr.criterion_id,
                "status": cr.status.value,
                "reason_codes": list(cr.reason_codes),
                "evidence_observation_ids": list(cr.evidence_observation_ids),
            }
            for cr in review.criterion_reviews
        ],
    }
