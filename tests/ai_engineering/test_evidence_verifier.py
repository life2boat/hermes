"""Tests for ai_engineering.evidence_verifier."""
from __future__ import annotations
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ai_engineering.evidence_verifier import (
    ArtifactVerificationError,
    verify_evidence_bundle_artifacts,
    serialize_verification_result,
    ARTIFACT_VERIFIER_VERSION,
)
from ai_engineering.convergence import (
    create_evidence_bundle,
    create_evidence_observation,
    EvidenceBundle,
    ObservationOutcome,
    TargetKind,
)
from ai_engineering.task_analysis import analyze
from ai_engineering.task_intent import deserialize_intent, intent_digest

SAMPLE_SHA = "a" * 40
SAMPLE_DIGEST = "b" * 64


def _write_file(root: Path, name: str, content: bytes) -> tuple[str, str]:
    """Write a file and return (name, sha256)."""
    p = root / name
    p.write_bytes(content)
    return name, hashlib.sha256(content).hexdigest()


def _make_bundle_with_ref(root: Path, artifact_ref: str, content: bytes,
                          analysis_id: str | None = None) -> EvidenceBundle:
    """Create a minimal valid EvidenceBundle with one artifact observation."""
    actual_digest = hashlib.sha256(content).hexdigest()
    obs = create_evidence_observation(
        target_kind=TargetKind.LINEAGE_EVIDENCE,
        target_id="ev-node-1",
        outcome=ObservationOutcome.PASS,
        producer_id="test-producer",
        artifact_ref=artifact_ref,
        artifact_digest=actual_digest,
    )
    a_id = analysis_id or ("a" * 64)
    return create_evidence_bundle(
        task_id="TEST-001",
        intent_digest="c" * 64,
        analysis_id=a_id,
        subject_sha=SAMPLE_SHA,
        observations=[obs],
    )


class TestValidPackage:
    def test_valid_file_passes(self, tmp_path: Path) -> None:
        content = b"hello artifact"
        name, digest = _write_file(tmp_path, "artifact.json", content)
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="test-producer",
            artifact_ref=name,
            artifact_digest=digest,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "PASS"
        assert len(result.entries) == 1
        assert result.entries[0].status == "PASS"
        assert result.entries[0].error_code is None

    def test_binary_artifact_passes(self, tmp_path: Path) -> None:
        content = bytes(range(256)) * 100  # arbitrary binary
        name, digest = _write_file(tmp_path, "binary.bin", content)
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="test-producer",
            artifact_ref=name,
            artifact_digest=digest,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "PASS"

    def test_serialize_result(self, tmp_path: Path) -> None:
        content = b"data"
        name, digest = _write_file(tmp_path, "data.json", content)
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref=name,
            artifact_digest=digest,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        serialized = serialize_verification_result(result)
        assert serialized["overall"] == "PASS"
        assert serialized["schema_version"] == ARTIFACT_VERIFIER_VERSION
        assert len(serialized["entries"]) == 1


class TestMissingArtifact:
    def test_missing_file_fails(self, tmp_path: Path) -> None:
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="nonexistent.json",
            artifact_digest="d" * 64,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_NOT_FOUND"


class TestDigestMismatch:
    def test_wrong_digest_fails(self, tmp_path: Path) -> None:
        content = b"real content"
        name, _ = _write_file(tmp_path, "artifact.json", content)
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref=name,
            artifact_digest="e" * 64,  # wrong digest
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_DIGEST_MISMATCH"


class TestPathTraversal:
    def test_dotdot_traversal_fails(self, tmp_path: Path) -> None:
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="../etc/passwd",
            artifact_digest="f" * 64,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_REF_PATH_TRAVERSAL"

    def test_nested_traversal_fails(self, tmp_path: Path) -> None:
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="subdir/../../secret.txt",
            artifact_digest="f" * 64,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_REF_PATH_TRAVERSAL"


class TestAbsolutePath:
    def test_absolute_posix_fails(self, tmp_path: Path) -> None:
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="/etc/passwd",
            artifact_digest="f" * 64,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_REF_ABSOLUTE_PATH"

    def test_absolute_windows_drive_fails(self, tmp_path: Path) -> None:
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="C:/secret.txt",
            artifact_digest="f" * 64,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_REF_ABSOLUTE_PATH"


class TestSymlinkEscape:
    def test_symlink_file_fails(self, tmp_path: Path) -> None:
        if not hasattr(os, 'symlink'):
            pytest.skip("symlinks not supported")
        real_file = tmp_path / "real.txt"
        real_file.write_bytes(b"content")
        digest = hashlib.sha256(b"content").hexdigest()
        link = tmp_path / "link.txt"
        try:
            link.symlink_to(real_file)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks require privileges on this platform")
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="link.txt",
            artifact_digest=digest,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_IS_SYMLINK"


class TestDuplicateRefs:
    def test_duplicate_ref_same_digest_deduplicated(self, tmp_path: Path) -> None:
        content = b"artifact content"
        name, digest = _write_file(tmp_path, "artifact.json", content)
        obs1 = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod1",
            artifact_ref=name,
            artifact_digest=digest,
        )
        # Different producer but same ref/digest -> same key, deduplicated
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs1],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "PASS"
        assert len(result.entries) == 1  # deduplicated


class TestWindowsPathEdge:
    def test_unc_path_fails(self, tmp_path: Path) -> None:
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="\\\\server\\share\\file.txt",
            artifact_digest="f" * 64,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_REF_ABSOLUTE_PATH"

    def test_posix_unc_path_fails(self, tmp_path: Path) -> None:
        obs = create_evidence_observation(
            target_kind=TargetKind.LINEAGE_EVIDENCE,
            target_id="ev-node-1",
            outcome=ObservationOutcome.PASS,
            producer_id="prod",
            artifact_ref="//server/share/file.txt",
            artifact_digest="f" * 64,
        )
        bundle = create_evidence_bundle(
            task_id="TEST-001",
            intent_digest="c" * 64,
            analysis_id="a" * 64,
            subject_sha=SAMPLE_SHA,
            observations=[obs],
        )
        result = verify_evidence_bundle_artifacts(bundle, tmp_path)
        assert result.overall == "FAIL"
        assert result.entries[0].error_code == "ARTIFACT_REF_ABSOLUTE_PATH"
