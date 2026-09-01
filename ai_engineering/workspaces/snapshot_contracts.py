"""Immutable strongly-typed contracts and blockers for Workspace Snapshots and Diff Artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
import json
from pathlib import Path
import re
from typing import Any, Mapping

_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$", re.IGNORECASE)

WORKSPACE_SNAPSHOT_CONTRACT_VERSION = "4.1.0"
DIFF_ARTIFACT_CONTRACT_VERSION = "4.1.0"


class SnapshotPhase(str, Enum):
    """Explicit phases in candidate workspace execution lifecycle."""

    PRE_EXECUTION = "PRE_EXECUTION"
    POST_EXECUTION = "POST_EXECUTION"
    POST_VALIDATION = "POST_VALIDATION"
    FINAL = "FINAL"


class SnapshotBlockingReason(str, Enum):
    """Machine-readable reason codes for workspace snapshot and diff artifact safety."""

    WORKSPACE_SNAPSHOT_PATH_INVALID = "WORKSPACE_SNAPSHOT_PATH_INVALID"
    WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH = "WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH"
    WORKSPACE_SNAPSHOT_BASE_MISMATCH = "WORKSPACE_SNAPSHOT_BASE_MISMATCH"
    WORKSPACE_SNAPSHOT_PHASE_INVALID = "WORKSPACE_SNAPSHOT_PHASE_INVALID"
    WORKSPACE_SNAPSHOT_CANONICAL_FORBIDDEN = "WORKSPACE_SNAPSHOT_CANONICAL_FORBIDDEN"
    WORKSPACE_SNAPSHOT_COLLISION = "WORKSPACE_SNAPSHOT_COLLISION"
    DIFF_ARTIFACT_INVALID = "DIFF_ARTIFACT_INVALID"
    DIFF_ARTIFACT_DIGEST_MISMATCH = "DIFF_ARTIFACT_DIGEST_MISMATCH"
    # Reused architectural blockers
    WORKTREE_IDENTITY_MISMATCH = "WORKTREE_IDENTITY_MISMATCH"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    CANDIDATE_BASE_DRIFT = "CANDIDATE_BASE_DRIFT"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"


class WorkspaceSnapshotError(Exception):
    """Fail-closed exception for workspace snapshot and diff artifact violations."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def validate_repository_relative_path(path_str: str) -> str:
    """Validate that a path is strictly repository-relative, normalized, and non-escaping."""
    if not path_str or not isinstance(path_str, str):
        raise WorkspaceSnapshotError(
            SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value,
            f"Invalid path string: {path_str!r}",
        )
    # Reject backslashes, absolute paths, drive letters, UNC prefixes, and traversal components
    if "\\" in path_str:
        raise WorkspaceSnapshotError(
            SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value,
            f"Backslashes forbidden in repository-relative paths: {path_str!r}",
        )
    if path_str.startswith("/") or ":" in path_str:
        raise WorkspaceSnapshotError(
            SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value,
            f"Absolute or drive paths forbidden: {path_str!r}",
        )
    p_obj = Path(path_str)
    if p_obj.is_absolute() or ".." in p_obj.parts or "." in p_obj.parts:
        raise WorkspaceSnapshotError(
            SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value,
            f"Path traversal escape forbidden: {path_str!r}",
        )
    return str(p_obj.as_posix())


@dataclass(frozen=True, slots=True)
class DiffArtifact:
    """Deterministic diff metadata and integrity evidence for an isolated workspace."""

    artifact_id: str
    workspace_id: str
    candidate_id: str | None
    base_sha: str
    head_sha: str
    changed_paths: tuple[str, ...]
    diff_stat: str
    diff_digest: str
    patch_size_bytes: int
    binary_files: tuple[str, ...]
    generated_at: str

    def __post_init__(self) -> None:
        if not self.artifact_id or not _IDENTIFIER_RE.match(self.artifact_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
                f"Invalid artifact_id: {self.artifact_id!r}",
            )
        if not self.workspace_id or not _IDENTIFIER_RE.match(self.workspace_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid workspace_id: {self.workspace_id!r}",
            )
        if self.candidate_id is not None and not _IDENTIFIER_RE.match(self.candidate_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid candidate_id: {self.candidate_id!r}",
            )
        if not self.base_sha or not _HEX_40_RE.match(self.base_sha):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_BASE_MISMATCH.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )
        if not self.head_sha or not _HEX_40_RE.match(self.head_sha):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
                f"Invalid head_sha: {self.head_sha!r}",
            )
        if not self.diff_digest or not _HEX_64_RE.match(self.diff_digest):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
                f"Invalid diff_digest: {self.diff_digest!r}",
            )
        if self.patch_size_bytes < 0:
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
                f"patch_size_bytes cannot be negative: {self.patch_size_bytes}",
            )

        # Validate changed paths
        if not isinstance(self.changed_paths, tuple):
            object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        for p in self.changed_paths:
            validate_repository_relative_path(p)

        # Validate binary files
        if not isinstance(self.binary_files, tuple):
            object.__setattr__(self, "binary_files", tuple(self.binary_files))
        for b in self.binary_files:
            validate_repository_relative_path(b)

        # Ensure no duplicates in changed_paths
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
                f"Duplicate paths found in changed_paths: {self.changed_paths}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DIFF_ARTIFACT_CONTRACT_VERSION,
            "artifact_id": self.artifact_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "base_sha": self.base_sha.lower(),
            "head_sha": self.head_sha.lower(),
            "changed_paths": list(self.changed_paths),
            "diff_stat": self.diff_stat,
            "diff_digest": self.diff_digest.lower(),
            "patch_size_bytes": self.patch_size_bytes,
            "binary_files": list(self.binary_files),
            "generated_at": self.generated_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DiffArtifact:
        return cls(
            artifact_id=str(data["artifact_id"]),
            workspace_id=str(data["workspace_id"]),
            candidate_id=str(data["candidate_id"]) if data.get("candidate_id") is not None else None,
            base_sha=str(data["base_sha"]).lower(),
            head_sha=str(data["head_sha"]).lower(),
            changed_paths=tuple(data.get("changed_paths", ())),
            diff_stat=str(data.get("diff_stat", "")),
            diff_digest=str(data["diff_digest"]).lower(),
            patch_size_bytes=int(data.get("patch_size_bytes", 0)),
            binary_files=tuple(data.get("binary_files", ())),
            generated_at=str(data["generated_at"]),
        )

    @classmethod
    def from_json(cls, raw: str) -> DiffArtifact:
        return cls.from_dict(json.loads(raw))


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """Immutable point-in-time snapshot of an isolated workspace state."""

    snapshot_id: str
    workspace_id: str
    task_id: str
    candidate_id: str | None
    run_id: str
    base_sha: str
    head_sha: str
    branch: str
    worktree_path: str
    execution_epoch: int
    phase: SnapshotPhase
    captured_at: str
    git_status: tuple[str, ...]
    changed_paths: tuple[str, ...]
    diff_stat: str
    diff_digest: str
    clean: bool

    def __post_init__(self) -> None:
        if not self.snapshot_id or not _IDENTIFIER_RE.match(self.snapshot_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid snapshot_id: {self.snapshot_id!r}",
            )
        if not self.workspace_id or not _IDENTIFIER_RE.match(self.workspace_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid workspace_id: {self.workspace_id!r}",
            )
        if not self.task_id or not _IDENTIFIER_RE.match(self.task_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if self.candidate_id is not None and not _IDENTIFIER_RE.match(self.candidate_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid candidate_id: {self.candidate_id!r}",
            )
        if not self.run_id or not _IDENTIFIER_RE.match(self.run_id):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid run_id: {self.run_id!r}",
            )
        if not self.base_sha or not _HEX_40_RE.match(self.base_sha):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_BASE_MISMATCH.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )
        if not self.head_sha or not _HEX_40_RE.match(self.head_sha):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_IDENTITY_MISMATCH.value,
                f"Invalid head_sha: {self.head_sha!r}",
            )
        if not self.diff_digest or not _HEX_64_RE.match(self.diff_digest):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.DIFF_ARTIFACT_INVALID.value,
                f"Invalid diff_digest: {self.diff_digest!r}",
            )
        if not isinstance(self.phase, SnapshotPhase):
            try:
                object.__setattr__(self, "phase", SnapshotPhase(str(self.phase)))
            except ValueError as exc:
                raise WorkspaceSnapshotError(
                    SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PHASE_INVALID.value,
                    f"Invalid snapshot phase: {self.phase!r}",
                ) from exc

        # Normalize tuples
        if not isinstance(self.git_status, tuple):
            object.__setattr__(self, "git_status", tuple(self.git_status))
        if not isinstance(self.changed_paths, tuple):
            object.__setattr__(self, "changed_paths", tuple(self.changed_paths))

        for p in self.changed_paths:
            validate_repository_relative_path(p)

        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise WorkspaceSnapshotError(
                SnapshotBlockingReason.WORKSPACE_SNAPSHOT_PATH_INVALID.value,
                f"Duplicate paths in changed_paths: {self.changed_paths}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORKSPACE_SNAPSHOT_CONTRACT_VERSION,
            "snapshot_id": self.snapshot_id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "candidate_id": self.candidate_id,
            "run_id": self.run_id,
            "base_sha": self.base_sha.lower(),
            "head_sha": self.head_sha.lower(),
            "branch": self.branch,
            "worktree_path": self.worktree_path,
            "execution_epoch": self.execution_epoch,
            "phase": self.phase.value,
            "captured_at": self.captured_at,
            "git_status": list(self.git_status),
            "changed_paths": list(self.changed_paths),
            "diff_stat": self.diff_stat,
            "diff_digest": self.diff_digest.lower(),
            "clean": self.clean,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> WorkspaceSnapshot:
        phase = SnapshotPhase(data["phase"]) if isinstance(data["phase"], str) else data["phase"]
        return cls(
            snapshot_id=str(data["snapshot_id"]),
            workspace_id=str(data["workspace_id"]),
            task_id=str(data["task_id"]),
            candidate_id=str(data["candidate_id"]) if data.get("candidate_id") is not None else None,
            run_id=str(data["run_id"]),
            base_sha=str(data["base_sha"]).lower(),
            head_sha=str(data["head_sha"]).lower(),
            branch=str(data["branch"]),
            worktree_path=str(data.get("worktree_path", "")),
            execution_epoch=int(data.get("execution_epoch", 1)),
            phase=phase,
            captured_at=str(data["captured_at"]),
            git_status=tuple(data.get("git_status", ())),
            changed_paths=tuple(data.get("changed_paths", ())),
            diff_stat=str(data.get("diff_stat", "")),
            diff_digest=str(data["diff_digest"]).lower(),
            clean=bool(data.get("clean", False)),
        )

    @classmethod
    def from_json(cls, raw: str) -> WorkspaceSnapshot:
        return cls.from_dict(json.loads(raw))
