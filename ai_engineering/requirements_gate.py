"""Clarification and Requirements Quality Gate contracts and logic."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, TypeVar

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


from ai_engineering.task_intent import (
    IntentStatus,
    TaskIntent,
    intent_digest,
    validate_intent,
)

CLARIFICATION_SCHEMA_VERSION = 1
QUALITY_REVIEW_SCHEMA_VERSION = 1
REQUIREMENTS_GATE_SCHEMA_VERSION = 1

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_CRITERION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ReviewStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_REVIEWED = "NOT_REVIEWED"


class CriterionDimension(StrEnum):
    CLEAR = "CLEAR"
    TESTABLE = "TESTABLE"
    BOUNDED = "BOUNDED"


class GlobalDimension(StrEnum):
    DESIRED_OUTCOME_CLEAR = "DESIRED_OUTCOME_CLEAR"
    SCOPE_BOUNDARIES_CLEAR = "SCOPE_BOUNDARIES_CLEAR"
    CONSTRAINTS_REVIEWED = "CONSTRAINTS_REVIEWED"
    UNKNOWN_RESOLUTION_REVIEWED = "UNKNOWN_RESOLUTION_REVIEWED"
    INTERNAL_CONFLICT_REVIEWED = "INTERNAL_CONFLICT_REVIEWED"


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class GateBlockingReason(StrEnum):
    INTENT_NOT_READY = "INTENT_NOT_READY"
    BLOCKING_UNKNOWNS_REMAIN = "BLOCKING_UNKNOWNS_REMAIN"
    CLARIFICATION_INTENT_MISMATCH = "CLARIFICATION_INTENT_MISMATCH"
    QUALITY_REVIEW_INTENT_MISMATCH = "QUALITY_REVIEW_INTENT_MISMATCH"
    QUALITY_REVIEW_INCOMPLETE = "QUALITY_REVIEW_INCOMPLETE"
    CRITERION_REVIEW_MISSING = "CRITERION_REVIEW_MISSING"
    CRITERION_REVIEW_UNKNOWN = "CRITERION_REVIEW_UNKNOWN"
    QUALITY_CHECK_FAILED = "QUALITY_CHECK_FAILED"


_EnumT = TypeVar("_EnumT")


# ---------------------------------------------------------------------------
# Exceptions & Helpers
# ---------------------------------------------------------------------------


class RequirementsGateError(ValueError):
    """Fail-closed validation error exposing only a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise RequirementsGateError(code)


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


def _enum(value: object, enum_type: type[_EnumT], code: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        _fail(code)
    try:
        return enum_type(value)
    except ValueError:
        _fail(code)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str):
        _fail("VALUE_INVALID")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        _fail("VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _fail("VALUE_INVALID")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


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
# ClarificationReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClarificationQuestion:
    question_id: str
    unknown_id: str
    description: str
    blocking: bool


@dataclass(frozen=True, slots=True)
class ClarificationReport:
    schema_version: int
    clarification_id: str
    task_id: str
    intent_digest: str
    intent_revision: int
    intent_status: IntentStatus
    questions: tuple[ClarificationQuestion, ...]
    blocking_question_count: int
    ready_for_quality_review: bool


_QUESTION_FIELDS = frozenset({"question_id", "unknown_id", "description", "blocking"})
_CLARIFICATION_FIELDS = frozenset({
    "schema_version",
    "clarification_id",
    "task_id",
    "intent_digest",
    "intent_revision",
    "intent_status",
    "questions",
    "blocking_question_count",
    "ready_for_quality_review",
})


def generate_clarification_report(intent: TaskIntent) -> ClarificationReport:
    """Generate a deterministic clarification report from a validated TaskIntent."""
    validate_intent(intent)

    questions = []
    blocking_count = 0
    for unknown in intent.unknowns:
        qid = f"{intent.task_id}::{intent.intent_revision}::{unknown.unknown_id}"
        questions.append(
            ClarificationQuestion(
                question_id=qid,
                unknown_id=unknown.unknown_id,
                description=unknown.description,
                blocking=unknown.blocking,
            )
        )
        if unknown.blocking:
            blocking_count += 1

    ready_for_qr = intent.status == IntentStatus.READY and blocking_count == 0

    intent_dgst = intent_digest(intent)

    payload_to_hash = {
        "intent_digest": intent_dgst,
        "questions": [
            {
                "blocking": q.blocking,
                "description": q.description,
                "question_id": q.question_id,
                "unknown_id": q.unknown_id,
            }
            for q in questions
        ],
        "schema_version": CLARIFICATION_SCHEMA_VERSION,
    }
    clarification_id = _compute_digest(payload_to_hash)

    return ClarificationReport(
        schema_version=CLARIFICATION_SCHEMA_VERSION,
        clarification_id=clarification_id,
        task_id=intent.task_id,
        intent_digest=intent_dgst,
        intent_revision=intent.intent_revision,
        intent_status=intent.status,
        questions=tuple(questions),
        blocking_question_count=blocking_count,
        ready_for_quality_review=ready_for_qr,
    )


def _clarification_to_dict(report: ClarificationReport) -> dict[str, object]:
    return {
        "schema_version": report.schema_version,
        "clarification_id": report.clarification_id,
        "task_id": report.task_id,
        "intent_digest": report.intent_digest,
        "intent_revision": report.intent_revision,
        "intent_status": report.intent_status.value,
        "questions": [
            {
                "question_id": q.question_id,
                "unknown_id": q.unknown_id,
                "description": q.description,
                "blocking": q.blocking,
            }
            for q in report.questions
        ],
        "blocking_question_count": report.blocking_question_count,
        "ready_for_quality_review": report.ready_for_quality_review,
    }


def serialize_clarification(report: ClarificationReport) -> str:
    return json.dumps(
        _clarification_to_dict(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_clarification(
    value: ClarificationReport | Mapping[str, object],
) -> ClarificationReport:
    if isinstance(value, ClarificationReport):
        value = _clarification_to_dict(value)

    payload = _exact_fields(value, _CLARIFICATION_FIELDS)
    sv = payload["schema_version"]
    if sv != CLARIFICATION_SCHEMA_VERSION or isinstance(sv, bool):
        _fail("SCHEMA_VERSION_UNSUPPORTED")

    questions = []
    q_ids = set()
    for q_val in _items(payload["questions"]):
        q_dict = _exact_fields(q_val, _QUESTION_FIELDS)
        qid = _string(q_dict["question_id"])
        if qid in q_ids:
            _fail("DUPLICATE_QUESTION_ID")
        q_ids.add(qid)
        questions.append(
            ClarificationQuestion(
                question_id=qid,
                unknown_id=_identifier(q_dict["unknown_id"]),
                description=_string(q_dict["description"]),
                blocking=_boolean(q_dict["blocking"]),
            )
        )

    rev = payload["intent_revision"]
    if not isinstance(rev, int) or rev < 1 or isinstance(rev, bool):
        _fail("VALUE_INVALID")

    bcount = payload["blocking_question_count"]
    if not isinstance(bcount, int) or bcount < 0 or isinstance(bcount, bool):
        _fail("VALUE_INVALID")

    return ClarificationReport(
        schema_version=CLARIFICATION_SCHEMA_VERSION,
        clarification_id=_digest(payload["clarification_id"]),
        task_id=_identifier(payload["task_id"]),
        intent_digest=_digest(payload["intent_digest"]),
        intent_revision=rev,
        intent_status=_enum(payload["intent_status"], IntentStatus, "VALUE_INVALID"),
        questions=tuple(questions),
        blocking_question_count=bcount,
        ready_for_quality_review=_boolean(payload["ready_for_quality_review"]),
    )


def deserialize_clarification(value: str | bytes) -> ClarificationReport:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as e:
            raise RequirementsGateError("JSON_INVALID") from e
    if not isinstance(value, str):
        _fail("JSON_INVALID")
    try:
        data = json.loads(value)
    except Exception as e:
        raise RequirementsGateError("JSON_INVALID") from e
    return validate_clarification(data)


# ---------------------------------------------------------------------------
# RequirementsQualityReview
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriterionReview:
    criterion_id: str
    dimensions: Mapping[CriterionDimension, ReviewStatus]


@dataclass(frozen=True, slots=True)
class RequirementsQualityReview:
    schema_version: int
    review_id: str
    task_id: str
    intent_digest: str
    intent_revision: int
    reviewer_id: str
    criterion_reviews: tuple[CriterionReview, ...]
    global_reviews: Mapping[GlobalDimension, ReviewStatus]


_CRITERION_REVIEW_FIELDS = frozenset({"criterion_id", "dimensions"})
_REVIEW_FIELDS = frozenset({
    "schema_version",
    "review_id",
    "task_id",
    "intent_digest",
    "intent_revision",
    "reviewer_id",
    "criterion_reviews",
    "global_reviews",
})


def _review_to_dict(review: RequirementsQualityReview) -> dict[str, object]:
    return {
        "schema_version": review.schema_version,
        "review_id": review.review_id,
        "task_id": review.task_id,
        "intent_digest": review.intent_digest,
        "intent_revision": review.intent_revision,
        "reviewer_id": review.reviewer_id,
        "criterion_reviews": [
            {
                "criterion_id": c.criterion_id,
                "dimensions": {k.value: v.value for k, v in c.dimensions.items()},
            }
            for c in review.criterion_reviews
        ],
        "global_reviews": {k.value: v.value for k, v in review.global_reviews.items()},
    }


def serialize_review(review: RequirementsQualityReview) -> str:
    return json.dumps(
        _review_to_dict(review),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def validate_review(
    value: RequirementsQualityReview | Mapping[str, object],
) -> RequirementsQualityReview:
    if isinstance(value, RequirementsQualityReview):
        value = _review_to_dict(value)

    payload = _exact_fields(value, _REVIEW_FIELDS)
    sv = payload["schema_version"]
    if sv != QUALITY_REVIEW_SCHEMA_VERSION or isinstance(sv, bool):
        _fail("SCHEMA_VERSION_UNSUPPORTED")

    rev = payload["intent_revision"]
    if not isinstance(rev, int) or rev < 1 or isinstance(rev, bool):
        _fail("VALUE_INVALID")

    r_id = payload["reviewer_id"]
    if not isinstance(r_id, str) or not r_id:
        _fail("VALUE_INVALID")

    c_reviews = []
    c_ids = set()
    for cr in _items(payload["criterion_reviews"]):
        cr_payload = _exact_fields(cr, _CRITERION_REVIEW_FIELDS)
        cid = _string(cr_payload["criterion_id"])
        if cid in c_ids:
            _fail("DUPLICATE_CRITERION_ID")
        c_ids.add(cid)
        if _CRITERION_ID_RE.fullmatch(cid) is None:
            _fail("VALUE_INVALID")

        dims_payload = _mapping(cr_payload["dimensions"])
        dims = {}
        for dk, dv in dims_payload.items():
            dim = _enum(dk, CriterionDimension, "VALUE_INVALID")
            stat = _enum(dv, ReviewStatus, "VALUE_INVALID")
            dims[dim] = stat
        c_reviews.append(CriterionReview(criterion_id=cid, dimensions=dims))

    g_payload = _mapping(payload["global_reviews"])
    g_reviews = {}
    for gk, gv in g_payload.items():
        gdim = _enum(gk, GlobalDimension, "VALUE_INVALID")
        gstat = _enum(gv, ReviewStatus, "VALUE_INVALID")
        g_reviews[gdim] = gstat

    return RequirementsQualityReview(
        schema_version=QUALITY_REVIEW_SCHEMA_VERSION,
        review_id=_digest(payload["review_id"]),
        task_id=_identifier(payload["task_id"]),
        intent_digest=_digest(payload["intent_digest"]),
        intent_revision=rev,
        reviewer_id=r_id,
        criterion_reviews=tuple(c_reviews),
        global_reviews=g_reviews,
    )


def deserialize_review(value: str | bytes) -> RequirementsQualityReview:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as e:
            raise RequirementsGateError("JSON_INVALID") from e
    if not isinstance(value, str):
        _fail("JSON_INVALID")
    try:
        data = json.loads(value)
    except Exception as e:
        raise RequirementsGateError("JSON_INVALID") from e
    return validate_review(data)


# ---------------------------------------------------------------------------
# RequirementsGateReport & Evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RequirementsGateReport:
    schema_version: int
    gate_id: str
    task_id: str
    intent_digest: str
    clarification_id: str
    quality_review_id: str
    status: GateStatus
    blocking_reasons: tuple[GateBlockingReason, ...]


def _gate_to_dict(gate: RequirementsGateReport) -> dict[str, object]:
    return {
        "schema_version": gate.schema_version,
        "gate_id": gate.gate_id,
        "task_id": gate.task_id,
        "intent_digest": gate.intent_digest,
        "clarification_id": gate.clarification_id,
        "quality_review_id": gate.quality_review_id,
        "status": gate.status.value,
        "blocking_reasons": [r.value for r in gate.blocking_reasons],
    }


def serialize_gate(gate: RequirementsGateReport) -> str:
    return json.dumps(
        _gate_to_dict(gate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def evaluate_requirements_gate(
    intent: TaskIntent,
    clarification: ClarificationReport,
    review: RequirementsQualityReview,
) -> RequirementsGateReport:
    """Deterministically evaluate if the TaskIntent is ready for downstream execution."""
    # Ensure inputs are valid PR-1/PR-3 canonical contracts
    validate_intent(intent)
    validate_clarification(clarification)
    validate_review(review)

    current_digest = intent_digest(intent)

    reasons: set[GateBlockingReason] = set()

    # 1. Intent & Clarification state
    if intent.status != IntentStatus.READY:
        reasons.add(GateBlockingReason.INTENT_NOT_READY)

    if clarification.intent_digest != current_digest:
        reasons.add(GateBlockingReason.CLARIFICATION_INTENT_MISMATCH)
    elif clarification.blocking_question_count > 0:
        reasons.add(GateBlockingReason.BLOCKING_UNKNOWNS_REMAIN)

    # 2. Review binding
    if review.intent_digest != current_digest:
        reasons.add(GateBlockingReason.QUALITY_REVIEW_INTENT_MISMATCH)

    # 3. Completeness of Review
    # Global dimensions
    has_global_failure = False
    has_global_incomplete = False
    for gd in GlobalDimension:
        status = review.global_reviews.get(gd)
        if status is None or status == ReviewStatus.NOT_REVIEWED:
            has_global_incomplete = True
        elif status == ReviewStatus.FAIL:
            has_global_failure = True

    if has_global_incomplete:
        reasons.add(GateBlockingReason.QUALITY_REVIEW_INCOMPLETE)
    if has_global_failure:
        reasons.add(GateBlockingReason.QUALITY_CHECK_FAILED)

    # Criterion dimensions
    c_review_map = {cr.criterion_id: cr for cr in review.criterion_reviews}
    intent_crit_ids = {c.criterion_id for c in intent.acceptance_criteria}

    missing_crit = intent_crit_ids - set(c_review_map.keys())
    if missing_crit:
        reasons.add(GateBlockingReason.CRITERION_REVIEW_MISSING)

    unknown_crit = set(c_review_map.keys()) - intent_crit_ids
    if unknown_crit:
        reasons.add(GateBlockingReason.CRITERION_REVIEW_UNKNOWN)

    has_crit_incomplete = False
    has_crit_failure = False

    # Only evaluate completeness on criteria that actually exist in the intent
    for cid in intent_crit_ids:
        cr = c_review_map.get(cid)
        if cr:
            for cd in CriterionDimension:
                status = cr.dimensions.get(cd)
                if status is None or status == ReviewStatus.NOT_REVIEWED:
                    has_crit_incomplete = True
                elif status == ReviewStatus.FAIL:
                    has_crit_failure = True

    if has_crit_incomplete:
        reasons.add(GateBlockingReason.QUALITY_REVIEW_INCOMPLETE)
    if has_crit_failure:
        reasons.add(GateBlockingReason.QUALITY_CHECK_FAILED)

    sorted_reasons = tuple(sorted(reasons, key=lambda x: x.value))
    gate_status = GateStatus.FAIL if sorted_reasons else GateStatus.PASS

    payload_to_hash = {
        "blocking_reasons": [r.value for r in sorted_reasons],
        "clarification_id": clarification.clarification_id,
        "intent_digest": current_digest,
        "quality_review_id": review.review_id,
        "schema_version": REQUIREMENTS_GATE_SCHEMA_VERSION,
        "status": gate_status.value,
        "task_id": intent.task_id,
    }
    gate_id = _compute_digest(payload_to_hash)

    return RequirementsGateReport(
        schema_version=REQUIREMENTS_GATE_SCHEMA_VERSION,
        gate_id=gate_id,
        task_id=intent.task_id,
        intent_digest=current_digest,
        clarification_id=clarification.clarification_id,
        quality_review_id=review.review_id,
        status=gate_status,
        blocking_reasons=sorted_reasons,
    )
