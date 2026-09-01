"""Unit tests for Workspace Snapshot contracts, DiffArtifacts, and serialization."""

from __future__ import annotations

import pytest

from ai_engineering.workspaces.snapshot_contracts import (
    DIFF_ARTIFACT_CONTRACT_VERSION,
    WORKSPACE_SNAPSHOT_CONTRACT_VERSION,
    DiffArtifact,
    SnapshotBlockingReason,
    SnapshotPhase,
    WorkspaceSnapshot,
    WorkspaceSnapshotError,
    validate_repository_relative_path,
)


def test_path_validation_and_fencing():
    # Valid relative paths
    assert validate_repository_relative_path("service.py") == "service.py"
    assert validate_repository_relative_path("src/core/utils.py") == "src/core/utils.py"

    # Absolute Linux path
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("/etc/passwd")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # Absolute Windows path
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("C:/Users/file.txt")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # UNC path
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("\\\\server\\share\\file.txt")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # Backslashes
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("src\\core\\utils.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    # Traversal ..
    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("../escape.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value

    with pytest.raises(WorkspaceSnapshotError) as exc:
        validate_repository_relative_path("src/../../escape.py")
    assert exc.value.code == SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value


def test_diff_artifact_serialization():
    art = DiffArtifact(
        artifact_id="diff-001",
        workspace_id="ws-01",
        candidate_id="cand-01",
        base_sha="ad62a7c79addf912a5b1f640c1ce0e84aa001f65",
        head_sha="1111111111111111111111111111111111111111",
        changed_paths=("service.py", "utils.py"),
        diff_stat="2 files changed, 10 insertions(+)",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        patch_size_bytes=128,
        binary_files=(),
        generated_at="2026-09-01T00:00:00Z",
    )
    d = art.to_dict()
    assert d["schema_version"] == DIFF_ARTIFACT_CONTRACT_VERSION
    assert d["artifact_id"] == "diff-001"
    assert d["changed_paths"] == ["service.py", "utils.py"]

    restored = DiffArtifact.from_dict(d)
    assert restored == art

    json_str = art.to_json()
    assert DiffArtifact.from_json(json_str) == art


def test_workspace_snapshot_serialization():
    snap = WorkspaceSnapshot(
        snapshot_id="snap-001",
        workspace_id="ws-01",
        task_id="task-01",
        candidate_id="cand-01",
        run_id="run-01",
        base_sha="ad62a7c79addf912a5b1f640c1ce0e84aa001f65",
        head_sha="1111111111111111111111111111111111111111",
        branch="codex/candidate/task-01/cand-01",
        worktree_path="/workspaces/ws-01",
        execution_epoch=1,
        phase=SnapshotPhase.PRE_EXECUTION,
        captured_at="2026-09-01T00:00:00Z",
        git_status=(),
        changed_paths=(),
        diff_stat="",
        diff_digest="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        clean=True,
    )
    d = snap.to_dict()
    assert d["schema_version"] == WORKSPACE_SNAPSHOT_CONTRACT_VERSION
    assert d["snapshot_id"] == "snap-001"
    assert d["phase"] == "PRE_EXECUTION"
    assert d["clean"] is True

    restored = WorkspaceSnapshot.from_dict(d)
    assert restored == snap

    json_str = snap.to_json()
    assert WorkspaceSnapshot.from_json(json_str) == snap
