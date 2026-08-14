"""Tests for Evidence-Bound Convergence."""

import copy
import json
import os
from pathlib import Path

import pytest

from ai_engineering.convergence import (
    CONVERGENCE_REPORT_SCHEMA_VERSION,
    EVIDENCE_BUNDLE_SCHEMA_VERSION,
    ConvergenceBlockingReason,
    ConvergenceError,
    ConvergenceReport,
    ConvergenceStatus,
    CriterionReason,
    CriterionResult,
    CriterionStatus,
    EvidenceBundle,
    EvidenceObservation,
    GateEvidenceStatus,
    ObservationOutcome,
    RequiredGateResult,
    TargetKind,
    _compute_bundle_id,
    _compute_convergence_id,
    _compute_observation_id,
    deserialize_convergence_report,
    deserialize_evidence_bundle,
    evaluate_convergence,
    serialize_convergence_report,
    serialize_evidence_bundle,
    validate_evidence_bundle,
)
from ai_engineering.requirements_gate import (
    CLARIFICATION_SCHEMA_VERSION,
    ClarificationQuestion,
    ClarificationReport,
    CriterionDimension,
    CriterionReview,
    GateBlockingReason,
    GateStatus,
    GlobalDimension,
    QUALITY_REVIEW_SCHEMA_VERSION,
    REQUIREMENTS_GATE_SCHEMA_VERSION,
    RequirementsGateReport,
    RequirementsQualityReview,
    ReviewStatus,
)
from ai_engineering.task_intent import (
    AcceptanceCriterion,
    IntentStatus,
    IntentUnknown,
    StopBoundary,
    TaskClass,
    TaskIntent,
    intent_digest,
)
from ai_engineering.task_intent import (
    NodeKind,
    RelationKind,
    TaskLineage,
    validate_lineage,
    deserialize_intent,
)

from ai_engineering.task_analysis import (
    AnalysisReport,
    Finding,
    FindingCode,
    FindingSeverity,
)

DUMMY_SHA = "1" * 40
DUMMY_SHA2 = "2" * 40
DUMMY_DIGEST = "a" * 64
DUMMY_DIGEST2 = "b" * 64


@pytest.fixture
def dummy_intent() -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id="TASK-1",
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Do things",
        source_repository="life2boat/hermes",
        source_main_ref="main",
        source_base_sha=DUMMY_SHA,
        constraints=(),
        allowed_mutations=(),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.MERGE,
        acceptance_criteria=(
            AcceptanceCriterion("AC1", "AC1 statement"),
            AcceptanceCriterion("AC2", "AC2 statement"),
        ),
        unknowns=(),
        applicable_invariants=(),
        required_gates=("GATE1",),
        parent_intent_digest=None,
    )


@pytest.fixture
def dummy_clarification(dummy_intent: TaskIntent) -> ClarificationReport:
    return ClarificationReport(
        schema_version=CLARIFICATION_SCHEMA_VERSION,
        clarification_id=DUMMY_DIGEST,
        task_id=dummy_intent.task_id,
        intent_digest=intent_digest(dummy_intent),
        intent_revision=dummy_intent.intent_revision,
        intent_status=dummy_intent.status,
        questions=(),
        blocking_question_count=0,
        ready_for_quality_review=True,
    )


@pytest.fixture
def dummy_quality_review(dummy_intent: TaskIntent) -> RequirementsQualityReview:
    return RequirementsQualityReview(
        schema_version=QUALITY_REVIEW_SCHEMA_VERSION,
        review_id=DUMMY_DIGEST,
        task_id=dummy_intent.task_id,
        intent_digest=intent_digest(dummy_intent),
        intent_revision=dummy_intent.intent_revision,
        reviewer_id="reviewer-1",
        criterion_reviews=tuple(
            CriterionReview(
                c.criterion_id, {d: ReviewStatus.PASS for d in CriterionDimension}
            )
            for c in dummy_intent.acceptance_criteria
        ),
        global_reviews={d: ReviewStatus.PASS for d in GlobalDimension},
    )


@pytest.fixture
def dummy_lineage(dummy_intent: TaskIntent) -> TaskLineage:
    # A valid lineage for dummy_intent
    # Let's create an EVIDENCE node for AC1 directly and an EVIDENCE node for a TASK implementing AC2
    # Plus an EVIDENCE node for GATE1 - wait GATE1 is not in lineage, it's evaluated via bundle directly
    payload = {
        "schema_version": 1,
        "nodes": [
            {"kind": "INTENT", "node_id": dummy_intent.task_id},
            {"kind": "CRITERION", "node_id": "TASK-1::AC1"},
            {"kind": "CRITERION", "node_id": "TASK-1::AC2"},
            {"kind": "TASK", "node_id": "T1"},
            {"kind": "TASK", "node_id": "T2"},
            {"kind": "EVIDENCE", "node_id": "EV1"},
            {"kind": "EVIDENCE", "node_id": "EV2"},
        ],
        "edges": [
            {"relation": "IMPLEMENTS", "source_id": "T1", "target_id": "TASK-1::AC1"},
            {"relation": "IMPLEMENTS", "source_id": "T2", "target_id": "TASK-1::AC2"},
            {"relation": "VERIFIES", "source_id": "EV1", "target_id": "TASK-1::AC1"},
            {"relation": "VERIFIES", "source_id": "EV2", "target_id": "T2"},
        ],
    }
    return validate_lineage(payload)


@pytest.fixture
def dummy_bundle(
    dummy_intent: TaskIntent, dummy_lineage: TaskLineage
) -> EvidenceBundle:
    obs1_payload = {
        "target_kind": "LINEAGE_EVIDENCE",
        "target_id": "EV1",
        "outcome": "PASS",
        "producer_id": "pytest",
        "artifact_ref": "log.txt",
        "artifact_digest": DUMMY_DIGEST,
    }
    obs1_id = _compute_observation_id(obs1_payload)
    obs1 = EvidenceObservation(
        obs1_id,
        TargetKind.LINEAGE_EVIDENCE,
        "EV1",
        ObservationOutcome.PASS,
        "pytest",
        "log.txt",
        DUMMY_DIGEST,
    )

    obs2_payload = {
        "target_kind": "LINEAGE_EVIDENCE",
        "target_id": "EV2",
        "outcome": "PASS",
        "producer_id": "pytest",
        "artifact_ref": "log2.txt",
        "artifact_digest": DUMMY_DIGEST,
    }
    obs2_id = _compute_observation_id(obs2_payload)
    obs2 = EvidenceObservation(
        obs2_id,
        TargetKind.LINEAGE_EVIDENCE,
        "EV2",
        ObservationOutcome.PASS,
        "pytest",
        "log2.txt",
        DUMMY_DIGEST,
    )

    obs3_payload = {
        "target_kind": "REQUIRED_GATE",
        "target_id": "GATE1",
        "outcome": "PASS",
        "producer_id": "pytest",
        "artifact_ref": "log3.txt",
        "artifact_digest": DUMMY_DIGEST,
    }
    obs3_id = _compute_observation_id(obs3_payload)
    obs3 = EvidenceObservation(
        obs3_id,
        TargetKind.REQUIRED_GATE,
        "GATE1",
        ObservationOutcome.PASS,
        "pytest",
        "log3.txt",
        DUMMY_DIGEST,
    )

    # We need analysis_id from actual analyze() so it matches
    from ai_engineering.task_analysis import analyze

    analysis = analyze(dummy_intent, dummy_lineage, expected_base_sha=DUMMY_SHA)
    analysis_id = analysis.analysis_id

    bundle_payload = {
        "task_id": dummy_intent.task_id,
        "intent_digest": intent_digest(dummy_intent),
        "analysis_id": analysis_id,
        "subject_sha": DUMMY_SHA,
        "observations": [
            {"observation_id": obs1_id},
            {"observation_id": obs2_id},
            {"observation_id": obs3_id},
        ],
    }
    bundle_id = _compute_bundle_id(bundle_payload)

    return EvidenceBundle(
        schema_version=1,
        bundle_id=bundle_id,
        task_id=dummy_intent.task_id,
        intent_digest=intent_digest(dummy_intent),
        analysis_id=analysis_id,
        subject_sha=DUMMY_SHA,
        observations=(obs1, obs2, obs3),
    )


def test_valid_evidence_bundle(dummy_bundle):
    # VALID_EVIDENCE_BUNDLE_V1=PASS
    data = serialize_evidence_bundle(dummy_bundle)
    deserialized = deserialize_evidence_bundle(data)
    assert deserialized == dummy_bundle


def test_unknown_evidence_schema_version(dummy_bundle):
    # UNKNOWN_EVIDENCE_SCHEMA_VERSION=FAIL_CLOSED
    data = serialize_evidence_bundle(dummy_bundle)
    data["schema_version"] = 2
    with pytest.raises(ConvergenceError) as exc:
        deserialize_evidence_bundle(data)
    assert exc.value.code == "UNKNOWN_EVIDENCE_SCHEMA_VERSION"


def test_tampered_bundle_id(dummy_bundle):
    # TAMPERED_BUNDLE_ID=FAIL_CLOSED
    data = serialize_evidence_bundle(dummy_bundle)
    data["bundle_id"] = DUMMY_DIGEST2
    with pytest.raises(ConvergenceError) as exc:
        deserialize_evidence_bundle(data)
    assert exc.value.code == "TAMPERED_BUNDLE_ID"


def test_tampered_observation_id(dummy_bundle):
    # TAMPERED_OBSERVATION_ID=FAIL_CLOSED
    data = serialize_evidence_bundle(dummy_bundle)
    data["observations"][0]["observation_id"] = DUMMY_DIGEST2
    with pytest.raises(ConvergenceError) as exc:
        deserialize_evidence_bundle(data)
    assert exc.value.code == "TAMPERED_OBSERVATION_ID"


def test_duplicate_evidence_target(dummy_bundle):
    # DUPLICATE_EVIDENCE_TARGET=FAIL_CLOSED
    data = serialize_evidence_bundle(dummy_bundle)
    data["observations"].append(data["observations"][0])
    with pytest.raises(ConvergenceError) as exc:
        deserialize_evidence_bundle(data)
    assert exc.value.code == "DUPLICATE_EVIDENCE_TARGET"


def test_invalid_subject_sha(dummy_bundle):
    # INVALID_SUBJECT_SHA=FAIL_CLOSED
    data = serialize_evidence_bundle(dummy_bundle)
    data["subject_sha"] = "invalid"
    with pytest.raises(ConvergenceError) as exc:
        deserialize_evidence_bundle(data)
    assert exc.value.code == "VALUE_INVALID"


def test_invalid_artifact_digest(dummy_bundle):
    # INVALID_ARTIFACT_DIGEST=FAIL_CLOSED
    data = serialize_evidence_bundle(dummy_bundle)
    data["observations"][0]["artifact_digest"] = "invalid"
    with pytest.raises(ConvergenceError) as exc:
        deserialize_evidence_bundle(data)
    assert exc.value.code == "VALUE_INVALID"


def test_binding_mismatches(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    # INTENT_DIGEST_MISMATCH=NOT_CONVERGED
    # ANALYSIS_ID_MISMATCH=NOT_CONVERGED
    # SUBJECT_SHA_MISMATCH=NOT_CONVERGED
    # TASK_ID_MISMATCH=NOT_CONVERGED

    # Mismatch Intent
    b_intent_payload = json.loads(json.dumps(serialize_evidence_bundle(dummy_bundle)))
    b_intent_payload["intent_digest"] = DUMMY_DIGEST2
    b_intent_payload["bundle_id"] = _compute_bundle_id(b_intent_payload)
    b_intent = deserialize_evidence_bundle(b_intent_payload)
    r_intent = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b_intent,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert r_intent.status == ConvergenceStatus.NOT_CONVERGED
    assert (
        ConvergenceBlockingReason.EVIDENCE_INTENT_MISMATCH in r_intent.blocking_reasons
    )

    # Mismatch Analysis
    b_analysis_payload = json.loads(json.dumps(serialize_evidence_bundle(dummy_bundle)))
    b_analysis_payload["analysis_id"] = DUMMY_DIGEST2
    b_analysis_payload["bundle_id"] = _compute_bundle_id(b_analysis_payload)
    b_analysis = deserialize_evidence_bundle(b_analysis_payload)
    r_analysis = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b_analysis,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert r_analysis.status == ConvergenceStatus.NOT_CONVERGED
    assert (
        ConvergenceBlockingReason.EVIDENCE_ANALYSIS_MISMATCH
        in r_analysis.blocking_reasons
    )

    # Mismatch Subject SHA
    b_subject_payload = json.loads(json.dumps(serialize_evidence_bundle(dummy_bundle)))
    b_subject_payload["subject_sha"] = DUMMY_SHA2
    b_subject_payload["bundle_id"] = _compute_bundle_id(b_subject_payload)
    b_subject = deserialize_evidence_bundle(b_subject_payload)
    r_subject = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b_subject,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert r_subject.status == ConvergenceStatus.NOT_CONVERGED
    assert (
        ConvergenceBlockingReason.EVIDENCE_SUBJECT_SHA_MISMATCH
        in r_subject.blocking_reasons
    )

    # Mismatch Task ID
    b_task_payload = json.loads(json.dumps(serialize_evidence_bundle(dummy_bundle)))
    b_task_payload["task_id"] = "TASK-2"
    b_task_payload["bundle_id"] = _compute_bundle_id(b_task_payload)
    b_task = deserialize_evidence_bundle(b_task_payload)
    r_task = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b_task,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert r_task.status == ConvergenceStatus.NOT_CONVERGED
    assert ConvergenceBlockingReason.EVIDENCE_TASK_MISMATCH in r_task.blocking_reasons


def test_unknown_targets(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    # UNKNOWN_LINEAGE_EVIDENCE_TARGET=FAIL_CLOSED_OR_NOT_CONVERGED
    obs4_payload = {
        "target_kind": "LINEAGE_EVIDENCE",
        "target_id": "EV_UNKNOWN",
        "outcome": "PASS",
        "producer_id": "pytest",
        "artifact_ref": "log.txt",
        "artifact_digest": DUMMY_DIGEST,
    }
    obs4_id = _compute_observation_id(obs4_payload)
    obs4 = EvidenceObservation(
        obs4_id,
        TargetKind.LINEAGE_EVIDENCE,
        "EV_UNKNOWN",
        ObservationOutcome.PASS,
        "pytest",
        "log.txt",
        DUMMY_DIGEST,
    )

    b2_obs = (*dummy_bundle.observations, obs4)
    b2_bundle_id = _compute_bundle_id({
        "task_id": dummy_bundle.task_id,
        "intent_digest": dummy_bundle.intent_digest,
        "analysis_id": dummy_bundle.analysis_id,
        "subject_sha": dummy_bundle.subject_sha,
        "observations": [{"observation_id": o.observation_id} for o in b2_obs],
    })
    b2 = EvidenceBundle(
        schema_version=1,
        bundle_id=b2_bundle_id,
        task_id=dummy_bundle.task_id,
        intent_digest=dummy_bundle.intent_digest,
        analysis_id=dummy_bundle.analysis_id,
        subject_sha=dummy_bundle.subject_sha,
        observations=b2_obs,
    )

    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b2,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    assert ConvergenceBlockingReason.EVIDENCE_TARGET_UNKNOWN in report.blocking_reasons


def test_criteria_evaluations(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    from ai_engineering.task_intent import validate_intent
    from ai_engineering.requirements_gate import evaluate_requirements_gate
    from ai_engineering.task_analysis import analyze

    validate_intent(dummy_intent)
    validate_lineage(dummy_lineage)
    assert (
        evaluate_requirements_gate(
            dummy_intent, dummy_clarification, dummy_quality_review
        ).status
        == GateStatus.PASS
    )
    assert (
        analyze(dummy_intent, dummy_lineage, expected_base_sha=DUMMY_SHA).has_errors
        is False
    )

    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        dummy_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.CONVERGED
    assert len(report.criterion_results) == 2

    # AC1 has direct pass evidence
    ac1 = next(c for c in report.criterion_results if c.criterion_id == "AC1")
    assert ac1.status == CriterionStatus.SATISFIED
    assert CriterionReason.SATISFIED_BY_DIRECT_EVIDENCE in ac1.blocking_reasons

    # AC2 has task pass evidence
    ac2 = next(c for c in report.criterion_results if c.criterion_id == "AC2")
    assert ac2.status == CriterionStatus.SATISFIED
    assert CriterionReason.SATISFIED_BY_TASK_EVIDENCE in ac2.blocking_reasons


def test_failure_dominates(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    # PASS_AND_FAIL_FAILURE_DOMINATES=PASS
    # We add a FAIL observation for EV1 (direct evidence for AC1). Wait, duplicate target in bundle is invalid.
    # So we change the outcome of EV1 to FAIL.
    obs1_payload = {
        "target_kind": "LINEAGE_EVIDENCE",
        "target_id": "EV1",
        "outcome": "FAIL",
        "producer_id": "pytest",
        "artifact_ref": "log.txt",
        "artifact_digest": DUMMY_DIGEST,
    }
    obs1_id = _compute_observation_id(obs1_payload)
    obs1 = EvidenceObservation(
        obs1_id,
        TargetKind.LINEAGE_EVIDENCE,
        "EV1",
        ObservationOutcome.FAIL,
        "pytest",
        "log.txt",
        DUMMY_DIGEST,
    )

    b2_obs = (obs1, dummy_bundle.observations[1], dummy_bundle.observations[2])
    b2_bundle_id = _compute_bundle_id({
        "task_id": dummy_bundle.task_id,
        "intent_digest": dummy_bundle.intent_digest,
        "analysis_id": dummy_bundle.analysis_id,
        "subject_sha": dummy_bundle.subject_sha,
        "observations": [{"observation_id": o.observation_id} for o in b2_obs],
    })
    b2 = EvidenceBundle(
        schema_version=1,
        bundle_id=b2_bundle_id,
        task_id=dummy_bundle.task_id,
        intent_digest=dummy_bundle.intent_digest,
        analysis_id=dummy_bundle.analysis_id,
        subject_sha=dummy_bundle.subject_sha,
        observations=b2_obs,
    )

    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b2,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    ac1 = next(c for c in report.criterion_results if c.criterion_id == "AC1")
    assert ac1.status == CriterionStatus.FAILED
    assert CriterionReason.FAILED_EVIDENCE in ac1.blocking_reasons


def test_missing_evidence_observation_does_not_satisfy(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    b2_obs = (
        dummy_bundle.observations[1],
        dummy_bundle.observations[2],
    )  # missing EV1
    b2_bundle_id = _compute_bundle_id({
        "task_id": dummy_bundle.task_id,
        "intent_digest": dummy_bundle.intent_digest,
        "analysis_id": dummy_bundle.analysis_id,
        "subject_sha": dummy_bundle.subject_sha,
        "observations": [{"observation_id": o.observation_id} for o in b2_obs],
    })
    b2 = EvidenceBundle(
        schema_version=1,
        bundle_id=b2_bundle_id,
        task_id=dummy_bundle.task_id,
        intent_digest=dummy_bundle.intent_digest,
        analysis_id=dummy_bundle.analysis_id,
        subject_sha=dummy_bundle.subject_sha,
        observations=b2_obs,
    )
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b2,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    ac1 = next(c for c in report.criterion_results if c.criterion_id == "AC1")
    assert ac1.status == CriterionStatus.UNEVIDENCED
    assert CriterionReason.EVIDENCE_OBSERVATION_MISSING in ac1.blocking_reasons


def test_requirements_gate_fail(
    dummy_intent, dummy_clarification, dummy_lineage, dummy_bundle
):
    # REQUIREMENTS_GATE_FAIL=NOT_CONVERGED
    bad_quality_review = RequirementsQualityReview(
        schema_version=QUALITY_REVIEW_SCHEMA_VERSION,
        review_id=DUMMY_DIGEST,
        task_id=dummy_intent.task_id,
        intent_digest=intent_digest(dummy_intent),
        intent_revision=dummy_intent.intent_revision,
        reviewer_id="reviewer-1",
        criterion_reviews=tuple(
            CriterionReview(
                c.criterion_id, {d: ReviewStatus.FAIL for d in CriterionDimension}
            )
            for c in dummy_intent.acceptance_criteria
        ),
        global_reviews={d: ReviewStatus.PASS for d in GlobalDimension},
    )
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        bad_quality_review,
        dummy_lineage,
        dummy_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    assert (
        ConvergenceBlockingReason.REQUIREMENTS_GATE_NOT_PASSING
        in report.blocking_reasons
    )


def test_analysis_error(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    # ANALYSIS_ERROR=NOT_CONVERGED
    # we can trigger this by having an invalid expected base sha
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        dummy_bundle,
        DUMMY_SHA2,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    assert ConvergenceBlockingReason.ANALYSIS_ERROR_PRESENT in report.blocking_reasons


def test_no_acceptance_criteria(
    dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    intent_payload = {
        "schema_version": 1,
        "task_id": "TASK-1",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Do things",
        "source_repository": "life2boat/hermes",
        "source_main_ref": "main",
        "source_base_sha": DUMMY_SHA,
        "constraints": [],
        "allowed_mutations": [],
        "forbidden_mutations": [],
        "stop_boundary": "MERGE",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": ["GATE1"],
        "parent_intent_digest": None,
    }
    intent = deserialize_intent(json.dumps(intent_payload))
    # The lineage and bundle will now have orphan evidence or mismatches, but the primary error will be NO_ACCEPTANCE_CRITERIA
    b2_obs = (dummy_bundle.observations[2],)  # only gate
    b2_bundle_id = _compute_bundle_id({
        "task_id": intent.task_id,
        "intent_digest": intent_digest(intent),
        "analysis_id": DUMMY_DIGEST2,
        "subject_sha": DUMMY_SHA,
        "observations": [{"observation_id": o.observation_id} for o in b2_obs],
    })
    b2 = EvidenceBundle(
        schema_version=1,
        bundle_id=b2_bundle_id,
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        analysis_id=DUMMY_DIGEST2,
        subject_sha=DUMMY_SHA,
        observations=b2_obs,
    )
    report = evaluate_convergence(
        intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b2,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert ConvergenceBlockingReason.NO_ACCEPTANCE_CRITERIA in report.blocking_reasons
    assert report.status == ConvergenceStatus.NOT_CONVERGED


def test_convergence_report_round_trip(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        dummy_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    data = serialize_convergence_report(report)
    deserialized = deserialize_convergence_report(data)
    assert deserialized == report


def test_changed_outcome_changed_convergence_id(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    # H-PR4-001 fix: b2 must have a correctly recomputed bundle_id — not a stale one.
    report1 = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        dummy_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )

    obs1_payload = {
        "target_kind": "LINEAGE_EVIDENCE",
        "target_id": "EV1",
        "outcome": "FAIL",
        "producer_id": "pytest",
        "artifact_ref": "log.txt",
        "artifact_digest": DUMMY_DIGEST,
    }
    obs1_id = _compute_observation_id(obs1_payload)
    obs1 = EvidenceObservation(
        obs1_id,
        TargetKind.LINEAGE_EVIDENCE,
        "EV1",
        ObservationOutcome.FAIL,
        "pytest",
        "log.txt",
        DUMMY_DIGEST,
    )

    # Compute the correct bundle_id for b2 with the updated observation.
    b2_obs = (obs1, dummy_bundle.observations[1], dummy_bundle.observations[2])
    b2_bundle_id = _compute_bundle_id({
        "task_id": dummy_bundle.task_id,
        "intent_digest": dummy_bundle.intent_digest,
        "analysis_id": dummy_bundle.analysis_id,
        "subject_sha": dummy_bundle.subject_sha,
        "observations": [{"observation_id": o.observation_id} for o in b2_obs],
    })
    b2 = EvidenceBundle(
        schema_version=1,
        bundle_id=b2_bundle_id,
        task_id=dummy_bundle.task_id,
        intent_digest=dummy_bundle.intent_digest,
        analysis_id=dummy_bundle.analysis_id,
        subject_sha=dummy_bundle.subject_sha,
        observations=b2_obs,
    )
    report2 = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        b2,
        DUMMY_SHA,
        DUMMY_SHA,
    )

    assert report1.convergence_id != report2.convergence_id


# ---------------------------------------------------------------------------
# H-PR4-001: Direct-API tamper protection regressions
# ---------------------------------------------------------------------------


def test_validate_evidence_bundle_accepts_valid_dataclass(dummy_bundle):
    """DIRECT_API_VALID_CANONICAL_BUNDLE=PASS."""
    validated = validate_evidence_bundle(dummy_bundle)
    assert validated.bundle_id == dummy_bundle.bundle_id
    assert validated == dummy_bundle


def test_direct_api_tampered_observation_id(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_TAMPERED_OBSERVATION_ID=FAIL_CLOSED (H-PR4-001 Manus exact repro)."""
    import dataclasses

    tampered_bundle = dataclasses.replace(
        dummy_bundle,
        bundle_id="f" * 64,
        observations=tuple(
            dataclasses.replace(obs, observation_id="0" * 64)
            for obs in dummy_bundle.observations
        ),
    )
    with pytest.raises(ConvergenceError) as exc:
        evaluate_convergence(
            dummy_intent,
            dummy_clarification,
            dummy_quality_review,
            dummy_lineage,
            tampered_bundle,
            DUMMY_SHA,
            DUMMY_SHA,
        )
    assert exc.value.code == "TAMPERED_OBSERVATION_ID"


def test_direct_api_tampered_bundle_id(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_TAMPERED_BUNDLE_ID=FAIL_CLOSED."""
    import dataclasses

    # Observation IDs are correct, but bundle_id is stale/wrong.
    tampered_bundle = dataclasses.replace(dummy_bundle, bundle_id="e" * 64)
    with pytest.raises(ConvergenceError) as exc:
        evaluate_convergence(
            dummy_intent,
            dummy_clarification,
            dummy_quality_review,
            dummy_lineage,
            tampered_bundle,
            DUMMY_SHA,
            DUMMY_SHA,
        )
    assert exc.value.code == "TAMPERED_BUNDLE_ID"


def test_direct_api_changed_observation_stale_id(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_CHANGED_OBSERVATION_STALE_ID=FAIL_CLOSED."""
    import dataclasses

    # Change observation semantic content (outcome PASS->FAIL) but keep stale observation_id.
    original_obs = dummy_bundle.observations[0]
    tampered_obs = dataclasses.replace(
        original_obs,
        outcome=ObservationOutcome.FAIL,
        # observation_id deliberately NOT updated — stale
    )
    tampered_bundle = dataclasses.replace(
        dummy_bundle,
        observations=(tampered_obs,) + dummy_bundle.observations[1:],
    )
    with pytest.raises(ConvergenceError) as exc:
        evaluate_convergence(
            dummy_intent,
            dummy_clarification,
            dummy_quality_review,
            dummy_lineage,
            tampered_bundle,
            DUMMY_SHA,
            DUMMY_SHA,
        )
    assert exc.value.code == "TAMPERED_OBSERVATION_ID"


def test_direct_api_recomputed_observation_stale_bundle_id(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_RECOMPUTED_OBSERVATION_STALE_BUNDLE_ID=FAIL_CLOSED."""
    import dataclasses

    # observation_id is correctly recomputed from new outcome but bundle_id is stale.
    obs1_payload = {
        "target_kind": "LINEAGE_EVIDENCE",
        "target_id": "EV1",
        "outcome": "FAIL",
        "producer_id": "pytest",
        "artifact_ref": "log.txt",
        "artifact_digest": DUMMY_DIGEST,
    }
    new_obs1_id = _compute_observation_id(obs1_payload)
    new_obs1 = EvidenceObservation(
        new_obs1_id,
        TargetKind.LINEAGE_EVIDENCE,
        "EV1",
        ObservationOutcome.FAIL,
        "pytest",
        "log.txt",
        DUMMY_DIGEST,
    )
    tampered_bundle = dataclasses.replace(
        dummy_bundle,
        bundle_id=dummy_bundle.bundle_id,  # original (now stale) bundle_id
        observations=(new_obs1,) + dummy_bundle.observations[1:],
    )
    with pytest.raises(ConvergenceError) as exc:
        evaluate_convergence(
            dummy_intent,
            dummy_clarification,
            dummy_quality_review,
            dummy_lineage,
            tampered_bundle,
            DUMMY_SHA,
            DUMMY_SHA,
        )
    assert exc.value.code == "TAMPERED_BUNDLE_ID"


def test_direct_api_valid_wrong_task_context(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_VALID_WRONG_TASK_CONTEXT=NOT_CONVERGED.

    A self-consistent bundle bound to wrong task_id passes validate_evidence_bundle
    but evaluate_convergence reports NOT_CONVERGED with EVIDENCE_TASK_MISMATCH.
    This is intentional context-mismatch semantics, not an integrity error.
    """
    import dataclasses

    # Build a self-consistent bundle with a different task_id.
    obs0 = dummy_bundle.observations[0]
    obs1 = dummy_bundle.observations[1]
    obs2 = dummy_bundle.observations[2]
    wrong_bundle_id = _compute_bundle_id({
        "task_id": "WRONG-TASK",
        "intent_digest": dummy_bundle.intent_digest,
        "analysis_id": dummy_bundle.analysis_id,
        "subject_sha": dummy_bundle.subject_sha,
        "observations": [
            {"observation_id": o.observation_id} for o in (obs0, obs1, obs2)
        ],
    })
    wrong_task_bundle = EvidenceBundle(
        schema_version=1,
        bundle_id=wrong_bundle_id,
        task_id="WRONG-TASK",
        intent_digest=dummy_bundle.intent_digest,
        analysis_id=dummy_bundle.analysis_id,
        subject_sha=dummy_bundle.subject_sha,
        observations=(obs0, obs1, obs2),
    )
    # Must pass integrity validation
    validated = validate_evidence_bundle(wrong_task_bundle)
    assert validated.task_id == "WRONG-TASK"
    # Must be rejected as context mismatch, not integrity error
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        wrong_task_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    assert ConvergenceBlockingReason.EVIDENCE_TASK_MISMATCH in report.blocking_reasons


def test_direct_api_valid_wrong_intent_context(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_VALID_WRONG_INTENT_CONTEXT=NOT_CONVERGED."""
    wrong_bundle_id = _compute_bundle_id({
        "task_id": dummy_bundle.task_id,
        "intent_digest": DUMMY_DIGEST2,
        "analysis_id": dummy_bundle.analysis_id,
        "subject_sha": dummy_bundle.subject_sha,
        "observations": [
            {"observation_id": o.observation_id} for o in dummy_bundle.observations
        ],
    })
    wrong_intent_bundle = EvidenceBundle(
        schema_version=1,
        bundle_id=wrong_bundle_id,
        task_id=dummy_bundle.task_id,
        intent_digest=DUMMY_DIGEST2,
        analysis_id=dummy_bundle.analysis_id,
        subject_sha=dummy_bundle.subject_sha,
        observations=dummy_bundle.observations,
    )
    validated = validate_evidence_bundle(wrong_intent_bundle)
    assert validated.intent_digest == DUMMY_DIGEST2
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        wrong_intent_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    assert ConvergenceBlockingReason.EVIDENCE_INTENT_MISMATCH in report.blocking_reasons


def test_direct_api_valid_wrong_analysis_context(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_VALID_WRONG_ANALYSIS_CONTEXT=NOT_CONVERGED."""
    wrong_bundle_id = _compute_bundle_id({
        "task_id": dummy_bundle.task_id,
        "intent_digest": dummy_bundle.intent_digest,
        "analysis_id": DUMMY_DIGEST2,
        "subject_sha": dummy_bundle.subject_sha,
        "observations": [
            {"observation_id": o.observation_id} for o in dummy_bundle.observations
        ],
    })
    wrong_analysis_bundle = EvidenceBundle(
        schema_version=1,
        bundle_id=wrong_bundle_id,
        task_id=dummy_bundle.task_id,
        intent_digest=dummy_bundle.intent_digest,
        analysis_id=DUMMY_DIGEST2,
        subject_sha=dummy_bundle.subject_sha,
        observations=dummy_bundle.observations,
    )
    validated = validate_evidence_bundle(wrong_analysis_bundle)
    assert validated.analysis_id == DUMMY_DIGEST2
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        wrong_analysis_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    assert (
        ConvergenceBlockingReason.EVIDENCE_ANALYSIS_MISMATCH in report.blocking_reasons
    )


def test_direct_api_valid_wrong_subject_sha_context(
    dummy_intent, dummy_clarification, dummy_quality_review, dummy_lineage, dummy_bundle
):
    """DIRECT_API_VALID_WRONG_SUBJECT_CONTEXT=NOT_CONVERGED."""
    wrong_bundle_id = _compute_bundle_id({
        "task_id": dummy_bundle.task_id,
        "intent_digest": dummy_bundle.intent_digest,
        "analysis_id": dummy_bundle.analysis_id,
        "subject_sha": DUMMY_SHA2,
        "observations": [
            {"observation_id": o.observation_id} for o in dummy_bundle.observations
        ],
    })
    wrong_sha_bundle = EvidenceBundle(
        schema_version=1,
        bundle_id=wrong_bundle_id,
        task_id=dummy_bundle.task_id,
        intent_digest=dummy_bundle.intent_digest,
        analysis_id=dummy_bundle.analysis_id,
        subject_sha=DUMMY_SHA2,
        observations=dummy_bundle.observations,
    )
    validated = validate_evidence_bundle(wrong_sha_bundle)
    assert validated.subject_sha == DUMMY_SHA2
    report = evaluate_convergence(
        dummy_intent,
        dummy_clarification,
        dummy_quality_review,
        dummy_lineage,
        wrong_sha_bundle,
        DUMMY_SHA,
        DUMMY_SHA,
    )
    assert report.status == ConvergenceStatus.NOT_CONVERGED
    assert (
        ConvergenceBlockingReason.EVIDENCE_SUBJECT_SHA_MISMATCH
        in report.blocking_reasons
    )
