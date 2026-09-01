"""Deterministic diff artifact generation and verification utilities."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Sequence
import uuid

from ai_engineering.workspaces.snapshot_contracts import (
    DiffArtifact,
    SnapshotBlockingReason,
    WorkspaceSnapshotError,
    validate_repository_relative_path,
)


def compute_diff_digest(diff_content: bytes | str) -> str:
    """Compute deterministic SHA-256 digest from diff bytes or text."""
    if isinstance(diff_content, str):
        diff_bytes = diff_content.encode("utf-8")
    elif isinstance(diff_content, (bytes, bytearray)):
        diff_bytes = bytes(diff_content)
    else:
        raise WorkspaceSnapshotError(
            SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
            f"Invalid diff content type: {type(diff_content)}",
        )
    return hashlib.sha256(diff_bytes).hexdigest()


def generate_diff_artifact(
    workspace_id: str,
    base_sha: str,
    head_sha: str,
    changed_paths: Sequence[str],
    diff_stat: str,
    raw_diff: bytes | str,
    binary_files: Sequence[str] = (),
    candidate_id: str | None = None,
    artifact_id: str | None = None,
    now: str | None = None,
) -> DiffArtifact:
    """Construct an immutable DiffArtifact with computed digest and validated paths."""
    if artifact_id is None:
        artifact_id = f"diff-{uuid.uuid4().hex[:16]}"
    if now is None:
        now = datetime.now(timezone.utc).isoformat()

    diff_digest = compute_diff_digest(raw_diff)
    diff_bytes = raw_diff.encode("utf-8") if isinstance(raw_diff, str) else bytes(raw_diff)
    patch_size_bytes = len(diff_bytes)

    # Normalize and sort paths deterministically
    sorted_changed = tuple(sorted(set(validate_repository_relative_path(p) for p in changed_paths)))
    sorted_binary = tuple(sorted(set(validate_repository_relative_path(b) for b in binary_files)))

    return DiffArtifact(
        artifact_id=artifact_id,
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        base_sha=base_sha,
        head_sha=head_sha,
        changed_paths=sorted_changed,
        diff_stat=diff_stat.strip(),
        diff_digest=diff_digest,
        patch_size_bytes=patch_size_bytes,
        binary_files=sorted_binary,
        generated_at=now,
    )


def verify_diff_artifact(
    artifact: DiffArtifact,
    raw_diff: bytes | str | None = None,
    expected_digest: str | None = None,
) -> bool:
    """Verify that diff artifact digest matches given diff content or expected digest."""
    if not isinstance(artifact, DiffArtifact):
        raise WorkspaceSnapshotError(
            SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
            "artifact must be an instance of DiffArtifact",
        )

    if raw_diff is not None:
        actual_digest = compute_diff_digest(raw_diff)
        if actual_digest.lower() != artifact.diff_digest.lower():
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_DIGEST_MISMATCH.value,
                f"Diff digest mismatch: computed {actual_digest} != artifact {artifact.diff_digest}",
            )
        return True

    if expected_digest is not None:
        if expected_digest.lower() != artifact.diff_digest.lower():
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_DIGEST_MISMATCH.value,
                f"Expected digest mismatch: {expected_digest} != artifact {artifact.diff_digest}",
            )
        return True

    raise WorkspaceSnapshotError(
        SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
        "Either raw_diff or expected_digest must be provided for verification",
    )
