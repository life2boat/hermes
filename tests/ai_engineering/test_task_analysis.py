"""Tests for ai_engineering/task_analysis.py — Cross-Artifact Analyzer.

Coverage:
- Report schema / serialization
- Finding deduplication
- Deterministic ordering
- ORPHAN_ACCEPTANCE_CRITERION rule
- ORPHAN_EXECUTION_TASK rule
- ORPHAN_EVIDENCE rule
- MUTATION_OUTSIDE_ALLOWED_SCOPE rule
- MUTATION_IN_FORBIDDEN_SCOPE rule
- REQUIRED_GATE_UNCOVERED rule
- SOURCE_IDENTITY_MISMATCH rule
- Read-only guarantee (input mutation=0)
- Offline guarantee (no provider calls)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import pytest

from ai_engineering.task_analysis import (
    ANALYSIS_REPORT_SCHEMA_VERSION,
    AnalysisInputError,
    AnalysisReport,
    ArtifactReference,
    Finding,
    FindingCode,
    FindingSeverity,
    analyze,
    load_intent_from_bytes,
    load_lineage_from_bytes,
    report_to_dict,
    serialize_report,
)
from ai_engineering.task_intent import (
    AcceptanceCriterion,
    IntentStatus,
    LineageEdge,
    LineageNode,
    NodeKind,
    RelationKind,
    TaskIntent,
    TaskLineage,
    deserialize_intent,
    validate_lineage,
)
from ai_engineering.contracts import StopBoundary, TaskClass


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_intent(
    task_id: str = "TASK-001",
    source_base_sha: str = "a" * 40,
    criteria: list[tuple[str, str]] | None = None,
    allowed_mutations: list[str] | None = None,
    forbidden_mutations: list[str] | None = None,
    required_gates: list[str] | None = None,
) -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id=task_id,
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Test",
        source_repository="life2boat/hermes",
        source_main_ref="main",
        source_base_sha=source_base_sha,
        constraints=(),
        allowed_mutations=tuple(allowed_mutations or []),
        forbidden_mutations=tuple(forbidden_mutations or []),
        stop_boundary=StopBoundary.LOCAL_DIFF,
        acceptance_criteria=tuple(
            AcceptanceCriterion(criterion_id=cid, statement=stmt)
            for cid, stmt in (criteria or [])
        ),
        unknowns=(),
        applicable_invariants=(),
        required_gates=tuple(required_gates or []),
        parent_intent_digest=None,
    )


def _make_lineage(
    nodes: list[tuple[str, str]],
    edges: list[tuple[str, str, str]],
) -> TaskLineage:
    """Build a TaskLineage from (node_id, kind) pairs and (src, tgt, relation) triples."""
    return validate_lineage({
        "schema_version": 1,
        "nodes": [{"node_id": nid, "kind": kind} for nid, kind in nodes],
        "edges": [
            {"source_id": src, "target_id": tgt, "relation": rel}
            for src, tgt, rel in edges
        ],
    })


def _empty_lineage() -> TaskLineage:
    return _make_lineage([], [])


# ---------------------------------------------------------------------------
# Report schema / serialization
# ---------------------------------------------------------------------------


class TestReportSchema:
    def test_schema_version_constant(self) -> None:
        assert ANALYSIS_REPORT_SCHEMA_VERSION == 1

    def test_valid_empty_findings_report(self) -> None:
        intent = _make_intent()
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        assert report.schema_version == 1
        assert report.intent_task_id == "TASK-001"
        assert report.source_base_sha == "a" * 40
        assert report.findings == ()
        assert not report.has_errors
        assert report.error_count == 0

    def test_report_dict_has_required_fields(self) -> None:
        report = analyze(_make_intent(), _empty_lineage())
        d = report_to_dict(report)
        assert d["schema_version"] == 1
        assert "analysis_id" in d
        assert "intent_task_id" in d
        assert "intent_digest" in d
        assert "source_base_sha" in d
        assert "findings" in d
        assert "summary" in d
        summary = d["summary"]
        assert "total" in summary
        assert "errors" in summary
        assert "warnings" in summary
        assert "infos" in summary

    def test_valid_report_with_findings(self) -> None:
        # Criterion with no task
        intent = _make_intent(criteria=[("AC-1", "Must pass tests")])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        assert len(report.findings) >= 1
        codes = {f.code for f in report.findings}
        assert FindingCode.ORPHAN_ACCEPTANCE_CRITERION in codes

    def test_deterministic_report_serialization(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "Must pass tests")])
        lineage = _empty_lineage()
        r1 = serialize_report(analyze(intent, lineage))
        r2 = serialize_report(analyze(intent, lineage))
        assert r1 == r2

    def test_analysis_id_is_deterministic(self) -> None:
        intent = _make_intent()
        lineage = _empty_lineage()
        r1 = analyze(intent, lineage)
        r2 = analyze(intent, lineage)
        assert r1.analysis_id == r2.analysis_id

    def test_analysis_id_changes_with_findings(self) -> None:
        intent_no_crit = _make_intent()
        intent_with_crit = _make_intent(criteria=[("AC-1", "stmt")])
        lineage = _empty_lineage()
        r1 = analyze(intent_no_crit, lineage)
        r2 = analyze(intent_with_crit, lineage)
        assert r1.analysis_id != r2.analysis_id

    def test_serialize_is_valid_json(self) -> None:
        report = analyze(_make_intent(), _empty_lineage())
        parsed = json.loads(serialize_report(report))
        assert parsed["schema_version"] == 1


# ---------------------------------------------------------------------------
# Deterministic finding ordering
# ---------------------------------------------------------------------------


class TestDeterministicOrdering:
    def test_same_findings_same_order_across_runs(self) -> None:
        intent = _make_intent(
            criteria=[("AC-1", "s1"), ("AC-2", "s2"), ("AC-3", "s3")],
            required_gates=["GATE-A", "GATE-B"],
        )
        lineage = _empty_lineage()
        r1 = analyze(intent, lineage)
        r2 = analyze(intent, lineage)
        assert [f.code for f in r1.findings] == [f.code for f in r2.findings]
        assert [f.primary_reference.identity for f in r1.findings] == [
            f.primary_reference.identity for f in r2.findings
        ]

    def test_errors_sort_before_warnings(self) -> None:
        intent = _make_intent(
            criteria=[("AC-1", "stmt")],
            required_gates=["GATE-A"],
        )
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        severities = [f.severity for f in report.findings]
        # All ERRORs before WARNINGs
        error_indices = [i for i, s in enumerate(severities) if s == FindingSeverity.ERROR]
        warning_indices = [i for i, s in enumerate(severities) if s == FindingSeverity.WARNING]
        if error_indices and warning_indices:
            assert max(error_indices) < min(warning_indices)


# ---------------------------------------------------------------------------
# Duplicate finding collapse
# ---------------------------------------------------------------------------


class TestDuplicateFindingCollapse:
    def test_duplicate_finding_key_collapsed(self) -> None:
        # Multiple traversal paths to same criterion should yield one finding.
        intent = _make_intent(criteria=[("AC-1", "stmt")])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        # Only one finding for AC-1
        ac1_findings = [f for f in orphans if "AC-1" in f.primary_reference.identity]
        assert len(ac1_findings) == 1


# ---------------------------------------------------------------------------
# ORPHAN_ACCEPTANCE_CRITERION rule
# ---------------------------------------------------------------------------


class TestOrphanAcceptanceCriterion:
    def test_criterion_with_implementing_task_no_finding(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "Must pass tests")])
        lineage = _make_lineage(
            nodes=[
                ("TASK-001", "INTENT"),
                ("AC-1", "CRITERION"),
                ("TASK-IMPL-1", "TASK"),
            ],
            edges=[("TASK-IMPL-1", "AC-1", "IMPLEMENTS")],
        )
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) == 0

    def test_criterion_without_task_is_error(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "Must pass tests")])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) >= 1
        assert all(f.severity == FindingSeverity.ERROR for f in orphans)

    def test_scoped_criterion_id_accepted(self) -> None:
        intent = _make_intent(task_id="MY-TASK", criteria=[("AC-1", "stmt")])
        lineage = _make_lineage(
            nodes=[
                ("MY-TASK", "INTENT"),
                ("MY-TASK::AC-1", "CRITERION"),
                ("T1", "TASK"),
            ],
            edges=[("T1", "MY-TASK::AC-1", "IMPLEMENTS")],
        )
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) == 0

    def test_multiple_criteria_some_orphaned(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "s1"), ("AC-2", "s2"), ("AC-3", "s3")])
        lineage = _make_lineage(
            nodes=[
                ("AC-1", "CRITERION"),
                ("AC-2", "CRITERION"),
                ("T1", "TASK"),
            ],
            edges=[("T1", "AC-1", "IMPLEMENTS")],  # Only AC-1 implemented
        )
        report = analyze(intent, lineage)
        orphans = {
            f.primary_reference.identity
            for f in report.findings
            if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION
        }
        # AC-2 orphaned (node exists, no impl)
        # AC-3 orphaned (no node, no impl)
        assert any("AC-2" in ident for ident in orphans)
        assert any("AC-3" in ident for ident in orphans)
        # AC-1 not orphaned
        assert not any(ident.endswith("::AC-1") for ident in orphans)

    def test_many_tasks_one_criterion_pass(self) -> None:
        """Many TASK → IMPLEMENTS → one CRITERION is valid (many-to-one)."""
        intent = _make_intent(criteria=[("AC-1", "stmt")])
        lineage = _make_lineage(
            nodes=[
                ("AC-1", "CRITERION"),
                ("T1", "TASK"),
                ("T2", "TASK"),
                ("T3", "TASK"),
            ],
            edges=[
                ("T1", "AC-1", "IMPLEMENTS"),
                ("T2", "AC-1", "IMPLEMENTS"),
                ("T3", "AC-1", "IMPLEMENTS"),
            ],
        )
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) == 0

    def test_one_task_many_criteria_pass(self) -> None:
        """One TASK → IMPLEMENTS → many CRITERION is valid (one-to-many)."""
        intent = _make_intent(
            criteria=[("AC-1", "s1"), ("AC-2", "s2"), ("AC-3", "s3")]
        )
        lineage = _make_lineage(
            nodes=[
                ("AC-1", "CRITERION"),
                ("AC-2", "CRITERION"),
                ("AC-3", "CRITERION"),
                ("T1", "TASK"),
            ],
            edges=[
                ("T1", "AC-1", "IMPLEMENTS"),
                ("T1", "AC-2", "IMPLEMENTS"),
                ("T1", "AC-3", "IMPLEMENTS"),
            ],
        )
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) == 0


# ---------------------------------------------------------------------------
# ORPHAN_EXECUTION_TASK rule
# ---------------------------------------------------------------------------


class TestOrphanExecutionTask:
    def test_task_with_criterion_no_finding(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[("AC-1", "CRITERION"), ("T1", "TASK")],
            edges=[("T1", "AC-1", "IMPLEMENTS")],
        )
        report = analyze(intent, lineage)
        orphan_tasks = [f for f in report.findings if f.code == FindingCode.ORPHAN_EXECUTION_TASK]
        assert len(orphan_tasks) == 0

    def test_task_without_criterion_is_warning(self) -> None:
        intent = _make_intent()
        lineage = _make_lineage(
            nodes=[("T1", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        orphan_tasks = [f for f in report.findings if f.code == FindingCode.ORPHAN_EXECUTION_TASK]
        assert len(orphan_tasks) == 1
        assert orphan_tasks[0].severity == FindingSeverity.WARNING

    def test_multiple_tasks_some_orphaned(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[
                ("AC-1", "CRITERION"),
                ("T1", "TASK"),  # implements AC-1
                ("T2", "TASK"),  # orphan
            ],
            edges=[("T1", "AC-1", "IMPLEMENTS")],
        )
        report = analyze(intent, lineage)
        orphan_tasks = [f for f in report.findings if f.code == FindingCode.ORPHAN_EXECUTION_TASK]
        assert len(orphan_tasks) == 1
        assert orphan_tasks[0].primary_reference.identity == "T2"


# ---------------------------------------------------------------------------
# ORPHAN_EVIDENCE rule
# ---------------------------------------------------------------------------


class TestOrphanEvidence:
    def test_valid_evidence_chain_pass(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[
                ("AC-1", "CRITERION"),
                ("T1", "TASK"),
                ("E1", "EVIDENCE"),
            ],
            edges=[
                ("T1", "AC-1", "IMPLEMENTS"),
                ("E1", "T1", "VERIFIES"),
            ],
        )
        report = analyze(intent, lineage)
        orphan_evid = [f for f in report.findings if f.code == FindingCode.ORPHAN_EVIDENCE]
        assert len(orphan_evid) == 0

    def test_evidence_without_verifies_is_warning(self) -> None:
        intent = _make_intent()
        lineage = _make_lineage(
            nodes=[("E1", "EVIDENCE")],
            edges=[],
        )
        report = analyze(intent, lineage)
        orphan_evid = [f for f in report.findings if f.code == FindingCode.ORPHAN_EVIDENCE]
        assert len(orphan_evid) == 1
        assert orphan_evid[0].severity == FindingSeverity.WARNING

    def test_evidence_verifies_criterion_pass(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[
                ("AC-1", "CRITERION"),
                ("T1", "TASK"),
                ("E1", "EVIDENCE"),
            ],
            edges=[
                ("T1", "AC-1", "IMPLEMENTS"),
                ("E1", "AC-1", "VERIFIES"),
            ],
        )
        report = analyze(intent, lineage)
        orphan_evid = [f for f in report.findings if f.code == FindingCode.ORPHAN_EVIDENCE]
        assert len(orphan_evid) == 0


# ---------------------------------------------------------------------------
# MUTATION_OUTSIDE_ALLOWED_SCOPE rule
# ---------------------------------------------------------------------------


class TestMutationOutsideAllowedScope:
    def test_task_within_allowed_scope_pass(self) -> None:
        intent = _make_intent(allowed_mutations=["ai_engineering/"])
        lineage = _make_lineage(
            nodes=[("ai_engineering/task_analysis.py", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mut_findings = [f for f in report.findings if f.code == FindingCode.MUTATION_OUTSIDE_ALLOWED_SCOPE]
        assert len(mut_findings) == 0

    def test_task_outside_allowed_scope_is_error(self) -> None:
        intent = _make_intent(allowed_mutations=["ai_engineering/"])
        lineage = _make_lineage(
            nodes=[("scripts/deploy.sh", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mut_findings = [f for f in report.findings if f.code == FindingCode.MUTATION_OUTSIDE_ALLOWED_SCOPE]
        assert len(mut_findings) == 1
        assert mut_findings[0].severity == FindingSeverity.ERROR

    def test_empty_allowed_mutations_no_outside_finding(self) -> None:
        """Empty allowed_mutations means no path restriction — no ERROR."""
        intent = _make_intent(allowed_mutations=[])
        lineage = _make_lineage(
            nodes=[("scripts/anything.py", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mut_findings = [f for f in report.findings if f.code == FindingCode.MUTATION_OUTSIDE_ALLOWED_SCOPE]
        assert len(mut_findings) == 0

    def test_abstract_task_id_not_checked_as_path(self) -> None:
        """TASK nodes without path separator are not treated as paths."""
        intent = _make_intent(allowed_mutations=["ai_engineering/"])
        lineage = _make_lineage(
            nodes=[("TASK-IMPL-1", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mut_findings = [
            f for f in report.findings
            if f.code in (FindingCode.MUTATION_OUTSIDE_ALLOWED_SCOPE, FindingCode.MUTATION_IN_FORBIDDEN_SCOPE)
        ]
        assert len(mut_findings) == 0

    def test_nested_path_within_allowed_prefix(self) -> None:
        intent = _make_intent(allowed_mutations=["tests/"])
        lineage = _make_lineage(
            nodes=[("tests/ai_engineering/test_task_analysis.py", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mut_findings = [f for f in report.findings if f.code == FindingCode.MUTATION_OUTSIDE_ALLOWED_SCOPE]
        assert len(mut_findings) == 0


# ---------------------------------------------------------------------------
# MUTATION_IN_FORBIDDEN_SCOPE rule
# ---------------------------------------------------------------------------


class TestMutationInForbiddenScope:
    def test_task_in_forbidden_scope_is_error(self) -> None:
        intent = _make_intent(
            allowed_mutations=["ai_engineering/"],
            forbidden_mutations=["scripts/"],
        )
        lineage = _make_lineage(
            nodes=[("scripts/deploy.sh", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        forbidden_findings = [f for f in report.findings if f.code == FindingCode.MUTATION_IN_FORBIDDEN_SCOPE]
        assert len(forbidden_findings) == 1
        assert forbidden_findings[0].severity == FindingSeverity.ERROR

    def test_forbidden_wins_over_allowed(self) -> None:
        """When both allowed and forbidden match, forbidden takes precedence."""
        intent = _make_intent(
            allowed_mutations=["scripts/"],
            forbidden_mutations=["scripts/"],
        )
        lineage = _make_lineage(
            nodes=[("scripts/deploy.sh", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        # Should be forbidden, not outside-allowed
        forbidden_findings = [f for f in report.findings if f.code == FindingCode.MUTATION_IN_FORBIDDEN_SCOPE]
        outside_findings = [f for f in report.findings if f.code == FindingCode.MUTATION_OUTSIDE_ALLOWED_SCOPE]
        assert len(forbidden_findings) >= 1
        assert len(outside_findings) == 0


# ---------------------------------------------------------------------------
# REQUIRED_GATE_UNCOVERED rule
# ---------------------------------------------------------------------------


class TestRequiredGateCoverage:
    def test_required_gate_covered_by_node_pass(self) -> None:
        intent = _make_intent(required_gates=["TESTS_PASS"])
        lineage = _make_lineage(
            nodes=[("TESTS_PASS", "EVIDENCE")],
            edges=[],
        )
        report = analyze(intent, lineage)
        gate_findings = [f for f in report.findings if f.code == FindingCode.REQUIRED_GATE_UNCOVERED]
        assert len(gate_findings) == 0

    def test_required_gate_uncovered_is_warning(self) -> None:
        intent = _make_intent(required_gates=["TESTS_PASS"])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        gate_findings = [f for f in report.findings if f.code == FindingCode.REQUIRED_GATE_UNCOVERED]
        assert len(gate_findings) == 1
        assert gate_findings[0].severity == FindingSeverity.WARNING

    def test_no_required_gates_no_findings(self) -> None:
        intent = _make_intent(required_gates=[])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        gate_findings = [f for f in report.findings if f.code == FindingCode.REQUIRED_GATE_UNCOVERED]
        assert len(gate_findings) == 0

    def test_multiple_gates_some_uncovered(self) -> None:
        intent = _make_intent(required_gates=["GATE-A", "GATE-B", "GATE-C"])
        lineage = _make_lineage(
            nodes=[("GATE-A", "EVIDENCE")],
            edges=[],
        )
        report = analyze(intent, lineage)
        gate_findings = [f for f in report.findings if f.code == FindingCode.REQUIRED_GATE_UNCOVERED]
        uncovered = {f.primary_reference.label for f in gate_findings}
        assert "GATE-B" in uncovered
        assert "GATE-C" in uncovered
        assert "GATE-A" not in uncovered


# ---------------------------------------------------------------------------
# SOURCE_IDENTITY_MISMATCH rule
# ---------------------------------------------------------------------------


class TestSourceIdentityMismatch:
    def test_matching_source_identity_pass(self) -> None:
        intent = _make_intent(task_id="TASK-001")
        lineage = _make_lineage(
            nodes=[("TASK-001", "INTENT")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mismatch = [f for f in report.findings if f.code == FindingCode.SOURCE_IDENTITY_MISMATCH]
        assert len(mismatch) == 0

    def test_mismatched_intent_node_is_error(self) -> None:
        intent = _make_intent(task_id="TASK-001")
        lineage = _make_lineage(
            nodes=[("DIFFERENT-TASK", "INTENT")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mismatch = [f for f in report.findings if f.code == FindingCode.SOURCE_IDENTITY_MISMATCH]
        assert len(mismatch) >= 1
        assert all(f.severity == FindingSeverity.ERROR for f in mismatch)

    def test_no_intent_nodes_in_lineage_pass(self) -> None:
        """If lineage has no INTENT nodes, no SOURCE_IDENTITY_MISMATCH is raised."""
        intent = _make_intent(task_id="TASK-001")
        lineage = _make_lineage(
            nodes=[("T1", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage)
        mismatch = [f for f in report.findings if f.code == FindingCode.SOURCE_IDENTITY_MISMATCH]
        assert len(mismatch) == 0


# ---------------------------------------------------------------------------
# Read-only guarantee (ANALYZER_INPUT_MUTATION=0)
# ---------------------------------------------------------------------------


class TestReadOnlyGuarantee:
    def test_intent_not_mutated_by_analysis(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "stmt")])
        # Capture initial state via serialization
        initial_criteria = intent.acceptance_criteria
        initial_constraints = intent.constraints
        analyze(intent, _empty_lineage())
        # Frozen dataclasses cannot be mutated; verify object identity preserved
        assert intent.acceptance_criteria is initial_criteria
        assert intent.constraints is initial_constraints

    def test_lineage_not_mutated_by_analysis(self) -> None:
        lineage = _make_lineage(
            nodes=[("T1", "TASK")],
            edges=[],
        )
        initial_nodes = lineage.nodes
        analyze(_make_intent(), lineage)
        assert lineage.nodes is initial_nodes

    def test_raw_bytes_not_mutated(self) -> None:
        """Loading from bytes does not mutate the original bytes."""
        import json as _json
        raw = _json.dumps({
            "schema_version": 1,
            "task_id": "T1",
            "intent_revision": 1,
            "status": "READY",
            "task_class": "BOUNDED_IMPLEMENTATION",
            "desired_outcome": "test",
            "source_repository": "test/repo",
            "source_main_ref": "main",
            "source_base_sha": "a" * 40,
            "constraints": [],
            "allowed_mutations": [],
            "forbidden_mutations": [],
            "stop_boundary": "LOCAL_DIFF",
            "acceptance_criteria": [],
            "unknowns": [],
            "applicable_invariants": [],
            "required_gates": [],
            "parent_intent_digest": None,
        }).encode("utf-8")
        before_hash = hashlib.sha256(raw).hexdigest()
        load_intent_from_bytes(raw)
        after_hash = hashlib.sha256(raw).hexdigest()
        assert before_hash == after_hash


# ---------------------------------------------------------------------------
# Offline guarantee
# ---------------------------------------------------------------------------


class TestOfflineGuarantee:
    def test_analyze_makes_no_network_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Patch socket to prove no network calls are made during analysis."""
        import socket

        def _no_connect(*args, **kwargs):  # noqa: ANN002,ANN003
            raise AssertionError("Network call detected during analysis!")

        monkeypatch.setattr(socket, "create_connection", _no_connect)
        monkeypatch.setattr(socket, "getaddrinfo", _no_connect)

        intent = _make_intent(criteria=[("AC-1", "s1")])
        lineage = _empty_lineage()
        # Should complete without triggering network
        analyze(intent, lineage)


# ---------------------------------------------------------------------------
# Input loading helpers
# ---------------------------------------------------------------------------


class TestInputLoading:
    def test_load_intent_from_valid_bytes(self) -> None:
        import json as _json
        raw = _json.dumps({
            "schema_version": 1,
            "task_id": "T1",
            "intent_revision": 1,
            "status": "READY",
            "task_class": "BOUNDED_IMPLEMENTATION",
            "desired_outcome": "test",
            "source_repository": "test/repo",
            "source_main_ref": "main",
            "source_base_sha": "a" * 40,
            "constraints": [],
            "allowed_mutations": [],
            "forbidden_mutations": [],
            "stop_boundary": "LOCAL_DIFF",
            "acceptance_criteria": [],
            "unknowns": [],
            "applicable_invariants": [],
            "required_gates": [],
            "parent_intent_digest": None,
        }).encode("utf-8")
        intent = load_intent_from_bytes(raw)
        assert intent.task_id == "T1"

    def test_load_intent_from_invalid_json_raises(self) -> None:
        with pytest.raises(AnalysisInputError) as exc:
            load_intent_from_bytes(b"not json {{{")
        assert "INTENT_INVALID" in exc.value.code

    def test_load_lineage_from_valid_bytes(self) -> None:
        import json as _json
        raw = _json.dumps({
            "schema_version": 1,
            "nodes": [],
            "edges": [],
        }).encode("utf-8")
        lineage = load_lineage_from_bytes(raw)
        assert lineage.nodes == ()

    def test_load_lineage_from_invalid_json_raises(self) -> None:
        with pytest.raises(AnalysisInputError) as exc:
            load_lineage_from_bytes(b"{bad}")
        assert "LINEAGE" in exc.value.code
