"""Immutable dataclasses, lifecycles, and typed error contracts for candidate implementations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
from ai_engineering.workspaces.snapshot_contracts import DiffArtifact, WorkspaceSnapshot

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_\-\.]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)


class CandidateState(StrEnum):
    """Lifecycle states for candidate implementation branches."""

    CREATED = "CREATED"
    WORKSPACE_READY = "WORKSPACE_READY"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"

    def is_terminal(self) -> bool:
        return self in (
            CandidateState.COMPLETED,
            CandidateState.FAILED,
            CandidateState.CANCELLED,
        )


class CandidateBlockingReason(StrEnum):
    """Machine-readable blocker codes for candidate implementations."""

    CANDIDATE_BASE_SHA_MISMATCH = "CANDIDATE_BASE_SHA_MISMATCH"
    CANDIDATE_PATH_ESCAPE = "CANDIDATE_PATH_ESCAPE"
    CANDIDATE_SCOPE_VIOLATION = "CANDIDATE_SCOPE_VIOLATION"
    CANDIDATE_MAIN_WORKTREE_FORBIDDEN = "CANDIDATE_MAIN_WORKTREE_FORBIDDEN"
    CANDIDATE_WORKSPACE_COLLISION = "CANDIDATE_WORKSPACE_COLLISION"
    CANDIDATE_ID_COLLISION = "CANDIDATE_ID_COLLISION"
    CANDIDATE_RESULT_INVALID = "CANDIDATE_RESULT_INVALID"
    CANDIDATE_VALIDATION_FAILED = "CANDIDATE_VALIDATION_FAILED"
    CANDIDATE_BATCH_PARTIAL = "CANDIDATE_BATCH_PARTIAL"
    CANDIDATE_NOT_AUTHORIZED = "CANDIDATE_NOT_AUTHORIZED"
    PARALLELIZATION_STRATEGY_INVALID = "PARALLELIZATION_STRATEGY_INVALID"
    PARALLELIZATION_BUDGET_EXCEEDED = "PARALLELIZATION_BUDGET_EXCEEDED"
    WORKTREE_IDENTITY_MISMATCH = "WORKTREE_IDENTITY_MISMATCH"
    WORKTREE_DIRTY_REUSE = "WORKTREE_DIRTY_REUSE"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"
    RUN_WORKSPACE_MISMATCH = "RUN_WORKSPACE_MISMATCH"


class CandidateError(ValueError):
    """Fail-closed error for candidate implementation lifecycle and policy violations."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True, slots=True)
class CandidateIdentity:
    """Immutable, strongly typed identity for a candidate implementation."""

    candidate_id: str
    task_id: str
    node_id: str
    base_sha: str
    workspace_id: str
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not _IDENTIFIER_RE.match(self.candidate_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid candidate_id: {self.candidate_id!r}",
            )
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if not isinstance(self.node_id, str) or not _IDENTIFIER_RE.match(self.node_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid node_id: {self.node_id!r}",
            )
        if not isinstance(self.base_sha, str) or not _SHA_RE.match(self.base_sha):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )
        if not isinstance(self.workspace_id, str) or not _IDENTIFIER_RE.match(self.workspace_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid workspace_id: {self.workspace_id!r}",
            )
        if not isinstance(self.run_id, str) or not _IDENTIFIER_RE.match(self.run_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid run_id: {self.run_id!r}",
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "base_sha": self.base_sha,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateIdentity:
        return cls(
            candidate_id=data["candidate_id"],
            task_id=data["task_id"],
            node_id=data["node_id"],
            base_sha=data["base_sha"],
            workspace_id=data["workspace_id"],
            run_id=data["run_id"],
        )


@dataclass(frozen=True, slots=True)
class CandidateImplementationRequest:
    """Immutable declaration of a candidate implementation request."""

    candidate_id: str
    task_id: str
    node_id: str
    base_sha: str
    repository: str
    implementation_brief: str
    allowed_paths: tuple[str, ...]
    validation_commands: tuple[tuple[str, ...], ...] = ()
    authorization: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not _IDENTIFIER_RE.match(self.candidate_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid candidate_id: {self.candidate_id!r}",
            )
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if not isinstance(self.node_id, str) or not _IDENTIFIER_RE.match(self.node_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid node_id: {self.node_id!r}",
            )
        if not isinstance(self.base_sha, str) or not _SHA_RE.match(self.base_sha):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value,
                "repository must be a non-empty string path",
            )

        # Validate allowed_paths: must be non-empty, repository-relative, no '..' traversal
        if not isinstance(self.allowed_paths, tuple):
            object.__setattr__(self, "allowed_paths", tuple(self.allowed_paths))

        if not self.allowed_paths:
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_SCOPE_VIOLATION.value,
                "allowed_paths must contain at least one allowed path pattern",
            )

        for p in self.allowed_paths:
            if not isinstance(p, str) or not p.strip():
                raise CandidateError(
                    CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value,
                    f"Invalid allowed path: {p!r}",
                )
            p_obj = Path(p)
            if p_obj.is_absolute() or p.startswith(("/", "\\")) or ":" in p or ".." in p_obj.parts:
                raise CandidateError(
                    CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value,
                    f"Path escape in allowed_paths: {p!r}",
                )

        if not isinstance(self.validation_commands, tuple):
            object.__setattr__(self, "validation_commands", tuple(self.validation_commands))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "base_sha": self.base_sha,
            "repository": self.repository,
            "implementation_brief": self.implementation_brief,
            "allowed_paths": list(self.allowed_paths),
            "validation_commands": [list(cmd) for cmd in self.validation_commands],
            "authorization": self.authorization,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateImplementationRequest:
        val_cmds = tuple(tuple(cmd) for cmd in data.get("validation_commands", ()))
        return cls(
            candidate_id=data["candidate_id"],
            task_id=data["task_id"],
            node_id=data["node_id"],
            base_sha=data["base_sha"],
            repository=data["repository"],
            implementation_brief=data["implementation_brief"],
            allowed_paths=tuple(data["allowed_paths"]),
            validation_commands=val_cmds,
            authorization=data.get("authorization"),
        )


@dataclass(frozen=True, slots=True)
class ValidationCommandResult:
    """Result of running a single validation command inside a candidate worktree."""

    command: tuple[str, ...]
    return_code: int
    stdout: str
    stderr: str
    success: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "return_code": self.return_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationCommandResult:
        return cls(
            command=tuple(data["command"]),
            return_code=data["return_code"],
            stdout=data["stdout"],
            stderr=data["stderr"],
            success=data["success"],
        )


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Immutable evidence result returned from executing a candidate implementation."""

    candidate_id: str
    task_id: str
    node_id: str
    workspace_id: str
    run_id: str
    base_sha: str
    branch: str
    changed_paths: tuple[str, ...]
    diff_summary: str
    validation_results: tuple[ValidationCommandResult, ...]
    state: CandidateState
    blockers: tuple[str, ...]
    completed_at: str
    success: bool
    candidate_head_sha: str | None = None
    pre_execution_snapshot: WorkspaceSnapshot | None = None
    post_execution_snapshot: WorkspaceSnapshot | None = None
    post_validation_snapshot: WorkspaceSnapshot | None = None
    final_snapshot: WorkspaceSnapshot | None = None
    diff_artifact: DiffArtifact | None = None

    def __post_init__(self) -> None:
        # Validate changed_paths are repository-relative and normalized
        if not isinstance(self.changed_paths, tuple):
            object.__setattr__(self, "changed_paths", tuple(self.changed_paths))
        if not isinstance(self.validation_results, tuple):
            object.__setattr__(self, "validation_results", tuple(self.validation_results))
        if not isinstance(self.blockers, tuple):
            object.__setattr__(self, "blockers", tuple(self.blockers))

        for p in self.changed_paths:
            p_obj = Path(p)
            if p_obj.is_absolute() or p.startswith(("/", "\\")) or ":" in p or ".." in p_obj.parts:
                raise CandidateError(
                    CandidateBlockingReason.CANDIDATE_PATH_ESCAPE.value,
                    f"Path escape in changed_paths: {p!r}",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "workspace_id": self.workspace_id,
            "run_id": self.run_id,
            "base_sha": self.base_sha,
            "branch": self.branch,
            "changed_paths": list(self.changed_paths),
            "diff_summary": self.diff_summary,
            "validation_results": [r.to_dict() for r in self.validation_results],
            "state": self.state.value if isinstance(self.state, CandidateState) else str(self.state),
            "blockers": list(self.blockers),
            "completed_at": self.completed_at,
            "success": self.success,
            "candidate_head_sha": self.candidate_head_sha,
            "pre_execution_snapshot": self.pre_execution_snapshot.to_dict() if self.pre_execution_snapshot else None,
            "post_execution_snapshot": self.post_execution_snapshot.to_dict() if self.post_execution_snapshot else None,
            "post_validation_snapshot": self.post_validation_snapshot.to_dict() if self.post_validation_snapshot else None,
            "final_snapshot": self.final_snapshot.to_dict() if self.final_snapshot else None,
            "diff_artifact": self.diff_artifact.to_dict() if self.diff_artifact else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateResult:
        val_results = tuple(ValidationCommandResult.from_dict(d) for d in data.get("validation_results", ()))
        state = CandidateState(data["state"]) if isinstance(data["state"], str) else data["state"]
        pre_snap = WorkspaceSnapshot.from_dict(data["pre_execution_snapshot"]) if data.get("pre_execution_snapshot") else None
        post_snap = WorkspaceSnapshot.from_dict(data["post_execution_snapshot"]) if data.get("post_execution_snapshot") else None
        val_snap = WorkspaceSnapshot.from_dict(data["post_validation_snapshot"]) if data.get("post_validation_snapshot") else None
        final_snap = WorkspaceSnapshot.from_dict(data["final_snapshot"]) if data.get("final_snapshot") else None
        diff_art = DiffArtifact.from_dict(data["diff_artifact"]) if data.get("diff_artifact") else None
        return cls(
            candidate_id=data["candidate_id"],
            task_id=data["task_id"],
            node_id=data["node_id"],
            workspace_id=data["workspace_id"],
            run_id=data["run_id"],
            base_sha=data["base_sha"],
            branch=data["branch"],
            changed_paths=tuple(data.get("changed_paths", ())),
            diff_summary=data.get("diff_summary", ""),
            validation_results=val_results,
            state=state,
            blockers=tuple(data.get("blockers", ())),
            completed_at=data["completed_at"],
            success=data["success"],
            candidate_head_sha=data.get("candidate_head_sha"),
            pre_execution_snapshot=pre_snap,
            post_execution_snapshot=post_snap,
            post_validation_snapshot=val_snap,
            final_snapshot=final_snap,
            diff_artifact=diff_art,
        )


@dataclass(frozen=True, slots=True)
class CandidateImplementationBatch:
    """Batch of candidate implementation requests executed concurrently."""

    batch_id: str
    task_id: str
    node_id: str
    base_sha: str
    candidates: tuple[CandidateImplementationRequest, ...]
    max_parallel: int = 2
    strategy: ParallelizationStrategy = ParallelizationStrategy.CANDIDATE

    def __post_init__(self) -> None:
        if not isinstance(self.batch_id, str) or not _IDENTIFIER_RE.match(self.batch_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid batch_id: {self.batch_id!r}",
            )
        if not isinstance(self.task_id, str) or not _IDENTIFIER_RE.match(self.task_id):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                f"Invalid task_id: {self.task_id!r}",
            )
        if not isinstance(self.base_sha, str) or not _SHA_RE.match(self.base_sha):
            raise CandidateError(
                CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,
                f"Invalid base_sha: {self.base_sha!r}",
            )
        if self.strategy != ParallelizationStrategy.CANDIDATE:
            raise CandidateError(
                CandidateBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value,
                f"Batch requires strategy CANDIDATE, got {self.strategy}",
            )
        if not isinstance(self.candidates, tuple):
            object.__setattr__(self, "candidates", tuple(self.candidates))

        if len(self.candidates) > 3:
            raise CandidateError(
                CandidateBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                f"Candidate count {len(self.candidates)} exceeds hard ceiling 3",
            )

        seen_ids: set[str] = set()
        for cand in self.candidates:
            if cand.task_id != self.task_id:
                raise CandidateError(
                    CandidateBlockingReason.CANDIDATE_RESULT_INVALID.value,
                    f"Candidate task_id {cand.task_id} != batch task_id {self.task_id}",
                )
            if cand.base_sha != self.base_sha:
                raise CandidateError(
                    CandidateBlockingReason.CANDIDATE_BASE_SHA_MISMATCH.value,
                    f"Candidate base_sha {cand.base_sha} != batch base_sha {self.base_sha}",
                )
            if cand.candidate_id in seen_ids:
                raise CandidateError(
                    CandidateBlockingReason.CANDIDATE_ID_COLLISION.value,
                    f"Duplicate candidate_id in batch: {cand.candidate_id}",
                )
            seen_ids.add(cand.candidate_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "base_sha": self.base_sha,
            "candidates": [c.to_dict() for c in self.candidates],
            "max_parallel": self.max_parallel,
            "strategy": self.strategy.value if isinstance(self.strategy, ParallelizationStrategy) else str(self.strategy),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateImplementationBatch:
        cands = tuple(CandidateImplementationRequest.from_dict(d) for d in data.get("candidates", ()))
        strat = ParallelizationStrategy(data.get("strategy", ParallelizationStrategy.CANDIDATE.value))
        return cls(
            batch_id=data["batch_id"],
            task_id=data["task_id"],
            node_id=data["node_id"],
            base_sha=data["base_sha"],
            candidates=cands,
            max_parallel=data.get("max_parallel", 2),
            strategy=strat,
        )


@dataclass(frozen=True, slots=True)
class CandidateBatchAggregate:
    """Aggregate outcome of executing a CandidateImplementationBatch."""

    batch_id: str
    base_sha: str
    results: tuple[CandidateResult, ...]
    failed_candidates: tuple[CandidateResult, ...]
    status: str  # SUCCESS, PARTIAL, FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "base_sha": self.base_sha,
            "results": [r.to_dict() for r in self.results],
            "failed_candidates": [f.to_dict() for f in self.failed_candidates],
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateBatchAggregate:
        res = tuple(CandidateResult.from_dict(d) for d in data.get("results", ()))
        fails = tuple(CandidateResult.from_dict(d) for d in data.get("failed_candidates", ()))
        return cls(
            batch_id=data["batch_id"],
            base_sha=data["base_sha"],
            results=res,
            failed_candidates=fails,
            status=data["status"],
        )
