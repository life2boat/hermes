"""Tests for ai_engineering.sufficiency_review."""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ai_engineering.sufficiency_review import (
    CriterionSufficiencyResult,
    EvidenceSufficiencyReview,
    ReviewerClass,
    SufficiencyReviewError,
    SufficiencyStatus,
    compute_review_id,
    create_criterion_sufficiency_result,
    create_sufficiency_review,
    deserialize_sufficiency_review,
    serialize_sufficiency_review,
    validate_sufficiency_review,
)

SAMPLE_SHA = "a" * 40
SAMPLE_DIGEST = "b" * 64
SAMPLE_TASK_ID = "TEST-TASK-001"


def _make_review(
    task_id: str = SAMPLE_TASK_ID,
    intent_digest: str = SAMPLE_DIGEST,
    analysis_id: str = SAMPLE_DIGEST,
    subject_sha: str = SAMPLE_SHA,
    evidence_bundle_id: str = SAMPLE_DIGEST,
    reviewer_id: str = "reviewer-1",
    reviewer_class: ReviewerClass = ReviewerClass.DETERMINISTIC,
    overall_status: SufficiencyStatus = SufficiencyStatus.PASS,
    criterion_reviews: list | None = None,
) -> EvidenceSufficiencyReview:
    if criterion_reviews is None:
        criterion_reviews = [
            create_criterion_sufficiency_result(
                criterion_id="AC1",
                status=SufficiencyStatus.PASS,
                reason_codes=["evidence_complete"],
            )
        ]
    return create_sufficiency_review(
        task_id=task_id,
        intent_digest=intent_digest,
        analysis_id=analysis_id,
        subject_sha=subject_sha,
        evidence_bundle_id=evidence_bundle_id,
        reviewer_id=reviewer_id,
        reviewer_class=reviewer_class,
        overall_status=overall_status,
        criterion_reviews=criterion_reviews,
    )


class TestCreateAndValidate:
    def test_creates_valid_review(self) -> None:
        review = _make_review()
        assert review.schema_version == 1
        assert review.task_id == SAMPLE_TASK_ID
        assert review.reviewer_class == ReviewerClass.DETERMINISTIC
        assert review.overall_status == SufficiencyStatus.PASS
        assert len(review.criterion_reviews) == 1

    def test_review_id_is_deterministic(self) -> None:
        r1 = _make_review()
        r2 = _make_review()
        assert r1.review_id == r2.review_id

    def test_review_id_changes_on_field_change(self) -> None:
        r1 = _make_review(reviewer_id="reviewer-A")
        r2 = _make_review(reviewer_id="reviewer-B")
        assert r1.review_id != r2.review_id

    def test_validate_roundtrip(self) -> None:
        review = _make_review()
        validated = validate_sufficiency_review(review)
        assert validated.review_id == review.review_id


class TestTamperingRejection:
    def test_tampered_review_id_rejected(self) -> None:
        review = _make_review()
        serialized = serialize_sufficiency_review(review)
        serialized["review_id"] = "f" * 64  # tamper
        with pytest.raises(SufficiencyReviewError) as exc_info:
            deserialize_sufficiency_review(serialized)
        assert exc_info.value.code == "TAMPERED_REVIEW_ID"

    def test_tampered_overall_status_rejected(self) -> None:
        review = _make_review(overall_status=SufficiencyStatus.FAIL)
        serialized = serialize_sufficiency_review(review)
        serialized["overall_status"] = "PASS"  # tamper status
        with pytest.raises(SufficiencyReviewError) as exc_info:
            deserialize_sufficiency_review(serialized)
        assert exc_info.value.code == "TAMPERED_REVIEW_ID"


class TestBindingMismatch:
    def test_wrong_bundle_id_produces_different_review_id(self) -> None:
        r1 = _make_review(evidence_bundle_id="b" * 64)
        r2 = _make_review(evidence_bundle_id="c" * 64)
        assert r1.review_id != r2.review_id

    def test_wrong_intent_digest_produces_different_review_id(self) -> None:
        r1 = _make_review(intent_digest="b" * 64)
        r2 = _make_review(intent_digest="c" * 64)
        assert r1.review_id != r2.review_id

    def test_wrong_subject_sha_produces_different_review_id(self) -> None:
        r1 = _make_review(subject_sha="a" * 40)
        r2 = _make_review(subject_sha="b" * 40)
        assert r1.review_id != r2.review_id


class TestReviewerClassPolicy:
    def test_independent_agent_not_human(self) -> None:
        review = _make_review(reviewer_class=ReviewerClass.INDEPENDENT_AGENT)
        assert review.reviewer_class == ReviewerClass.INDEPENDENT_AGENT
        assert review.reviewer_class != ReviewerClass.HUMAN

    def test_human_not_independent_agent(self) -> None:
        review = _make_review(reviewer_class=ReviewerClass.HUMAN)
        assert review.reviewer_class == ReviewerClass.HUMAN
        assert review.reviewer_class != ReviewerClass.INDEPENDENT_AGENT

    def test_reviewer_class_enum_values(self) -> None:
        assert ReviewerClass.DETERMINISTIC != ReviewerClass.HUMAN
        assert ReviewerClass.DETERMINISTIC != ReviewerClass.INDEPENDENT_AGENT
        assert ReviewerClass.HUMAN != ReviewerClass.INDEPENDENT_AGENT


class TestSufficiencyStatus:
    def test_all_statuses_distinct(self) -> None:
        statuses = list(SufficiencyStatus)
        assert len(set(statuses)) == len(statuses)

    def test_pass_not_fail(self) -> None:
        assert SufficiencyStatus.PASS != SufficiencyStatus.FAIL

    def test_insufficient_evidence_distinct(self) -> None:
        assert SufficiencyStatus.INSUFFICIENT_EVIDENCE != SufficiencyStatus.INCONCLUSIVE


class TestSerializationRoundtrip:
    def test_json_roundtrip(self) -> None:
        review = _make_review(reviewer_class=ReviewerClass.INDEPENDENT_AGENT)
        serialized = serialize_sufficiency_review(review)
        json_str = json.dumps(serialized)
        deserialized = deserialize_sufficiency_review(json_str)
        assert deserialized.review_id == review.review_id
        assert deserialized.reviewer_class == ReviewerClass.INDEPENDENT_AGENT

    def test_bytes_roundtrip(self) -> None:
        review = _make_review()
        serialized = serialize_sufficiency_review(review)
        json_bytes = json.dumps(serialized).encode("utf-8")
        deserialized = deserialize_sufficiency_review(json_bytes)
        assert deserialized.review_id == review.review_id


class TestUnknownSchemaVersion:
    def test_wrong_schema_version_rejected(self) -> None:
        review = _make_review()
        serialized = serialize_sufficiency_review(review)
        serialized["schema_version"] = 99
        with pytest.raises(SufficiencyReviewError) as exc_info:
            deserialize_sufficiency_review(serialized)
        assert exc_info.value.code == "UNKNOWN_REVIEW_SCHEMA_VERSION"

    def test_missing_schema_version_rejected(self) -> None:
        review = _make_review()
        serialized = serialize_sufficiency_review(review)
        del serialized["schema_version"]
        with pytest.raises(SufficiencyReviewError):
            deserialize_sufficiency_review(serialized)
