"""Runtime evidence helpers and canonical candidate-result adapter (PR-13).

Real execution evidence is converted into the existing canonical
:class:`CandidateResult` (no duplicate model). Candidate completion
requires the full evidence chain: a proven real execution result, the
correct workspace/run/candidate/base bindings, and a POST_EXECUTION
snapshot with diff evidence. Exit code 0 alone is never sufficient.

The adapter never produces validation results or judgement outcomes:
deterministic validation and the CandidateJudge remain mandatory
downstream barriers (RUNTIME-9).
"""

from __future__ import annotations

from ai_engineering.candidates.candidate_contracts import (
    CandidateResult,
    CandidateState,
    ValidationCommandResult,
)
from ai_engineering.runtime.runtime_contracts import (
    AgentExecutionEvidence,
    AgentRuntimeError,
    RuntimeBlockingReason,
)
from ai_engineering.workspaces.snapshot_contracts import DiffArtifact, WorkspaceSnapshot


def build_candidate_result_from_evidence(
    evidence: AgentExecutionEvidence,
    *,
    branch: str,
    post_execution_snapshot: WorkspaceSnapshot | None,
    diff_artifact: DiffArtifact | None,
    pre_execution_snapshot: WorkspaceSnapshot | None = None,
) -> CandidateResult:
    """Convert runtime evidence into a canonical CandidateResult.

    Fail-closed mapping:

    - proven exit 0 + complete snapshot/diff evidence => COMPLETED
    - proven non-zero exit => FAILED (CANDIDATE_VALIDATION_FAILED is NOT
      claimed here; validators own that blocker — a plain FAILED state
      with the runtime blockers is emitted)
    - proven cancellation => CANCELLED
    - timeout / unproven exit / missing snapshot or diff evidence =>
      FAILED with RUNTIME_EVIDENCE_INCOMPLETE (never a false success)
    """
    blockers = list(evidence.blockers)
    completed_evidence = (
        evidence.exit_proven
        and post_execution_snapshot is not None
        and diff_artifact is not None
        and post_execution_snapshot.workspace_id == evidence.workspace_id
        and post_execution_snapshot.run_id == evidence.run_id
        and post_execution_snapshot.base_sha.lower() == evidence.base_sha.lower()
        and post_execution_snapshot.execution_epoch == evidence.execution_epoch
        and diff_artifact.workspace_id == evidence.workspace_id
    )
    if evidence.exit_proven and not completed_evidence:
        blockers.append(RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value)

    changed_paths = tuple(post_execution_snapshot.changed_paths) if post_execution_snapshot is not None else ()
    diff_summary = diff_artifact.diff_stat if diff_artifact is not None else ""

    state = CandidateState.FAILED
    if evidence.cancel_terminal:
        state = CandidateState.CANCELLED
    elif completed_evidence and evidence.exit_code == 0 and not evidence.timed_out:
        state = CandidateState.COMPLETED

    return CandidateResult(
        candidate_id=evidence.candidate_id,
        task_id=evidence.task_id,
        node_id=evidence.node_id,
        workspace_id=evidence.workspace_id,
        run_id=evidence.run_id,
        base_sha=evidence.base_sha,
        branch=branch,
        changed_paths=changed_paths,
        diff_summary=diff_summary,
        # No validation results here: deterministic validators run as a
        # separate downstream barrier and own their own evidence.
        validation_results=(),
        state=state,
        blockers=tuple(dict.fromkeys(blockers)),
        completed_at=evidence.completed_at,
        success=state == CandidateState.COMPLETED,
        candidate_head_sha=post_execution_snapshot.head_sha if post_execution_snapshot is not None else None,
        pre_execution_snapshot=pre_execution_snapshot,
        post_execution_snapshot=post_execution_snapshot,
        diff_artifact=diff_artifact,
    )


def evidence_run_event_payload(evidence: AgentExecutionEvidence) -> dict[str, object]:
    """Build a bounded, secret-free payload for a run lifecycle event."""
    return {
        "exit_code": evidence.exit_code,
        "state": evidence.state,
        "timed_out": evidence.timed_out,
        "cancelled": evidence.cancelled,
        "cancel_terminal": evidence.cancel_terminal,
        "stdout_truncated": evidence.stdout_truncated,
        "stderr_truncated": evidence.stderr_truncated,
        "blockers": list(evidence.blockers),
    }


def require_terminal_evidence(evidence: AgentExecutionEvidence) -> AgentExecutionEvidence:
    """Fail-closed helper: only proven terminal evidence may pass."""
    if not evidence.exit_proven and evidence.state != "FAILED":
        raise AgentRuntimeError(
            RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value,
            f"Evidence for {evidence.execution_id!r} is not proven terminal "
            f"(state={evidence.state}, exit_proven={evidence.exit_proven})",
        )
    return evidence


def validation_results_placeholder() -> tuple[ValidationCommandResult, ...]:
    """Explicit empty marker: the runtime never fabricates validation outcomes."""
    return ()
