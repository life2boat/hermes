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
from pathlib import Path

from ai_engineering.contracts import StopBoundary, TaskClass
from ai_engineering.convergence import (
    ConvergenceBlockingReason,
    ConvergenceStatus,
    EvidenceBundle,
    EvidenceObservation,
    ObservationOutcome,
    TargetKind,
    compute_bundle_id,
    compute_observation_id,
    create_evidence_bundle,
    create_evidence_observation,
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
    create_requirements_quality_review,
    evaluate_requirements_gate,
    generate_clarification_report,
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
                AcceptanceCriterion(
                    "AC2", "All required release gates pass deterministically"
                ),
            ),
            unknowns=(),
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
        clarification = generate_clarification_report(intent)
        assert clarification.ready_for_quality_review is True
        assert clarification.blocking_question_count == 0

        c_reviews = [
            CriterionReview(
                c.criterion_id, {d: ReviewStatus.PASS for d in CriterionDimension}
            )
            for c in intent.acceptance_criteria
        ]
        g_reviews = {d: ReviewStatus.PASS for d in GlobalDimension}
        quality_review = create_requirements_quality_review(
            task_id=intent.task_id,
            intent_digest=i_digest,
            intent_revision=intent.intent_revision,
            reviewer_id="reviewer-automated",
            criterion_reviews=c_reviews,
            global_reviews=g_reviews,
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

        obs1 = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="EV1",
            outcome=ObservationOutcome.PASS,
            producer_id="pytest",
            artifact_ref="logs/test1.log",
            artifact_digest=art1_digest,
        )
        obs2 = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="EV2",
            outcome=ObservationOutcome.PASS,
            producer_id="pytest",
            artifact_ref="logs/test2.log",
            artifact_digest=art1_digest,
        )
        obs3 = create_evidence_observation(
            target_kind=TargetKind.REQUIRED_GATE,
            target_id="CODE_GATE",
            outcome=ObservationOutcome.PASS,
            producer_id="code-eval",
            artifact_ref="logs/code_gate.json",
            artifact_digest=art1_digest,
        )
        obs4 = create_evidence_observation(
            target_kind=TargetKind.REQUIRED_GATE,
            target_id="SECURITY_GATE",
            outcome=ObservationOutcome.PASS,
            producer_id="security-scan",
            artifact_ref="logs/security_gate.json",
            artifact_digest=art2_digest,
        )

        evidence_bundle = create_evidence_bundle(
            task_id=intent.task_id,
            intent_digest=i_digest,
            analysis_id=analysis_id,
            subject_sha=subject_sha,
            observations=(obs1, obs2, obs3, obs4),
        )

        # Validate bundle at public boundary (PR-4.1)
        evidence_bundle = validate_evidence_bundle(evidence_bundle)
        assert evidence_bundle.bundle_id == evidence_bundle.bundle_id

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
        assert conv_report.evidence_bundle_id == evidence_bundle.bundle_id
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

        clarification = generate_clarification_report(intent)

        quality_review = create_requirements_quality_review(
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

        obs1 = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="EV1",
            outcome=ObservationOutcome.PASS,
            producer_id="pytest",
            artifact_ref="logs/test.log",
            artifact_digest=_compute_sha256("pass"),
        )
        obs2 = create_evidence_observation(
            target_kind=TargetKind.REQUIRED_GATE,
            target_id="CODE_GATE",
            outcome=ObservationOutcome.PASS,
            producer_id="code-eval",
            artifact_ref="logs/code_gate.json",
            artifact_digest=_compute_sha256("code_gate"),
        )

        evidence_bundle = create_evidence_bundle(
            task_id=intent.task_id,
            intent_digest=i_digest,
            analysis_id=analysis.analysis_id,
            subject_sha=wrong_sha,  # Mismatched SHA
            observations=(obs1, obs2),
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

    def test_full_operational_cli_e2e_subprocess_suite(self, tmp_path) -> None:
        """Full end-to-end integration test executing every CLI entry point as real subprocesses without network."""
        import json
        import subprocess
        import sys
        from ai_engineering.convergence import (
            create_evidence_bundle,
            create_evidence_observation,
            serialize_evidence_bundle,
        )
        from ai_engineering.requirements_gate import (
            create_requirements_quality_review,
            serialize_review,
        )
        from ai_engineering.task_intent import (
            AcceptanceCriterion,
            IntentStatus,
            StopBoundary,
            TaskClass,
            TaskIntent,
            intent_digest,
            serialize_intent,
            serialize_lineage,
            validate_intent,
            validate_lineage,
        )

        # Get current HEAD sha from git
        head_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()

        # 1. Create TaskIntent
        intent = validate_intent(
            TaskIntent(
                schema_version=1,
                task_id="TASK-CLI-E2E-001",
                intent_revision=1,
                status=IntentStatus.READY,
                task_class=TaskClass.SMALL_PRECISE_FIX,
                desired_outcome="CLI subprocess pipeline integration verification",
                source_repository="life2boat/hermes",
                source_main_ref="main",
                source_base_sha=head_sha,
                constraints=("Offline execution only",),
                allowed_mutations=("ai_engineering/", "scripts/"),
                forbidden_mutations=("deploy/", "production/"),
                stop_boundary=StopBoundary.DRAFT_PR,
                acceptance_criteria=(
                    AcceptanceCriterion("AC1", "CLI runs exit 0 deterministically"),
                ),
                unknowns=(),
                applicable_invariants=("A1", "A2"),
                required_gates=("CODE_GATE",),
                parent_intent_digest=None,
            )
        )
        i_digest = intent_digest(intent)

        intent_file = tmp_path / "intent.json"
        intent_file.write_text(serialize_intent(intent), encoding="utf-8")

        # Step 1: scripts/prepare_task.py --intent
        context_file = Path(".task_context/test_cli_e2e_context.json")
        try:
            p1 = subprocess.run(
                [
                    sys.executable,
                    "scripts/prepare_task.py",
                    "--intent",
                    str(intent_file),
                    "--output",
                    str(context_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p1.returncode == 0, f"prepare_task failed: {p1.stderr}"
            assert context_file.exists()

            # Step 2: scripts/clarify_task.py
            clar_file = tmp_path / "clarification.json"
            p2 = subprocess.run(
                [
                    sys.executable,
                    "scripts/clarify_task.py",
                    "--intent",
                    str(intent_file),
                    "--output",
                    str(clar_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p2.returncode == 0, f"clarify_task failed: {p2.stderr}"
            assert clar_file.exists()

            # Step 3: scripts/requirements_gate.py
            rev = create_requirements_quality_review(
                task_id=intent.task_id,
                intent_digest=i_digest,
                intent_revision=intent.intent_revision,
                reviewer_id="reviewer-cli",
                criterion_reviews=[
                    CriterionReview(
                        "AC1", {d: ReviewStatus.PASS for d in CriterionDimension}
                    )
                ],
                global_reviews={d: ReviewStatus.PASS for d in GlobalDimension},
            )
            rev_file = tmp_path / "quality_review.json"
            rev_file.write_text(serialize_review(rev), encoding="utf-8")

            req_gate_file = tmp_path / "requirements_gate.json"
            p3 = subprocess.run(
                [
                    sys.executable,
                    "scripts/requirements_gate.py",
                    "--intent",
                    str(intent_file),
                    "--clarification",
                    str(clar_file),
                    "--review",
                    str(rev_file),
                    "--output",
                    str(req_gate_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p3.returncode == 0, f"requirements_gate failed: {p3.stderr}"
            assert req_gate_file.exists()

            # Step 4: scripts/analyze_task.py
            lineage = validate_lineage({
                "schema_version": 1,
                "nodes": [
                    {"kind": "INTENT", "node_id": intent.task_id},
                    {"kind": "CRITERION", "node_id": f"{intent.task_id}::AC1"},
                    {"kind": "TASK", "node_id": "T1"},
                    {"kind": "EVIDENCE", "node_id": "EV1"},
                ],
                "edges": [
                    {
                        "relation": "IMPLEMENTS",
                        "source_id": "T1",
                        "target_id": f"{intent.task_id}::AC1",
                    },
                    {
                        "relation": "VERIFIES",
                        "source_id": "EV1",
                        "target_id": f"{intent.task_id}::AC1",
                    },
                ],
            })
            lineage_file = tmp_path / "lineage.json"
            lineage_file.write_text(serialize_lineage(lineage), encoding="utf-8")

            analysis_file = tmp_path / "analysis.json"
            p4 = subprocess.run(
                [
                    sys.executable,
                    "scripts/analyze_task.py",
                    "--intent",
                    str(intent_file),
                    "--lineage",
                    str(lineage_file),
                    "--expected-sha",
                    head_sha,
                    "--output",
                    str(analysis_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p4.returncode == 0, f"analyze_task failed: {p4.stderr}"
            assert analysis_file.exists()
            analysis_data = json.loads(analysis_file.read_text(encoding="utf-8"))
            analysis_id = analysis_data["analysis_id"]

            # Step 5: scripts/converge_task.py
            obs1 = create_evidence_observation(
                target_kind=TargetKind.LINEAGE_EVIDENCE,
                target_id="EV1",
                outcome=ObservationOutcome.PASS,
                producer_id="pytest",
                artifact_ref="logs/test.log",
                artifact_digest="0" * 64,
            )
            obs2 = create_evidence_observation(
                target_kind=TargetKind.REQUIRED_GATE,
                target_id="CODE_GATE",
                outcome=ObservationOutcome.PASS,
                producer_id="gate-check",
                artifact_ref="logs/gate.log",
                artifact_digest="1" * 64,
            )
            bundle = create_evidence_bundle(
                task_id=intent.task_id,
                intent_digest=i_digest,
                analysis_id=analysis_id,
                subject_sha=head_sha,
                observations=(obs1, obs2),
            )
            evidence_file = tmp_path / "evidence.json"
            evidence_file.write_text(
                json.dumps(serialize_evidence_bundle(bundle)), encoding="utf-8"
            )

            conv_file = tmp_path / "convergence.json"
            p5 = subprocess.run(
                [
                    sys.executable,
                    "scripts/converge_task.py",
                    "--intent",
                    str(intent_file),
                    "--clarification",
                    str(clar_file),
                    "--review",
                    str(rev_file),
                    "--lineage",
                    str(lineage_file),
                    "--evidence",
                    str(evidence_file),
                    "--expected-base-sha",
                    head_sha,
                    "--subject-sha",
                    head_sha,
                    "--output",
                    str(conv_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p5.returncode == 0, f"converge_task failed: {p5.stderr}"
            assert conv_file.exists()

            # Step 6: scripts/explain_effective_policy.py
            policy_file = tmp_path / "effective_policy.json"
            p6 = subprocess.run(
                [
                    sys.executable,
                    "scripts/explain_effective_policy.py",
                    "--intent",
                    str(intent_file),
                    "--output",
                    str(policy_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            assert p6.returncode == 0, f"explain_effective_policy failed: {p6.stderr}"
            assert policy_file.exists()

            # Step 7: verify_effective_policy_report API
            from ai_engineering.effective_policy import (
                deserialize_effective_policy_report,
                verify_effective_policy_report,
            )

            policy_data = policy_file.read_text(encoding="utf-8")
            policy_report = deserialize_effective_policy_report(policy_data)
            verify_effective_policy_report(policy_report, intent)
        finally:
            context_file.unlink(missing_ok=True)
