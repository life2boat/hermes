"""Unit tests for diff artifact generation, digest computation, and verification."""

from __future__ import annotations

import pytest

from ai_engineering.workspaces.diff_artifacts import (
    compute_diff_digest,
    generate_diff_artifact,
    verify_diff_artifact,
)
from ai_engineering.workspaces.snapshot_contracts import (
    SnapshotBlockingReason,
    WorkspaceSnapshotError,
)

BASE_SHA = "ad62a7c79addf912a5b1f640c1ce0e84aa001f65"
HEAD_SHA = "1111111111111111111111111111111111111111"


def test_diff_digest_determinism():
    raw_diff = "diff --git a/service.py b/service.py\n+def foo(): pass\n"
    d1 = compute_diff_digest(raw_diff)
    d2 = compute_diff_digest(raw_diff)
    assert d1 == d2
    assert len(d1) == 64

    # Changed content yields different digest
    diff_modified = "diff --git a/service.py b/service.py\n+def foo(): return 42\n"
    d3 = compute_diff_digest(diff_modified)
    assert d3 != d1


def test_generate_diff_artifact():
    raw_diff = "diff --git a/service.py b/service.py\n+def foo(): pass\n"
    art = generate_diff_artifact(
        workspace_id="ws-01",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        changed_paths=("service.py",),
        diff_stat="1 file changed, 1 insertion(+)",
        raw_diff=raw_diff,
        binary_files=(),
        candidate_id="cand-01",
    )

    assert art.workspace_id == "ws-01"
    assert art.candidate_id == "cand-01"
    assert art.changed_paths == ("service.py",)
    assert art.patch_size_bytes == len(raw_diff.encode("utf-8"))
    assert art.diff_digest == compute_diff_digest(raw_diff)


def test_verify_diff_artifact_success_and_tampering():
    raw_diff = "diff --git a/service.py b/service.py\n+def foo(): pass\n"
    art = generate_diff_artifact(
        workspace_id="ws-01",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        changed_paths=("service.py",),
        diff_stat="1 file changed, 1 insertion(+)",
        raw_diff=raw_diff,
    )

    # Verification with exact diff passes
    assert verify_diff_artifact(art, raw_diff=raw_diff) is True

    # Verification with exact expected digest passes
    assert verify_diff_artifact(art, expected_digest=art.diff_digest) is True

    # Tampered raw diff fails closed
    tampered_diff = "diff --git a/service.py b/service.py\n+def malicious(): pass\n"
    with pytest.raises(WorkspaceSnapshotError) as exc:
        verify_diff_artifact(art, raw_diff=tampered_diff)
    assert exc.value.code == SnapshotBlockingReason.DIFF_ARTIFACT_DIGEST_MISMATCH.value

    # Tampered expected digest fails closed
    with pytest.raises(WorkspaceSnapshotError) as exc:
        verify_diff_artifact(art, expected_digest="0" * 64)
    assert exc.value.code == SnapshotBlockingReason.DIFF_ARTIFACT_DIGEST_MISMATCH.value
