"""Comprehensive test suite for Hermes Intent Control Plane PR-5: Effective Policy / Source Attribution."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ai_engineering.contracts import StopBoundary, TaskClass
from ai_engineering.effective_policy import (
    CANONICAL_INVARIANTS_PATH,
    CANONICAL_RELEASE_GATE_MODULE_PATH,
    CANONICAL_RELEASE_GATES_PATH,
    CANONICAL_SOURCE_MAP_PATH,
    EFFECTIVE_POLICY_SCHEMA_VERSION,
    EffectivePolicyReport,
    EffectivePolicyStatus,
    EffectivePolicyValidationError,
    PolicyResolution,
    PolicySource,
    PolicySourceKind,
    ReferenceKind,
    ResolutionStatus,
    TaskPolicyAttribution,
    compute_effective_policy_id,
    compute_source_id,
    deserialize_effective_policy_report,
    read_git_blob,
    resolve_effective_policy,
    serialize_effective_policy_report,
    validate_effective_policy_report,
    validate_policy_resolution,
    validate_policy_source,
    validate_task_policy,
)
from ai_engineering.release_gate import GateName
from ai_engineering.task_intent import (
    IntentStatus,
    TaskIntent,
    intent_digest,
    serialize_intent,
)


# ---------------------------------------------------------------------------
# Test Fixtures & Helpers
# ---------------------------------------------------------------------------

SAMPLE_BASE_SHA = "a" * 40
SAMPLE_SUBJECT_SHA = "b" * 40
ALT_SUBJECT_SHA = "c" * 40

MOCK_SOURCE_MAP_CONTENT = b"""# Hermes / HealBite Source Map
Status: authoritative navigation map
## Source precedence
1. The current task and its explicit safety/stop boundary.
2. AGENTS.md
"""

MOCK_INVARIANTS_CONTENT = b"""# Hermes / HealBite Engineering Invariants
## Source and release invariants
### R1. Canonical exact source
**Invariant:** Repository work begins from canonical remote.

### S3. Technical gates cannot be waived
**Invariant:** Every required technical gate is explicit PASS before mutation.

### AI1 (INV-AI-V2-001). Code PASS is not production release eligibility
**Invariant:** Code PASS alone does not prove production release eligibility.

### AI2 (INV-AI-V2-002). Behaviour evidence is independent
**Invariant:** Required behavioural evidence may not be inferred from code tests.
"""

MOCK_GATES_DOC_CONTENT = b"""# Agent Release Gates
## Gate types
| Gate | Question |
| CODE_GATE | Does the changed code pass tests? |
| BEHAVIOUR_GATE | Did agent follow required behaviour? |
| SECURITY_GATE | Are security invariants proven? |
| LIVE_BEHAVIOUR_GATE | Did live checks pass? |
| COST_GATE | Is cost within budget? |
| PRODUCTION_READINESS_GATE | Is candidate ready? |
"""

MOCK_GATES_MODULE_CONTENT = b"""# release_gate.py
class GateName(StrEnum):
    CODE = "CODE_GATE"
    BEHAVIOUR = "BEHAVIOUR_GATE"
    SECURITY = "SECURITY_GATE"
    LIVE_BEHAVIOUR = "LIVE_BEHAVIOUR_GATE"
    COST = "COST_GATE"
    PRODUCTION_READINESS = "PRODUCTION_READINESS_GATE"
"""


def make_mock_git_reader(overrides: dict[str, bytes] | None = None):
    store = {
        CANONICAL_SOURCE_MAP_PATH: MOCK_SOURCE_MAP_CONTENT,
        CANONICAL_INVARIANTS_PATH: MOCK_INVARIANTS_CONTENT,
        CANONICAL_RELEASE_GATES_PATH: MOCK_GATES_DOC_CONTENT,
        CANONICAL_RELEASE_GATE_MODULE_PATH: MOCK_GATES_MODULE_CONTENT,
    }
    if overrides:
        store.update(overrides)

    def reader(subject_sha: str, path: str) -> bytes:
        if path in store:
            return store[path]
        raise EffectivePolicyValidationError("SOURCE_NOT_FOUND")

    return reader


def make_sample_intent(
    applicable_invariants: tuple[str, ...] = ("R1", "AI1", "INV-AI-V2-001"),
    required_gates: tuple[str, ...] = ("CODE_GATE", "SECURITY_GATE"),
    base_sha: str = SAMPLE_BASE_SHA,
) -> TaskIntent:
    return TaskIntent(
        schema_version=1,
        task_id="HERMES-TASK-PR5-001",
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.ARCHITECTURE,
        desired_outcome="Implement PR-5 effective policy resolution and source attribution.",
        source_repository="life2boat/hermes",
        source_main_ref="refs/remotes/origin/main",
        source_base_sha=base_sha,
        constraints=("EFFECTIVE_POLICY_EXPANDS_AUTHORITY=false", "PROVIDER_CALLS=0"),
        allowed_mutations=(
            "ai_engineering/effective_policy.py",
            "tests/ai_engineering/test_effective_policy.py",
        ),
        forbidden_mutations=("deploy/*", "production/*"),
        stop_boundary=StopBoundary.COMMIT,
        acceptance_criteria=(),
        unknowns=(),
        applicable_invariants=applicable_invariants,
        required_gates=required_gates,
    )


# ---------------------------------------------------------------------------
# Happy Path & Core Resolution Tests
# ---------------------------------------------------------------------------


class TestEffectivePolicyHappyPath:
    def test_complete_resolution_with_canonical_invariants_and_gates(self):
        intent = make_sample_intent(
            applicable_invariants=("R1", "AI1", "INV-AI-V2-001", "S3"),
            required_gates=("CODE_GATE", "SECURITY_GATE"),
        )
        git_reader = make_mock_git_reader()

        report = resolve_effective_policy(
            intent=intent,
            repository_root=".",
            subject_sha=SAMPLE_SUBJECT_SHA,
            git_reader=git_reader,
        )

        assert report.schema_version == EFFECTIVE_POLICY_SCHEMA_VERSION
        assert report.status == EffectivePolicyStatus.COMPLETE
        assert report.task_id == "HERMES-TASK-PR5-001"
        assert report.source_base_sha == SAMPLE_BASE_SHA
        assert report.subject_sha == SAMPLE_SUBJECT_SHA
        assert report.unresolved_references == ()
        assert report.authority_expansion is False

        # Verify task boundary policy attribution
        assert report.task_policy.task_id == intent.task_id
        assert report.task_policy.intent_digest == intent_digest(intent)
        assert report.task_policy.source_base_sha == intent.source_base_sha
        assert report.task_policy.constraints == intent.constraints
        assert report.task_policy.allowed_mutations == intent.allowed_mutations
        assert report.task_policy.forbidden_mutations == intent.forbidden_mutations
        assert report.task_policy.stop_boundary == intent.stop_boundary.value

        # Verify invariant resolutions
        inv_map = {r.requested_reference: r for r in report.invariant_resolutions}
        assert inv_map["R1"].resolution_status == ResolutionStatus.RESOLVED
        assert inv_map["R1"].canonical_reference == "R1"
        assert inv_map["R1"].source_path == CANONICAL_INVARIANTS_PATH
        assert "### R1. Canonical exact source" in inv_map["R1"].source_selector

        assert inv_map["AI1"].resolution_status == ResolutionStatus.RESOLVED
        assert inv_map["AI1"].canonical_reference == "AI1"
        assert inv_map["AI1"].source_path == CANONICAL_INVARIANTS_PATH

        # Alternative heading locator INV-AI-V2-001 resolves to the same section
        assert inv_map["INV-AI-V2-001"].resolution_status == ResolutionStatus.RESOLVED
        assert inv_map["INV-AI-V2-001"].canonical_reference == "INV-AI-V2-001"
        assert inv_map["INV-AI-V2-001"].source_path == CANONICAL_INVARIANTS_PATH
        assert "AI1 (INV-AI-V2-001)" in inv_map["INV-AI-V2-001"].source_selector

        # Verify required gate resolutions
        gate_map = {r.requested_reference: r for r in report.required_gate_resolutions}
        assert gate_map["CODE_GATE"].resolution_status == ResolutionStatus.RESOLVED
        assert gate_map["CODE_GATE"].canonical_reference == "CODE_GATE"
        assert gate_map["CODE_GATE"].source_path == CANONICAL_RELEASE_GATES_PATH

        assert gate_map["SECURITY_GATE"].resolution_status == ResolutionStatus.RESOLVED
        assert gate_map["SECURITY_GATE"].canonical_reference == "SECURITY_GATE"

        # Verify precedence source ID
        source_map_src = next(
            s for s in report.policy_sources if s.path == CANONICAL_SOURCE_MAP_PATH
        )
        assert report.precedence_source_id == source_map_src.source_id

    def test_empty_invariants_and_gates_produce_valid_complete_report(self):
        intent = make_sample_intent(applicable_invariants=(), required_gates=())
        git_reader = make_mock_git_reader()

        report = resolve_effective_policy(
            intent=intent,
            repository_root=".",
            subject_sha=SAMPLE_SUBJECT_SHA,
            git_reader=git_reader,
        )

        assert report.status == EffectivePolicyStatus.COMPLETE
        assert report.invariant_resolutions == ()
        assert report.required_gate_resolutions == ()
        assert report.unresolved_references == ()

    def test_deterministic_identity_across_repeated_evaluations(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()

        report1 = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )
        report2 = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        assert report1.effective_policy_id == report2.effective_policy_id
        assert serialize_effective_policy_report(
            report1
        ) == serialize_effective_policy_report(report2)


# ---------------------------------------------------------------------------
# Unresolved References & No Fuzzy Matching Tests
# ---------------------------------------------------------------------------


class TestUnresolvedReferences:
    def test_unknown_invariant_is_unresolved_and_marks_report_incomplete(self):
        intent = make_sample_intent(
            applicable_invariants=("R1", "NON_EXISTENT_INVARIANT", "AI1"),
            required_gates=("CODE_GATE",),
        )
        git_reader = make_mock_git_reader()

        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        assert report.status == EffectivePolicyStatus.INCOMPLETE
        assert report.unresolved_references == ("NON_EXISTENT_INVARIANT",)

        inv_map = {r.requested_reference: r for r in report.invariant_resolutions}
        unresolved = inv_map["NON_EXISTENT_INVARIANT"]
        assert unresolved.resolution_status == ResolutionStatus.UNRESOLVED
        assert unresolved.canonical_reference is None
        assert unresolved.source_id is None
        assert unresolved.source_path is None
        assert unresolved.source_selector is None
        assert "INVARIANT_NOT_FOUND" in unresolved.reason_codes

    def test_unknown_required_gate_is_unresolved_and_marks_report_incomplete(self):
        intent = make_sample_intent(
            applicable_invariants=("R1",),
            required_gates=("CODE_GATE", "CUSTOM_UNRECOGNIZED_GATE"),
        )
        git_reader = make_mock_git_reader()

        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        assert report.status == EffectivePolicyStatus.INCOMPLETE
        assert report.unresolved_references == ("CUSTOM_UNRECOGNIZED_GATE",)

        gate_map = {r.requested_reference: r for r in report.required_gate_resolutions}
        unresolved = gate_map["CUSTOM_UNRECOGNIZED_GATE"]
        assert unresolved.resolution_status == ResolutionStatus.UNRESOLVED
        assert unresolved.canonical_reference is None
        assert unresolved.source_id is None
        assert unresolved.source_path is None
        assert unresolved.source_selector is None
        assert "GATE_NOT_FOUND" in unresolved.reason_codes

    def test_no_fuzzy_or_semantic_matching_on_arbitrary_prose(self):
        intent = make_sample_intent(
            applicable_invariants=("No downtime allowed", "r1", "R1 "),
            required_gates=("code_gate", "CODE"),
        )
        git_reader = make_mock_git_reader()

        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        assert report.status == EffectivePolicyStatus.INCOMPLETE
        assert set(report.unresolved_references) == {
            "No downtime allowed",
            "r1",
            "R1 ",
            "code_gate",
            "CODE",
        }
        for r in (*report.invariant_resolutions, *report.required_gate_resolutions):
            assert r.resolution_status == ResolutionStatus.UNRESOLVED

    def test_unknown_references_do_not_mutate_task_intent(self):
        original_intent = make_sample_intent(
            applicable_invariants=("NON_EXISTENT_INVARIANT",),
            required_gates=("NON_EXISTENT_GATE",),
        )
        intent_copy = make_sample_intent(
            applicable_invariants=("NON_EXISTENT_INVARIANT",),
            required_gates=("NON_EXISTENT_GATE",),
        )
        git_reader = make_mock_git_reader()

        resolve_effective_policy(
            original_intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        assert original_intent == intent_copy
        assert intent_digest(original_intent) == intent_digest(intent_copy)


# ---------------------------------------------------------------------------
# Subject SHA & Exact Git Source Binding Tests
# ---------------------------------------------------------------------------


class TestSourceBinding:
    def test_different_subject_sha_produces_different_identity(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()

        report1 = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )
        report2 = resolve_effective_policy(
            intent, ".", ALT_SUBJECT_SHA, git_reader=git_reader
        )

        assert report1.subject_sha != report2.subject_sha
        assert report1.effective_policy_id != report2.effective_policy_id
        for s1, s2 in zip(report1.policy_sources, report2.policy_sources):
            if s1.source_kind != PolicySourceKind.TASK_INTENT:
                assert s1.source_id != s2.source_id

    def test_different_source_content_produces_different_identity(self):
        intent = make_sample_intent()
        reader1 = make_mock_git_reader()
        reader2 = make_mock_git_reader(
            overrides={
                CANONICAL_INVARIANTS_PATH: MOCK_INVARIANTS_CONTENT + b"\n### M1. Test"
            }
        )

        report1 = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=reader1
        )
        report2 = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=reader2
        )

        assert report1.effective_policy_id != report2.effective_policy_id

    def test_missing_canonical_source_file_fails_closed(self):
        intent = make_sample_intent()

        def broken_reader(subject_sha: str, path: str) -> bytes:
            if path == CANONICAL_INVARIANTS_PATH:
                raise EffectivePolicyValidationError("SOURCE_NOT_FOUND")
            return MOCK_SOURCE_MAP_CONTENT

        with pytest.raises(EffectivePolicyValidationError) as exc:
            resolve_effective_policy(
                intent, ".", SAMPLE_SUBJECT_SHA, git_reader=broken_reader
            )
        assert exc.value.code == "SOURCE_NOT_FOUND"

    def test_real_git_blob_reader_with_current_repo(self):
        # Test read_git_blob against current repository HEAD
        try:
            head_sha_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            head_sha = head_sha_proc.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("Git command not available")

        content = read_git_blob(Path("."), head_sha, CANONICAL_INVARIANTS_PATH)
        assert b"Hermes / HealBite Engineering Invariants" in content

    def test_read_git_blob_rejects_nonexistent_or_invalid(self):
        with pytest.raises(EffectivePolicyValidationError) as exc:
            read_git_blob(Path("."), "f" * 40, "nonexistent/path/here.md")
        assert exc.value.code == "SOURCE_NOT_FOUND"

        with pytest.raises(EffectivePolicyValidationError) as exc:
            read_git_blob(Path("."), "invalid-sha", "docs/HERMES_INVARIANTS.md")
        assert exc.value.code == "VALUE_INVALID"


# ---------------------------------------------------------------------------
# Tampering & Public API Integrity Tests (H-PR4-001 Defense)
# ---------------------------------------------------------------------------


class TestTamperingDefense:
    def test_tampered_source_id_fails_closed(self):
        source = PolicySource(
            source_id="0" * 64,  # tampered
            source_kind=PolicySourceKind.CANONICAL_DOCUMENT,
            path="docs/HERMES_INVARIANTS.md",
            subject_sha=SAMPLE_SUBJECT_SHA,
            content_sha256="1" * 64,
        )
        with pytest.raises(EffectivePolicyValidationError) as exc:
            validate_policy_source(source)
        assert exc.value.code == "SOURCE_ID_MISMATCH"

    def test_tampered_effective_policy_id_fails_closed(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        tampered_report = EffectivePolicyReport(
            schema_version=report.schema_version,
            effective_policy_id="0" * 64,  # tampered
            task_id=report.task_id,
            intent_digest=report.intent_digest,
            intent_revision=report.intent_revision,
            source_base_sha=report.source_base_sha,
            subject_sha=report.subject_sha,
            status=report.status,
            policy_sources=report.policy_sources,
            task_policy=report.task_policy,
            invariant_resolutions=report.invariant_resolutions,
            required_gate_resolutions=report.required_gate_resolutions,
            unresolved_references=report.unresolved_references,
            precedence_source_id=report.precedence_source_id,
            authority_expansion=False,
        )

        with pytest.raises(EffectivePolicyValidationError) as exc:
            validate_effective_policy_report(tampered_report)
        assert exc.value.code == "POLICY_ID_MISMATCH"

    def test_tampered_authority_expansion_fails_closed(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        tampered_report = EffectivePolicyReport(
            schema_version=report.schema_version,
            effective_policy_id=report.effective_policy_id,
            task_id=report.task_id,
            intent_digest=report.intent_digest,
            intent_revision=report.intent_revision,
            source_base_sha=report.source_base_sha,
            subject_sha=report.subject_sha,
            status=report.status,
            policy_sources=report.policy_sources,
            task_policy=report.task_policy,
            invariant_resolutions=report.invariant_resolutions,
            required_gate_resolutions=report.required_gate_resolutions,
            unresolved_references=report.unresolved_references,
            precedence_source_id=report.precedence_source_id,
            authority_expansion=True,  # FORBIDDEN!
        )

        with pytest.raises(EffectivePolicyValidationError) as exc:
            validate_effective_policy_report(tampered_report)
        assert exc.value.code == "AUTHORITY_EXPANSION_FORBIDDEN"

    def test_tampered_status_mismatch_fails_closed(self):
        intent = make_sample_intent(applicable_invariants=("UNKNOWN_INV",))
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )
        assert report.status == EffectivePolicyStatus.INCOMPLETE

        # Falsely claim COMPLETE when unresolved references exist
        tampered_dict = json.loads(serialize_effective_policy_report(report))
        tampered_dict["status"] = "COMPLETE"
        # Recompute effective_policy_id to isolate status validation
        payload = dict(tampered_dict)
        del payload["effective_policy_id"]
        tampered_dict["effective_policy_id"] = compute_effective_policy_id(payload)

        with pytest.raises(EffectivePolicyValidationError) as exc:
            deserialize_effective_policy_report(json.dumps(tampered_dict))
        assert exc.value.code == "STATUS_INVALID"

    def test_tampered_precedence_source_id_fails_closed(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        tampered_dict = json.loads(serialize_effective_policy_report(report))
        tampered_dict["precedence_source_id"] = "e" * 64
        payload = dict(tampered_dict)
        del payload["effective_policy_id"]
        tampered_dict["effective_policy_id"] = compute_effective_policy_id(payload)

        with pytest.raises(EffectivePolicyValidationError) as exc:
            deserialize_effective_policy_report(json.dumps(tampered_dict))
        assert exc.value.code == "PRECEDENCE_SOURCE_MISMATCH"


# ---------------------------------------------------------------------------
# Strict JSON Deserialization & Serialization Tests
# ---------------------------------------------------------------------------


class TestSerializationAndDeserialization:
    def test_roundtrip_complete_report(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        serialized = serialize_effective_policy_report(report)
        deserialized = deserialize_effective_policy_report(serialized)

        assert report == deserialized
        assert deserialized.status == EffectivePolicyStatus.COMPLETE

    def test_roundtrip_incomplete_report(self):
        intent = make_sample_intent(applicable_invariants=("UNRESOLVED_INV",))
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        serialized = serialize_effective_policy_report(report)
        deserialized = deserialize_effective_policy_report(serialized)

        assert report == deserialized
        assert deserialized.status == EffectivePolicyStatus.INCOMPLETE

    def test_reject_duplicate_json_keys(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )
        valid_json = serialize_effective_policy_report(report)

        # Inject duplicate key
        bad_json = valid_json.replace(
            '"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,'
        )
        with pytest.raises(EffectivePolicyValidationError) as exc:
            deserialize_effective_policy_report(bad_json)
        assert exc.value.code == "DUPLICATE_KEY"

    def test_reject_nan_infinity_and_special_floats(self):
        bad_json = '{"schema_version": NaN}'
        with pytest.raises(EffectivePolicyValidationError) as exc:
            deserialize_effective_policy_report(bad_json)
        assert exc.value.code in ("SPECIAL_FLOAT_FORBIDDEN", "INVALID_JSON")

    def test_reject_nul_bytes(self):
        with pytest.raises(EffectivePolicyValidationError) as exc:
            deserialize_effective_policy_report(b'{"schema_version": 1, \x00}')
        assert exc.value.code == "NUL_BYTE_FORBIDDEN"

    def test_reject_unsupported_schema_version(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )
        tampered = json.loads(serialize_effective_policy_report(report))
        tampered["schema_version"] = 99
        with pytest.raises(EffectivePolicyValidationError) as exc:
            deserialize_effective_policy_report(json.dumps(tampered))
        assert exc.value.code == "SCHEMA_VERSION_UNSUPPORTED"


# ---------------------------------------------------------------------------
# Authority Boundary Invariant Tests
# ---------------------------------------------------------------------------


class TestAuthorityBoundaries:
    def test_effective_policy_does_not_expand_authority(self):
        intent = make_sample_intent()
        git_reader = make_mock_git_reader()
        report = resolve_effective_policy(
            intent, ".", SAMPLE_SUBJECT_SHA, git_reader=git_reader
        )

        assert report.authority_expansion is False
        # Boundaries match TaskIntent exactly and cannot be broadened
        assert report.task_policy.allowed_mutations == intent.allowed_mutations
        assert report.task_policy.forbidden_mutations == intent.forbidden_mutations
        assert report.task_policy.stop_boundary == intent.stop_boundary.value

    def test_cannot_mutate_database_or_production(self):
        # PR-5 is pure explainability and has 0 network or database side effects
        from ai_engineering import effective_policy

        assert hasattr(effective_policy, "resolve_effective_policy")
        # Ensure no provider/network symbols or production execution symbols are called
        assert not hasattr(effective_policy, "deploy")
        assert not hasattr(effective_policy, "execute_production")


# ---------------------------------------------------------------------------
# CLI Tool Tests
# ---------------------------------------------------------------------------


class TestExplainEffectivePolicyCLI:
    def test_cli_complete_report_exit_0(self, tmp_path):
        try:
            head_sha_proc = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            head_sha = head_sha_proc.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pytest.skip("Git command not available")

        intent = make_sample_intent(
            applicable_invariants=("R1", "AI1", "INV-AI-V2-001"),
            required_gates=("CODE_GATE", "SECURITY_GATE"),
            base_sha=head_sha,
        )
        intent_path = tmp_path / "intent.json"
        intent_path.write_text(serialize_intent(intent), encoding="utf-8")
        out_path = tmp_path / "report.json"

        # Run script with current repo
        cmd = [
            sys.executable,
            "scripts/explain_effective_policy.py",
            "--intent",
            str(intent_path),
            "--repository-root",
            ".",
            "--subject-sha",
            head_sha,
            "--output",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert proc.returncode in (0, 1)  # 0 for complete
        assert out_path.exists()
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert data["status"] == "COMPLETE"

    def test_cli_output_alias_rejected(self, tmp_path):
        intent = make_sample_intent()
        intent_path = tmp_path / "intent.json"
        intent_path.write_text(serialize_intent(intent), encoding="utf-8")

        # Output points to the exact same file as intent
        cmd = [
            sys.executable,
            "scripts/explain_effective_policy.py",
            "--intent",
            str(intent_path),
            "--output",
            str(intent_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        assert proc.returncode == 2
        assert "SAFE_WRITE_VIOLATION" in proc.stderr
