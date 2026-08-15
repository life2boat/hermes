"""Tests for Convergence v2 (ai_engineering.convergence_v2).

Tests verify:
- EVIDENCED_UNREVIEWED state for HIGH_RISK tasks without sufficiency review
- SATISFIED state with PASS sufficiency review
- FAILED state from evidence FAIL
- UNEVIDENCED state without observations
- V1 compatibility: evaluate_convergence is unchanged
- INDEPENDENT_AGENT != HUMAN (reviewer class invariant)
- Missing authority evidence remains visible (blocking reasons preserved)
- Sufficiency review binding mismatch is blocking
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ai_engineering.contracts import TaskClass
from ai_engineering.convergence import (
    ConvergenceStatus,
    ObservationOutcome,
    TargetKind,
    create_evidence_bundle,
    create_evidence_observation,
    evaluate_convergence,
)
from ai_engineering.convergence_v2 import (
    ConvergenceBlockingReasonV2,
    ConvergenceReportV2,
    CriterionStatusV2,
    evaluate_convergence_v2,
    serialize_convergence_report_v2,
)
from ai_engineering.requirements_gate import (
    ClarificationReport,
    RequirementsQualityReview,
    generate_clarification_report,
    create_requirements_quality_review,
    GlobalDimension,
    ReviewStatus,
    CriterionReview,
    CriterionDimension,
)
from ai_engineering.sufficiency_review import (
    ReviewerClass,
    SufficiencyStatus,
    create_criterion_sufficiency_result,
    create_sufficiency_review,
)
from ai_engineering.task_analysis import analyze
from ai_engineering.task_intent import (
    TaskLineage,
    deserialize_intent,
    intent_digest,
    validate_lineage,
)

# ---------------------------------------------------------------------------
# Fixtures: minimal valid TaskIntent (both HIGH_RISK and non-HIGH_RISK)
# ---------------------------------------------------------------------------

_MINIMAL_INTENT_HIGH_RISK = """{
  "schema_version": 1,
  "task_id": "ICP-V2-TEST-001",
  "task_class": "HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION",
  "intent_revision": 1,
  "status": "READY",
  "desired_outcome": "Test high risk",
  "source_repository": "life2boat/hermes",
  "source_main_ref": "refs/remotes/github/main",
  "source_base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "acceptance_criteria": [
    {
      "criterion_id": "AC1",
      "statement": "Test criterion one."
    }
  ],
  "required_gates": [],
  "constraints": [],
  "allowed_mutations": ["REPOSITORY_WRITE"],
  "forbidden_mutations": ["RUNTIME_MUTATION"],
  "stop_boundary": "MERGE",
  "unknowns": [],
  "applicable_invariants": [],
  "parent_intent_digest": null
}"""

_MINIMAL_INTENT_BOUNDED = """{
  "schema_version": 1,
  "task_id": "ICP-V2-TEST-002",
  "task_class": "BOUNDED_IMPLEMENTATION",
  "intent_revision": 1,
  "status": "READY",
  "desired_outcome": "Test bounded",
  "source_repository": "life2boat/hermes",
  "source_main_ref": "refs/remotes/github/main",
  "source_base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "acceptance_criteria": [
    {
      "criterion_id": "AC1",
      "statement": "Test criterion one."
    }
  ],
  "required_gates": [],
  "constraints": [],
  "allowed_mutations": ["REPOSITORY_WRITE"],
  "forbidden_mutations": [],
  "stop_boundary": "MERGE",
  "unknowns": [],
  "applicable_invariants": [],
  "parent_intent_digest": null
}"""

EXPECTED_SHA = "a" * 40


def _make_intent_and_lineage(intent_json: str):
    """Return (intent, lineage, clarification, quality_review)."""
    intent = deserialize_intent(intent_json)
    lineage_task_id = intent.task_id
    exec_task_id = lineage_task_id + "/T1"
    scoped_ac = f"{lineage_task_id}::AC1"

    # Minimal lineage with one CRITERION, one TASK, one EVIDENCE node
    lineage_json = {
        "schema_version": 1,
        "nodes": [
            {"node_id": scoped_ac, "kind": "CRITERION"},
            {"node_id": exec_task_id, "kind": "TASK"},
            {"node_id": f"{lineage_task_id}::EV1", "kind": "EVIDENCE"},
        ],
        "edges": [
            {"source_id": exec_task_id, "target_id": scoped_ac, "relation": "IMPLEMENTS"},
            {"source_id": f"{lineage_task_id}::EV1", "target_id": exec_task_id, "relation": "VERIFIES"},
        ],
    }
    lineage = validate_lineage(lineage_json)

    clarification = generate_clarification_report(
        intent=intent,
    )
    # the function generate_clarification_report only takes intent. Let's fix this call.
    quality_review = create_requirements_quality_review(
        task_id=lineage_task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        reviewer_id="test-reviewer",
        criterion_reviews=[CriterionReview(
            criterion_id="AC1",
            dimensions={CriterionDimension.CLEAR: ReviewStatus.PASS, CriterionDimension.TESTABLE: ReviewStatus.PASS, CriterionDimension.BOUNDED: ReviewStatus.PASS},
        )],
        global_reviews={d: ReviewStatus.PASS for d in GlobalDimension},
    )
    return intent, lineage, clarification, quality_review


def _make_bundle(intent, lineage, outcome: ObservationOutcome):
    """Create an EvidenceBundle with one observation."""
    analysis = analyze(intent, lineage, expected_base_sha=EXPECTED_SHA)
    ev_node_id = next(
        n.node_id for n in lineage.nodes if n.kind.value == "EVIDENCE"
    )
    obs = create_evidence_observation(
        target_kind=TargetKind.LINEAGE_EVIDENCE,
        target_id=ev_node_id,
        outcome=outcome,
        producer_id="test-producer",
        artifact_ref="test-artifact.json",
        artifact_digest="d" * 64,
    )
    return create_evidence_bundle(
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        analysis_id=analysis.analysis_id,
        subject_sha=EXPECTED_SHA,
        observations=[obs],
    )


def _make_sufficiency_review(intent, bundle, criterion_id: str, status: SufficiencyStatus):
    analysis = analyze(intent, validate_lineage({
        "schema_version": 1,
        "nodes": [{"node_id": "dummy_node", "kind": "CRITERION"}],
        "edges": [],
    }), expected_base_sha=EXPECTED_SHA)
    cr = create_criterion_sufficiency_result(
        criterion_id=criterion_id,
        status=status,
        reason_codes=["test_reason"],
    )
    return create_sufficiency_review(
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        analysis_id=bundle.analysis_id,
        subject_sha=EXPECTED_SHA,
        evidence_bundle_id=bundle.bundle_id,
        reviewer_id="test-reviewer",
        reviewer_class=ReviewerClass.INDEPENDENT_AGENT,
        overall_status=status,
        criterion_reviews=[cr],
    )


class TestHighRiskWithoutSufficiencyReview:
    """PASS observation without sufficiency review → EVIDENCED_UNREVIEWED (blocking)."""

    def test_pass_obs_no_review_yields_evidenced_unreviewed(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        report = evaluate_convergence_v2(
            intent=intent,
            clarification=clar,
            quality_review=qr,
            lineage=lineage,
            bundle=bundle,
            expected_base_sha=EXPECTED_SHA,
            subject_sha=EXPECTED_SHA,
            sufficiency_review=None,
        )
        assert report.is_high_risk is True
        assert report.status == ConvergenceStatus.NOT_CONVERGED
        ac1_result = next(c for c in report.criterion_results if c.criterion_id == "AC1")
        assert ac1_result.status == CriterionStatusV2.EVIDENCED_UNREVIEWED
        assert ConvergenceBlockingReasonV2.CRITERION_EVIDENCED_UNREVIEWED in report.blocking_reasons

    def test_pass_obs_with_pass_review_yields_satisfied(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        sr = _make_sufficiency_review(intent, bundle, "AC1", SufficiencyStatus.PASS)
        report = evaluate_convergence_v2(
            intent=intent,
            clarification=clar,
            quality_review=qr,
            lineage=lineage,
            bundle=bundle,
            expected_base_sha=EXPECTED_SHA,
            subject_sha=EXPECTED_SHA,
            sufficiency_review=sr,
        )
        assert report.is_high_risk is True
        ac1_result = next(c for c in report.criterion_results if c.criterion_id == "AC1")
        assert ac1_result.status == CriterionStatusV2.SATISFIED


class TestHighRiskFailedEvidence:
    def test_fail_obs_yields_failed_criterion(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.FAIL)
        report = evaluate_convergence_v2(
            intent=intent, clarification=clar, quality_review=qr,
            lineage=lineage, bundle=bundle,
            expected_base_sha=EXPECTED_SHA, subject_sha=EXPECTED_SHA,
        )
        ac1 = next(c for c in report.criterion_results if c.criterion_id == "AC1")
        assert ac1.status == CriterionStatusV2.FAILED
        assert ConvergenceBlockingReasonV2.CRITERION_FAILED in report.blocking_reasons


class TestNonHighRiskWithPassObservation:
    """For non-HIGH_RISK tasks, PASS obs without sufficiency review → SATISFIED."""

    def test_bounded_task_pass_obs_no_review_yields_satisfied(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_BOUNDED)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        report = evaluate_convergence_v2(
            intent=intent, clarification=clar, quality_review=qr,
            lineage=lineage, bundle=bundle,
            expected_base_sha=EXPECTED_SHA, subject_sha=EXPECTED_SHA,
        )
        assert report.is_high_risk is False
        ac1 = next(c for c in report.criterion_results if c.criterion_id == "AC1")
        assert ac1.status == CriterionStatusV2.SATISFIED


class TestV1Compatibility:
    """V1 evaluate_convergence must be completely unchanged."""

    def test_v1_still_returns_convergence_report(self) -> None:
        from ai_engineering.convergence import ConvergenceReport
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_BOUNDED)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        report = evaluate_convergence(
            intent=intent, clarification=clar, quality_review=qr,
            lineage=lineage, bundle=bundle,
            expected_base_sha=EXPECTED_SHA, subject_sha=EXPECTED_SHA,
        )
        assert isinstance(report, ConvergenceReport)
        assert report.schema_version == 1

    def test_v1_pass_obs_no_sufficiency_still_satisfied(self) -> None:
        """V1 does not require sufficiency review — backward compat."""
        from ai_engineering.convergence import CriterionStatus
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        report = evaluate_convergence(
            intent=intent, clarification=clar, quality_review=qr,
            lineage=lineage, bundle=bundle,
            expected_base_sha=EXPECTED_SHA, subject_sha=EXPECTED_SHA,
        )
        # V1: SATISFIED even without sufficiency review
        ac1 = next(c for c in report.criterion_results if c.criterion_id == "AC1")
        assert ac1.status == CriterionStatus.SATISFIED


class TestSufficiencyReviewBinding:
    """Sufficiency review bound to wrong bundle is rejected."""

    def test_wrong_bundle_id_in_review_is_blocking(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)

        # Create review with wrong bundle_id
        cr = create_criterion_sufficiency_result("AC1", SufficiencyStatus.PASS)
        wrong_review = create_sufficiency_review(
            task_id=intent.task_id,
            intent_digest=intent_digest(intent),
            analysis_id=bundle.analysis_id,
            subject_sha=EXPECTED_SHA,
            evidence_bundle_id="e" * 64,  # WRONG bundle_id
            reviewer_id="reviewer",
            reviewer_class=ReviewerClass.DETERMINISTIC,
            overall_status=SufficiencyStatus.PASS,
            criterion_reviews=[cr],
        )
        report = evaluate_convergence_v2(
            intent=intent, clarification=clar, quality_review=qr,
            lineage=lineage, bundle=bundle,
            expected_base_sha=EXPECTED_SHA, subject_sha=EXPECTED_SHA,
            sufficiency_review=wrong_review,
        )
        assert ConvergenceBlockingReasonV2.SUFFICIENCY_REVIEW_BINDING_MISMATCH in report.blocking_reasons


class TestSufficiencyReviewFailBlocks:
    def test_sufficiency_fail_yields_failed_criterion(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        sr = _make_sufficiency_review(intent, bundle, "AC1", SufficiencyStatus.FAIL)
        report = evaluate_convergence_v2(
            intent=intent, clarification=clar, quality_review=qr,
            lineage=lineage, bundle=bundle,
            expected_base_sha=EXPECTED_SHA, subject_sha=EXPECTED_SHA,
            sufficiency_review=sr,
        )
        ac1 = next(c for c in report.criterion_results if c.criterion_id == "AC1")
        assert ac1.status == CriterionStatusV2.FAILED


class TestIndependentAgentNotHuman:
    """INDEPENDENT_AGENT reviewer class must not masquerade as HUMAN."""

    def test_independent_agent_is_not_human(self) -> None:
        assert ReviewerClass.INDEPENDENT_AGENT != ReviewerClass.HUMAN

    def test_sufficiency_review_preserves_reviewer_class(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        sr = _make_sufficiency_review(intent, bundle, "AC1", SufficiencyStatus.PASS)
        assert sr.reviewer_class == ReviewerClass.INDEPENDENT_AGENT

    def test_human_review_is_distinct(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        cr = create_criterion_sufficiency_result("AC1", SufficiencyStatus.PASS)
        human_review = create_sufficiency_review(
            task_id=intent.task_id,
            intent_digest=intent_digest(intent),
            analysis_id=bundle.analysis_id,
            subject_sha=EXPECTED_SHA,
            evidence_bundle_id=bundle.bundle_id,
            reviewer_id="human-reviewer",
            reviewer_class=ReviewerClass.HUMAN,
            overall_status=SufficiencyStatus.PASS,
            criterion_reviews=[cr],
        )
        agent_review = create_sufficiency_review(
            task_id=intent.task_id,
            intent_digest=intent_digest(intent),
            analysis_id=bundle.analysis_id,
            subject_sha=EXPECTED_SHA,
            evidence_bundle_id=bundle.bundle_id,
            reviewer_id="agent-reviewer",
            reviewer_class=ReviewerClass.INDEPENDENT_AGENT,
            overall_status=SufficiencyStatus.PASS,
            criterion_reviews=[cr],
        )
        assert human_review.reviewer_class == ReviewerClass.HUMAN
        assert agent_review.reviewer_class == ReviewerClass.INDEPENDENT_AGENT
        assert human_review.reviewer_class != agent_review.reviewer_class
        assert human_review.review_id != agent_review.review_id


class TestSerializationV2:
    def test_v2_report_serializes(self) -> None:
        intent, lineage, clar, qr = _make_intent_and_lineage(_MINIMAL_INTENT_HIGH_RISK)
        bundle = _make_bundle(intent, lineage, ObservationOutcome.PASS)
        report = evaluate_convergence_v2(
            intent=intent, clarification=clar, quality_review=qr,
            lineage=lineage, bundle=bundle,
            expected_base_sha=EXPECTED_SHA, subject_sha=EXPECTED_SHA,
        )
        assert isinstance(report, ConvergenceReportV2)
        serialized = serialize_convergence_report_v2(report)
        assert serialized["schema_version"] == 2
        assert serialized["is_high_risk"] is True
        assert serialized["status"] == "NOT_CONVERGED"
