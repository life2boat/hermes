"""Convergence v2 — production-sensitive evidence-bound convergence.

BACKWARD COMPATIBILITY
======================
Convergence v1 (evaluate_convergence in convergence.py) is UNCHANGED.
This module adds versioned v2 semantics alongside it.  Historical reports
produced by v1 remain byte-stable; their convergence_id computation is
not affected by this module.

V2 SEMANTICS
============
For TaskClass.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION, a direct PASS
EvidenceObservation alone is NOT sufficient to mark a criterion SATISFIED.
An EvidenceSufficiencyReview with status PASS is required.

Without a matching validated sufficiency review:
  PASS observation → EVIDENCED_UNREVIEWED  (blocking for HIGH_RISK tasks)
  This prevents silent collapse of semantic review into byte-binding proof.

CRITERION STATE MACHINE (v2)
=============================
no observation                              → UNEVIDENCED
observation FAIL                            → FAILED
observation INCONCLUSIVE                    → INCONCLUSIVE
observation PASS, no/insufficient review    → EVIDENCED_UNREVIEWED  (HIGH_RISK blocks)
observation PASS + sufficiency review PASS  → SATISFIED
sufficiency review FAIL                     → FAILED
sufficiency review INCONCLUSIVE/INSUFFICIENT→ not SATISFIED

SCOPE BOUNDARY
==============
Required gates remain a separate convergence dimension from criterion status.
Gate evidence is not inferred from criterion satisfaction.

AI6 POLICY
==========
A sufficiency review with reviewer_class=INDEPENDENT_AGENT may satisfy
EVIDENCED_UNREVIEWED for HIGH_RISK tasks, but systems requiring HUMAN
review must not accept INDEPENDENT_AGENT as a substitute.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, NoReturn

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass

from ai_engineering.contracts import TaskClass
from ai_engineering.convergence import (
    CONVERGENCE_REPORT_SCHEMA_VERSION,
    EVIDENCE_BUNDLE_SCHEMA_VERSION,
    CriterionReason,
    ConvergenceBlockingReason,
    ConvergenceError,
    ConvergenceStatus,
    EvidenceBundle,
    GateEvidenceStatus,
    NodeKind,
    ObservationOutcome,
    RelationKind,
    RequiredGateResult,
    TargetKind,
    _compute_convergence_id,
    _compute_digest,
    _digest,
    _enum,
    _fail,
    _identifier,
    _items,
    _sha,
    _string,
    evaluate_convergence,
    intent_digest,
    validate_evidence_bundle,
    validate_intent,
    validate_lineage,
)
from ai_engineering.requirements_gate import (
    ClarificationReport,
    RequirementsQualityReview,
    evaluate_requirements_gate,
)
from ai_engineering.task_analysis import analyze
from ai_engineering.task_intent import TaskIntent, TaskLineage

CONVERGENCE_REPORT_SCHEMA_VERSION_V2 = 2


# ---------------------------------------------------------------------------
# V2-specific enums
# ---------------------------------------------------------------------------


class CriterionStatusV2(StrEnum):
    """Extended criterion status for Convergence v2.

    UNEVIDENCED
        No evidence observation found for this criterion.
    EVIDENCED_UNREVIEWED
        Evidence PASS found, but required sufficiency review is absent or not PASS.
        For HIGH_RISK tasks this is blocking.
    SATISFIED
        Evidence PASS + sufficiency review PASS (or non-HIGH_RISK with PASS obs).
    FAILED
        Evidence FAIL, or sufficiency review FAIL.
    INCONCLUSIVE
        Evidence INCONCLUSIVE, or sufficiency INCONCLUSIVE/INSUFFICIENT_EVIDENCE.
    """
    UNEVIDENCED = "UNEVIDENCED"
    EVIDENCED_UNREVIEWED = "EVIDENCED_UNREVIEWED"
    SATISFIED = "SATISFIED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class CriterionReasonV2(StrEnum):
    """Reasons associated with v2 criterion results."""
    FAILED_EVIDENCE = "FAILED_EVIDENCE"
    DIRECT_EVIDENCE_MISSING = "DIRECT_EVIDENCE_MISSING"
    TASK_EVIDENCE_INCOMPLETE = "TASK_EVIDENCE_INCOMPLETE"
    EVIDENCE_OBSERVATION_MISSING = "EVIDENCE_OBSERVATION_MISSING"
    EVIDENCE_INCONCLUSIVE = "EVIDENCE_INCONCLUSIVE"
    SATISFIED_BY_DIRECT_EVIDENCE = "SATISFIED_BY_DIRECT_EVIDENCE"
    SATISFIED_BY_TASK_EVIDENCE = "SATISFIED_BY_TASK_EVIDENCE"
    SUFFICIENCY_REVIEW_REQUIRED = "SUFFICIENCY_REVIEW_REQUIRED"
    SUFFICIENCY_REVIEW_PASS = "SUFFICIENCY_REVIEW_PASS"
    SUFFICIENCY_REVIEW_FAIL = "SUFFICIENCY_REVIEW_FAIL"
    SUFFICIENCY_REVIEW_INCONCLUSIVE = "SUFFICIENCY_REVIEW_INCONCLUSIVE"
    SUFFICIENCY_REVIEW_INSUFFICIENT = "SUFFICIENCY_REVIEW_INSUFFICIENT"


class ConvergenceBlockingReasonV2(StrEnum):
    """V2 blocking reasons (superset of v1)."""
    # Inherited from v1
    REQUIREMENTS_GATE_NOT_PASSING = "REQUIREMENTS_GATE_NOT_PASSING"
    ANALYSIS_ERROR_PRESENT = "ANALYSIS_ERROR_PRESENT"
    EVIDENCE_TASK_MISMATCH = "EVIDENCE_TASK_MISMATCH"
    EVIDENCE_INTENT_MISMATCH = "EVIDENCE_INTENT_MISMATCH"
    EVIDENCE_ANALYSIS_MISMATCH = "EVIDENCE_ANALYSIS_MISMATCH"
    EVIDENCE_SUBJECT_SHA_MISMATCH = "EVIDENCE_SUBJECT_SHA_MISMATCH"
    EVIDENCE_TARGET_UNKNOWN = "EVIDENCE_TARGET_UNKNOWN"
    EVIDENCE_OBSERVATION_MISSING = "EVIDENCE_OBSERVATION_MISSING"
    EVIDENCE_FAILED = "EVIDENCE_FAILED"
    EVIDENCE_INCONCLUSIVE = "EVIDENCE_INCONCLUSIVE"
    CRITERION_UNEVIDENCED = "CRITERION_UNEVIDENCED"
    CRITERION_FAILED = "CRITERION_FAILED"
    REQUIRED_GATE_EVIDENCE_MISSING = "REQUIRED_GATE_EVIDENCE_MISSING"
    REQUIRED_GATE_FAILED = "REQUIRED_GATE_FAILED"
    REQUIRED_GATE_INCONCLUSIVE = "REQUIRED_GATE_INCONCLUSIVE"
    NO_ACCEPTANCE_CRITERIA = "NO_ACCEPTANCE_CRITERIA"
    # V2-specific
    CRITERION_EVIDENCED_UNREVIEWED = "CRITERION_EVIDENCED_UNREVIEWED"
    SUFFICIENCY_REVIEW_BINDING_MISMATCH = "SUFFICIENCY_REVIEW_BINDING_MISMATCH"


# ---------------------------------------------------------------------------
# V2 dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriterionResultV2:
    criterion_id: str
    scoped_criterion_id: str
    status: CriterionStatusV2
    evidence_node_ids: tuple[str, ...]
    blocking_reasons: tuple[CriterionReasonV2, ...]


@dataclass(frozen=True, slots=True)
class ConvergenceReportV2:
    schema_version: int
    convergence_id: str

    task_id: str
    intent_digest: str
    intent_revision: int

    source_base_sha: str
    subject_sha: str

    requirements_gate_id: str
    analysis_id: str
    evidence_bundle_id: str
    sufficiency_review_id: str | None  # None if no review provided

    status: ConvergenceStatus

    criterion_results: tuple[CriterionResultV2, ...]
    required_gate_results: tuple[RequiredGateResult, ...]
    blocking_reasons: tuple[ConvergenceBlockingReasonV2, ...]

    is_high_risk: bool  # True when intent task_class == HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION


def _compute_convergence_id_v2(payload: dict[str, Any]) -> str:
    return _compute_digest({
        "schema_version": CONVERGENCE_REPORT_SCHEMA_VERSION_V2,
        "task_id": payload["task_id"],
        "intent_digest": payload["intent_digest"],
        "intent_revision": payload["intent_revision"],
        "source_base_sha": payload["source_base_sha"],
        "subject_sha": payload["subject_sha"],
        "requirements_gate_id": payload["requirements_gate_id"],
        "analysis_id": payload["analysis_id"],
        "evidence_bundle_id": payload["evidence_bundle_id"],
        "sufficiency_review_id": payload["sufficiency_review_id"],
        "status": payload["status"],
        "criterion_results": payload["criterion_results"],
        "required_gate_results": payload["required_gate_results"],
        "blocking_reasons": payload["blocking_reasons"],
        "is_high_risk": payload["is_high_risk"],
    })


def serialize_convergence_report_v2(report: ConvergenceReportV2) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "convergence_id": report.convergence_id,
        "task_id": report.task_id,
        "intent_digest": report.intent_digest,
        "intent_revision": report.intent_revision,
        "source_base_sha": report.source_base_sha,
        "subject_sha": report.subject_sha,
        "requirements_gate_id": report.requirements_gate_id,
        "analysis_id": report.analysis_id,
        "evidence_bundle_id": report.evidence_bundle_id,
        "sufficiency_review_id": report.sufficiency_review_id,
        "status": report.status.value,
        "criterion_results": [
            {
                "criterion_id": c.criterion_id,
                "scoped_criterion_id": c.scoped_criterion_id,
                "status": c.status.value,
                "evidence_node_ids": list(c.evidence_node_ids),
                "blocking_reasons": [r.value for r in c.blocking_reasons],
            }
            for c in report.criterion_results
        ],
        "required_gate_results": [
            {"gate_name": g.gate_name, "status": g.status.value}
            for g in report.required_gate_results
        ],
        "blocking_reasons": [r.value for r in report.blocking_reasons],
        "is_high_risk": report.is_high_risk,
    }


# ---------------------------------------------------------------------------
# Core v2 evaluation logic
# ---------------------------------------------------------------------------


def _get_sufficiency_for_criterion(
    criterion_id: str,
    task_id: str,
    sufficiency_review: Any | None,
) -> str | None:
    """Return the sufficiency status string for a criterion, or None if not found."""
    if sufficiency_review is None:
        return None
    # Import here to avoid circular import at module level
    from ai_engineering.sufficiency_review import (
        EvidenceSufficiencyReview,
        SufficiencyStatus,
    )
    if not isinstance(sufficiency_review, EvidenceSufficiencyReview):
        return None
    for cr in sufficiency_review.criterion_reviews:
        if cr.criterion_id == criterion_id:
            return cr.status.value
    return None


def evaluate_convergence_v2(
    intent: TaskIntent,
    clarification: ClarificationReport,
    quality_review: RequirementsQualityReview,
    lineage: TaskLineage,
    bundle: EvidenceBundle,
    expected_base_sha: str,
    subject_sha: str,
    sufficiency_review: Any | None = None,
) -> ConvergenceReportV2:
    """Evaluate evidence-bound convergence with v2 production-sensitive semantics.

    Parameters
    ----------
    sufficiency_review:
        Optional EvidenceSufficiencyReview.  For HIGH_RISK tasks, absence of a
        matching sufficiency review with status PASS causes criterion state
        EVIDENCED_UNREVIEWED (blocking) instead of SATISFIED.

    V1 compatibility
    ----------------
    evaluate_convergence() in convergence.py is NOT called or modified.
    V2 has its own separate evaluation path.
    """
    from ai_engineering.contracts import TaskClass as TC

    validate_intent(intent)
    validate_lineage(lineage)
    bundle = validate_evidence_bundle(bundle)

    if _sha(expected_base_sha) != expected_base_sha:
        raise ConvergenceError("INVALID_SUBJECT_SHA")
    if _sha(subject_sha) != subject_sha:
        raise ConvergenceError("INVALID_SUBJECT_SHA")

    is_high_risk = (intent.task_class == TC.HIGH_RISK_PRODUCTION_DEPLOYMENT_OR_MIGRATION)

    # Validate sufficiency review binding if provided
    if sufficiency_review is not None:
        from ai_engineering.sufficiency_review import EvidenceSufficiencyReview
        if isinstance(sufficiency_review, EvidenceSufficiencyReview):
            from ai_engineering.sufficiency_review import validate_sufficiency_review
            try:
                sufficiency_review = validate_sufficiency_review(sufficiency_review)
            except Exception:
                sufficiency_review = None

    blocking_reasons: list[ConvergenceBlockingReasonV2] = []

    # Bundle binding checks
    if bundle.task_id != intent.task_id:
        blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_TASK_MISMATCH)
    if bundle.intent_digest != intent_digest(intent):
        blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_INTENT_MISMATCH)
    if bundle.subject_sha != subject_sha:
        blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_SUBJECT_SHA_MISMATCH)

    # Sufficiency review binding check
    sufficiency_review_id: str | None = None
    if sufficiency_review is not None:
        from ai_engineering.sufficiency_review import EvidenceSufficiencyReview
        if isinstance(sufficiency_review, EvidenceSufficiencyReview):
            sr = sufficiency_review
            if (sr.task_id != intent.task_id or
                    sr.intent_digest != intent_digest(intent) or
                    sr.evidence_bundle_id != bundle.bundle_id or
                    sr.subject_sha != subject_sha):
                blocking_reasons.append(
                    ConvergenceBlockingReasonV2.SUFFICIENCY_REVIEW_BINDING_MISMATCH
                )
                sufficiency_review = None
            else:
                sufficiency_review_id = sr.review_id

    # Requirements gate
    try:
        requirements_report = evaluate_requirements_gate(intent, clarification, quality_review)
        requirements_gate_id = requirements_report.gate_id
        if requirements_report.status.value != "PASS":
            blocking_reasons.append(ConvergenceBlockingReasonV2.REQUIREMENTS_GATE_NOT_PASSING)
    except Exception:
        requirements_gate_id = hashlib.sha256(b"error").hexdigest()
        blocking_reasons.append(ConvergenceBlockingReasonV2.REQUIREMENTS_GATE_NOT_PASSING)

    # Analysis recomputation
    try:
        analysis_report = analyze(intent, lineage, expected_base_sha=expected_base_sha)
        analysis_id = analysis_report.analysis_id
        if analysis_report.has_errors:
            blocking_reasons.append(ConvergenceBlockingReasonV2.ANALYSIS_ERROR_PRESENT)
    except Exception:
        analysis_id = hashlib.sha256(b"error").hexdigest()
        blocking_reasons.append(ConvergenceBlockingReasonV2.ANALYSIS_ERROR_PRESENT)

    if bundle.analysis_id != analysis_id:
        blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_ANALYSIS_MISMATCH)

    # Build observation maps
    lineage_obs: dict[str, Any] = {}
    gate_obs: dict[str, Any] = {}
    for obs in bundle.observations:
        if obs.target_kind == TargetKind.LINEAGE_EVIDENCE:
            lineage_obs[obs.target_id] = obs
        elif obs.target_kind == TargetKind.REQUIRED_GATE:
            gate_obs[obs.target_id] = obs

    # Evidence target validation
    lineage_evidence_ids = {n.node_id for n in lineage.nodes if n.kind == NodeKind.EVIDENCE}
    for ev_id in lineage_obs:
        if ev_id not in lineage_evidence_ids:
            blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_TARGET_UNKNOWN)

    intent_gate_names = set(intent.required_gates)
    for gate_name in gate_obs:
        if gate_name not in intent_gate_names:
            blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_TARGET_UNKNOWN)

    # Acceptance criteria evaluation (v2 state machine)
    criterion_results: list[CriterionResultV2] = []
    if not intent.acceptance_criteria:
        blocking_reasons.append(ConvergenceBlockingReasonV2.NO_ACCEPTANCE_CRITERIA)

    for criterion in intent.acceptance_criteria:
        scoped_cid = f"{intent.task_id}::{criterion.criterion_id}"

        # Collect direct evidence nodes
        direct_evidence_nodes: list[str] = []
        for e in lineage.edges:
            if e.relation == RelationKind.VERIFIES and e.target_id == scoped_cid:
                for n in lineage.nodes:
                    if n.node_id == e.source_id and n.kind == NodeKind.EVIDENCE:
                        direct_evidence_nodes.append(n.node_id)

        # Collect task evidence nodes
        implementing_tasks: list[str] = []
        for e in lineage.edges:
            if e.relation == RelationKind.IMPLEMENTS and e.target_id == scoped_cid:
                implementing_tasks.append(e.source_id)

        task_evidence_nodes: dict[str, list[str]] = {}
        for t_id in implementing_tasks:
            task_evidence_nodes[t_id] = []
            for e in lineage.edges:
                if e.relation == RelationKind.VERIFIES and e.target_id == t_id:
                    for n in lineage.nodes:
                        if n.node_id == e.source_id and n.kind == NodeKind.EVIDENCE:
                            task_evidence_nodes[t_id].append(n.node_id)

        all_evidence_ids: set[str] = set(direct_evidence_nodes)
        for nodes in task_evidence_nodes.values():
            all_evidence_ids.update(nodes)

        # Apply v2 state machine
        c_status = CriterionStatusV2.UNEVIDENCED
        c_reasons: list[CriterionReasonV2] = []

        # Check for any FAIL anywhere (dominates)
        any_fail_anywhere = False
        for e_id in all_evidence_ids:
            if e_id in lineage_obs and lineage_obs[e_id].outcome == ObservationOutcome.FAIL:
                any_fail_anywhere = True

        if any_fail_anywhere:
            c_reasons.append(CriterionReasonV2.FAILED_EVIDENCE)
            c_status = CriterionStatusV2.FAILED
        else:
            # Check for PASS evidence
            direct_pass = False
            has_direct_obs = False
            for e_id in direct_evidence_nodes:
                if e_id in lineage_obs:
                    has_direct_obs = True
                    if lineage_obs[e_id].outcome == ObservationOutcome.PASS:
                        direct_pass = True

            all_tasks_pass = bool(implementing_tasks)
            any_task_fail = False
            task_obs_missing = False
            for t_id in implementing_tasks:
                task_ev_ids = task_evidence_nodes[t_id]
                t_pass = False
                if not task_ev_ids:
                    all_tasks_pass = False
                for e_id in task_ev_ids:
                    if e_id in lineage_obs:
                        obs = lineage_obs[e_id]
                        if obs.outcome == ObservationOutcome.FAIL:
                            any_task_fail = True
                        elif obs.outcome == ObservationOutcome.PASS:
                            t_pass = True
                    else:
                        task_obs_missing = True
                if not t_pass:
                    all_tasks_pass = False

            evidence_pass = direct_pass or (not any_task_fail and all_tasks_pass)

            if not evidence_pass:
                # No clear PASS evidence — check inconclusive
                has_inconclusive = False
                for e_id in all_evidence_ids:
                    if e_id in lineage_obs and lineage_obs[e_id].outcome == ObservationOutcome.INCONCLUSIVE:
                        has_inconclusive = True
                if has_inconclusive:
                    c_reasons.append(CriterionReasonV2.EVIDENCE_INCONCLUSIVE)
                    c_status = CriterionStatusV2.INCONCLUSIVE
                else:
                    if direct_evidence_nodes and not has_direct_obs:
                        c_reasons.append(CriterionReasonV2.EVIDENCE_OBSERVATION_MISSING)
                    elif not direct_evidence_nodes and not implementing_tasks:
                        c_reasons.append(CriterionReasonV2.DIRECT_EVIDENCE_MISSING)
                    else:
                        c_reasons.append(CriterionReasonV2.TASK_EVIDENCE_INCOMPLETE)
                    c_status = CriterionStatusV2.UNEVIDENCED
            else:
                # PASS evidence found — apply v2 sufficiency check
                if direct_pass:
                    c_reasons.append(CriterionReasonV2.SATISFIED_BY_DIRECT_EVIDENCE)
                else:
                    c_reasons.append(CriterionReasonV2.SATISFIED_BY_TASK_EVIDENCE)

                # Get sufficiency for this criterion
                suff_status = _get_sufficiency_for_criterion(
                    criterion.criterion_id, intent.task_id, sufficiency_review
                )

                if suff_status == "PASS":
                    c_reasons.append(CriterionReasonV2.SUFFICIENCY_REVIEW_PASS)
                    c_status = CriterionStatusV2.SATISFIED
                elif suff_status == "FAIL":
                    c_reasons.append(CriterionReasonV2.SUFFICIENCY_REVIEW_FAIL)
                    c_status = CriterionStatusV2.FAILED
                elif suff_status in ("INCONCLUSIVE", "INSUFFICIENT_EVIDENCE"):
                    if suff_status == "INCONCLUSIVE":
                        c_reasons.append(CriterionReasonV2.SUFFICIENCY_REVIEW_INCONCLUSIVE)
                    else:
                        c_reasons.append(CriterionReasonV2.SUFFICIENCY_REVIEW_INSUFFICIENT)
                    if is_high_risk:
                        c_status = CriterionStatusV2.EVIDENCED_UNREVIEWED
                    else:
                        c_status = CriterionStatusV2.SATISFIED
                else:
                    # No sufficiency review or criterion not covered
                    if is_high_risk:
                        c_reasons.append(CriterionReasonV2.SUFFICIENCY_REVIEW_REQUIRED)
                        c_status = CriterionStatusV2.EVIDENCED_UNREVIEWED
                    else:
                        c_status = CriterionStatusV2.SATISFIED

        # Map criterion status to blocking reasons
        if c_status == CriterionStatusV2.FAILED:
            if ConvergenceBlockingReasonV2.CRITERION_FAILED not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReasonV2.CRITERION_FAILED)
        elif c_status == CriterionStatusV2.UNEVIDENCED:
            if ConvergenceBlockingReasonV2.CRITERION_UNEVIDENCED not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReasonV2.CRITERION_UNEVIDENCED)
        elif c_status == CriterionStatusV2.EVIDENCED_UNREVIEWED and is_high_risk:
            if ConvergenceBlockingReasonV2.CRITERION_EVIDENCED_UNREVIEWED not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReasonV2.CRITERION_EVIDENCED_UNREVIEWED)
        elif c_status == CriterionStatusV2.INCONCLUSIVE:
            if ConvergenceBlockingReasonV2.EVIDENCE_INCONCLUSIVE not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_INCONCLUSIVE)

        criterion_results.append(CriterionResultV2(
            criterion_id=criterion.criterion_id,
            scoped_criterion_id=scoped_cid,
            status=c_status,
            evidence_node_ids=tuple(sorted(all_evidence_ids)),
            blocking_reasons=tuple(sorted(set(c_reasons))),
        ))

    # Required gates (unchanged from v1)
    required_gate_results: list[RequiredGateResult] = []
    for gate_name in sorted(intent_gate_names):
        if gate_name in gate_obs:
            obs = gate_obs[gate_name]
            if obs.outcome == ObservationOutcome.PASS:
                gstatus = GateEvidenceStatus.PASS
            elif obs.outcome == ObservationOutcome.FAIL:
                gstatus = GateEvidenceStatus.FAIL
                blocking_reasons.append(ConvergenceBlockingReasonV2.REQUIRED_GATE_FAILED)
            else:
                gstatus = GateEvidenceStatus.INCONCLUSIVE
                blocking_reasons.append(ConvergenceBlockingReasonV2.REQUIRED_GATE_INCONCLUSIVE)
        else:
            gstatus = GateEvidenceStatus.MISSING
            blocking_reasons.append(ConvergenceBlockingReasonV2.REQUIRED_GATE_EVIDENCE_MISSING)
        required_gate_results.append(RequiredGateResult(gate_name=gate_name, status=gstatus))

    # Global evidence checks
    for obs in bundle.observations:
        if obs.outcome == ObservationOutcome.FAIL:
            if ConvergenceBlockingReasonV2.EVIDENCE_FAILED not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_FAILED)
        elif obs.outcome == ObservationOutcome.INCONCLUSIVE:
            if ConvergenceBlockingReasonV2.EVIDENCE_INCONCLUSIVE not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReasonV2.EVIDENCE_INCONCLUSIVE)

    final_status = (
        ConvergenceStatus.NOT_CONVERGED
        if blocking_reasons
        else ConvergenceStatus.CONVERGED
    )

    c_id_payload = {
        "task_id": intent.task_id,
        "intent_digest": intent_digest(intent),
        "intent_revision": intent.intent_revision,
        "source_base_sha": intent.source_base_sha,
        "subject_sha": subject_sha,
        "requirements_gate_id": requirements_gate_id,
        "analysis_id": analysis_id,
        "evidence_bundle_id": bundle.bundle_id,
        "sufficiency_review_id": sufficiency_review_id,
        "status": final_status.value,
        "criterion_results": [
            {
                "criterion_id": c.criterion_id,
                "scoped_criterion_id": c.scoped_criterion_id,
                "status": c.status.value,
                "evidence_node_ids": list(c.evidence_node_ids),
                "blocking_reasons": [r.value for r in c.blocking_reasons],
            }
            for c in criterion_results
        ],
        "required_gate_results": [
            {"gate_name": g.gate_name, "status": g.status.value}
            for g in required_gate_results
        ],
        "blocking_reasons": [r.value for r in sorted(set(blocking_reasons))],
        "is_high_risk": is_high_risk,
    }
    convergence_id = _compute_convergence_id_v2(c_id_payload)

    return ConvergenceReportV2(
        schema_version=CONVERGENCE_REPORT_SCHEMA_VERSION_V2,
        convergence_id=convergence_id,
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        source_base_sha=intent.source_base_sha,
        subject_sha=subject_sha,
        requirements_gate_id=requirements_gate_id,
        analysis_id=analysis_id,
        evidence_bundle_id=bundle.bundle_id,
        sufficiency_review_id=sufficiency_review_id,
        status=final_status,
        criterion_results=tuple(criterion_results),
        required_gate_results=tuple(required_gate_results),
        blocking_reasons=tuple(sorted(set(blocking_reasons))),
        is_high_risk=is_high_risk,
    )
