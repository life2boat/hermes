"""Strongly typed contracts for Parallel Repository Investigation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy

try:
    from enum import StrEnum
except ImportError:
    from enum import Enum

    class StrEnum(str, Enum):
        pass

INVESTIGATION_CONTRACT_VERSION = 1
INVESTIGATION_RESULT_SCHEMA_VERSION = 1
MAX_SNIPPET_LENGTH = 500


class InvestigationBlockingReason(StrEnum):
    """Deterministic machine-readable reason codes for repository investigation failures."""

    INVESTIGATION_BASE_SHA_MISMATCH = "INVESTIGATION_BASE_SHA_MISMATCH"
    INVESTIGATION_PATH_ESCAPE = "INVESTIGATION_PATH_ESCAPE"
    INVESTIGATION_WRITE_FORBIDDEN = "INVESTIGATION_WRITE_FORBIDDEN"
    INVESTIGATION_COMMAND_FORBIDDEN = "INVESTIGATION_COMMAND_FORBIDDEN"
    INVESTIGATION_SCOPE_INVALID = "INVESTIGATION_SCOPE_INVALID"
    INVESTIGATION_RESULT_INVALID = "INVESTIGATION_RESULT_INVALID"
    INVESTIGATION_BATCH_PARTIAL = "INVESTIGATION_BATCH_PARTIAL"
    PARALLELIZATION_STRATEGY_INVALID = "PARALLELIZATION_STRATEGY_INVALID"
    PARALLELIZATION_BUDGET_EXCEEDED = "PARALLELIZATION_BUDGET_EXCEEDED"
    STALE_RUN_EVENT = "STALE_RUN_EVENT"
    STALE_RUN_MUTATION = "STALE_RUN_MUTATION"


class InvestigationError(ValueError):
    """Fail-closed error for repository investigation contract or execution violations."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _validate_iso_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError as exc:
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
                f"Invalid ISO datetime string: {value}",
            ) from exc
    raise InvestigationError(
        InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
        f"Expected datetime or ISO string, got {type(value)}",
    )


def _format_iso_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@dataclass(frozen=True, slots=True)
class RepositoryMatch:
    """Safe, repository-relative match record."""

    path: str
    line_start: int
    line_end: int
    snippet: str
    match_kind: str = "TEXT"

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
                "path must be a non-empty string",
            )
        # Check for path escape or absolute path
        p = Path(self.path)
        if p.is_absolute() or self.path.startswith(("/", "\\")) or ":" in self.path or ".." in p.parts:
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value,
                f"Match path must be strictly repository-relative, got: {self.path}",
            )
        if not isinstance(self.line_start, int) or self.line_start < 1:
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
                f"line_start must be integer >= 1, got {self.line_start!r}",
            )
        if not isinstance(self.line_end, int) or self.line_end < self.line_start:
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
                f"line_end must be integer >= line_start, got {self.line_end!r}",
            )
        if not isinstance(self.snippet, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
                "snippet must be string",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "snippet": self.snippet[:MAX_SNIPPET_LENGTH],
            "match_kind": self.match_kind,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RepositoryMatch:
        return cls(
            path=str(payload["path"]),
            line_start=int(payload["line_start"]),
            line_end=int(payload["line_end"]),
            snippet=str(payload.get("snippet", "")),
            match_kind=str(payload.get("match_kind", "TEXT")),
        )


@dataclass(frozen=True, slots=True)
class RepositoryInvestigationRequest:
    """Request specification for a single repository investigation branch."""

    investigation_id: str
    task_id: str
    node_id: str
    base_sha: str
    repository_root: str
    query: str
    scope_paths: tuple[str, ...] = ()
    max_results: int = 50

    def __post_init__(self) -> None:
        if not self.investigation_id or not isinstance(self.investigation_id, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_SCOPE_INVALID.value,
                "investigation_id required",
            )
        if not self.task_id or not isinstance(self.task_id, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_SCOPE_INVALID.value,
                "task_id required",
            )
        if not self.base_sha or not isinstance(self.base_sha, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_BASE_SHA_MISMATCH.value,
                "base_sha required",
            )
        if not self.repository_root or not isinstance(self.repository_root, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_SCOPE_INVALID.value,
                "repository_root required",
            )
        # Validate scope paths
        for sp in self.scope_paths:
            if not isinstance(sp, str) or not sp.strip():
                raise InvestigationError(
                    InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value,
                    "scope_path must be non-empty",
                )
            p = Path(sp)
            if p.is_absolute() or sp.startswith(("/", "\\")) or ":" in sp or ".." in p.parts:
                raise InvestigationError(
                    InvestigationBlockingReason.INVESTIGATION_PATH_ESCAPE.value,
                    f"Scope path must be strictly repository-relative, got: {sp}",
                )
        if not isinstance(self.max_results, int) or self.max_results < 1:
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_SCOPE_INVALID.value,
                "max_results must be >= 1",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "base_sha": self.base_sha,
            "repository_root": self.repository_root,
            "query": self.query,
            "scope_paths": list(self.scope_paths),
            "max_results": self.max_results,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RepositoryInvestigationRequest:
        return cls(
            investigation_id=str(payload["investigation_id"]),
            task_id=str(payload["task_id"]),
            node_id=str(payload["node_id"]),
            base_sha=str(payload["base_sha"]),
            repository_root=str(payload["repository_root"]),
            query=str(payload.get("query", "")),
            scope_paths=tuple(str(p) for p in payload.get("scope_paths", ())),
            max_results=int(payload.get("max_results", 50)),
        )


@dataclass(frozen=True, slots=True)
class RepositoryInvestigationResult:
    """Result of a single repository investigation run."""

    investigation_id: str
    run_id: str
    base_sha: str
    matches: tuple[RepositoryMatch, ...]
    summary: str
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    success: bool = True
    error_code: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if not self.investigation_id or not isinstance(self.investigation_id, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
                "investigation_id required",
            )
        if not self.run_id or not isinstance(self.run_id, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_RESULT_INVALID.value,
                "run_id required",
            )
        if not self.base_sha or not isinstance(self.base_sha, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_BASE_SHA_MISMATCH.value,
                "base_sha required",
            )
        object.__setattr__(self, "completed_at", _validate_iso_datetime(self.completed_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INVESTIGATION_RESULT_SCHEMA_VERSION,
            "investigation_id": self.investigation_id,
            "run_id": self.run_id,
            "base_sha": self.base_sha,
            "matches": [m.to_dict() for m in self.matches],
            "summary": self.summary,
            "completed_at": _format_iso_datetime(self.completed_at),
            "success": self.success,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RepositoryInvestigationResult:
        matches = tuple(RepositoryMatch.from_dict(m) for m in payload.get("matches", ()))
        return cls(
            investigation_id=str(payload["investigation_id"]),
            run_id=str(payload["run_id"]),
            base_sha=str(payload["base_sha"]),
            matches=matches,
            summary=str(payload.get("summary", "")),
            completed_at=_validate_iso_datetime(payload.get("completed_at", datetime.now(timezone.utc))),
            success=bool(payload.get("success", True)),
            error_code=payload.get("error_code"),
            error_message=payload.get("error_message"),
        )

    @classmethod
    def from_json(cls, raw: str) -> RepositoryInvestigationResult:
        payload = json.loads(raw)
        return cls.from_dict(payload)


@dataclass(frozen=True, slots=True)
class RepositoryInvestigationBatch:
    """Batch specification for concurrent read-only repository investigations."""

    batch_id: str
    task_id: str
    base_sha: str
    strategy: ParallelizationStrategy = ParallelizationStrategy.PREPARATORY
    investigations: tuple[RepositoryInvestigationRequest, ...] = ()
    max_parallel: int = 3

    def __post_init__(self) -> None:
        if not self.batch_id or not isinstance(self.batch_id, str):
            raise InvestigationError(
                InvestigationBlockingReason.INVESTIGATION_SCOPE_INVALID.value,
                "batch_id required",
            )
        if self.strategy != ParallelizationStrategy.PREPARATORY:
            raise InvestigationError(
                InvestigationBlockingReason.PARALLELIZATION_STRATEGY_INVALID.value,
                f"Repository investigation requires PREPARATORY strategy, got: {self.strategy}",
            )
        if not isinstance(self.max_parallel, int) or self.max_parallel < 1:
            raise InvestigationError(
                InvestigationBlockingReason.PARALLELIZATION_BUDGET_EXCEEDED.value,
                "max_parallel must be integer >= 1",
            )
        for inv in self.investigations:
            if inv.base_sha != self.base_sha:
                raise InvestigationError(
                    InvestigationBlockingReason.INVESTIGATION_BASE_SHA_MISMATCH.value,
                    f"Investigation base_sha {inv.base_sha} does not match batch base_sha {self.base_sha}",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "strategy": self.strategy.value,
            "investigations": [inv.to_dict() for inv in self.investigations],
            "max_parallel": self.max_parallel,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RepositoryInvestigationBatch:
        strat = ParallelizationStrategy(payload.get("strategy", ParallelizationStrategy.PREPARATORY.value))
        invs = tuple(
            RepositoryInvestigationRequest.from_dict(i)
            for i in payload.get("investigations", ())
        )
        return cls(
            batch_id=str(payload["batch_id"]),
            task_id=str(payload["task_id"]),
            base_sha=str(payload["base_sha"]),
            strategy=strat,
            investigations=invs,
            max_parallel=int(payload.get("max_parallel", 3)),
        )


@dataclass(frozen=True, slots=True)
class RepositoryInvestigationAggregate:
    """Consolidated aggregate of multiple parallel repository investigations."""

    batch_id: str
    base_sha: str
    results: tuple[RepositoryInvestigationResult, ...]
    failed_investigations: tuple[RepositoryInvestigationResult, ...] = ()
    status: str = "SUCCESS"  # SUCCESS, PARTIAL, FAILED

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": INVESTIGATION_RESULT_SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "base_sha": self.base_sha,
            "results": [r.to_dict() for r in self.results],
            "failed_investigations": [f.to_dict() for f in self.failed_investigations],
            "status": self.status,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RepositoryInvestigationAggregate:
        results = tuple(RepositoryInvestigationResult.from_dict(r) for r in payload.get("results", ()))
        failed = tuple(RepositoryInvestigationResult.from_dict(f) for f in payload.get("failed_investigations", ()))
        return cls(
            batch_id=str(payload["batch_id"]),
            base_sha=str(payload["base_sha"]),
            results=results,
            failed_investigations=failed,
            status=str(payload.get("status", "SUCCESS")),
        )

    @classmethod
    def from_json(cls, raw: str) -> RepositoryInvestigationAggregate:
        payload = json.loads(raw)
        return cls.from_dict(payload)
