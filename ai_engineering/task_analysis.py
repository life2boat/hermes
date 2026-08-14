"""Deterministic read-only Cross-Artifact Analyzer for TaskIntent / TaskLineage.

Contract
--------
* READ ONLY — never mutates input artifacts.
* OFFLINE    — zero provider calls, zero network calls.
* DETERMINISTIC — same inputs produce byte-equivalent canonical JSON output.
* SCHEMA_VERSION = 1

Rules implemented in schema v1
-------------------------------
ORPHAN_ACCEPTANCE_CRITERION
    Criterion with no TASK→IMPLEMENTS edge.
    Uses canonical scoped identity <task_id>::<criterion_id> only.

ORPHAN_EXECUTION_TASK
    TASK node with no IMPLEMENTS edge.

ORPHAN_EVIDENCE
    EVIDENCE node with no VERIFIES edge.

SOURCE_IDENTITY_MISMATCH
    intent.source_base_sha != expected_base_sha (only when an independent
    anchor is supplied via the expected_base_sha keyword argument).

TASK_IDENTITY_INCONSISTENCY
    A lineage INTENT node_id != TaskIntent.task_id.
    This is task-identity consistency, not SHA verification.

Rules deferred (no canonical structured mapping in schema v1)
-------------------------------------------------------------
MUTATION_OUTSIDE_ALLOWED_SCOPE
    DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
    PR-1 TaskLineage has no canonical structured path-reference type.

MUTATION_IN_FORBIDDEN_SCOPE
    DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE
    Same reason as above.

REQUIRED_GATE_COVERAGE
    DEFERRED_DUE_TO_MISSING_CANONICAL_MAPPING
    PR-1 lineage schema defines no GATE node kind.

Criterion identity
------------------
Only canonical scoped identity is accepted:

    <task_id>::<criterion_id>

Bare criterion_id is NOT a canonical reference in the Cross-Artifact Analyzer.

Input validation
----------------
All paths to analyze() run inputs through canonical PR-1 validators
(validate_intent, validate_lineage) so invalid dataclasses are rejected
fail-closed before any analysis rule executes.

Report identity
---------------
analysis_id = sha256 of a canonical identity payload that includes:
  schema_version, intent_digest, lineage_digest, source_base_sha,
  expected_base_sha (or empty string), and canonical full finding set.

lineage_digest is computed by extracting validated TaskLineage nodes and edges,
performing deterministic node sorting (by kind then node_id) and edge sorting
(by relation, source_id, then target_id), formatting as a canonical JSON graph,
and computing its SHA-256 digest.

CLI safety
----------
--output must not alias --intent or --lineage (resolved via Path.resolve()).
Output aliasing any input is rejected with exit code 2.

Not implemented in PR-2
-----------------------
Clarify, Converge, LLM-as-judge, auto-remediation, remote artifact fetch.
"""

from __future__ import annotations

import hashlib
import json
import re
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
    validate_intent,
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

    artifact_kind: str  # "INTENT", "LINEAGE_NODE", "CRITERION", etc.
    identity: str  # scoped identity (<task_id>::<criterion_id>, node_id, …)
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
    """Output of one cross-artifact analysis run.

    Fields
    ------
    schema_version      : always 1 in this release.
    analysis_id         : sha256 of the canonical identity payload (see below).
    intent_task_id      : TaskIntent.task_id.
    intent_digest       : sha256 of canonical TaskIntent JSON.
    lineage_digest      : sha256 of canonical TaskLineage JSON (serialize_lineage).
    source_base_sha     : copied from TaskIntent.source_base_sha.
    expected_base_sha   : the independent expected base SHA supplied to analyze(),
                          or None when no independent anchor was available.
    findings            : deterministically ordered, deduplicated findings.
    """

    schema_version: int
    analysis_id: str
    intent_task_id: str
    intent_digest: str
    lineage_digest: str
    source_base_sha: str
    expected_base_sha: str | None
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
# Analysis input / report errors
# ---------------------------------------------------------------------------


class AnalysisInputError(ValueError):
    """Fail-closed error for invalid analyzer inputs."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class AnalysisReportError(ValueError):
    """Fail-closed error for invalid report payloads."""

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
        "lineage_digest": report.lineage_digest,
        "source_base_sha": report.source_base_sha,
        "expected_base_sha": report.expected_base_sha,
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
# Report schema validator
# ---------------------------------------------------------------------------

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_report(value: Mapping[str, object]) -> dict[str, Any]:
    """Validate a deserialized report dict against schema version 1.

    Returns the validated dict on success.
    Raises AnalysisReportError on any schema violation.

    Does NOT reconstruct AnalysisReport dataclass; used for external round-trip
    validation only.
    """
    if not isinstance(value, Mapping):
        raise AnalysisReportError("REPORT_NOT_A_MAPPING")
    sv = value.get("schema_version")
    if sv != ANALYSIS_REPORT_SCHEMA_VERSION or isinstance(sv, bool):
        raise AnalysisReportError("REPORT_SCHEMA_VERSION_UNSUPPORTED")
    required = {
        "schema_version",
        "analysis_id",
        "intent_task_id",
        "intent_digest",
        "lineage_digest",
        "source_base_sha",
        "expected_base_sha",
        "findings",
        "summary",
    }
    missing = required - set(value.keys())
    if missing:
        raise AnalysisReportError("REPORT_REQUIRED_FIELD_MISSING")

    # Format validations
    if not isinstance(value["analysis_id"], str) or not _SHA256_RE.fullmatch(
        value["analysis_id"]
    ):
        raise AnalysisReportError("REPORT_ANALYSIS_ID_INVALID")
    if not isinstance(value["intent_task_id"], str) or not value["intent_task_id"]:
        raise AnalysisReportError("REPORT_INTENT_TASK_ID_INVALID")
    if not isinstance(value["intent_digest"], str) or not _SHA256_RE.fullmatch(
        value["intent_digest"]
    ):
        raise AnalysisReportError("REPORT_INTENT_DIGEST_INVALID")
    if not isinstance(value["lineage_digest"], str) or not _SHA256_RE.fullmatch(
        value["lineage_digest"]
    ):
        raise AnalysisReportError("REPORT_LINEAGE_DIGEST_INVALID")
    if not isinstance(value["source_base_sha"], str) or not _SHA1_RE.fullmatch(
        value["source_base_sha"]
    ):
        raise AnalysisReportError("REPORT_SOURCE_BASE_SHA_INVALID")

    exp_sha = value["expected_base_sha"]
    if exp_sha is not None and (
        not isinstance(exp_sha, str) or not _SHA1_RE.fullmatch(exp_sha)
    ):
        raise AnalysisReportError("REPORT_EXPECTED_BASE_SHA_INVALID")

    if not isinstance(value["findings"], list):
        raise AnalysisReportError("REPORT_FINDINGS_INVALID")

    # Check severity / finding codes
    for f in value["findings"]:
        if not isinstance(f, dict):
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")
        if (
            "severity" not in f
            or "code" not in f
            or "message" not in f
            or "primary_reference" not in f
        ):
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")

        if not isinstance(f["message"], str):
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")

        try:
            FindingSeverity(f["severity"])
            FindingCode(f["code"])
        except ValueError:
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")

        pref = f["primary_reference"]
        if (
            not isinstance(pref, dict)
            or "artifact_kind" not in pref
            or "identity" not in pref
        ):
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")

        if not isinstance(pref["artifact_kind"], str) or not pref["artifact_kind"]:
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")
        if not isinstance(pref["identity"], str) or not pref["identity"]:
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")

        rel_refs = f.get("related_references", [])
        if not isinstance(rel_refs, list):
            raise AnalysisReportError("REPORT_FINDINGS_INVALID")
        for rref in rel_refs:
            if (
                not isinstance(rref, dict)
                or "artifact_kind" not in rref
                or "identity" not in rref
            ):
                raise AnalysisReportError("REPORT_FINDINGS_INVALID")

            if not isinstance(rref["artifact_kind"], str) or not rref["artifact_kind"]:
                raise AnalysisReportError("REPORT_FINDINGS_INVALID")
            if not isinstance(rref["identity"], str) or not rref["identity"]:
                raise AnalysisReportError("REPORT_FINDINGS_INVALID")

    summary = value["summary"]
    if not isinstance(summary, dict):
        raise AnalysisReportError("REPORT_SUMMARY_INVALID")

    for key in ("total", "errors", "warnings", "infos"):
        if key not in summary:
            raise AnalysisReportError("REPORT_SUMMARY_INVALID")
        val = summary[key]
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            raise AnalysisReportError("REPORT_SUMMARY_INVALID")

    findings = value["findings"]
    actual_total = len(findings)
    actual_errors = sum(
        1 for f in findings if f.get("severity") == FindingSeverity.ERROR.value
    )
    actual_warnings = sum(
        1 for f in findings if f.get("severity") == FindingSeverity.WARNING.value
    )
    actual_infos = sum(
        1 for f in findings if f.get("severity") == FindingSeverity.INFO.value
    )

    if (
        summary["total"] != actual_total
        or summary["errors"] != actual_errors
        or summary["warnings"] != actual_warnings
        or summary["infos"] != actual_infos
    ):
        raise AnalysisReportError("REPORT_SUMMARY_INVALID")

    return dict(value)


def deserialize_report(raw: str | bytes) -> dict[str, Any]:
    """Deserialize and validate a JSON report string.

    Returns the validated dict on success.
    Raises AnalysisReportError on any error.
    """
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AnalysisReportError("REPORT_JSON_INVALID") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise AnalysisReportError("REPORT_JSON_INVALID")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AnalysisReportError("REPORT_JSON_INVALID") from exc
    return validate_report(payload)


# ---------------------------------------------------------------------------
# Lineage digest (deterministic structural graph)
# ---------------------------------------------------------------------------


def _lineage_digest(lineage: TaskLineage) -> str:
    """Canonical sha256 digest of a validated TaskLineage graph.

    Nodes/edges ordering is not semantically significant in the lineage graph,
    but PR-1 serialization only normalizes dict keys, not array element order.
    We must perform analyzer-local deterministic normalization.
    """
    sorted_nodes = sorted(
        [{"node_id": n.node_id, "kind": n.kind.value} for n in lineage.nodes],
        key=lambda x: (x["kind"], x["node_id"]),
    )
    sorted_edges = sorted(
        [
            {
                "source_id": e.source_id,
                "target_id": e.target_id,
                "relation": e.relation.value,
            }
            for e in lineage.edges
        ],
        key=lambda x: (x["relation"], x["source_id"], x["target_id"]),
    )
    canonical_dict = {
        "edges": sorted_edges,
        "nodes": sorted_nodes,
        "schema_version": lineage.schema_version,
    }
    canonical_json = json.dumps(
        canonical_dict,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Finding deduplication key
# ---------------------------------------------------------------------------


def _finding_key(f: Finding) -> str:
    """Stable deduplication key — prevents duplicate reporting of the same issue."""
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
# Rule implementations
# ---------------------------------------------------------------------------


def _rule_source_identity(ctx: _AnalysisContext) -> None:
    """SOURCE_IDENTITY_MISMATCH: intent.source_base_sha != expected_base_sha.

    Only fires when an independent expected_base_sha is supplied to analyze().
    """
    if ctx.expected_base_sha is None:
        return
    if ctx.intent.source_base_sha != ctx.expected_base_sha:
        ctx.add(
            Finding(
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
            )
        )


def _rule_task_identity_consistency(ctx: _AnalysisContext) -> None:
    """TASK_IDENTITY_INCONSISTENCY: lineage INTENT node_id != intent.task_id."""
    for node in ctx.lineage.nodes:
        if node.kind == NodeKind.INTENT and node.node_id != ctx.intent.task_id:
            ctx.add(
                Finding(
                    code=FindingCode.TASK_IDENTITY_INCONSISTENCY,
                    severity=FindingSeverity.ERROR,
                    message=(
                        f"Lineage INTENT node '{node.node_id}' does not match "
                        f"TaskIntent task_id '{ctx.intent.task_id}'. "
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
                            identity=ctx.intent.task_id,
                            label="TaskIntent.task_id",
                        ),
                    ),
                )
            )


def _scoped_criterion_id(task_id: str, criterion_id: str) -> str:
    return f"{task_id}::{criterion_id}"


def _rule_orphan_acceptance_criterion(ctx: _AnalysisContext) -> None:
    """ORPHAN_ACCEPTANCE_CRITERION: criterion with no TASK→IMPLEMENTS edge.

    Only canonical scoped identity <task_id>::<criterion_id> is accepted.
    Bare criterion_id is not a canonical reference.
    """
    implemented_targets: set[str] = {
        edge.target_id
        for edge in ctx.lineage.edges
        if edge.relation == RelationKind.IMPLEMENTS
    }
    criterion_node_ids: set[str] = {
        n.node_id for n in ctx.lineage.nodes if n.kind == NodeKind.CRITERION
    }

    for crit in ctx.intent.acceptance_criteria:
        scoped_id = _scoped_criterion_id(ctx.intent.task_id, crit.criterion_id)

        if scoped_id not in implemented_targets:
            ctx.add(
                Finding(
                    code=FindingCode.ORPHAN_ACCEPTANCE_CRITERION,
                    severity=FindingSeverity.ERROR,
                    message=(
                        f"Acceptance criterion '{scoped_id}' has no TASK→IMPLEMENTS edge. "
                        "No execution task is recorded as implementing this criterion. "
                        "Use canonical scoped identity <task_id>::<criterion_id>."
                    ),
                    primary_reference=ArtifactReference(
                        artifact_kind="CRITERION",
                        identity=scoped_id,
                        label=crit.statement[:120] if crit.statement else None,
                    ),
                )
            )
        elif scoped_id not in criterion_node_ids:
            ctx.add(
                Finding(
                    code=FindingCode.ORPHAN_ACCEPTANCE_CRITERION,
                    severity=FindingSeverity.WARNING,
                    message=(
                        f"Acceptance criterion '{scoped_id}' has a TASK→IMPLEMENTS edge "
                        "but no corresponding CRITERION node is declared in the lineage."
                    ),
                    primary_reference=ArtifactReference(
                        artifact_kind="CRITERION",
                        identity=scoped_id,
                        label=crit.statement[:120] if crit.statement else None,
                    ),
                )
            )


def _rule_orphan_execution_task(ctx: _AnalysisContext) -> None:
    """ORPHAN_EXECUTION_TASK: TASK node with no outgoing IMPLEMENTS edge."""
    task_nodes_with_impl: set[str] = {
        edge.source_id
        for edge in ctx.lineage.edges
        if edge.relation == RelationKind.IMPLEMENTS
    }
    for node in ctx.lineage.nodes:
        if node.kind == NodeKind.TASK and node.node_id not in task_nodes_with_impl:
            ctx.add(
                Finding(
                    code=FindingCode.ORPHAN_EXECUTION_TASK,
                    severity=FindingSeverity.WARNING,
                    message=(
                        f"TASK node '{node.node_id}' has no IMPLEMENTS edge. "
                        "This task does not implement any recorded acceptance criterion."
                    ),
                    primary_reference=ArtifactReference(
                        artifact_kind="LINEAGE_NODE",
                        identity=node.node_id,
                        label="TASK without criterion",
                    ),
                )
            )


def _rule_orphan_evidence(ctx: _AnalysisContext) -> None:
    """ORPHAN_EVIDENCE: EVIDENCE node with no outgoing VERIFIES edge."""
    evidence_with_verifies: set[str] = {
        edge.source_id
        for edge in ctx.lineage.edges
        if edge.relation == RelationKind.VERIFIES
    }
    for node in ctx.lineage.nodes:
        if (
            node.kind == NodeKind.EVIDENCE
            and node.node_id not in evidence_with_verifies
        ):
            ctx.add(
                Finding(
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
                )
            )


# ---------------------------------------------------------------------------
# Finding sort key (deterministic ordering)
# ---------------------------------------------------------------------------


_SEVERITY_ORDER = {
    FindingSeverity.ERROR: 0,
    FindingSeverity.WARNING: 1,
    FindingSeverity.INFO: 2,
}


def _finding_sort_key(f: Finding) -> tuple[int, str, str, str]:
    related = "|".join(sorted(r.identity for r in f.related_references))
    return (
        _SEVERITY_ORDER[f.severity],
        f.code.value,
        f.primary_reference.identity,
        related,
    )


# ---------------------------------------------------------------------------
# Analysis ID (deterministic, full-payload hash)
# ---------------------------------------------------------------------------


def _compute_analysis_id(
    schema_version: int,
    intent_dgst: str,
    lin_dgst: str,
    source_base_sha: str,
    expected_base_sha: str | None,
    findings: tuple[Finding, ...],
) -> str:
    """sha256 of a canonical identity payload.

    Payload (canonical compact JSON, sort_keys):
        schema_version, intent_digest, lineage_digest, source_base_sha,
        expected_base_sha (null when absent), findings (full canonical form).
    """
    payload_dict: dict[str, Any] = {
        "expected_base_sha": expected_base_sha,
        "findings": [_finding_to_dict(f) for f in findings],
        "intent_digest": intent_dgst,
        "lineage_digest": lin_dgst,
        "schema_version": schema_version,
        "source_base_sha": source_base_sha,
    }
    payload_json = json.dumps(
        payload_dict,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


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
        TaskIntent (schema v1). Validated through canonical PR-1 validators
        on entry — invalid dataclasses are rejected fail-closed.
    lineage:
        TaskLineage (schema v1). Same validation guarantee.
    expected_base_sha:
        Optional independent 40-hex base SHA to compare against
        intent.source_base_sha. When None, SOURCE_IDENTITY_MISMATCH is
        not emitted (no independent anchor is available).

    Guarantees
    ----------
    * Read-only: inputs are never mutated.
    * Offline: no network or provider calls.
    * Deterministic: same inputs + same expected_base_sha → same report.
    * Deduplicated: one logical issue → one finding.
    * Canonical PR-1 validation on all inputs.

    Deferred rules
    --------------
    MUTATION_OUTSIDE_ALLOWED_SCOPE, MUTATION_IN_FORBIDDEN_SCOPE:
        DEFERRED_DUE_TO_MISSING_STRUCTURED_MUTATION_REFERENCE

    REQUIRED_GATE_COVERAGE:
        DEFERRED_DUE_TO_MISSING_CANONICAL_MAPPING

    Raises
    ------
    AnalysisInputError
        When intent or lineage cannot be validated through PR-1 validators.
    """
    # Canonical PR-1 validation of all inputs.
    try:
        intent = validate_intent(intent)
    except TaskIntentValidationError as exc:
        raise AnalysisInputError(f"INTENT_INVALID_{exc.code}") from exc

    try:
        lineage = validate_lineage(lineage)
    except LineageValidationError as exc:
        raise AnalysisInputError(f"LINEAGE_INVALID_{exc.code}") from exc

    if expected_base_sha is not None:
        if not isinstance(expected_base_sha, str) or not _SHA1_RE.fullmatch(
            expected_base_sha
        ):
            raise AnalysisInputError("EXPECTED_BASE_SHA_INVALID")

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
    intent_dgst = intent_digest(intent)
    lin_dgst = _lineage_digest(lineage)

    analysis_id = _compute_analysis_id(
        schema_version=ANALYSIS_REPORT_SCHEMA_VERSION,
        intent_dgst=intent_dgst,
        lin_dgst=lin_dgst,
        source_base_sha=intent.source_base_sha,
        expected_base_sha=expected_base_sha,
        findings=sorted_findings,
    )

    return AnalysisReport(
        schema_version=ANALYSIS_REPORT_SCHEMA_VERSION,
        analysis_id=analysis_id,
        intent_task_id=intent.task_id,
        intent_digest=intent_dgst,
        lineage_digest=lin_dgst,
        source_base_sha=intent.source_base_sha,
        expected_base_sha=expected_base_sha,
        findings=sorted_findings,
    )


# ---------------------------------------------------------------------------
# Input loading helpers (used by CLI)
# ---------------------------------------------------------------------------


def load_lineage_from_bytes(raw: bytes) -> TaskLineage:
    """Deserialize and validate a TaskLineage from raw JSON bytes."""
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
    """Deserialize and validate a TaskIntent from raw JSON bytes."""
    try:
        return deserialize_intent(raw)
    except TaskIntentValidationError as exc:
        raise AnalysisInputError(f"INTENT_INVALID_{exc.code}") from exc
