"""Read-only runtime observability views (PR-13, additive to PR-12).

Projects runtime execution evidence into a bounded, redacted,
deterministic operator view in the same style as the PR-12 Operator
Observability Plane. The projection is descriptive only: it never
mutates runtime or control-plane state and never exposes secrets, raw
prompts, full child environments, or foreign absolute paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ai_engineering.observability.contracts import ProjectionStatus
from ai_engineering.observability.redaction import redact_operator_dict
from ai_engineering.runtime.runtime_contracts import (
    RUNTIME_SCHEMA_VERSION,
    AgentExecutionEvidence,
)

_MAX_PROCESSES = 100
_MAX_STDOUT_CHARS = 4096
_MAX_STDERR_CHARS = 4096


@dataclass(frozen=True, slots=True)
class RuntimeProcessView:
    """Bounded, redacted operator view of one runtime process."""

    process_id: str
    run_id: str
    workspace_id: str
    candidate_id: str
    execution_host_id: str
    execution_epoch: int
    state: str
    exit_code: int | None
    exit_proven: bool
    timed_out: bool
    cancelled: bool
    cancel_terminal: bool
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_bytes: int
    stderr_bytes: int
    working_directory: str
    started_at: str
    completed_at: str
    blockers: tuple[str, ...]
    stdout_preview: str
    stderr_preview: str

    def to_dict(self) -> dict[str, object]:
        return {
            "process_id": self.process_id,
            "run_id": self.run_id,
            "workspace_id": self.workspace_id,
            "candidate_id": self.candidate_id,
            "execution_host_id": self.execution_host_id,
            "execution_epoch": self.execution_epoch,
            "state": self.state,
            "exit_code": self.exit_code,
            "exit_proven": self.exit_proven,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "cancel_terminal": self.cancel_terminal,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "working_directory": self.working_directory,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "blockers": list(self.blockers),
            "stdout_preview": self.stdout_preview,
            "stderr_preview": self.stderr_preview,
        }


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    """Bounded projection over runtime evidence with disclosed truncation."""

    schema_version: int
    processes: tuple[RuntimeProcessView, ...]
    projection_status: ProjectionStatus
    reason_codes: tuple[str, ...]
    processes_truncated: bool
    process_original_count: int
    process_returned_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "projection_status": self.projection_status.value,
            "reason_codes": list(self.reason_codes),
            "processes": [p.to_dict() for p in self.processes],
            "truncation": {
                "processes_truncated": self.processes_truncated,
                "original_count": self.process_original_count,
                "returned_count": self.process_returned_count,
            },
        }


def _bounded_preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def build_runtime_views(
    evidences: Sequence[AgentExecutionEvidence],
    *,
    max_processes: int = _MAX_PROCESSES,
) -> RuntimeProjection:
    """Project runtime evidence into a deterministic operator view.

    Deterministic ordering (sorted by process_id, then execution_id);
    disclosed truncation; centralized redaction applied to the
    serialized form so secret-shaped values and raw prompt fields can
    never cross the operator boundary.
    """
    reason_codes: list[str] = []

    ordered = sorted(
        evidences,
        key=lambda e: (e.process.process_id if e.process is not None else "", e.execution_id),
    )
    original_count = len(ordered)
    processes_truncated = original_count > max_processes
    selected = ordered[:max_processes]
    if processes_truncated:
        reason_codes.append("OBSERVABILITY_OUTPUT_LIMIT_EXCEEDED")

    views: list[RuntimeProcessView] = []
    for evidence in selected:
        preview_stdout = _bounded_preview(evidence.stdout, _MAX_STDOUT_CHARS)
        if len(evidence.stdout) > _MAX_STDOUT_CHARS:
            reason_codes.append("OBSERVABILITY_OUTPUT_LIMIT_EXCEEDED")
        preview_stderr = _bounded_preview(evidence.stderr, _MAX_STDERR_CHARS)
        if len(evidence.stderr) > _MAX_STDERR_CHARS:
            reason_codes.append("OBSERVABILITY_OUTPUT_LIMIT_EXCEEDED")
        # Redact secret-shaped preview values before they cross the
        # operator boundary (defense in depth; producer correctness is
        # never assumed).
        redacted_stdout, records = redact_operator_dict({"v": preview_stdout})
        redacted_stderr, records_err = redact_operator_dict({"v": preview_stderr})
        if records or records_err:
            reason_codes.append("OBSERVABILITY_REDACTION_REQUIRED")
        preview_stdout = str(redacted_stdout["v"])  # type: ignore[index]
        preview_stderr = str(redacted_stderr["v"])  # type: ignore[index]
        views.append(
            RuntimeProcessView(
                process_id=evidence.process.process_id if evidence.process is not None else evidence.execution_id,
                run_id=evidence.run_id,
                workspace_id=evidence.workspace_id,
                candidate_id=evidence.candidate_id,
                execution_host_id=evidence.execution_host_id,
                execution_epoch=evidence.execution_epoch,
                state=evidence.state,
                exit_code=evidence.exit_code,
                exit_proven=evidence.exit_proven,
                timed_out=evidence.timed_out,
                cancelled=evidence.cancelled,
                cancel_terminal=evidence.cancel_terminal,
                stdout_truncated=evidence.stdout_truncated,
                stderr_truncated=evidence.stderr_truncated,
                stdout_bytes=evidence.stdout_bytes,
                stderr_bytes=evidence.stderr_bytes,
                working_directory=evidence.working_directory,
                started_at=evidence.started_at,
                completed_at=evidence.completed_at,
                blockers=evidence.blockers,
                stdout_preview=preview_stdout,
                stderr_preview=preview_stderr,
            )
        )

    if any(v.blockers for v in views):
        reason_codes.append("OBSERVABILITY_PROJECTION_INCOMPLETE")

    status = ProjectionStatus.COMPLETE
    if processes_truncated or reason_codes:
        status = ProjectionStatus.PARTIAL
    if any("UNVERIFIABLE" in code for code in reason_codes) or any(
        "RUNTIME_PROCESS_UNVERIFIABLE" in v.blockers for v in views
    ):
        status = ProjectionStatus.UNVERIFIABLE

    return RuntimeProjection(
        schema_version=RUNTIME_SCHEMA_VERSION,
        processes=tuple(views),
        projection_status=status,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        processes_truncated=processes_truncated,
        process_original_count=original_count,
        process_returned_count=len(views),
    )
