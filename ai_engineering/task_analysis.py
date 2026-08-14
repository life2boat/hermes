"""Deterministic read-only Cross-Artifact Analyzer for TaskIntent / TaskLineage.

Contract
--------
* READ ONLY — never mutates input artifacts.
* OFFLINE    — zero provider calls, zero network calls.
* DETERMINISTIC — same inputs produce byte-equivalent canonical JSON output.
* SCHEMA_VERSION = 1

Analysis report schema v1 identifies cross-artifact inconsistencies between:
  TaskIntent, AcceptanceCriteria, TaskLineage nodes/edges, and source identity.

Rules implemented in schema v1
-------------------------------
ORPHAN_ACCEPTANCE_CRITERION   – criterion with no TASK→IMPLEMENTS edge
                                 (scoped identity <task_id>::<criterion_id>)
ORPHAN_EXECUTION_TASK          – TASK node with no IMPLEMENTS edge
ORPHAN_EVIDENCE                – EVIDENCE node with no VERIFIES edge
SOURCE_IDENTITY_MISMATCH      – intent.source_base_sha != expected_base_sha
                                 (when an independent expected SHA is supplied)
TASK_IDENTITY_INCONSISTENCY   – lineage INTENT node_id doesn't match task_id
                                 (task-identity consistency check, not SHA check)

Rules deferred (no canonical structured mapping in schema v1)
-------------------------------------------------------------
MUTATION_OUTSIDE_ALLOWED_SCOPE
  Reason: DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
  PR-1 TaskLineage has no canonical structured path-reference type.
  Inferring filesystem paths from arbitrary TASK node_ids (e.g. "feature/auth",
  "TASK/001") violates the deterministic structural-certainty requirement.

MUTATION_IN_FORBIDDEN_SCOPE
  Reason: DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
  Same reason as above.

REQUIRED_GATE_COVERAGE
  Reason: DEFERRED_DUE_TO_MISSING_CANONICAL_MAPPING
  PR-1 lineage schema defines no GATE node kind. Matching required_gate strings
  against arbitrary lineage node_ids creates an undocumented naming convention,
  not a provable structural mapping.

Not implemented in PR-2
-----------------------
Clarify, Converge, LLM-as-judge, auto-remediation, remote artifact fetch.

Criterion identity
------------------
Only canonical scoped identity is accepted:

    <task_id>::<criterion_id>

Bare criterion_id is NOT a canonical reference in the Cross-Artifact Analyzer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass

from ai_engineering.task_intent import (
    LineageValidationError,
    NodeKind,
    RelationKind,
    TaskIntent,
    TaskIntentValidationError,
    TaskLineage,
    deserialize_intent,
    intent_digest,
    validate_lineage,
)

ANALYSIS_REPORT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Finding severity
# ---------------------------------------------------------------------------


class FindingSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


# ---------------------------------------------------------------------------
# Finding codes
# ---------------------------------------------------------------------------


class FindingCode(StrEnum):
    ORPHAN_ACCEPTANCE_CRITERION = "ORPHAN_ACCEPTANCE_CRITERION"
    ORPHAN_EXECUTION_TASK = "ORPHAN_EXECUTION_TASK"
    ORPHAN_EVIDENCE = "ORPHAN_EVIDENCE"
    SOURCE_IDENTITY_MISMATCH = "SOURCE_IDENTITY_MISMATCH"
    TASK_IDENTITY_INCONSISTENCY = "TASK_IDENTITY_INCONSISTENCY"


# ---------------------------------------------------------------------------
# Artifact reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """A stable, scoped reference to an artifact element."""

    artifact_kind: str  # e.g. "INTENT", "LINEAGE_NODE", "CRITERION"
    identity: str       # scoped identity (task_id::criterion_id, node_id, etc.)
    label: str | None = None  # optional human-readable label


# ---------------------------------------------------------------------------
# Finding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Finding:
    """One deterministic cross-artifact inconsistency."""

    code: FindingCode
    severity: FindingSeverity
    message: str
    primary_reference: ArtifactReference
    related_references: tuple[ArtifactReference, ...] = ()


# ---------------------------------------------------------------------------
# Analysis report
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalysisReport:
    """Output of one cross-artifact analysis run."""

    schema_version: int
    analysis_id: str        # deterministic sha256 of canonical finding set + intent digest
    intent_task_id: str
    intent_digest: str
    source_base_sha: str    # copied from TaskIntent — verified against expected_base_sha
    findings: tuple[Finding, ...]

    @property
    def has_errors(self) -> bool:
        return any(f.severity == FindingSeverity.ERROR for f in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == FindingSeverity.WARNING)


# ---------------------------------------------------------------------------
# Analysis input error
# ---------------------------------------------------------------------------


class AnalysisInputError(ValueError):
    """Fail-closed input error for invalid/incompatible analyzer inputs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _finding_to_dict(f: Finding) -> dict[str, Any]:
    def ref_to_dict(r: ArtifactReference) -> dict[str, Any]:
        d: dict[str, Any] = {
            "artifact_kind": r.artifact_kind,
            "identity": r.identity,
        }
        if r.label is not None:
            d["label"] = r.label
        return d

    return {
        "code": f.code.value,
        "severity": f.severity.value,
        "message": f.message,
        "primary_reference": ref_to_dict(f.primary_reference),
        "related_references": [ref_to_dict(r) for r in f.related_references],
    }


def report_to_dict(report: AnalysisReport) -> dict[str, Any]:
    """Convert report to canonical dict (deterministic, no wall-clock fields)."""
    return {
        "schema_version": report.schema_version,
        "analysis_id": report.analysis_id,
        "intent_task_id": report.intent_task_id,
        "intent_digest": report.intent_digest,
        "source_base_sha": report.source_base_sha,
        "findings": [_finding_to_dict(f) for f in report.findings],
        "summary": {
            "total": len(report.findings),
            "errors": report.error_count,
            "warnings": report.warning_count,
            "infos": len(report.findings) - report.error_count - report.warning_count,
        },
    }


def serialize_report(report: AnalysisReport) -> str:
    """Canonical deterministic JSON for the report (sort_keys, compact separators)."""
    return json.dumps(
        report_to_dict(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


# ---------------------------------------------------------------------------
# Finding deduplication key
# ---------------------------------------------------------------------------


def _finding_key(f: Finding) -> str:
    """Stable deduplication key — prevents duplicate reporting of same logical issue."""
    return f"{f.code.value}::{f.primary_reference.artifact_kind}::{f.primary_reference.identity}"


# ---------------------------------------------------------------------------
# Analysis context
# ---------------------------------------------------------------------------


@dataclass
class _AnalysisContext:
    intent: TaskIntent
    lineage: TaskLineage
    expected_base_sha: str | None
    findings: list[Finding] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set)

    def add(self, finding: Finding) -> None:
        key = _finding_key(finding)
        if key not in self._seen_keys:
            self._seen_keys.add(key)
            self.findings.append(finding)


# ---------------------------------------------------------------------------
# Rule: source identity mismatch
# ---------------------------------------------------------------------------


def _rule_source_identity(ctx: _AnalysisContext) -> None:
    """SOURCE_IDENTITY_MISMATCH: intent.source_base_sha != expected_base_sha.

    Only fires when an independent expected_base_sha is supplied to analyze().
    Without an independent anchor, source-SHA verification cannot be deterministic.
    """
    if ctx.expected_base_sha is None:
        return
    if ctx.intent.source_base_sha != ctx.expected_base_sha:
        ctx.add(Finding(
            code=FindingCode.SOURCE_IDENTITY_MISMATCH,
            severity=FindingSeverity.ERROR,
            message=(
                f"TaskIntent source_base_sha '{ctx.intent.source_base_sha}' "
                f"does not match the supplied expected base SHA "
                f"'{ctx.expected_base_sha}'. "
                "Artifacts may originate from a different repository state."
            ),
            primary_reference=ArtifactReference(
                artifact_kind="INTENT",
                identity=ctx.intent.task_id,
                label="TaskIntent.source_base_sha",
            ),
        ))


# ---------------------------------------------------------------------------
# Rule: task identity consistency
# ---------------------------------------------------------------------------


def _rule_task_identity_consistency(ctx: _AnalysisContext) -> None:
    """TASK_IDENTITY_INCONSISTENCY: lineage INTENT nodes must match intent.task_id.

    This is task-identity consistency, not SHA verification.
    An INTENT node whose node_id differs from intent.task_id indicates the
    lineage was built for a different task context.
    """
    intent = ctx.intent
    lineage = ctx.lineage
    for node in lineage.nodes:
        if node.kind == NodeKind.INTENT and node.node_id != intent.task_id:
            ctx.add(Finding(
                code=FindingCode.TASK_IDENTITY_INCONSISTENCY,
                severity=FindingSeverity.ERROR,
                message=(
                    f"Lineage INTENT node '{node.node_id}' does not match "
                    f"TaskIntent task_id '{intent.task_id}'. "
                    "The lineage graph was likely built for a different task."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="LINEAGE_NODE",
                    identity=node.node_id,
                    label="INTENT node mismatch",
                ),
                related_references=(
                    ArtifactReference(
                        artifact_kind="INTENT",
                        identity=intent.task_id,
                        label="TaskIntent.task_id",
                    ),
                ),
            ))


# ---------------------------------------------------------------------------
# Rule: orphan acceptance criterion
# ---------------------------------------------------------------------------


def _scoped_criterion_id(task_id: str, criterion_id: str) -> str:
    """Return canonical scoped criterion identity: <task_id>::<criterion_id>."""
    return f"{task_id}::{criterion_id}"


def _rule_orphan_acceptance_criterion(ctx: _AnalysisContext) -> None:
    """ORPHAN_ACCEPTANCE_CRITERION: criterion with no TASK→IMPLEMENTS edge.

    Criterion matching uses ONLY canonical scoped identity:
        <task_id>::<criterion_id>

    Bare criterion_id is not accepted as a canonical reference.
    This matches PR-2 scoped-identity policy.
    """
    intent = ctx.intent
    lineage = ctx.lineage

    # Build set of scoped criterion node_ids that are IMPLEMENTS targets.
    implemented_targets: set[str] = set()
    for edge in lineage.edges:
        if edge.relation == RelationKind.IMPLEMENTS:
            implemented_targets.add(edge.target_id)

    # Build set of CRITERION node_ids declared in the lineage.
    criterion_node_ids: set[str] = {
        n.node_id for n in lineage.nodes if n.kind == NodeKind.CRITERION
    }

    for crit in intent.acceptance_criteria:
        scoped_id = _scoped_criterion_id(intent.task_id, crit.criterion_id)

        has_impl = scoped_id in implemented_targets
        is_node = scoped_id in criterion_node_ids

        if not has_impl:
            ctx.add(Finding(
                code=FindingCode.ORPHAN_ACCEPTANCE_CRITERION,
                severity=FindingSeverity.ERROR,
                message=(
                    f"Acceptance criterion '{scoped_id}' has no TASK→IMPLEMENTS edge. "
                    "No execution task is recorded as implementing this criterion. "
                    "Canonical scoped identity required: <task_id>::<criterion_id>."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="CRITERION",
                    identity=scoped_id,
                    label=crit.statement[:120] if crit.statement else None,
                ),
            ))
        elif not is_node:
            # Edge target exists but no CRITERION node declared — WARNING
            ctx.add(Finding(
                code=FindingCode.ORPHAN_ACCEPTANCE_CRITERION,
                severity=FindingSeverity.WARNING,
                message=(
                    f"Acceptance criterion '{scoped_id}' has a TASK→IMPLEMENTS edge "
                    "but no corresponding CRITERION node is declared in the lineage graph. "
                    "Declare a CRITERION node with the canonical scoped identity."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="CRITERION",
                    identity=scoped_id,
                    label=crit.statement[:120] if crit.statement else None,
                ),
            ))


# ---------------------------------------------------------------------------
# Rule: orphan execution task
# ---------------------------------------------------------------------------


def _rule_orphan_execution_task(ctx: _AnalysisContext) -> None:
    """ORPHAN_EXECUTION_TASK: TASK node with no outgoing IMPLEMENTS edge."""
    lineage = ctx.lineage
    task_nodes_with_impl: set[str] = set()
    for edge in lineage.edges:
        if edge.relation == RelationKind.IMPLEMENTS:
            task_nodes_with_impl.add(edge.source_id)

    for node in lineage.nodes:
        if node.kind == NodeKind.TASK and node.node_id not in task_nodes_with_impl:
            ctx.add(Finding(
                code=FindingCode.ORPHAN_EXECUTION_TASK,
                severity=FindingSeverity.WARNING,
                message=(
                    f"TASK node '{node.node_id}' has no IMPLEMENTS edge. "
                    "This task does not implement any recorded acceptance criterion "
                    "and may represent unauthorized scope creep."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="LINEAGE_NODE",
                    identity=node.node_id,
                    label="TASK without criterion",
                ),
            ))


# ---------------------------------------------------------------------------
# Rule: orphan evidence
# ---------------------------------------------------------------------------


def _rule_orphan_evidence(ctx: _AnalysisContext) -> None:
    """ORPHAN_EVIDENCE: EVIDENCE node with no outgoing VERIFIES edge."""
    lineage = ctx.lineage
    evidence_with_verifies: set[str] = set()
    for edge in lineage.edges:
        if edge.relation == RelationKind.VERIFIES:
            evidence_with_verifies.add(edge.source_id)

    for node in lineage.nodes:
        if node.kind == NodeKind.EVIDENCE and node.node_id not in evidence_with_verifies:
            ctx.add(Finding(
                code=FindingCode.ORPHAN_EVIDENCE,
                severity=FindingSeverity.WARNING,
                message=(
                    f"EVIDENCE node '{node.node_id}' has no VERIFIES edge. "
                    "This evidence artifact does not verify any task or criterion."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="LINEAGE_NODE",
                    identity=node.node_id,
                    label="EVIDENCE without verified target",
                ),
            ))


# ---------------------------------------------------------------------------
# Finding sort key (deterministic ordering)
# ---------------------------------------------------------------------------


_SEVERITY_ORDER = {
    FindingSeverity.ERROR: 0,
    FindingSeverity.WARNING: 1,
    FindingSeverity.INFO: 2,
}


def _finding_sort_key(f: Finding) -> tuple[int, str, str, str]:
    """Deterministic sort: severity → code → primary identity → related identities."""
    related = "|".join(sorted(r.identity for r in f.related_references))
    return (
        _SEVERITY_ORDER[f.severity],
        f.code.value,
        f.primary_reference.identity,
        related,
    )


# ---------------------------------------------------------------------------
# Analysis ID (deterministic)
# ---------------------------------------------------------------------------


def _compute_analysis_id(intent_dgst: str, findings: tuple[Finding, ...]) -> str:
    """Deterministic analysis ID: sha256 of intent_digest + sorted finding keys."""
    parts = [intent_dgst] + sorted(_finding_key(f) for f in findings)
    payload = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze(
    intent: TaskIntent,
    lineage: TaskLineage,
    *,
    expected_base_sha: str | None = None,
) -> AnalysisReport:
    """Run all analysis rules and return a deterministic AnalysisReport.

    Parameters
    ----------
    intent:
        Validated TaskIntent (schema v1).
    lineage:
        Validated TaskLineage (schema v1).
    expected_base_sha:
        Optional independent canonical base SHA to verify against
        intent.source_base_sha. When None, SOURCE_IDENTITY_MISMATCH is
        not emitted (no independent anchor is available).

    Guarantees
    ----------
    * Read-only: inputs are never mutated.
    * Offline: no network or provider calls.
    * Deterministic: same inputs + same expected_base_sha → same report.
    * Deduplicated: one logical issue → one finding.

    Deferred rules (not implemented in schema v1)
    ---------------------------------------------
    MUTATION_OUTSIDE_ALLOWED_SCOPE, MUTATION_IN_FORBIDDEN_SCOPE:
        Deferred due to missing canonical structured mutation path reference
        in TaskLineage schema v1.

    REQUIRED_GATE_COVERAGE:
        Deferred due to missing canonical gate-to-lineage-node mapping
        in TaskLineage schema v1.
    """
    ctx = _AnalysisContext(
        intent=intent,
        lineage=lineage,
        expected_base_sha=expected_base_sha,
    )

    _rule_source_identity(ctx)
    _rule_task_identity_consistency(ctx)
    _rule_orphan_acceptance_criterion(ctx)
    _rule_orphan_execution_task(ctx)
    _rule_orphan_evidence(ctx)

    sorted_findings = tuple(sorted(ctx.findings, key=_finding_sort_key))
    dgst = intent_digest(intent)
    analysis_id = _compute_analysis_id(dgst, sorted_findings)

    return AnalysisReport(
        schema_version=ANALYSIS_REPORT_SCHEMA_VERSION,
        analysis_id=analysis_id,
        intent_task_id=intent.task_id,
        intent_digest=dgst,
        source_base_sha=intent.source_base_sha,
        findings=sorted_findings,
    )


# ---------------------------------------------------------------------------
# Input loading helpers (used by CLI)
# ---------------------------------------------------------------------------


def load_lineage_from_bytes(raw: bytes) -> TaskLineage:
    """Deserialize and validate a TaskLineage from raw JSON bytes.

    Raises AnalysisInputError on failure.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AnalysisInputError("LINEAGE_JSON_INVALID") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalysisInputError("LINEAGE_JSON_INVALID") from exc
    if not isinstance(payload, Mapping):
        raise AnalysisInputError("LINEAGE_JSON_INVALID")
    try:
        return validate_lineage(payload)
    except LineageValidationError as exc:
        raise AnalysisInputError(f"LINEAGE_INVALID_{exc.code}") from exc


def load_intent_from_bytes(raw: bytes) -> TaskIntent:
    """Deserialize and validate a TaskIntent from raw JSON bytes.

    Raises AnalysisInputError on failure.
    """
    try:
        return deserialize_intent(raw)
    except TaskIntentValidationError as exc:
        raise AnalysisInputError(f"INTENT_INVALID_{exc.code}") from exc
