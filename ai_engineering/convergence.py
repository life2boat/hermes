"""Deterministic Evidence-Bound Convergence."""

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
    TaskLineage,
    NodeKind,
    RelationKind,
    intent_digest,
    validate_intent,
    validate_lineage,
)
from ai_engineering.requirements_gate import (
    ClarificationReport,
    RequirementsQualityReview,
    evaluate_requirements_gate,
)
from ai_engineering.task_analysis import (
    analyze,
)

EVIDENCE_BUNDLE_SCHEMA_VERSION = 1
CONVERGENCE_REPORT_SCHEMA_VERSION = 1

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TargetKind(StrEnum):
    LINEAGE_EVIDENCE = "LINEAGE_EVIDENCE"
    REQUIRED_GATE = "REQUIRED_GATE"


class ObservationOutcome(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class ConvergenceStatus(StrEnum):
    CONVERGED = "CONVERGED"
    NOT_CONVERGED = "NOT_CONVERGED"


class CriterionStatus(StrEnum):
    SATISFIED = "SATISFIED"
    FAILED = "FAILED"
    UNEVIDENCED = "UNEVIDENCED"


class GateEvidenceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    MISSING = "MISSING"


class ConvergenceBlockingReason(StrEnum):
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


class CriterionReason(StrEnum):
    FAILED_EVIDENCE = "FAILED_EVIDENCE"
    DIRECT_EVIDENCE_MISSING = "DIRECT_EVIDENCE_MISSING"
    TASK_EVIDENCE_INCOMPLETE = "TASK_EVIDENCE_INCOMPLETE"
    EVIDENCE_OBSERVATION_MISSING = "EVIDENCE_OBSERVATION_MISSING"
    EVIDENCE_INCONCLUSIVE = "EVIDENCE_INCONCLUSIVE"
    SATISFIED_BY_DIRECT_EVIDENCE = "SATISFIED_BY_DIRECT_EVIDENCE"
    SATISFIED_BY_TASK_EVIDENCE = "SATISFIED_BY_TASK_EVIDENCE"


_EnumT = TypeVar("_EnumT")


# ---------------------------------------------------------------------------
# Exceptions & Helpers
# ---------------------------------------------------------------------------


class ConvergenceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> NoReturn:
    raise ConvergenceError(code)


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
    if not isinstance(value, str) or not value.strip():
        _fail("VALUE_INVALID")
    return value


def _sha(value: object) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        _fail("VALUE_INVALID")
    return value


def _items(value: object) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
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
# EvidenceBundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    observation_id: str
    target_kind: TargetKind
    target_id: str
    outcome: ObservationOutcome
    producer_id: str
    artifact_ref: str
    artifact_digest: str


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    schema_version: int
    bundle_id: str
    task_id: str
    intent_digest: str
    analysis_id: str
    subject_sha: str
    observations: tuple[EvidenceObservation, ...]


_OBSERVATION_FIELDS = frozenset({
    "observation_id",
    "target_kind",
    "target_id",
    "outcome",
    "producer_id",
    "artifact_ref",
    "artifact_digest",
})

_BUNDLE_FIELDS = frozenset({
    "schema_version",
    "bundle_id",
    "task_id",
    "intent_digest",
    "analysis_id",
    "subject_sha",
    "observations",
})


def _compute_observation_id(obs: dict[str, Any]) -> str:
    payload = {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "target_kind": obs["target_kind"],
        "target_id": obs["target_id"],
        "outcome": obs["outcome"],
        "producer_id": obs["producer_id"],
        "artifact_ref": obs["artifact_ref"],
        "artifact_digest": obs["artifact_digest"],
    }
    return _compute_digest(payload)


def _compute_bundle_id(payload: dict[str, Any]) -> str:
    return _compute_digest({
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "task_id": payload["task_id"],
        "intent_digest": payload["intent_digest"],
        "analysis_id": payload["analysis_id"],
        "subject_sha": payload["subject_sha"],
        "observations": [
            {
                "observation_id": obs["observation_id"],
            }
            for obs in payload["observations"]
        ],
    })


def deserialize_evidence_bundle(value: Mapping[str, object]) -> EvidenceBundle:
    raw_version = value.get("schema_version")
    if raw_version != EVIDENCE_BUNDLE_SCHEMA_VERSION or isinstance(raw_version, bool):
        _fail("UNKNOWN_EVIDENCE_SCHEMA_VERSION")

    payload = _exact_fields(value, _BUNDLE_FIELDS)
    task_id = _identifier(payload["task_id"])
    intent_dgst = _digest(payload["intent_digest"])
    analysis_id = _digest(payload["analysis_id"])
    subject_sha = _sha(payload["subject_sha"])

    observations: list[EvidenceObservation] = []
    seen_targets = set()
    for item in _items(payload["observations"]):
        obs_payload = _exact_fields(item, _OBSERVATION_FIELDS)
        target_kind = _enum(obs_payload["target_kind"], TargetKind, "VALUE_INVALID")
        target_id = (
            _identifier(obs_payload["target_id"])
            if target_kind == TargetKind.LINEAGE_EVIDENCE
            else _string(obs_payload["target_id"])
        )

        target_key = (target_kind, target_id)
        if target_key in seen_targets:
            _fail("DUPLICATE_EVIDENCE_TARGET")
        seen_targets.add(target_key)

        outcome = _enum(obs_payload["outcome"], ObservationOutcome, "VALUE_INVALID")
        producer_id = _string(obs_payload["producer_id"])
        artifact_ref = _string(obs_payload["artifact_ref"])
        artifact_digest = _digest(obs_payload["artifact_digest"])

        expected_obs_id = _compute_observation_id({
            "target_kind": target_kind.value,
            "target_id": target_id,
            "outcome": outcome.value,
            "producer_id": producer_id,
            "artifact_ref": artifact_ref,
            "artifact_digest": artifact_digest,
        })
        if expected_obs_id != obs_payload["observation_id"]:
            _fail("TAMPERED_OBSERVATION_ID")

        observations.append(
            EvidenceObservation(
                observation_id=expected_obs_id,
                target_kind=target_kind,
                target_id=target_id,
                outcome=outcome,
                producer_id=producer_id,
                artifact_ref=artifact_ref,
                artifact_digest=artifact_digest,
            )
        )

    expected_bundle_id = _compute_bundle_id({
        "task_id": task_id,
        "intent_digest": intent_dgst,
        "analysis_id": analysis_id,
        "subject_sha": subject_sha,
        "observations": [{"observation_id": o.observation_id} for o in observations],
    })

    if expected_bundle_id != payload["bundle_id"]:
        _fail("TAMPERED_BUNDLE_ID")

    return EvidenceBundle(
        schema_version=EVIDENCE_BUNDLE_SCHEMA_VERSION,
        bundle_id=expected_bundle_id,
        task_id=task_id,
        intent_digest=intent_dgst,
        analysis_id=analysis_id,
        subject_sha=subject_sha,
        observations=tuple(observations),
    )


def serialize_evidence_bundle(bundle: EvidenceBundle) -> dict[str, Any]:
    return {
        "schema_version": bundle.schema_version,
        "bundle_id": bundle.bundle_id,
        "task_id": bundle.task_id,
        "intent_digest": bundle.intent_digest,
        "analysis_id": bundle.analysis_id,
        "subject_sha": bundle.subject_sha,
        "observations": [
            {
                "observation_id": o.observation_id,
                "target_kind": o.target_kind.value,
                "target_id": o.target_id,
                "outcome": o.outcome.value,
                "producer_id": o.producer_id,
                "artifact_ref": o.artifact_ref,
                "artifact_digest": o.artifact_digest,
            }
            for o in bundle.observations
        ],
    }


# ---------------------------------------------------------------------------
# ConvergenceReport
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion_id: str
    scoped_criterion_id: str
    status: CriterionStatus
    evidence_node_ids: tuple[str, ...]
    blocking_reasons: tuple[CriterionReason, ...]


@dataclass(frozen=True, slots=True)
class RequiredGateResult:
    gate_name: str
    status: GateEvidenceStatus


@dataclass(frozen=True, slots=True)
class ConvergenceReport:
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

    status: ConvergenceStatus

    criterion_results: tuple[CriterionResult, ...]
    required_gate_results: tuple[RequiredGateResult, ...]
    blocking_reasons: tuple[ConvergenceBlockingReason, ...]


_CRITERION_RESULT_FIELDS = frozenset({
    "criterion_id",
    "scoped_criterion_id",
    "status",
    "evidence_node_ids",
    "blocking_reasons",
})

_GATE_RESULT_FIELDS = frozenset({"gate_name", "status"})

_CONVERGENCE_FIELDS = frozenset({
    "schema_version",
    "convergence_id",
    "task_id",
    "intent_digest",
    "intent_revision",
    "source_base_sha",
    "subject_sha",
    "requirements_gate_id",
    "analysis_id",
    "evidence_bundle_id",
    "status",
    "criterion_results",
    "required_gate_results",
    "blocking_reasons",
})


def _compute_convergence_id(payload: dict[str, Any]) -> str:
    return _compute_digest({
        "schema_version": CONVERGENCE_REPORT_SCHEMA_VERSION,
        "task_id": payload["task_id"],
        "intent_digest": payload["intent_digest"],
        "intent_revision": payload["intent_revision"],
        "source_base_sha": payload["source_base_sha"],
        "subject_sha": payload["subject_sha"],
        "requirements_gate_id": payload["requirements_gate_id"],
        "analysis_id": payload["analysis_id"],
        "evidence_bundle_id": payload["evidence_bundle_id"],
        "status": payload["status"],
        "criterion_results": payload["criterion_results"],
        "required_gate_results": payload["required_gate_results"],
        "blocking_reasons": payload["blocking_reasons"],
    })


def deserialize_convergence_report(value: Mapping[str, object]) -> ConvergenceReport:
    raw_version = value.get("schema_version")
    if raw_version != CONVERGENCE_REPORT_SCHEMA_VERSION or isinstance(
        raw_version, bool
    ):
        _fail("UNKNOWN_CONVERGENCE_SCHEMA_VERSION")

    payload = _exact_fields(value, _CONVERGENCE_FIELDS)
    task_id = _identifier(payload["task_id"])
    intent_dgst = _digest(payload["intent_digest"])
    intent_rev = payload["intent_revision"]
    if not isinstance(intent_rev, int) or intent_rev < 1:
        _fail("VALUE_INVALID")
    source_base_sha = _sha(payload["source_base_sha"])
    subject_sha = _sha(payload["subject_sha"])
    requirements_gate_id = _digest(payload["requirements_gate_id"])
    analysis_id = _digest(payload["analysis_id"])
    evidence_bundle_id = _digest(payload["evidence_bundle_id"])
    status = _enum(payload["status"], ConvergenceStatus, "VALUE_INVALID")

    criterion_results: list[CriterionResult] = []
    for item in _items(payload["criterion_results"]):
        crit_payload = _exact_fields(item, _CRITERION_RESULT_FIELDS)
        criterion_results.append(
            CriterionResult(
                criterion_id=_string(crit_payload["criterion_id"]),
                scoped_criterion_id=_string(crit_payload["scoped_criterion_id"]),
                status=_enum(crit_payload["status"], CriterionStatus, "VALUE_INVALID"),
                evidence_node_ids=tuple(
                    _identifier(x) for x in _items(crit_payload["evidence_node_ids"])
                ),
                blocking_reasons=tuple(
                    _enum(x, CriterionReason, "VALUE_INVALID")
                    for x in _items(crit_payload["blocking_reasons"])
                ),
            )
        )

    required_gate_results: list[RequiredGateResult] = []
    for item in _items(payload["required_gate_results"]):
        gate_payload = _exact_fields(item, _GATE_RESULT_FIELDS)
        required_gate_results.append(
            RequiredGateResult(
                gate_name=_string(gate_payload["gate_name"]),
                status=_enum(
                    gate_payload["status"], GateEvidenceStatus, "VALUE_INVALID"
                ),
            )
        )

    blocking_reasons = tuple(
        _enum(x, ConvergenceBlockingReason, "VALUE_INVALID")
        for x in _items(payload["blocking_reasons"])
    )

    c_id_payload = {
        "task_id": task_id,
        "intent_digest": intent_dgst,
        "intent_revision": intent_rev,
        "source_base_sha": source_base_sha,
        "subject_sha": subject_sha,
        "requirements_gate_id": requirements_gate_id,
        "analysis_id": analysis_id,
        "evidence_bundle_id": evidence_bundle_id,
        "status": status.value,
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
        "blocking_reasons": [r.value for r in blocking_reasons],
    }

    expected_convergence_id = _compute_convergence_id(c_id_payload)
    if expected_convergence_id != payload["convergence_id"]:
        _fail("TAMPERED_CONVERGENCE_ID")

    return ConvergenceReport(
        schema_version=CONVERGENCE_REPORT_SCHEMA_VERSION,
        convergence_id=expected_convergence_id,
        task_id=task_id,
        intent_digest=intent_dgst,
        intent_revision=intent_rev,
        source_base_sha=source_base_sha,
        subject_sha=subject_sha,
        requirements_gate_id=requirements_gate_id,
        analysis_id=analysis_id,
        evidence_bundle_id=evidence_bundle_id,
        status=status,
        criterion_results=tuple(criterion_results),
        required_gate_results=tuple(required_gate_results),
        blocking_reasons=blocking_reasons,
    )


def serialize_convergence_report(report: ConvergenceReport) -> dict[str, Any]:
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
    }


# ---------------------------------------------------------------------------
# Core Logic
# ---------------------------------------------------------------------------


def evaluate_convergence(
    intent: TaskIntent,
    clarification: ClarificationReport,
    quality_review: RequirementsQualityReview,
    lineage: TaskLineage,
    bundle: EvidenceBundle,
    expected_base_sha: str,
    subject_sha: str,
) -> ConvergenceReport:
    """Evaluates evidence-bound convergence of a task."""
    validate_intent(intent)
    validate_lineage(lineage)
    if _sha(expected_base_sha) != expected_base_sha:
        _fail("INVALID_SUBJECT_SHA")
    if _sha(subject_sha) != subject_sha:
        _fail("INVALID_SUBJECT_SHA")

    blocking_reasons: list[ConvergenceBlockingReason] = []

    # Check bundle binding
    if bundle.task_id != intent.task_id:
        blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_TASK_MISMATCH)
    if bundle.intent_digest != intent_digest(intent):
        blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_INTENT_MISMATCH)
    if bundle.subject_sha != subject_sha:
        blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_SUBJECT_SHA_MISMATCH)

    # 1. PR-3 Requirements Gate Recomputation
    try:
        requirements_report = evaluate_requirements_gate(
            intent, clarification, quality_review
        )
        requirements_gate_id = requirements_report.gate_id
        if requirements_report.status.value != "PASS":
            blocking_reasons.append(
                ConvergenceBlockingReason.REQUIREMENTS_GATE_NOT_PASSING
            )
    except Exception:
        requirements_gate_id = _digest(hashlib.sha256(b"error").hexdigest())
        blocking_reasons.append(ConvergenceBlockingReason.REQUIREMENTS_GATE_NOT_PASSING)

    # 2. PR-2 Analysis Recomputation
    try:
        analysis_report = analyze(intent, lineage, expected_base_sha=expected_base_sha)
        analysis_id = analysis_report.analysis_id
        if analysis_report.has_errors:
            blocking_reasons.append(ConvergenceBlockingReason.ANALYSIS_ERROR_PRESENT)
    except Exception:
        analysis_id = _digest(hashlib.sha256(b"error").hexdigest())
        blocking_reasons.append(ConvergenceBlockingReason.ANALYSIS_ERROR_PRESENT)

    if bundle.analysis_id != analysis_id:
        blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_ANALYSIS_MISMATCH)

    # Setup observations map
    lineage_obs: dict[str, EvidenceObservation] = {}
    gate_obs: dict[str, EvidenceObservation] = {}

    for obs in bundle.observations:
        if obs.target_kind == TargetKind.LINEAGE_EVIDENCE:
            lineage_obs[obs.target_id] = obs
        elif obs.target_kind == TargetKind.REQUIRED_GATE:
            gate_obs[obs.target_id] = obs

    # 3. Evidence Target Validation
    lineage_evidence_ids = {
        n.node_id for n in lineage.nodes if n.kind == NodeKind.EVIDENCE
    }
    for ev_id in lineage_obs:
        if ev_id not in lineage_evidence_ids:
            blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_TARGET_UNKNOWN)

    intent_gate_names = set(intent.required_gates)
    for gate_name in gate_obs:
        if gate_name not in intent_gate_names:
            blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_TARGET_UNKNOWN)

    # 4. Acceptance Criteria Aggregation
    criterion_results: list[CriterionResult] = []
    if not intent.acceptance_criteria:
        blocking_reasons.append(ConvergenceBlockingReason.NO_ACCEPTANCE_CRITERIA)

    for criterion in intent.acceptance_criteria:
        scoped_cid = f"{intent.task_id}::{criterion.criterion_id}"

        # Collect direct evidence (EVIDENCE -> VERIFIES -> CRITERION)
        direct_evidence_nodes = []
        for e in lineage.edges:
            if e.relation == RelationKind.VERIFIES and e.target_id == scoped_cid:
                for n in lineage.nodes:
                    if n.node_id == e.source_id and n.kind == NodeKind.EVIDENCE:
                        direct_evidence_nodes.append(n.node_id)

        # Collect task evidence (TASK -> IMPLEMENTS -> CRITERION, EVIDENCE -> VERIFIES -> TASK)
        implementing_tasks = []
        for e in lineage.edges:
            if e.relation == RelationKind.IMPLEMENTS and e.target_id == scoped_cid:
                implementing_tasks.append(e.source_id)

        task_evidence_nodes = {}
        for t_id in implementing_tasks:
            task_evidence_nodes[t_id] = []
            for e in lineage.edges:
                if e.relation == RelationKind.VERIFIES and e.target_id == t_id:
                    for n in lineage.nodes:
                        if n.node_id == e.source_id and n.kind == NodeKind.EVIDENCE:
                            task_evidence_nodes[t_id].append(n.node_id)

        all_evidence_ids = set(direct_evidence_nodes)
        for nodes in task_evidence_nodes.values():
            all_evidence_ids.update(nodes)

        c_status = CriterionStatus.UNEVIDENCED
        c_reasons: list[CriterionReason] = []

        # Check direct evidence
        direct_pass = False
        has_direct_fail = False
        has_direct_obs = False

        for e_id in direct_evidence_nodes:
            if e_id in lineage_obs:
                has_direct_obs = True
                obs = lineage_obs[e_id]
                if obs.outcome == ObservationOutcome.FAIL:
                    has_direct_fail = True
                elif obs.outcome == ObservationOutcome.PASS:
                    direct_pass = True

        if has_direct_fail:
            c_reasons.append(CriterionReason.FAILED_EVIDENCE)
            c_status = CriterionStatus.FAILED
        elif direct_pass:
            c_reasons.append(CriterionReason.SATISFIED_BY_DIRECT_EVIDENCE)
            c_status = CriterionStatus.SATISFIED
        else:
            if direct_evidence_nodes and not has_direct_obs:
                c_reasons.append(CriterionReason.EVIDENCE_OBSERVATION_MISSING)
            elif direct_evidence_nodes and has_direct_obs:
                c_reasons.append(CriterionReason.EVIDENCE_INCONCLUSIVE)
            else:
                c_reasons.append(CriterionReason.DIRECT_EVIDENCE_MISSING)

            # Fallback to task evidence
            all_tasks_pass = True
            any_task_fail = False
            task_obs_missing = False

            if not implementing_tasks:
                all_tasks_pass = False

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

            if any_task_fail:
                if CriterionReason.FAILED_EVIDENCE not in c_reasons:
                    c_reasons.append(CriterionReason.FAILED_EVIDENCE)
                c_status = CriterionStatus.FAILED
            elif all_tasks_pass:
                c_reasons.append(CriterionReason.SATISFIED_BY_TASK_EVIDENCE)
                c_status = CriterionStatus.SATISFIED
            else:
                if task_obs_missing:
                    if CriterionReason.EVIDENCE_OBSERVATION_MISSING not in c_reasons:
                        c_reasons.append(CriterionReason.EVIDENCE_OBSERVATION_MISSING)
                c_reasons.append(CriterionReason.TASK_EVIDENCE_INCOMPLETE)

        # Failure dominates all evidence for this criterion across both direct and task levels
        any_fail_anywhere = False
        for e_id in all_evidence_ids:
            if (
                e_id in lineage_obs
                and lineage_obs[e_id].outcome == ObservationOutcome.FAIL
            ):
                any_fail_anywhere = True

        if any_fail_anywhere:
            if CriterionReason.FAILED_EVIDENCE not in c_reasons:
                c_reasons.append(CriterionReason.FAILED_EVIDENCE)
            c_status = CriterionStatus.FAILED

        if c_status == CriterionStatus.FAILED:
            if ConvergenceBlockingReason.CRITERION_FAILED not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReason.CRITERION_FAILED)
        elif c_status == CriterionStatus.UNEVIDENCED:
            if ConvergenceBlockingReason.CRITERION_UNEVIDENCED not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReason.CRITERION_UNEVIDENCED)

        criterion_results.append(
            CriterionResult(
                criterion_id=criterion.criterion_id,
                scoped_criterion_id=scoped_cid,
                status=c_status,
                evidence_node_ids=tuple(sorted(all_evidence_ids)),
                blocking_reasons=tuple(sorted(set(c_reasons))),
            )
        )

    # 5. Required Gates Validation
    required_gate_results: list[RequiredGateResult] = []
    for gate_name in sorted(intent_gate_names):
        if gate_name in gate_obs:
            obs = gate_obs[gate_name]
            if obs.outcome == ObservationOutcome.PASS:
                status = GateEvidenceStatus.PASS
            elif obs.outcome == ObservationOutcome.FAIL:
                status = GateEvidenceStatus.FAIL
                blocking_reasons.append(ConvergenceBlockingReason.REQUIRED_GATE_FAILED)
            else:
                status = GateEvidenceStatus.INCONCLUSIVE
                blocking_reasons.append(
                    ConvergenceBlockingReason.REQUIRED_GATE_INCONCLUSIVE
                )
        else:
            status = GateEvidenceStatus.MISSING
            blocking_reasons.append(
                ConvergenceBlockingReason.REQUIRED_GATE_EVIDENCE_MISSING
            )

        required_gate_results.append(
            RequiredGateResult(
                gate_name=gate_name,
                status=status,
            )
        )

    # 6. Global Evidence Outcome Checks
    for obs in bundle.observations:
        if obs.outcome == ObservationOutcome.FAIL:
            if ConvergenceBlockingReason.EVIDENCE_FAILED not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_FAILED)
        elif obs.outcome == ObservationOutcome.INCONCLUSIVE:
            if ConvergenceBlockingReason.EVIDENCE_INCONCLUSIVE not in blocking_reasons:
                blocking_reasons.append(ConvergenceBlockingReason.EVIDENCE_INCONCLUSIVE)

    status = (
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
        "status": status.value,
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
    }
    convergence_id = _compute_convergence_id(c_id_payload)

    return ConvergenceReport(
        schema_version=CONVERGENCE_REPORT_SCHEMA_VERSION,
        convergence_id=convergence_id,
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        source_base_sha=intent.source_base_sha,
        subject_sha=subject_sha,
        requirements_gate_id=requirements_gate_id,
        analysis_id=analysis_id,
        evidence_bundle_id=bundle.bundle_id,
        status=status,
        criterion_results=tuple(criterion_results),
        required_gate_results=tuple(required_gate_results),
        blocking_reasons=tuple(sorted(set(blocking_reasons))),
    )
