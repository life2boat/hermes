"""Tests for ai_engineering/task_analysis.py — Cross-Artifact Analyzer.

Validates corrected PR-2 semantics:
- Scoped criterion identity only (<task_id>::<criterion_id>)
- SOURCE_IDENTITY_MISMATCH uses independent expected_base_sha
- TASK_IDENTITY_INCONSISTENCY for INTENT node mismatch
- No path-heuristic mutation findings
- Deferred rules not present

Coverage:
- Report schema / serialization
- Finding deduplication
- Deterministic ordering
- ORPHAN_ACCEPTANCE_CRITERION rule (scoped identity)
- ORPHAN_EXECUTION_TASK rule
- ORPHAN_EVIDENCE rule
- SOURCE_IDENTITY_MISMATCH rule
- TASK_IDENTITY_INCONSISTENCY rule
- Deferred rules: no MUTATION_* or REQUIRED_GATE_* findings
- False-positive review: path-like node_ids do NOT create mutation findings
- Read-only guarantee (input mutation=0)
- Offline guarantee (no provider calls)
"""

from __future__ import annotations

import hashlib
import json

import pytest

from ai_engineering.task_analysis import (
    ANALYSIS_REPORT_SCHEMA_VERSION,
    AnalysisInputError,
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
    TaskIntent,
    TaskLineage,
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
        )
        lineage = _empty_lineage()
        r1 = analyze(intent, lineage)
        r2 = analyze(intent, lineage)
        assert [f.code for f in r1.findings] == [f.code for f in r2.findings]
        assert [f.primary_reference.identity for f in r1.findings] == [
            f.primary_reference.identity for f in r2.findings
        ]

    def test_errors_sort_before_warnings(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "stmt")])
        lineage = _make_lineage(nodes=[("T1", "TASK")], edges=[])
        report = analyze(intent, lineage)
        severities = [f.severity for f in report.findings]
        error_indices = [i for i, s in enumerate(severities) if s == FindingSeverity.ERROR]
        warning_indices = [i for i, s in enumerate(severities) if s == FindingSeverity.WARNING]
        if error_indices and warning_indices:
            assert max(error_indices) < min(warning_indices)


# ---------------------------------------------------------------------------
# Duplicate finding collapse
# ---------------------------------------------------------------------------


class TestDuplicateFindingCollapse:
    def test_duplicate_finding_key_collapsed(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "stmt")])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        scoped = f"TASK-001::AC-1"
        ac1_findings = [f for f in orphans if f.primary_reference.identity == scoped]
        assert len(ac1_findings) == 1


# ---------------------------------------------------------------------------
# ORPHAN_ACCEPTANCE_CRITERION — scoped identity
# ---------------------------------------------------------------------------


class TestOrphanAcceptanceCriterion:
    def test_scoped_criterion_id_accepted_pass(self) -> None:
        """Only canonical <task_id>::<criterion_id> is recognized."""
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

    def test_bare_criterion_id_not_canonical(self) -> None:
        """Bare criterion_id without task_id prefix is NOT accepted as canonical."""
        intent = _make_intent(task_id="TASK-001", criteria=[("AC-1", "stmt")])
        lineage = _make_lineage(
            nodes=[
                ("AC-1", "CRITERION"),  # bare, not scoped
                ("T1", "TASK"),
            ],
            edges=[("T1", "AC-1", "IMPLEMENTS")],  # targets bare id
        )
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        # Bare "AC-1" is not a canonical match for scoped "TASK-001::AC-1" → ERROR
        assert len(orphans) >= 1
        assert any(f.severity == FindingSeverity.ERROR for f in orphans)

    def test_criterion_without_implementing_task_is_error(self) -> None:
        intent = _make_intent(task_id="TASK-001", criteria=[("AC-1", "Must pass tests")])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) >= 1
        assert all(f.severity == FindingSeverity.ERROR for f in orphans)

    def test_scoped_finding_identity(self) -> None:
        """Finding identity must use scoped form <task_id>::<criterion_id>."""
        intent = _make_intent(task_id="TASK-001", criteria=[("AC-1", "stmt")])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) == 1
        assert orphans[0].primary_reference.identity == "TASK-001::AC-1"

    def test_many_tasks_one_criterion_pass(self) -> None:
        intent = _make_intent(task_id="T", criteria=[("AC-1", "stmt")])
        lineage = _make_lineage(
            nodes=[
                ("T::AC-1", "CRITERION"),
                ("T1", "TASK"),
                ("T2", "TASK"),
            ],
            edges=[
                ("T1", "T::AC-1", "IMPLEMENTS"),
                ("T2", "T::AC-1", "IMPLEMENTS"),
            ],
        )
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) == 0

    def test_one_task_many_criteria_pass(self) -> None:
        intent = _make_intent(
            task_id="T",
            criteria=[("AC-1", "s1"), ("AC-2", "s2"), ("AC-3", "s3")],
        )
        lineage = _make_lineage(
            nodes=[
                ("T::AC-1", "CRITERION"),
                ("T::AC-2", "CRITERION"),
                ("T::AC-3", "CRITERION"),
                ("T1", "TASK"),
            ],
            edges=[
                ("T1", "T::AC-1", "IMPLEMENTS"),
                ("T1", "T::AC-2", "IMPLEMENTS"),
                ("T1", "T::AC-3", "IMPLEMENTS"),
            ],
        )
        report = analyze(intent, lineage)
        orphans = [f for f in report.findings if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION]
        assert len(orphans) == 0

    def test_multiple_criteria_some_orphaned(self) -> None:
        intent = _make_intent(
            task_id="TASK-001",
            criteria=[("AC-1", "s1"), ("AC-2", "s2"), ("AC-3", "s3")],
        )
        lineage = _make_lineage(
            nodes=[
                ("TASK-001::AC-1", "CRITERION"),
                ("TASK-001::AC-2", "CRITERION"),
                ("T1", "TASK"),
            ],
            edges=[("T1", "TASK-001::AC-1", "IMPLEMENTS")],
        )
        report = analyze(intent, lineage)
        orphan_ids = {
            f.primary_reference.identity
            for f in report.findings
            if f.code == FindingCode.ORPHAN_ACCEPTANCE_CRITERION
        }
        assert "TASK-001::AC-2" in orphan_ids
        assert "TASK-001::AC-3" in orphan_ids
        assert "TASK-001::AC-1" not in orphan_ids


# ---------------------------------------------------------------------------
# ORPHAN_EXECUTION_TASK
# ---------------------------------------------------------------------------


class TestOrphanExecutionTask:
    def test_task_with_criterion_no_finding(self) -> None:
        intent = _make_intent(task_id="T", criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[("T::AC-1", "CRITERION"), ("T1", "TASK")],
            edges=[("T1", "T::AC-1", "IMPLEMENTS")],
        )
        report = analyze(intent, lineage)
        orphan_tasks = [f for f in report.findings if f.code == FindingCode.ORPHAN_EXECUTION_TASK]
        assert len(orphan_tasks) == 0

    def test_task_without_criterion_is_warning(self) -> None:
        intent = _make_intent()
        lineage = _make_lineage(nodes=[("T1", "TASK")], edges=[])
        report = analyze(intent, lineage)
        orphan_tasks = [f for f in report.findings if f.code == FindingCode.ORPHAN_EXECUTION_TASK]
        assert len(orphan_tasks) == 1
        assert orphan_tasks[0].severity == FindingSeverity.WARNING

    def test_multiple_tasks_some_orphaned(self) -> None:
        intent = _make_intent(task_id="T", criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[
                ("T::AC-1", "CRITERION"),
                ("T1", "TASK"),
                ("T2", "TASK"),
            ],
            edges=[("T1", "T::AC-1", "IMPLEMENTS")],
        )
        report = analyze(intent, lineage)
        orphan_tasks = [f for f in report.findings if f.code == FindingCode.ORPHAN_EXECUTION_TASK]
        assert len(orphan_tasks) == 1
        assert orphan_tasks[0].primary_reference.identity == "T2"


# ---------------------------------------------------------------------------
# ORPHAN_EVIDENCE
# ---------------------------------------------------------------------------


class TestOrphanEvidence:
    def test_valid_evidence_chain_pass(self) -> None:
        intent = _make_intent(task_id="T", criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[
                ("T::AC-1", "CRITERION"),
                ("T1", "TASK"),
                ("E1", "EVIDENCE"),
            ],
            edges=[
                ("T1", "T::AC-1", "IMPLEMENTS"),
                ("E1", "T1", "VERIFIES"),
            ],
        )
        report = analyze(intent, lineage)
        orphan_evid = [f for f in report.findings if f.code == FindingCode.ORPHAN_EVIDENCE]
        assert len(orphan_evid) == 0

    def test_evidence_without_verifies_is_warning(self) -> None:
        intent = _make_intent()
        lineage = _make_lineage(nodes=[("E1", "EVIDENCE")], edges=[])
        report = analyze(intent, lineage)
        orphan_evid = [f for f in report.findings if f.code == FindingCode.ORPHAN_EVIDENCE]
        assert len(orphan_evid) == 1
        assert orphan_evid[0].severity == FindingSeverity.WARNING

    def test_evidence_verifies_criterion_pass(self) -> None:
        intent = _make_intent(task_id="T", criteria=[("AC-1", "s1")])
        lineage = _make_lineage(
            nodes=[
                ("T::AC-1", "CRITERION"),
                ("T1", "TASK"),
                ("E1", "EVIDENCE"),
            ],
            edges=[
                ("T1", "T::AC-1", "IMPLEMENTS"),
                ("E1", "T::AC-1", "VERIFIES"),
            ],
        )
        report = analyze(intent, lineage)
        orphan_evid = [f for f in report.findings if f.code == FindingCode.ORPHAN_EVIDENCE]
        assert len(orphan_evid) == 0


# ---------------------------------------------------------------------------
# SOURCE_IDENTITY_MISMATCH — independent expected_base_sha
# ---------------------------------------------------------------------------


class TestSourceIdentityMismatch:
    def test_matching_expected_base_sha_pass(self) -> None:
        sha = "b" * 40
        intent = _make_intent(source_base_sha=sha)
        lineage = _empty_lineage()
        report = analyze(intent, lineage, expected_base_sha=sha)
        mismatch = [f for f in report.findings if f.code == FindingCode.SOURCE_IDENTITY_MISMATCH]
        assert len(mismatch) == 0

    def test_mismatched_expected_base_sha_is_error(self) -> None:
        intent = _make_intent(source_base_sha="a" * 40)
        lineage = _empty_lineage()
        report = analyze(intent, lineage, expected_base_sha="c" * 40)
        mismatch = [f for f in report.findings if f.code == FindingCode.SOURCE_IDENTITY_MISMATCH]
        assert len(mismatch) == 1
        assert mismatch[0].severity == FindingSeverity.ERROR

    def test_no_expected_sha_no_finding(self) -> None:
        """When expected_base_sha is omitted, no SOURCE_IDENTITY_MISMATCH fires."""
        intent = _make_intent(source_base_sha="a" * 40)
        lineage = _empty_lineage()
        report = analyze(intent, lineage)  # no expected_base_sha
        mismatch = [f for f in report.findings if f.code == FindingCode.SOURCE_IDENTITY_MISMATCH]
        assert len(mismatch) == 0

    def test_report_source_base_sha_field_equals_intent_sha(self) -> None:
        sha = "d" * 40
        intent = _make_intent(source_base_sha=sha)
        lineage = _empty_lineage()
        report = analyze(intent, lineage, expected_base_sha=sha)
        assert report.source_base_sha == sha

    def test_mismatch_deterministic(self) -> None:
        intent = _make_intent(source_base_sha="a" * 40)
        lineage = _empty_lineage()
        r1 = analyze(intent, lineage, expected_base_sha="c" * 40)
        r2 = analyze(intent, lineage, expected_base_sha="c" * 40)
        assert serialize_report(r1) == serialize_report(r2)


# ---------------------------------------------------------------------------
# TASK_IDENTITY_INCONSISTENCY
# ---------------------------------------------------------------------------


class TestTaskIdentityInconsistency:
    def test_matching_intent_node_pass(self) -> None:
        intent = _make_intent(task_id="TASK-001")
        lineage = _make_lineage(nodes=[("TASK-001", "INTENT")], edges=[])
        report = analyze(intent, lineage)
        incons = [f for f in report.findings if f.code == FindingCode.TASK_IDENTITY_INCONSISTENCY]
        assert len(incons) == 0

    def test_mismatched_intent_node_is_error(self) -> None:
        intent = _make_intent(task_id="TASK-001")
        lineage = _make_lineage(nodes=[("DIFFERENT-TASK", "INTENT")], edges=[])
        report = analyze(intent, lineage)
        incons = [f for f in report.findings if f.code == FindingCode.TASK_IDENTITY_INCONSISTENCY]
        assert len(incons) >= 1
        assert all(f.severity == FindingSeverity.ERROR for f in incons)

    def test_no_intent_nodes_in_lineage_pass(self) -> None:
        intent = _make_intent(task_id="TASK-001")
        lineage = _make_lineage(nodes=[("T1", "TASK")], edges=[])
        report = analyze(intent, lineage)
        incons = [f for f in report.findings if f.code == FindingCode.TASK_IDENTITY_INCONSISTENCY]
        assert len(incons) == 0


# ---------------------------------------------------------------------------
# FALSE-POSITIVE REVIEW — path-like node_ids must NOT create mutation findings
# ---------------------------------------------------------------------------


class TestPathHeuristicFalsePositives:
    """Regression suite proving path-like TASK node_ids do NOT cause findings.

    PATH_HEURISTIC_FALSE_POSITIVES=0.
    Deferred rules MUTATION_OUTSIDE_ALLOWED_SCOPE and MUTATION_IN_FORBIDDEN_SCOPE
    must not appear in any report.
    """

    _PATH_LIKE_NODE_IDS = [
        "TASK/001",
        "feature/auth",
        "design/v2",
        "AC/group/one",
        "namespace/task",
        "component/subtask",
        "a/b/c/d",
    ]

    def _assert_no_mutation_findings(self, report) -> None:
        from ai_engineering.task_analysis import FindingCode
        mutation_codes = {"MUTATION_OUTSIDE_ALLOWED_SCOPE", "MUTATION_IN_FORBIDDEN_SCOPE"}
        for f in report.findings:
            assert f.code.value not in mutation_codes, (
                f"Unexpected mutation finding {f.code} for path-like node_id"
            )

    def test_path_like_task_ids_no_mutation_findings_empty_allowed(self) -> None:
        intent = _make_intent(allowed_mutations=["ai_engineering/"])
        for nid in self._PATH_LIKE_NODE_IDS:
            lineage = _make_lineage(nodes=[(nid, "TASK")], edges=[])
            report = analyze(intent, lineage)
            self._assert_no_mutation_findings(report)

    def test_feature_auth_no_mutation_finding(self) -> None:
        intent = _make_intent(
            allowed_mutations=["ai_engineering/"],
            forbidden_mutations=["scripts/"],
        )
        lineage = _make_lineage(nodes=[("feature/auth", "TASK")], edges=[])
        report = analyze(intent, lineage)
        self._assert_no_mutation_findings(report)

    def test_no_finding_codes_outside_implemented_set(self) -> None:
        """All emitted finding codes must be from the implemented set."""
        implemented = {
            "ORPHAN_ACCEPTANCE_CRITERION",
            "ORPHAN_EXECUTION_TASK",
            "ORPHAN_EVIDENCE",
            "SOURCE_IDENTITY_MISMATCH",
            "TASK_IDENTITY_INCONSISTENCY",
        }
        intent = _make_intent(
            task_id="T",
            criteria=[("AC-1", "s1")],
            allowed_mutations=["scripts/"],
            forbidden_mutations=["tests/"],
            required_gates=["TESTS_PASS"],
        )
        lineage = _make_lineage(
            nodes=[("feature/auth", "TASK"), ("TASK/001", "TASK")],
            edges=[],
        )
        report = analyze(intent, lineage, expected_base_sha="f" * 40)
        for f in report.findings:
            assert f.code.value in implemented, (
                f"Unexpected finding code emitted: {f.code}"
            )


# ---------------------------------------------------------------------------
# Deferred rules: must NOT appear in reports
# ---------------------------------------------------------------------------


class TestDeferredRulesNotEmitted:
    """Prove that deferred rules produce no findings."""

    def test_mutation_rules_not_emitted(self) -> None:
        intent = _make_intent(
            allowed_mutations=["ai_engineering/"],
            forbidden_mutations=["scripts/"],
        )
        # Include path-like nodes to verify no mutation findings
        lineage = _make_lineage(
            nodes=[
                ("scripts/deploy.sh", "TASK"),
                ("ai_engineering/core.py", "TASK"),
            ],
            edges=[],
        )
        report = analyze(intent, lineage)
        codes = {f.code.value for f in report.findings}
        assert "MUTATION_OUTSIDE_ALLOWED_SCOPE" not in codes
        assert "MUTATION_IN_FORBIDDEN_SCOPE" not in codes

    def test_gate_coverage_rule_not_emitted(self) -> None:
        intent = _make_intent(required_gates=["TESTS_PASS", "SECURITY_SCAN"])
        lineage = _make_lineage(
            nodes=[("TESTS_PASS", "EVIDENCE"), ("SECURITY_SCAN", "EVIDENCE")],
            edges=[],
        )
        report = analyze(intent, lineage)
        codes = {f.code.value for f in report.findings}
        assert "REQUIRED_GATE_UNCOVERED" not in codes

    def test_no_gate_finding_even_for_uncovered_gates(self) -> None:
        intent = _make_intent(required_gates=["GATE-A", "GATE-B"])
        lineage = _empty_lineage()
        report = analyze(intent, lineage)
        codes = {f.code.value for f in report.findings}
        assert "REQUIRED_GATE_UNCOVERED" not in codes


# ---------------------------------------------------------------------------
# Read-only guarantee (ANALYZER_INPUT_MUTATION=0)
# ---------------------------------------------------------------------------


class TestReadOnlyGuarantee:
    def test_intent_not_mutated_by_analysis(self) -> None:
        intent = _make_intent(criteria=[("AC-1", "stmt")])
        initial_criteria = intent.acceptance_criteria
        initial_constraints = intent.constraints
        analyze(intent, _empty_lineage())
        assert intent.acceptance_criteria is initial_criteria
        assert intent.constraints is initial_constraints

    def test_lineage_not_mutated_by_analysis(self) -> None:
        lineage = _make_lineage(nodes=[("T1", "TASK")], edges=[])
        initial_nodes = lineage.nodes
        analyze(_make_intent(), lineage)
        assert lineage.nodes is initial_nodes

    def test_raw_bytes_not_mutated(self) -> None:
        raw = json.dumps({
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
        import socket

        def _no_connect(*args, **kwargs):
            raise AssertionError("Network call detected during analysis!")

        monkeypatch.setattr(socket, "create_connection", _no_connect)
        monkeypatch.setattr(socket, "getaddrinfo", _no_connect)

        intent = _make_intent(criteria=[("AC-1", "s1")])
        lineage = _empty_lineage()
        analyze(intent, lineage)


# ---------------------------------------------------------------------------
# Input loading helpers
# ---------------------------------------------------------------------------


class TestInputLoading:
    def test_load_intent_from_valid_bytes(self) -> None:
        raw = json.dumps({
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
        raw = json.dumps({
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
