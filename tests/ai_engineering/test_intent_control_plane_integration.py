"""End-to-end integration test for Hermes Intent Control Plane v1.

Proves complete deterministic composition of PR-1 -> PR-5 without provider or network calls:
1. TaskIntent (v1) & TaskLineage (v1)
2. Clarification & Requirements Quality Gate (PR-3)
3. Cross-Artifact Semantic Analysis (PR-2)
4. Evidence-Bound Convergence (PR-4 / PR-4.1)
5. Effective Policy / Source Attribution (PR-5)
"""

from __future__ import annotations

import hashlib

from ai_engineering.contracts import StopBoundary, TaskClass
from ai_engineering.convergence import (
    ConvergenceBlockingReason,
    ConvergenceStatus,
    EvidenceBundle,
    EvidenceObservation,
    ObservationOutcome,
    TargetKind,
    _compute_bundle_id,
    _compute_observation_id,
    evaluate_convergence,
    validate_evidence_bundle,
)
from ai_engineering.effective_policy import (
    EffectivePolicyStatus,
    ResolutionStatus,
    resolve_effective_policy,
    validate_effective_policy_report,
    verify_effective_policy_report,
)
from ai_engineering.requirements_gate import (
    CLARIFICATION_SCHEMA_VERSION,
    QUALITY_REVIEW_SCHEMA_VERSION,
    ClarificationReport,
    CriterionDimension,
    CriterionReview,
    GateStatus,
    GlobalDimension,
    RequirementsQualityReview,
    ReviewStatus,
    evaluate_requirements_gate,
)
from ai_engineering.task_analysis import analyze
from ai_engineering.task_intent import (
    AcceptanceCriterion,
    IntentStatus,
    TaskIntent,
    TaskLineage,
    intent_digest,
    validate_intent,
    validate_lineage,
)


def _compute_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class TestIntentControlPlaneIntegration:
    """End-to-end integration suite testing PR-1 through PR-5 composition."""

    def test_full_intent_control_plane_deterministic_composition(self) -> None:
        # ----------------------------------------------------------------------
        # 0. Setup test repository context and canonical artifacts
        # ----------------------------------------------------------------------
        subject_sha = "a" * 40
        base_sha = subject_sha

        # ----------------------------------------------------------------------
        # 1. Step 1: TaskIntent (PR-1)
        # ----------------------------------------------------------------------
        raw_intent = TaskIntent(
            schema_version=1,
            task_id="TASK-E2E-001",
            intent_revision=1,
            status=IntentStatus.READY,
            task_class=TaskClass.BOUNDED_IMPLEMENTATION,
            desired_outcome="Implement end-to-end intent control plane integration verification",
            source_repository="life2boat/hermes",
            source_main_ref="main",
            source_base_sha=base_sha,
            constraints=("Must run offline with 0 provider calls",),
            allowed_mutations=(
                "ai_engineering/effective_policy.py",
                "tests/ai_engineering/test_effective_policy.py",
            ),
            forbidden_mutations=(
                "deploy/",
                "production/",
                ".env",
                "gateway/healbite_*.db",
            ),
            stop_boundary=StopBoundary.DRAFT_PR,
            acceptance_criteria=(
                AcceptanceCriterion(
                    "AC1", "All engineering invariants compile without errors"
                ),
                AcceptanceCriterion("AC2", "Unit tests reach 100% pass rate"),
            ),
            unknowns=(),  # Zero unknowns -> Clarification complete
            applicable_invariants=("A1", "A2", "S3", "AI1"),
            required_gates=("CODE_GATE", "SECURITY_GATE"),
            parent_intent_digest=None,
        )

        # Validate intent
        intent = validate_intent(raw_intent)
        i_digest = intent_digest(intent)
        assert i_digest is not None and len(i_digest) == 64

        # ----------------------------------------------------------------------
        # 2. Step 2: Clarification & Requirements Quality Gate (PR-3)
        # ----------------------------------------------------------------------
        clarification = ClarificationReport(
            schema_version=CLARIFICATION_SCHEMA_VERSION,
            clarification_id=_compute_sha256("clarification:TASK-E2E-001"),
            task_id=intent.task_id,
            intent_digest=i_digest,
            intent_revision=intent.intent_revision,
            intent_status=intent.status,
            questions=(),
            blocking_question_count=0,
            ready_for_quality_review=True,
        )

        quality_review = RequirementsQualityReview(
            schema_version=QUALITY_REVIEW_SCHEMA_VERSION,
            review_id=_compute_sha256("review:TASK-E2E-001"),
            task_id=intent.task_id,
            intent_digest=i_digest,
            intent_revision=intent.intent_revision,
            reviewer_id="reviewer-automated",
            criterion_reviews=tuple(
                CriterionReview(
                    c.criterion_id, {d: ReviewStatus.PASS for d in CriterionDimension}
                )
                for c in intent.acceptance_criteria
            ),
            global_reviews={d: ReviewStatus.PASS for d in GlobalDimension},
        )

        req_gate_report = evaluate_requirements_gate(
            intent=intent,
            clarification=clarification,
            review=quality_review,
        )
        assert req_gate_report.status == GateStatus.PASS
        assert req_gate_report.intent_digest == i_digest
        req_gate_id = req_gate_report.gate_id

        # ----------------------------------------------------------------------
        # 3. Step 3: TaskLineage (PR-1) & Cross-Artifact Analysis (PR-2)
        # ----------------------------------------------------------------------
        # Lineage nodes: intent -> criterion -> task -> evidence
        lineage_payload = {
            "schema_version": 1,
            "nodes": [
                {"kind": "INTENT", "node_id": intent.task_id},
                {"kind": "CRITERION", "node_id": f"{intent.task_id}::AC1"},
                {"kind": "CRITERION", "node_id": f"{intent.task_id}::AC2"},
                {"kind": "TASK", "node_id": "T1"},
                {"kind": "TASK", "node_id": "T2"},
                {"kind": "EVIDENCE", "node_id": "EV1"},
                {"kind": "EVIDENCE", "node_id": "EV2"},
            ],
            "edges": [
                {
                    "relation": "IMPLEMENTS",
                    "source_id": "T1",
                    "target_id": f"{intent.task_id}::AC1",
                },
                {
                    "relation": "IMPLEMENTS",
                    "source_id": "T2",
                    "target_id": f"{intent.task_id}::AC2",
                },
                {
                    "relation": "VERIFIES",
                    "source_id": "EV1",
                    "target_id": f"{intent.task_id}::AC1",
                },
                {"relation": "VERIFIES", "source_id": "EV2", "target_id": "T2"},
            ],
        }
        lineage = validate_lineage(lineage_payload)

        # Cross-Artifact Analyzer (PR-2)
        analysis_report = analyze(
            intent=intent,
            lineage=lineage,
            expected_base_sha=base_sha,
        )
        # Zero error findings
        assert analysis_report.error_count == 0
        analysis_id = analysis_report.analysis_id
        assert analysis_id is not None and len(analysis_id) == 64

        # ----------------------------------------------------------------------
        # 4. Step 4: EvidenceBundle & Evidence-Bound Convergence (PR-4 / PR-4.1)
        # ----------------------------------------------------------------------
        art1_digest = _compute_sha256("test run pytest output pass")
        art2_digest = _compute_sha256("security scan passed cleanly")

        obs1_dict = {
            "target_kind": "LINEAGE_EVIDENCE",
            "target_id": "EV1",
            "outcome": "PASS",
            "producer_id": "pytest",
            "artifact_ref": "logs/test1.log",
            "artifact_digest": art1_digest,
        }
        obs2_dict = {
            "target_kind": "LINEAGE_EVIDENCE",
            "target_id": "EV2",
            "outcome": "PASS",
            "producer_id": "pytest",
            "artifact_ref": "logs/test2.log",
            "artifact_digest": art1_digest,
        }
        obs3_dict = {
            "target_kind": "REQUIRED_GATE",
            "target_id": "CODE_GATE",
            "outcome": "PASS",
            "producer_id": "code-eval",
            "artifact_ref": "logs/code_gate.json",
            "artifact_digest": art1_digest,
        }
        obs4_dict = {
            "target_kind": "REQUIRED_GATE",
            "target_id": "SECURITY_GATE",
            "outcome": "PASS",
            "producer_id": "security-scan",
            "artifact_ref": "logs/security_gate.json",
            "artifact_digest": art2_digest,
        }

        observations = (
            EvidenceObservation(
                _compute_observation_id(obs1_dict),
                TargetKind.LINEAGE_EVIDENCE,
                "EV1",
                ObservationOutcome.PASS,
                "pytest",
                "logs/test1.log",
                art1_digest,
            ),
            EvidenceObservation(
                _compute_observation_id(obs2_dict),
                TargetKind.LINEAGE_EVIDENCE,
                "EV2",
                ObservationOutcome.PASS,
                "pytest",
                "logs/test2.log",
                art1_digest,
            ),
            EvidenceObservation(
                _compute_observation_id(obs3_dict),
                TargetKind.REQUIRED_GATE,
                "CODE_GATE",
                ObservationOutcome.PASS,
                "code-eval",
                "logs/code_gate.json",
                art1_digest,
            ),
            EvidenceObservation(
                _compute_observation_id(obs4_dict),
                TargetKind.REQUIRED_GATE,
                "SECURITY_GATE",
                ObservationOutcome.PASS,
                "security-scan",
                "logs/security_gate.json",
                art2_digest,
            ),
        )

        bundle_payload = {
            "task_id": intent.task_id,
            "intent_digest": i_digest,
            "analysis_id": analysis_id,
            "subject_sha": subject_sha,
            "observations": [
                {"observation_id": o.observation_id} for o in observations
            ],
        }
        bundle_id = _compute_bundle_id(bundle_payload)

        raw_bundle = EvidenceBundle(
            schema_version=1,
            bundle_id=bundle_id,
            task_id=intent.task_id,
            intent_digest=i_digest,
            analysis_id=analysis_id,
            subject_sha=subject_sha,
            observations=observations,
        )

        # Validate bundle at public boundary (PR-4.1)
        evidence_bundle = validate_evidence_bundle(raw_bundle)
        assert evidence_bundle.bundle_id == bundle_id

        # Evaluate convergence (PR-4)
        conv_report = evaluate_convergence(
            intent=intent,
            clarification=clarification,
            quality_review=quality_review,
            lineage=lineage,
            bundle=evidence_bundle,
            expected_base_sha=base_sha,
            subject_sha=subject_sha,
        )
        assert conv_report.status == ConvergenceStatus.CONVERGED
        assert conv_report.intent_digest == i_digest
        assert conv_report.analysis_id == analysis_id
        assert conv_report.evidence_bundle_id == bundle_id
        assert conv_report.subject_sha == subject_sha
        assert conv_report.requirements_gate_id == req_gate_id
        assert len(conv_report.blocking_reasons) == 0

        # ----------------------------------------------------------------------
        # 5. Step 5: Effective Policy / Source Attribution (PR-5)
        # ----------------------------------------------------------------------
        invariants_content = """# Hermes Invariants
### A1. Stable conversation prefix
Authority: AGENTS.md, run_agent.py

### A2. Narrow core, gated edges
Authority: AGENTS.md, tools/registry.py

### S3. Production DB mutation barrier
Authority: gateway/healbite_*.py

### AI1 (INV-AI-V2-001). Closed replay evidence
Authority: docs/AGENT_BEHAVIOUR_CONTRACT.md
"""
        gates_content = """# Release Gates
## CODE_GATE
Code quality gate

## SECURITY_GATE
Security vulnerability scan
"""
        source_map_content = """# Hermes Source Map
Authoritative navigation map.
"""
        release_gate_module_content = """from enum import Enum
class GateName(str, Enum):
    CODE_GATE = "CODE_GATE"
    SECURITY_GATE = "SECURITY_GATE"
"""

        mock_blobs = {
            (subject_sha, "docs/HERMES_INVARIANTS.md"): invariants_content.encode(
                "utf-8"
            ),
            (subject_sha, "docs/AGENT_RELEASE_GATES.md"): gates_content.encode("utf-8"),
            (subject_sha, "docs/HERMES_SOURCE_MAP.md"): source_map_content.encode(
                "utf-8"
            ),
            (
                subject_sha,
                "ai_engineering/release_gate.py",
            ): release_gate_module_content.encode("utf-8"),
        }

        def mock_git_reader(sha: str, rel_path: str) -> bytes:
            key = (sha, rel_path)
            if key in mock_blobs:
                return mock_blobs[key]
            raise FileNotFoundError(f"Blob not found: {rel_path} @ {sha}")

        policy_report = resolve_effective_policy(
            intent=intent,
            repository_root="/mock/repo",
            subject_sha=subject_sha,
            git_reader=mock_git_reader,
        )

        validated_policy = validate_effective_policy_report(policy_report)
        assert validated_policy.status == EffectivePolicyStatus.COMPLETE

        # PR-5.1: Authoritative semantic verification against trusted intent and git sources
        verified_policy = verify_effective_policy_report(
            report=validated_policy,
            intent=intent,
            repository_root="/mock/repo",
            subject_sha=subject_sha,
            git_reader=mock_git_reader,
        )
        assert verified_policy.status == EffectivePolicyStatus.COMPLETE
        assert verified_policy.intent_digest == i_digest
        assert verified_policy.subject_sha == subject_sha
        assert len(verified_policy.unresolved_references) == 0

        # Verify all 4 invariants resolved
        assert len(validated_policy.invariant_resolutions) == 4
        for inv_res in validated_policy.invariant_resolutions:
            assert inv_res.resolution_status == ResolutionStatus.RESOLVED
            assert inv_res.source_id is not None

        # Verify both required gates resolved
        assert len(validated_policy.required_gate_resolutions) == 2
        for gate_res in validated_policy.required_gate_resolutions:
            assert gate_res.resolution_status == ResolutionStatus.RESOLVED
            assert gate_res.source_id is not None

        # ----------------------------------------------------------------------
        # 6. Step 6: End-to-End Invariant Assertions
        # ----------------------------------------------------------------------
        # Invariants verification
        assert intent.source_base_sha == base_sha
        assert req_gate_report.intent_digest == i_digest
        assert analysis_report.intent_digest == i_digest
        assert evidence_bundle.intent_digest == i_digest
        assert evidence_bundle.analysis_id == analysis_id
        assert evidence_bundle.subject_sha == subject_sha
        assert conv_report.status == ConvergenceStatus.CONVERGED
        assert validated_policy.status == EffectivePolicyStatus.COMPLETE

        # Authority invariant verification
        assert (
            "deploy/" in intent.forbidden_mutations
            and "production/" in intent.forbidden_mutations
        )

    def test_negative_wrong_evidence_bundle_subject_sha_fails_convergence(self) -> None:
        """Proves that an evidence bundle bound to a mismatched subject_sha results in NOT_CONVERGED."""
        subject_sha = "a" * 40
        wrong_sha = "b" * 40

        intent = validate_intent(
            TaskIntent(
                schema_version=1,
                task_id="TASK-NEG-001",
                intent_revision=1,
                status=IntentStatus.READY,
                task_class=TaskClass.BOUNDED_IMPLEMENTATION,
                desired_outcome="Test negative subject_sha binding in convergence",
                source_repository="life2boat/hermes",
                source_main_ref="main",
                source_base_sha=subject_sha,
                constraints=(),
                allowed_mutations=("ai_engineering/test.py",),
                forbidden_mutations=("production/",),
                stop_boundary=StopBoundary.DRAFT_PR,
                acceptance_criteria=(AcceptanceCriterion("AC1", "Must be green"),),
                unknowns=(),
                applicable_invariants=("A1",),
                required_gates=("CODE_GATE",),
                parent_intent_digest=None,
            )
        )
        i_digest = intent_digest(intent)

        clarification = ClarificationReport(
            schema_version=CLARIFICATION_SCHEMA_VERSION,
            clarification_id=_compute_sha256("clarification:TASK-NEG-001"),
            task_id=intent.task_id,
            intent_digest=i_digest,
            intent_revision=intent.intent_revision,
            intent_status=intent.status,
            questions=(),
            blocking_question_count=0,
            ready_for_quality_review=True,
        )

        quality_review = RequirementsQualityReview(
            schema_version=QUALITY_REVIEW_SCHEMA_VERSION,
            review_id=_compute_sha256("review:TASK-NEG-001"),
            task_id=intent.task_id,
            intent_digest=i_digest,
            intent_revision=intent.intent_revision,
            reviewer_id="reviewer-automated",
            criterion_reviews=(
                CriterionReview(
                    "AC1", {d: ReviewStatus.PASS for d in CriterionDimension}
                ),
            ),
            global_reviews={d: ReviewStatus.PASS for d in GlobalDimension},
        )

        lineage_payload = {
            "schema_version": 1,
            "nodes": [
                {"kind": "INTENT", "node_id": intent.task_id},
                {"kind": "CRITERION", "node_id": f"{intent.task_id}::AC1"},
                {"kind": "EVIDENCE", "node_id": "EV1"},
            ],
            "edges": [
                {
                    "relation": "VERIFIES",
                    "source_id": "EV1",
                    "target_id": f"{intent.task_id}::AC1",
                },
            ],
        }
        lineage = validate_lineage(lineage_payload)
        analysis = analyze(
            intent=intent, lineage=lineage, expected_base_sha=subject_sha
        )

        obs1_dict = {
            "target_kind": "LINEAGE_EVIDENCE",
            "target_id": "EV1",
            "outcome": "PASS",
            "producer_id": "pytest",
            "artifact_ref": "logs/test.log",
            "artifact_digest": _compute_sha256("pass"),
        }
        obs2_dict = {
            "target_kind": "REQUIRED_GATE",
            "target_id": "CODE_GATE",
            "outcome": "PASS",
            "producer_id": "code-eval",
            "artifact_ref": "logs/code_gate.json",
            "artifact_digest": _compute_sha256("code_gate"),
        }

        observations = (
            EvidenceObservation(
                _compute_observation_id(obs1_dict),
                TargetKind.LINEAGE_EVIDENCE,
                "EV1",
                ObservationOutcome.PASS,
                "pytest",
                "logs/test.log",
                _compute_sha256("pass"),
            ),
            EvidenceObservation(
                _compute_observation_id(obs2_dict),
                TargetKind.REQUIRED_GATE,
                "CODE_GATE",
                ObservationOutcome.PASS,
                "code-eval",
                "logs/code_gate.json",
                _compute_sha256("code_gate"),
            ),
        )

        bundle_payload = {
            "task_id": intent.task_id,
            "intent_digest": i_digest,
            "analysis_id": analysis.analysis_id,
            "subject_sha": wrong_sha,  # Mismatched SHA
            "observations": [
                {"observation_id": o.observation_id} for o in observations
            ],
        }
        bundle_id = _compute_bundle_id(bundle_payload)

        evidence_bundle = validate_evidence_bundle(
            EvidenceBundle(
                schema_version=1,
                bundle_id=bundle_id,
                task_id=intent.task_id,
                intent_digest=i_digest,
                analysis_id=analysis.analysis_id,
                subject_sha=wrong_sha,
                observations=observations,
            )
        )

        conv_report = evaluate_convergence(
            intent=intent,
            clarification=clarification,
            quality_review=quality_review,
            lineage=lineage,
            bundle=evidence_bundle,
            expected_base_sha=subject_sha,
            subject_sha=subject_sha,  # Evaluator evaluates against subject_sha
        )
        assert conv_report.status == ConvergenceStatus.NOT_CONVERGED
        assert (
            ConvergenceBlockingReason.EVIDENCE_SUBJECT_SHA_MISMATCH
            in conv_report.blocking_reasons
        )

    def test_negative_unknown_invariant_or_gate_marks_policy_incomplete(self) -> None:
        """Proves that unknown policy references mark EffectivePolicyReport INCOMPLETE without expanding authority."""
        subject_sha = "c" * 40

        intent = validate_intent(
            TaskIntent(
                schema_version=1,
                task_id="TASK-NEG-002",
                intent_revision=1,
                status=IntentStatus.READY,
                task_class=TaskClass.BOUNDED_IMPLEMENTATION,
                desired_outcome="Test unresolved invariant reference in effective policy",
                source_repository="life2boat/hermes",
                source_main_ref="main",
                source_base_sha=subject_sha,
                constraints=(),
                allowed_mutations=("ai_engineering/test.py",),
                forbidden_mutations=("production/",),
                stop_boundary=StopBoundary.DRAFT_PR,
                acceptance_criteria=(AcceptanceCriterion("AC1", "Must be green"),),
                unknowns=(),
                applicable_invariants=("NONEXISTENT_INVARIANT_XYZ",),
                required_gates=("NONEXISTENT_GATE_ABC",),
                parent_intent_digest=None,
            )
        )

        invariants_content = "# Hermes Invariants\n### A1. Stable prefix\n"
        gates_content = "# Release Gates\n## CODE_GATE\n"
        source_map_content = "# Hermes Source Map\n"
        release_gate_module_content = "class GateName:\n    CODE_GATE = 'CODE_GATE'\n"

        def mock_git_reader(sha: str, rel_path: str) -> bytes:
            mapping = {
                "docs/HERMES_INVARIANTS.md": invariants_content.encode("utf-8"),
                "docs/AGENT_RELEASE_GATES.md": gates_content.encode("utf-8"),
                "docs/HERMES_SOURCE_MAP.md": source_map_content.encode("utf-8"),
                "ai_engineering/release_gate.py": release_gate_module_content.encode(
                    "utf-8"
                ),
            }
            if rel_path in mapping:
                return mapping[rel_path]
            raise FileNotFoundError(rel_path)

        policy_report = resolve_effective_policy(
            intent=intent,
            repository_root="/mock/repo",
            subject_sha=subject_sha,
            git_reader=mock_git_reader,
        )

        assert policy_report.status == EffectivePolicyStatus.INCOMPLETE
        assert len(policy_report.unresolved_references) == 2
        assert "NONEXISTENT_INVARIANT_XYZ" in policy_report.unresolved_references
        assert "NONEXISTENT_GATE_ABC" in policy_report.unresolved_references

        # Proves authority was NOT expanded
        assert "production/" in policy_report.task_policy.forbidden_mutations
