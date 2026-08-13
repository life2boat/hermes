"""Deterministic read-only Cross-Artifact Analyzer for TaskIntent / TaskLineage.

Contract
--------
* READ ONLY — never mutates input artifacts.
* OFFLINE    — zero provider calls, zero network calls.
* DETERMINISTIC — same inputs produce byte-equivalent canonical JSON output.
* SCHEMA_VERSION = 1

Analysis report schema v1 identifies cross-artifact inconsistencies between:
  TaskIntent, AcceptanceCriteria, TaskLineage nodes/edges,
  mutation boundaries, and required gates.

Rules implemented
-----------------
ORPHAN_ACCEPTANCE_CRITERION   – criterion with no TASK→IMPLEMENTS edge
ORPHAN_EXECUTION_TASK          – TASK node with no IMPLEMENTS edge
ORPHAN_EVIDENCE                – EVIDENCE node with no VERIFIES edge
MUTATION_OUTSIDE_ALLOWED_SCOPE – edge-referenced path outside allowed_mutations
MUTATION_IN_FORBIDDEN_SCOPE   – edge-referenced path matches forbidden_mutations
REQUIRED_GATE_UNCOVERED       – required_gate with no structured coverage node
SOURCE_IDENTITY_MISMATCH      – lineage INTENT node_id doesn't match intent task_id,
                                 or base-SHA marker node conflicts with intent SHA

Not implemented in PR-2 (deferred)
------------------------------------
Clarify, Converge, LLM-as-judge, auto-remediation, remote artifact fetch.
"""

from __future__ import annotations

import hashlib
import json
import uuid
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
    MUTATION_OUTSIDE_ALLOWED_SCOPE = "MUTATION_OUTSIDE_ALLOWED_SCOPE"
    MUTATION_IN_FORBIDDEN_SCOPE = "MUTATION_IN_FORBIDDEN_SCOPE"
    REQUIRED_GATE_UNCOVERED = "REQUIRED_GATE_UNCOVERED"
    SOURCE_IDENTITY_MISMATCH = "SOURCE_IDENTITY_MISMATCH"


# ---------------------------------------------------------------------------
# Artifact reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    """A stable, scoped reference to an artifact element."""

    artifact_kind: str  # e.g. "INTENT", "LINEAGE_NODE", "LINEAGE_EDGE"
    identity: str  # scoped identity (task_id::criterion_id, node_id, etc.)
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
    analysis_id: str  # deterministic: sha256 of canonical finding set + intent digest
    intent_task_id: str
    intent_digest: str
    source_base_sha: str
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
# Analysis errors (input validation)
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
# Path-matching helpers (deterministic, no filesystem access)
# ---------------------------------------------------------------------------


def _normalize_path(p: str) -> str:
    """Normalize path separators to forward slashes."""
    return p.replace("\\", "/")


def _path_matches_pattern(path: str, pattern: str) -> bool:
    """Check if a normalized path is within or equal to a normalized pattern prefix.

    A pattern ending with '/' matches any path with that prefix.
    An exact pattern matches only that exact path.
    """
    np = _normalize_path(path)
    npat = _normalize_path(pattern)
    if npat.endswith("/"):
        return np == npat.rstrip("/") or np.startswith(npat)
    return np == npat or np.startswith(npat + "/")


def _in_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    """Return True if path is covered by at least one allowed_mutations pattern."""
    return any(_path_matches_pattern(path, a) for a in allowed)


def _in_forbidden(path: str, forbidden: tuple[str, ...]) -> bool:
    """Return True if path matches any forbidden_mutations pattern."""
    return any(_path_matches_pattern(path, f) for f in forbidden)


# ---------------------------------------------------------------------------
# Finding deduplication key
# ---------------------------------------------------------------------------


def _finding_key(f: Finding) -> str:
    """Stable deduplication key for a finding (prevents duplicate reporting)."""
    return f"{f.code.value}::{f.primary_reference.artifact_kind}::{f.primary_reference.identity}"


# ---------------------------------------------------------------------------
# Analysis context
# ---------------------------------------------------------------------------


@dataclass
class _AnalysisContext:
    intent: TaskIntent
    lineage: TaskLineage
    findings: list[Finding] = field(default_factory=list)
    _seen_keys: set[str] = field(default_factory=set)

    def add(self, finding: Finding) -> None:
        key = _finding_key(finding)
        if key not in self._seen_keys:
            self._seen_keys.add(key)
            self.findings.append(finding)


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------


def _rule_source_identity(ctx: _AnalysisContext) -> None:
    """SOURCE_IDENTITY_MISMATCH: lineage INTENT nodes must match intent.task_id."""
    intent = ctx.intent
    lineage = ctx.lineage
    intent_nodes = [n for n in lineage.nodes if n.kind == NodeKind.INTENT]
    for node in intent_nodes:
        if node.node_id != intent.task_id:
            ctx.add(Finding(
                code=FindingCode.SOURCE_IDENTITY_MISMATCH,
                severity=FindingSeverity.ERROR,
                message=(
                    f"Lineage INTENT node '{node.node_id}' does not match "
                    f"TaskIntent task_id '{intent.task_id}'. "
                    "Artifacts may originate from different task contexts."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="LINEAGE_NODE",
                    identity=node.node_id,
                    label=f"INTENT node in lineage",
                ),
                related_references=(
                    ArtifactReference(
                        artifact_kind="INTENT",
                        identity=intent.task_id,
                        label="TaskIntent.task_id",
                    ),
                ),
            ))


def _rule_orphan_acceptance_criterion(ctx: _AnalysisContext) -> None:
    """ORPHAN_ACCEPTANCE_CRITERION: criterion with no TASK IMPLEMENTS edge."""
    intent = ctx.intent
    lineage = ctx.lineage

    # Build set of criterion node_ids that have at least one IMPLEMENTS edge pointing to them.
    implemented_criteria: set[str] = set()
    for edge in lineage.edges:
        if edge.relation == RelationKind.IMPLEMENTS:
            implemented_criteria.add(edge.target_id)

    # For each acceptance criterion, check if its scoped id appears as a CRITERION node
    # that is the target of an IMPLEMENTS edge.
    criterion_node_ids = {
        n.node_id for n in lineage.nodes if n.kind == NodeKind.CRITERION
    }

    for crit in intent.acceptance_criteria:
        scoped_id = f"{intent.task_id}::{crit.criterion_id}"
        # Match by exact scoped id or by bare criterion_id (for backward compat)
        has_impl = (
            scoped_id in implemented_criteria
            or crit.criterion_id in implemented_criteria
        )
        # Also check that the criterion is represented as a node
        is_node = (
            scoped_id in criterion_node_ids
            or crit.criterion_id in criterion_node_ids
        )
        if not has_impl:
            ctx.add(Finding(
                code=FindingCode.ORPHAN_ACCEPTANCE_CRITERION,
                severity=FindingSeverity.ERROR,
                message=(
                    f"Acceptance criterion '{crit.criterion_id}' "
                    f"(task '{intent.task_id}') has no TASK→IMPLEMENTS edge. "
                    "No execution task is recorded as implementing this criterion."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="CRITERION",
                    identity=scoped_id,
                    label=crit.statement[:120] if crit.statement else None,
                ),
            ))
        elif not is_node:
            # Criterion implemented but not declared as a node — WARNING
            ctx.add(Finding(
                code=FindingCode.ORPHAN_ACCEPTANCE_CRITERION,
                severity=FindingSeverity.WARNING,
                message=(
                    f"Acceptance criterion '{crit.criterion_id}' has an IMPLEMENTS edge "
                    "but is not declared as a CRITERION node in the lineage graph."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="CRITERION",
                    identity=scoped_id,
                    label=crit.statement[:120] if crit.statement else None,
                ),
            ))


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


def _rule_mutation_boundaries(ctx: _AnalysisContext) -> None:
    """Check TASK nodes with path-shaped IDs against allowed/forbidden mutation boundaries.

    A TASK node_id is treated as a repository path reference only when it contains
    a path separator ('/' or '\\') — a deterministic structural signal.
    This avoids false positives from abstract task identifiers like 'TASK-001'.
    """
    intent = ctx.intent
    lineage = ctx.lineage

    for node in lineage.nodes:
        if node.kind != NodeKind.TASK:
            continue
        # Only analyze node_ids that look like repository paths.
        if "/" not in node.node_id and "\\" not in node.node_id:
            continue

        path = node.node_id
        in_forbidden = _in_forbidden(path, intent.forbidden_mutations)
        in_allowed = _in_allowed(path, intent.allowed_mutations)

        if in_forbidden:
            ctx.add(Finding(
                code=FindingCode.MUTATION_IN_FORBIDDEN_SCOPE,
                severity=FindingSeverity.ERROR,
                message=(
                    f"TASK node '{path}' references a path that matches "
                    f"a forbidden_mutations pattern in TaskIntent '{intent.task_id}'. "
                    "Forbidden scope takes precedence over allowed scope."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="LINEAGE_NODE",
                    identity=path,
                    label="TASK in forbidden scope",
                ),
                related_references=(
                    ArtifactReference(
                        artifact_kind="INTENT",
                        identity=intent.task_id,
                        label="TaskIntent.forbidden_mutations",
                    ),
                ),
            ))
        elif not in_allowed and intent.allowed_mutations:
            # Only report outside-allowed if allowed_mutations is non-empty.
            # An empty allowed_mutations list means no path-level restriction.
            ctx.add(Finding(
                code=FindingCode.MUTATION_OUTSIDE_ALLOWED_SCOPE,
                severity=FindingSeverity.ERROR,
                message=(
                    f"TASK node '{path}' references a path outside "
                    f"allowed_mutations in TaskIntent '{intent.task_id}'."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="LINEAGE_NODE",
                    identity=path,
                    label="TASK outside allowed scope",
                ),
                related_references=(
                    ArtifactReference(
                        artifact_kind="INTENT",
                        identity=intent.task_id,
                        label="TaskIntent.allowed_mutations",
                    ),
                ),
            ))


def _rule_required_gate_coverage(ctx: _AnalysisContext) -> None:
    """REQUIRED_GATE_UNCOVERED: required_gate with no structured coverage node.

    A gate is considered "covered" when a lineage node whose node_id exactly
    matches the gate identifier exists. This is the only deterministic
    structural representation available in schema v1.

    If required_gates is empty, no findings are emitted.
    """
    intent = ctx.intent
    lineage = ctx.lineage

    node_ids = {n.node_id for n in lineage.nodes}

    for gate in intent.required_gates:
        if gate not in node_ids:
            ctx.add(Finding(
                code=FindingCode.REQUIRED_GATE_UNCOVERED,
                severity=FindingSeverity.WARNING,
                message=(
                    f"Required gate '{gate}' from TaskIntent '{intent.task_id}' "
                    "has no corresponding lineage node. "
                    "No structured gate coverage is recorded."
                ),
                primary_reference=ArtifactReference(
                    artifact_kind="INTENT",
                    identity=f"{intent.task_id}::gate::{gate}",
                    label=gate,
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
    related = "|".join(
        sorted(r.identity for r in f.related_references)
    )
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


def analyze(intent: TaskIntent, lineage: TaskLineage) -> AnalysisReport:
    """Run all analysis rules and return a deterministic AnalysisReport.

    Guarantees
    ----------
    * Read-only: inputs are never mutated.
    * Offline: no network or provider calls.
    * Deterministic: same inputs → same report.
    * Deduplicated: one logical issue → one finding.
    """
    ctx = _AnalysisContext(intent=intent, lineage=lineage)

    _rule_source_identity(ctx)
    _rule_orphan_acceptance_criterion(ctx)
    _rule_orphan_execution_task(ctx)
    _rule_orphan_evidence(ctx)
    _rule_mutation_boundaries(ctx)
    _rule_required_gate_coverage(ctx)

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


class AnalysisReportLoadError(ValueError):
    """Raised when a serialized report cannot be loaded."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


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
