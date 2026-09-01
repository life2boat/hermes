"""Deterministic read-only projection engine (PR-12).

Builds the immutable :class:`OperatorSnapshot` from explicitly supplied
authoritative records. The projection:

- owns no lifecycle state and never mutates its inputs;
- classifies identity disagreements as CONFLICTED (fail closed);
- keeps remote UNVERIFIABLE distinct from terminal states;
- explains every barrier with machine-readable reason codes;
- bounds and deterministically orders all collections.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from ai_engineering.candidates.candidate_contracts import (
    CandidateIdentity,
    CandidateResult,
    CandidateState,
)
from ai_engineering.contracts import AuthorityBoundary, EffectClass
from ai_engineering.control_plane._evidence_refs import validate_evidence_ref
from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import ControlPlanePhase
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.control_plane.registry import EngineeringCycleRegistry
from ai_engineering.control_plane.contracts import ValidationEvidence
from ai_engineering.execution.host_contracts import ExecutionHostIdentity
from ai_engineering.execution.remote_contracts import RemoteExecutionState
from ai_engineering.execution.remote_state import RemoteExecutionLifecycle
from ai_engineering.execution.run_contracts import RunState
from ai_engineering.execution.run_state import AgentRunRecord
from ai_engineering.judge.judge_contracts import CandidateJudgeResult
from ai_engineering.observability.contracts import (
    OBSERVABILITY_SCHEMA_VERSION,
    BarrierName,
    ObservabilityReasonCode,
    OperatorHealthState,
    OperatorSource,
    ProjectionHealth,
    ProjectionLimits,
    ProjectionProvenance,
    ProjectionStatus,
    TruncationInfo,
)
from ai_engineering.observability.views import (
    REDACTED,
    ArtifactView,
    AuthorityView,
    BarrierView,
    BlockerView,
    CandidateJudgementView,
    CandidateView,
    ControlPlaneView,
    CycleView,
    EventTimelineEntry,
    ExecutionHostView,
    HandoffView,
    JudgementView,
    LeaseView,
    ParallelizationView,
    ProductionSerializationView,
    RequalificationView,
    RedactionRecord,
    RunView,
    TaskIntentView,
    TaskLineageView,
    ValidationView,
    WorkspaceView,
)
from ai_engineering.parallel.parallel_contracts import ConcurrencyBudget, ParallelizationDecision
from ai_engineering.requalification.requalification_contracts import (
    CandidateRequalificationResult,
    RequalificationDecisionState,
    ValidationFreshness,
)
from ai_engineering.task_intent import NodeKind, TaskIntent, TaskLineage, intent_digest
from ai_engineering.workspaces.snapshot_contracts import (
    DiffArtifact,
    validate_repository_relative_path,
)
from ai_engineering.workspaces.workspace_contracts import (
    LeaseState,
    WorkspaceIdentity,
    WorktreeLease,
)

_TERMINAL_PHASES = frozenset(
    {ControlPlanePhase.COMPLETED, ControlPlanePhase.FAILED, ControlPlanePhase.CANCELLED}
)

_HEALTH_PRECEDENCE: tuple[OperatorHealthState, ...] = (
    OperatorHealthState.CONFLICTED,
    OperatorHealthState.UNVERIFIABLE,
    OperatorHealthState.BLOCKED,
    OperatorHealthState.STALE,
    OperatorHealthState.DEGRADED,
    OperatorHealthState.OK,
)

_UNVERIFIABLE_REMOTE_STATES = frozenset(
    {RemoteExecutionState.UNVERIFIABLE, RemoteExecutionState.DISCONNECTED}
)


@dataclass(frozen=True, slots=True)
class OperatorSnapshot:
    """Top-level immutable operator snapshot (versioned schema)."""

    schema_version: int
    generated_from: ProjectionProvenance
    projection_status: ProjectionStatus
    projection_health: ProjectionHealth
    cycle: CycleView | None
    control_plane: ControlPlaneView | None
    task_intent: TaskIntentView | None
    lineage: TaskLineageView | None
    authority: AuthorityView | None
    parallelization: ParallelizationView | None
    workspaces: tuple[WorkspaceView, ...]
    leases: tuple[LeaseView, ...]
    runs: tuple[RunView, ...]
    execution_hosts: tuple[ExecutionHostView, ...]
    candidates: tuple[CandidateView, ...]
    judgement: JudgementView | None
    validation: ValidationView | None
    requalification: RequalificationView | None
    handoff: HandoffView | None
    barriers: tuple[BarrierView, ...]
    blockers: tuple[BlockerView, ...]
    event_timeline: tuple[EventTimelineEntry, ...]
    artifacts: tuple[ArtifactView, ...]
    production_serialization: ProductionSerializationView | None
    redactions: tuple[RedactionRecord, ...]
    truncations: tuple[TruncationInfo, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generated_from": self.generated_from.to_dict(),
            "projection_status": self.projection_status.value,
            "projection_health": self.projection_health.to_dict(),
            "cycle": self.cycle.to_dict() if self.cycle else None,
            "control_plane": self.control_plane.to_dict() if self.control_plane else None,
            "task_intent": self.task_intent.to_dict() if self.task_intent else None,
            "lineage": self.lineage.to_dict() if self.lineage else None,
            "authority": self.authority.to_dict() if self.authority else None,
            "parallelization": (
                self.parallelization.to_dict() if self.parallelization else None
            ),
            "workspaces": [w.to_dict() for w in self.workspaces],
            "leases": [item.to_dict() for item in self.leases],
            "runs": [r.to_dict() for r in self.runs],
            "execution_hosts": [h.to_dict() for h in self.execution_hosts],
            "candidates": [c.to_dict() for c in self.candidates],
            "judgement": self.judgement.to_dict() if self.judgement else None,
            "validation": self.validation.to_dict() if self.validation else None,
            "requalification": (
                self.requalification.to_dict() if self.requalification else None
            ),
            "handoff": self.handoff.to_dict() if self.handoff else None,
            "barriers": [b.to_dict() for b in self.barriers],
            "blockers": [b.to_dict() for b in self.blockers],
            "event_timeline": [e.to_dict() for e in self.event_timeline],
            "artifacts": [a.to_dict() for a in self.artifacts],
            "production_serialization": (
                self.production_serialization.to_dict()
                if self.production_serialization
                else None
            ),
            "redactions": [r.to_dict() for r in self.redactions],
            "truncations": [t.to_dict() for t in self.truncations],
        }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


class _ConflictCollector:
    """Accumulates machine reason codes for conflicts, staleness, absence."""

    def __init__(self) -> None:
        self.conflicts: list[str] = []
        self.stale: list[str] = []
        self.unverifiable: list[str] = []
        self.incomplete: list[str] = []

    def conflict(self, code: str) -> None:
        if code not in self.conflicts:
            self.conflicts.append(code)

    def mark_stale(self, code: str) -> None:
        if code not in self.stale:
            self.stale.append(code)

    def mark_unverifiable(self, code: str) -> None:
        if code not in self.unverifiable:
            self.unverifiable.append(code)

    def mark_incomplete(self, code: str) -> None:
        if code not in self.incomplete:
            self.incomplete.append(code)

    def all_codes(self) -> tuple[str, ...]:
        merged = self.conflicts + self.unverifiable + self.stale + self.incomplete
        return tuple(sorted(set(merged)))


def _collect_artifacts(
    candidate_results: Mapping[str, CandidateResult],
    *,
    on_malformed: Any = None,
) -> list[ArtifactView]:
    artifacts: list[ArtifactView] = []
    for result in candidate_results.values():
        for snapshot in (
            result.pre_execution_snapshot,
            result.post_execution_snapshot,
            result.post_validation_snapshot,
            result.final_snapshot,
        ):
            if snapshot is None:
                continue
            try:
                artifacts.append(
                    ArtifactView(
                        artifact_id=snapshot.snapshot_id,
                        kind="SNAPSHOT",
                        workspace_id=snapshot.workspace_id,
                        candidate_id=snapshot.candidate_id,
                        phase=snapshot.phase.value,
                        base_sha=snapshot.base_sha,
                        head_sha=snapshot.head_sha,
                        digest=snapshot.diff_digest,
                        changed_path_count=len(snapshot.changed_paths),
                        recorded_at=snapshot.captured_at,
                    )
                )
            except Exception:
                # Optional artifact degradation: one malformed optional
                # record must not destroy visibility into the cycle.
                if on_malformed is not None:
                    on_malformed()
                continue
        diff = result.diff_artifact
        if diff is not None:
            try:
                artifacts.append(
                    ArtifactView(
                        artifact_id=diff.artifact_id,
                        kind="DIFF_ARTIFACT",
                        workspace_id=diff.workspace_id,
                        candidate_id=diff.candidate_id,
                        phase=None,
                        base_sha=diff.base_sha,
                        head_sha=diff.head_sha,
                        digest=diff.diff_digest,
                        changed_path_count=len(diff.changed_paths),
                        recorded_at=diff.generated_at,
                    )
                )
            except Exception:
                if on_malformed is not None:
                    on_malformed()
                continue
    artifacts.sort(key=lambda a: (a.kind, a.artifact_id))
    return artifacts


def _classify_lease(
    lease: WorktreeLease, clock: datetime | None
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if lease.state == LeaseState.RELEASED:
        return "LEASE_RELEASED", tuple(reasons)
    if lease.state == LeaseState.QUARANTINED:
        return "LEASE_QUARANTINED", tuple(reasons)
    if lease.state == LeaseState.RELEASE_PENDING:
        return "LEASE_RELEASE_PENDING", tuple(reasons)
    if lease.state in (LeaseState.ACTIVE, LeaseState.RESERVED):
        if lease.expires_at is not None and clock is not None and clock > lease.expires_at:
            return "LEASE_EXPIRED", tuple(reasons)
        if lease.expires_at is not None and clock is None:
            reasons.append(ObservabilityReasonCode.OBSERVABILITY_SOURCE_UNVERIFIABLE.value)
        return "LEASE_ACTIVE", tuple(reasons)
    return f"LEASE_{lease.state.value}", tuple(reasons)


def _host_remote_state(
    host_id: str,
    remote_lifecycles: Mapping[str, RemoteExecutionLifecycle],
) -> tuple[str | None, bool, bool, tuple[str, ...]]:
    """Return (operator remote state, reconciled terminal, unverifiable, reasons)."""

    lifecycle = remote_lifecycles.get(host_id)
    if lifecycle is None:
        return None, False, False, ()
    state = lifecycle.state
    if state == RemoteExecutionState.EXITED:
        return "EXITED", True, False, ()
    if state in _UNVERIFIABLE_REMOTE_STATES:
        return "UNVERIFIABLE", False, True, (ObservabilityReasonCode.OBSERVABILITY_SOURCE_UNVERIFIABLE.value,)
    if state == RemoteExecutionState.FAILED:
        return "FAILED", True, False, ()
    if state in (RemoteExecutionState.LIVE, RemoteExecutionState.CONNECTED):
        return "LIVE", False, False, ()
    return state.value, False, False, ()


def project(
    *,
    cycle: EngineeringCycleState,
    intent: TaskIntent | None = None,
    lineage: TaskLineage | None = None,
    authority: AuthorityBoundary | None = None,
    parallelization_decision: ParallelizationDecision | None = None,
    budget: ConcurrencyBudget | None = None,
    workspaces: Sequence[WorkspaceIdentity] = (),
    leases: Sequence[WorktreeLease] = (),
    runs: Sequence[AgentRunRecord] = (),
    hosts: Sequence[ExecutionHostIdentity] = (),
    remote_lifecycles: Mapping[str, RemoteExecutionLifecycle] | None = None,
    candidates: Sequence[CandidateIdentity] = (),
    candidate_results: Mapping[str, CandidateResult] | None = None,
    judge_result: CandidateJudgeResult | None = None,
    validation: ValidationEvidence | None = None,
    requalification_result: CandidateRequalificationResult | None = None,
    current_main_sha: str | None = None,
    handoff: NodeHandoff | None = None,
    registry: EngineeringCycleRegistry | None = None,
    raw_events: Sequence[ControlPlaneEvent] | None = None,
    production_barrier: ProductionSerializationBarrier | None = None,
    clock: datetime | None = None,
    limits: ProjectionLimits | None = None,
) -> OperatorSnapshot:
    """Build the deterministic operator snapshot. Strictly read-only."""

    bounds = limits or ProjectionLimits()
    flags = _ConflictCollector()
    redaction_records: list[RedactionRecord] = []
    truncations: list[TruncationInfo] = []

    # ---------------- identity binding: TaskIntent ----------------
    intent_view: TaskIntentView | None = None
    if intent is None:
        flags.mark_incomplete(ObservabilityReasonCode.OBSERVABILITY_PROJECTION_INCOMPLETE.value)
    else:
        if intent.task_id != cycle.task_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if intent_digest(intent) != cycle.intent_digest:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if intent.intent_revision != cycle.intent_revision:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if intent.source_repository != cycle.repository_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if intent.source_base_sha.lower() != cycle.base_sha.lower():
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        intent_view = TaskIntentView(
            task_id=intent.task_id,
            intent_digest=intent_digest(intent),
            intent_revision=intent.intent_revision,
            status=intent.status.value,
            task_class=intent.task_class.value,
            source_repository=intent.source_repository,
            source_main_ref=intent.source_main_ref,
            source_base_sha=intent.source_base_sha,
            stop_boundary=intent.stop_boundary.value,
            acceptance_criteria_count=len(intent.acceptance_criteria),
            unknowns_count=len(intent.unknowns),
            blocking_unknowns_count=sum(1 for u in intent.unknowns if u.blocking),
            applicable_invariants=tuple(sorted(intent.applicable_invariants)),
            required_gates=tuple(sorted(intent.required_gates)),
        )

    # ---------------- identity binding: TaskLineage ----------------
    lineage_view: TaskLineageView | None = None
    if lineage is None:
        flags.mark_incomplete(ObservabilityReasonCode.OBSERVABILITY_PROJECTION_INCOMPLETE.value)
    else:
        kinds = {node.node_id: node.kind for node in lineage.nodes}
        bound_kind = kinds.get(cycle.node_id)
        if bound_kind is None or bound_kind != NodeKind.TASK:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        lineage_view = TaskLineageView(
            node_count=len(lineage.nodes),
            edge_count=len(lineage.edges),
            bound_node_id=cycle.node_id,
            bound_node_kind=bound_kind.value if bound_kind else None,
            bound_node_present=bound_kind is not None,
        )

    # ---------------- authority view ----------------
    authority_view: AuthorityView | None = None
    if authority is not None:
        production_classes = {
            EffectClass.DEPLOY,
            EffectClass.RUNTIME_MUTATION,
        }
        datastore_classes = {
            EffectClass.DATA_MUTATION,
            EffectClass.VECTOR_MUTATION,
        }
        secret_classes = {EffectClass.SECRET_MUTATION}
        authority_view = AuthorityView(
            allowed_effect_classes=tuple(sorted(c.value for c in authority.allowed_effect_classes)),
            forbidden_effect_classes=tuple(
                sorted(c.value for c in authority.forbidden_effect_classes)
            ),
            stop_boundary=authority.stop_boundary.value,
            production_authorized=authority.production_authorized,
            secret_access_authorized=authority.secret_access_authorized,
            data_access_authorized=authority.data_access_authorized,
            authority_restricted=(
                authority.production_authorized
                or authority.secret_access_authorized
                or authority.data_access_authorized
            ),
            authority_production_capable=authority.production_authorized
            or bool(production_classes & set(authority.allowed_effect_classes)),
            authority_secret_capable=authority.secret_access_authorized
            or bool(secret_classes & set(authority.allowed_effect_classes)),
            authority_datastore_capable=authority.data_access_authorized
            or bool(datastore_classes & set(authority.allowed_effect_classes)),
        )

    # ---------------- workspaces ----------------
    workspace_by_id: dict[str, WorkspaceIdentity] = {ws.workspace_id: ws for ws in workspaces}
    host_ids = {h.execution_host_id for h in hosts}
    workspace_views: list[WorkspaceView] = []
    for ws in sorted(workspaces, key=lambda w: w.workspace_id):
        if ws.task_id != cycle.task_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
            workspace_reasons: tuple[str, ...] = (
                ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value,
            )
        else:
            workspace_reasons = ()
        if ws.repository != cycle.repository_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if ws.base_sha.lower() != cycle.base_sha.lower():
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if hosts and ws.execution_host_id not in host_ids:
            flags.conflict("EXECUTION_HOST_MISMATCH")
        if ws.candidate_id is not None and candidates:
            if not any(c.candidate_id == ws.candidate_id for c in candidates):
                flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        try:
            validate_repository_relative_path(ws.worktree_path)
            path_disclosure: str = ws.worktree_path
        except Exception:
            path_disclosure = REDACTED
            redaction_records.append(
                RedactionRecord(
                    field_path=f"workspaces[{ws.workspace_id}].worktree_path",
                )
            )
        lease_for_ws = next(
            (item for item in leases if item.workspace_id == ws.workspace_id), None
        )
        workspace_views.append(
            WorkspaceView(
                workspace_id=ws.workspace_id,
                task_id=ws.task_id,
                candidate_id=ws.candidate_id,
                repository=ws.repository,
                base_ref=ws.base_ref,
                base_sha=ws.base_sha,
                branch=ws.branch,
                execution_host_id=ws.execution_host_id,
                execution_mode=ws.execution_mode,
                lease_state=lease_for_ws.state.value if lease_for_ws else None,
                owner_run_id=lease_for_ws.owner_run_id if lease_for_ws else None,
                created_at=_iso(ws.created_at) or "",
                worktree_path_disclosure=path_disclosure,
            )
        )
    truncated_workspaces = len(workspace_views) > bounds.max_workspaces
    if truncated_workspaces:
        truncations.append(
            TruncationInfo(
                field="workspaces",
                truncated=True,
                original_count=len(workspace_views),
                returned_count=bounds.max_workspaces,
            )
        )
        workspace_views = workspace_views[: bounds.max_workspaces]

    # ---------------- leases ----------------
    lease_views: list[LeaseView] = []
    for lease in sorted(leases, key=lambda item: (item.workspace_id, item.owner_run_id)):
        classification, lease_reasons = _classify_lease(lease, clock)
        lease_views.append(
            LeaseView(
                workspace_id=lease.workspace_id,
                owner_run_id=lease.owner_run_id,
                task_id=lease.task_id,
                state=lease.state.value,
                acquired_at=_iso(lease.acquired_at) or "",
                expires_at=_iso(lease.expires_at),
                classification=classification,
                reason_codes=lease_reasons,
            )
        )

    # ---------------- remote lifecycles ----------------
    remote_map = remote_lifecycles or {}

    # ---------------- execution hosts ----------------
    host_unverifiable = False
    host_views: list[ExecutionHostView] = []
    for host in sorted(hosts, key=lambda h: h.execution_host_id):
        remote_state, reconciled_terminal, unverifiable, host_reasons = _host_remote_state(
            host.execution_host_id, remote_map
        )
        if unverifiable:
            host_unverifiable = True
            flags.mark_unverifiable("REMOTE_EXECUTION_UNVERIFIABLE")
        host_views.append(
            ExecutionHostView(
                execution_host_id=host.execution_host_id,
                mode=host.mode.value,
                available=host.available,
                capabilities=tuple(sorted(c.value for c in host.capabilities)),
                remote_reconciliation_required=host.mode.value == "SSH",
                remote_state=remote_state,
                remote_reconciled_terminal=reconciled_terminal,
                reason_codes=host_reasons,
            )
        )

    # ---------------- runs ----------------
    run_views: list[RunView] = []
    any_stale_run = False
    for record in sorted(runs, key=lambda r: r.identity.run_id):
        identity = record.identity
        run_reasons: list[str] = []
        identity_conflict = False
        if identity.task_id != cycle.task_id or identity.node_id != cycle.node_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
            identity_conflict = True
            run_reasons.append(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if workspaces and identity.workspace_id not in workspace_by_id:
            flags.conflict("RUN_WORKSPACE_MISMATCH")
            identity_conflict = True
            run_reasons.append("RUN_WORKSPACE_MISMATCH")
        if hosts and identity.execution_host_id not in host_ids:
            flags.conflict("EXECUTION_HOST_MISMATCH")
            identity_conflict = True
            run_reasons.append("EXECUTION_HOST_MISMATCH")
        if identity.execution_epoch != cycle.execution_epoch:
            any_stale_run = True
            flags.mark_stale(ObservabilityReasonCode.OBSERVABILITY_PROJECTION_STALE.value)
            run_reasons.append(ObservabilityReasonCode.OBSERVABILITY_PROJECTION_STALE.value)
        host_lifecycle = remote_map.get(identity.execution_host_id)
        host_unverifiable_run = host_lifecycle is not None and host_lifecycle.state in (
            _UNVERIFIABLE_REMOTE_STATES
        )
        if identity_conflict:
            operator_state = "IDENTITY_CONFLICT"
        elif host_unverifiable_run:
            operator_state = "UNVERIFIABLE"
            flags.mark_unverifiable("REMOTE_EXECUTION_UNVERIFIABLE")
            run_reasons.append("REMOTE_EXECUTION_UNVERIFIABLE")
        elif identity.execution_epoch != cycle.execution_epoch:
            operator_state = "STALE"
        elif record.state == RunState.CANCEL_REQUESTED:
            operator_state = "CANCEL_REQUESTED"
        elif record.state in (RunState.CREATED, RunState.START_REQUESTED, RunState.LIVE):
            operator_state = "ACTIVE"
        elif record.state == RunState.EXITED:
            operator_state = "COMPLETED"
        else:
            operator_state = "FAILED"
        run_views.append(
            RunView(
                run_id=identity.run_id,
                task_id=identity.task_id,
                node_id=identity.node_id,
                workspace_id=identity.workspace_id,
                candidate_id=identity.candidate_id,
                model=identity.model,
                agent_capability=identity.agent_capability,
                execution_host_id=identity.execution_host_id,
                execution_epoch=identity.execution_epoch,
                state=record.state.value,
                operator_state=operator_state,
                start_time=_iso(identity.start_time) or "",
                exit_code=record.exit_code,
                reason_codes=tuple(sorted(set(run_reasons))),
            )
        )
    truncated_runs = len(run_views) > bounds.max_runs
    if truncated_runs:
        truncations.append(
            TruncationInfo(
                field="runs",
                truncated=True,
                original_count=len(run_views),
                returned_count=bounds.max_runs,
            )
        )
        run_views = run_views[: bounds.max_runs]

    # ---------------- candidates ----------------
    results = candidate_results or {}
    candidate_views: list[CandidateView] = []
    for candidate in sorted(candidates, key=lambda c: c.candidate_id):
        candidate_reasons: list[str] = []
        if candidate.task_id != cycle.task_id or candidate.node_id != cycle.node_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
            candidate_reasons.append(
                ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value
            )
        if candidate.base_sha.lower() != cycle.base_sha.lower():
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
            candidate_reasons.append(
                ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value
            )
        if workspaces and candidate.workspace_id not in workspace_by_id:
            flags.conflict("WORKTREE_IDENTITY_MISMATCH")
            candidate_reasons.append("WORKTREE_IDENTITY_MISMATCH")
        result = results.get(candidate.candidate_id)
        if result is not None:
            if result.task_id != candidate.task_id or result.node_id != candidate.node_id:
                flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
                candidate_reasons.append(
                    ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value
                )
            if result.base_sha.lower() != candidate.base_sha.lower():
                flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
                candidate_reasons.append(
                    ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value
                )
            completion_state = result.state.value
            validation_eligible = (
                result.state == CandidateState.COMPLETED
                and result.success
                and not result.blockers
            )
        else:
            completion_state = "REGISTERED"
            validation_eligible = False
        freshness: str | None = None
        if requalification_result is not None and requalification_result.evidence is not None:
            freshness = requalification_result.evidence.validation_status.value
        elif current_main_sha is not None and candidate.base_sha.lower() != current_main_sha.lower():
            freshness = "STALE_BASE"
            flags.mark_stale("CANDIDATE_BASE_DRIFT")
        judge_for_candidate = None
        if judge_result is not None:
            judge_for_candidate = next(
                (
                    j
                    for j in judge_result.judgements
                    if j.candidate_id == candidate.candidate_id
                ),
                None,
            )
        judgement_eligible = bool(judge_for_candidate.eligible) if judge_for_candidate else False
        candidate_views.append(
            CandidateView(
                candidate_id=candidate.candidate_id,
                task_id=candidate.task_id,
                node_id=candidate.node_id,
                workspace_id=candidate.workspace_id,
                run_id=candidate.run_id,
                base_sha=candidate.base_sha,
                completion_state=completion_state,
                validation_eligible=validation_eligible,
                judgement_eligible=judgement_eligible,
                freshness=freshness,
                selected=cycle.selected_candidate_id == candidate.candidate_id,
                reason_codes=tuple(sorted(set(candidate_reasons))),
            )
        )
    truncated_candidates = len(candidate_views) > bounds.max_candidates
    if truncated_candidates:
        truncations.append(
            TruncationInfo(
                field="candidates",
                truncated=True,
                original_count=len(candidate_views),
                returned_count=bounds.max_candidates,
            )
        )
        candidate_views = candidate_views[: bounds.max_candidates]

    # ---------------- parallelization ----------------
    active_candidates = sum(
        1
        for view in candidate_views
        if view.completion_state
        not in (
            CandidateState.COMPLETED.value,
            CandidateState.FAILED.value,
            CandidateState.CANCELLED.value,
        )
    )
    terminal_candidates = sum(
        1
        for view in candidate_views
        if view.completion_state
        in (
            CandidateState.COMPLETED.value,
            CandidateState.FAILED.value,
            CandidateState.CANCELLED.value,
        )
    )
    active_mutation_candidates = sum(
        1 for view in candidate_views if view.completion_state == CandidateState.RUNNING.value
    )
    budget_max = budget.max_candidates if budget is not None else (
        parallelization_decision.max_candidates if parallelization_decision else None
    )
    slots_used = len(candidate_views)
    if budget_max is not None:
        within = slots_used <= budget_max
        concurrency_status = "CONCURRENCY_WITHIN_BUDGET" if within else "CONCURRENCY_BUDGET_EXCEEDED"
        slots_remaining = max(0, budget_max - slots_used)
    else:
        concurrency_status = "CONCURRENCY_WITHIN_BUDGET"
        slots_remaining = None
    parallelization_view = ParallelizationView(
        strategy=cycle.selected_strategy.value,
        budget_max_candidates=budget_max,
        active_candidates=active_candidates,
        terminal_candidates=terminal_candidates,
        active_mutation_candidates=active_mutation_candidates,
        candidate_slots_used=slots_used,
        candidate_slots_remaining=slots_remaining,
        concurrency_status=concurrency_status,
        requires_single_mutation_owner=(
            parallelization_decision.requires_single_mutation_owner
            if parallelization_decision
            else None
        ),
        requires_serialization_barrier=(
            parallelization_decision.requires_serialization_barrier
            if parallelization_decision
            else None
        ),
    )

    # ---------------- judgement ----------------
    judge_conflict = False
    if judge_result is not None:
        if judge_result.task_id != cycle.task_id or judge_result.node_id != cycle.node_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
            judge_conflict = True
        if judge_result.base_sha.lower() != cycle.base_sha.lower():
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
            judge_conflict = True
        if (
            judge_result.selected_candidate_id is not None
            and judge_result.selected_candidate_id not in {c.candidate_id for c in candidates}
        ):
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
            judge_conflict = True
        judgement_view = JudgementView(
            present=True,
            judge_id=judge_result.judge_id,
            task_id=judge_result.task_id,
            node_id=judge_result.node_id,
            base_sha=judge_result.base_sha,
            decision_state=judge_result.decision_state.value,
            selected_candidate_id=judge_result.selected_candidate_id,
            judgements=tuple(
                CandidateJudgementView(
                    candidate_id=j.candidate_id,
                    hard_gate_passed=j.hard_gate_passed,
                    eligible=j.eligible,
                    semantic_score=j.semantic_score.score if j.semantic_score else None,
                    semantic_review_present=j.semantic_score is not None,
                    rank=j.rank,
                    blockers=tuple(sorted(j.blockers)),
                )
                for j in sorted(judge_result.judgements, key=lambda item: item.candidate_id)
            ),
            completed_at=judge_result.completed_at,
        )
    else:
        judgement_view = JudgementView(
            present=False,
            judge_id=None,
            task_id=None,
            node_id=None,
            base_sha=None,
            decision_state=None,
            selected_candidate_id=None,
            judgements=(),
            completed_at=None,
        )

    # ---------------- validation ----------------
    validation_freshness: str | None = None
    if requalification_result is not None and requalification_result.evidence is not None:
        validation_freshness = requalification_result.evidence.validation_status.value
    elif cycle.requalification_required:
        validation_freshness = ValidationFreshness.REQUIRES_RERUN.value
        flags.mark_stale("CANDIDATE_VALIDATION_STALE")
    elif current_main_sha is not None and current_main_sha.lower() != cycle.base_sha.lower():
        validation_freshness = ValidationFreshness.REQUIRES_RERUN.value
        flags.mark_stale("CANDIDATE_BASE_DRIFT")
    validation_bindings_ok = True
    if validation is not None:
        validation_bindings_ok = (
            validation.cycle_id == cycle.cycle_id
            and validation.task_id == cycle.task_id
            and validation.node_id == cycle.node_id
            and validation.base_sha.lower() == cycle.base_sha.lower()
            and validation.execution_epoch == cycle.execution_epoch
        )
        if not validation_bindings_ok:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
    validation_refs: tuple[str, ...] = ()
    if validation is not None:
        for ref in validation.evidence_refs:
            try:
                validate_evidence_ref(ref, ValueError)
            except Exception:
                flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        validation_refs = validation.evidence_refs
        truncated_refs = len(validation_refs) > bounds.max_evidence_refs
        if truncated_refs:
            truncations.append(
                TruncationInfo(
                    field="validation.evidence_refs",
                    truncated=True,
                    original_count=len(validation_refs),
                    returned_count=bounds.max_evidence_refs,
                )
            )
            validation_refs = validation_refs[: bounds.max_evidence_refs]
    if validation is None:
        validation_status = "MISSING"
    elif not validation_bindings_ok:
        validation_status = "INVALID"
    elif validation_freshness in (ValidationFreshness.REQUIRES_RERUN.value, ValidationFreshness.INVALID.value):
        validation_status = "STALE"
    else:
        validation_status = "VALID"
    validation_view = ValidationView(
        present=validation is not None,
        evidence_id=validation.evidence_id if validation else None,
        candidate_id=validation.candidate_id if validation else None,
        cycle_binding_ok=(validation.cycle_id == cycle.cycle_id) if validation else None,
        task_binding_ok=(validation.task_id == cycle.task_id) if validation else None,
        node_binding_ok=(validation.node_id == cycle.node_id) if validation else None,
        base_sha_binding_ok=(
            validation.base_sha.lower() == cycle.base_sha.lower() if validation else None
        ),
        execution_epoch_binding_ok=(
            validation.execution_epoch == cycle.execution_epoch if validation else None
        ),
        base_sha=validation.base_sha if validation else None,
        execution_epoch=validation.execution_epoch if validation else None,
        evidence_refs_count=len(validation.evidence_refs) if validation else 0,
        evidence_refs=validation_refs,
        freshness=validation_freshness,
        status=validation_status,
    )

    # ---------------- requalification ----------------
    requalification_view = RequalificationView(
        requalification_required=cycle.requalification_required,
        decision_state=(
            requalification_result.decision_state.value if requalification_result else None
        ),
        candidate_base_sha=(
            requalification_result.candidate_base_sha if requalification_result else cycle.base_sha
        ),
        qualified_against_sha=cycle.base_sha,
        current_authoritative_sha=(
            current_main_sha
            if current_main_sha is not None
            else (requalification_result.current_main_sha if requalification_result else None)
        ),
        relationship=requalification_result.relationship.value if requalification_result else None,
        validation_rerun_required=(
            validation_freshness in (ValidationFreshness.REQUIRES_RERUN.value, ValidationFreshness.INVALID.value)
            if validation_freshness is not None
            else None
        ),
        judgement_rerun_required=(
            (
                judge_result is not None
                and current_main_sha is not None
                and judge_result.base_sha.lower() != current_main_sha.lower()
            )
            if (judge_result is not None or current_main_sha is not None)
            else None
        ),
        reason_codes=(
            ("CANDIDATE_REQUALIFICATION_REQUIRED",) if cycle.requalification_required else ()
        ),
    )
    if requalification_result is not None:
        if requalification_result.decision_state in (
            RequalificationDecisionState.REQUALIFICATION_REQUIRED,
            RequalificationDecisionState.FAILED,
            RequalificationDecisionState.REQUALIFICATION_REJECTED,
        ):
            flags.mark_stale("CANDIDATE_REQUALIFICATION_REQUIRED")

    # ---------------- handoff ----------------
    handoff_registered: bool | None = None
    if handoff is not None and registry is not None:
        handoff_registered = registry.get_handoff(handoff.handoff_id) is not None
    if handoff is not None and handoff_registered is False:
        flags.mark_incomplete(ObservabilityReasonCode.OBSERVABILITY_PROJECTION_INCOMPLETE.value)

    # ---------------- blockers ----------------
    blocker_views: list[BlockerView] = []
    for code in sorted(set(cycle.blockers)):
        blocker_views.append(BlockerView(code=code, scope="cycle", affected_identity=cycle.cycle_id, source="CONTROL_PLANE_STATE"))
    for candidate in sorted(candidates, key=lambda c: c.candidate_id):
        result = results.get(candidate.candidate_id)
        if result is not None:
            for code in sorted(set(result.blockers)):
                blocker_views.append(
                    BlockerView(
                        code=code,
                        scope="candidate",
                        affected_identity=candidate.candidate_id,
                        source="CANDIDATE_RESULT",
                    )
                )
    if requalification_result is not None:
        for code in sorted(set(requalification_result.blockers)):
            blocker_views.append(
                BlockerView(
                    code=code,
                    scope="requalification",
                    affected_identity=requalification_result.candidate_id,
                    source="REQUALIFICATION_RESULT",
                )
            )
    if judge_result is not None:
        for code in sorted(set(judge_result.blockers)):
            blocker_views.append(
                BlockerView(
                    code=code,
                    scope="judgement",
                    affected_identity=judge_result.judge_id,
                    source="JUDGE_RESULT",
                )
            )
    blocker_views.sort(key=lambda b: (b.code, b.scope, b.affected_identity, b.source))
    truncated_blockers = len(blocker_views) > bounds.max_blockers
    if truncated_blockers:
        truncations.append(
            TruncationInfo(
                field="blockers",
                truncated=True,
                original_count=len(blocker_views),
                returned_count=bounds.max_blockers,
            )
        )
        blocker_views = blocker_views[: bounds.max_blockers]

    # ---------------- event timeline ----------------
    if raw_events is not None:
        timeline_source: list[ControlPlaneEvent] = list(raw_events)
    elif registry is not None:
        timeline_source = list(registry.get_events(cycle.cycle_id))
    else:
        timeline_source = []
    seen_events: dict[str, ControlPlaneEvent] = {}
    entries: list[EventTimelineEntry] = []
    for index, event in enumerate(timeline_source):
        if event.event_id in seen_events:
            status = "DUPLICATE" if seen_events[event.event_id] == event else "COLLISION_EVIDENCE"
            if status == "COLLISION_EVIDENCE":
                flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        else:
            status = "ACCEPTED"
            seen_events[event.event_id] = event
        if event.cycle_id != cycle.cycle_id or event.task_id != cycle.task_id:
            flags.conflict(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
        if event.execution_epoch != cycle.execution_epoch:
            flags.mark_stale(ObservabilityReasonCode.OBSERVABILITY_PROJECTION_STALE.value)
        entries.append(
            EventTimelineEntry(
                event_id=event.event_id,
                event_type=event.event_type.value,
                cycle_id=event.cycle_id,
                task_id=event.task_id,
                node_id=event.node_id,
                execution_epoch=event.execution_epoch,
                source_kind=event.source_kind,
                source_id=event.source_id,
                created_at=event.created_at,
                run_id=event.run_id,
                workspace_id=event.workspace_id,
                candidate_id=event.candidate_id,
                execution_host_id=event.execution_host_id,
                status=status,
            )
        )
    entries.sort(key=lambda e: (e.created_at, e.event_id))
    truncated_events = len(entries) > bounds.max_events
    if truncated_events:
        truncations.append(
            TruncationInfo(
                field="event_timeline",
                truncated=True,
                original_count=len(entries),
                returned_count=bounds.max_events,
            )
        )
        entries = entries[: bounds.max_events]

    # ---------------- artifacts ----------------
    artifact_views = _collect_artifacts(
        results,
        on_malformed=lambda: flags.mark_incomplete(
            ObservabilityReasonCode.OBSERVABILITY_PROJECTION_INCOMPLETE.value
        ),
    )
    truncated_artifacts = len(artifact_views) > bounds.max_artifacts
    if truncated_artifacts:
        truncations.append(
            TruncationInfo(
                field="artifacts",
                truncated=True,
                original_count=len(artifact_views),
                returned_count=bounds.max_artifacts,
            )
        )
        artifact_views = artifact_views[: bounds.max_artifacts]

    # ---------------- production serialization ----------------
    production_view: ProductionSerializationView | None = None
    if production_barrier is not None:
        production_reasons: list[str] = []
        if production_barrier.active_mutation_agents > 0:
            production_reasons.append("PARALLEL_MUTATION_CONFLICT")
        if production_barrier.single_production_owner is None:
            production_reasons.append(
                ObservabilityReasonCode.OBSERVABILITY_EVIDENCE_MISSING.value
            )
        owner_count = 1 if production_barrier.single_production_owner else 0
        production_view = ProductionSerializationView(
            active_mutation_agents=production_barrier.active_mutation_agents,
            owner_count=owner_count,
            production_owner=production_barrier.single_production_owner,
            ready=production_barrier.ready,
            reason_codes=tuple(sorted(set(production_reasons))),
        )

    # ---------------- barriers ----------------
    selected_candidate = cycle.selected_candidate_id
    selected_view = next(
        (c for c in candidate_views if c.candidate_id == selected_candidate), None
    )
    completed_selected = selected_view is not None and selected_view.completion_state in (
        CandidateState.COMPLETED.value,
    )
    validation_ready = validation is not None and validation_bindings_ok and validation_status == "VALID"
    requalification_ready = (not cycle.requalification_required) or (
        requalification_result is not None
        and requalification_result.decision_state
        in (RequalificationDecisionState.REQUALIFIED, RequalificationDecisionState.NO_REQUALIFICATION_REQUIRED)
    )
    judgement_ready = (
        judge_result is not None
        and judge_result.selected_candidate_id is not None
        and not judge_conflict
    )
    remote_ready = not host_unverifiable
    candidate_completion_ready = completed_selected
    handoff_ready = validation_ready and requalification_ready and judgement_ready and candidate_completion_ready and handoff is not None and remote_ready

    validation_missing: list[str] = []
    if validation is None:
        validation_missing.append(ObservabilityReasonCode.OBSERVABILITY_EVIDENCE_MISSING.value)
    elif not validation_bindings_ok:
        validation_missing.append(ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value)
    elif validation_status == "STALE":
        validation_missing.append("CANDIDATE_VALIDATION_STALE")

    requalification_missing: list[str] = []
    if cycle.requalification_required and requalification_result is None:
        requalification_missing.append("CANDIDATE_REQUALIFICATION_REQUIRED")
    if current_main_sha is not None and current_main_sha.lower() != cycle.base_sha.lower():
        requalification_missing.append("CANDIDATE_BASE_DRIFT")

    handoff_missing: list[str] = []
    if validation is None:
        handoff_missing.append("CONTROL_PLANE_HANDOFF_INCOMPLETE")
        handoff_missing.append(ObservabilityReasonCode.OBSERVABILITY_EVIDENCE_MISSING.value)
    elif not validation_ready:
        handoff_missing.extend(validation_missing)
    if not requalification_ready:
        handoff_missing.extend(requalification_missing or ["CANDIDATE_REQUALIFICATION_REQUIRED"])
    if not judgement_ready:
        handoff_missing.append(ObservabilityReasonCode.OBSERVABILITY_EVIDENCE_MISSING.value)
    if not candidate_completion_ready:
        handoff_missing.append(ObservabilityReasonCode.OBSERVABILITY_EVIDENCE_MISSING.value)
    if handoff is None:
        handoff_missing.append("CONTROL_PLANE_HANDOFF_INCOMPLETE")
    if not remote_ready:
        handoff_missing.append("REMOTE_EXECUTION_UNVERIFIABLE")

    production_missing: list[str] = []
    production_ready = False
    if production_barrier is None:
        production_missing.append(ObservabilityReasonCode.OBSERVABILITY_PROJECTION_INCOMPLETE.value)
    else:
        production_ready = production_barrier.ready
        production_missing.extend(production_view.reason_codes if production_view else ())

    remote_missing: list[str] = ["REMOTE_EXECUTION_UNVERIFIABLE"] if not remote_ready else []
    candidate_completion_missing: list[str] = (
        [] if candidate_completion_ready else [ObservabilityReasonCode.OBSERVABILITY_EVIDENCE_MISSING.value]
    )
    judgement_missing: list[str] = (
        [] if judgement_ready else [ObservabilityReasonCode.OBSERVABILITY_EVIDENCE_MISSING.value]
    )

    barriers: tuple[BarrierView, ...] = (
        BarrierView(
            barrier_name=BarrierName.VALIDATION.value,
            ready=validation_ready,
            reason_codes=tuple(sorted(set(validation_missing))),
            missing_requirements=tuple(sorted(set(validation_missing))),
        ),
        BarrierView(
            barrier_name=BarrierName.REQUALIFICATION.value,
            ready=requalification_ready,
            reason_codes=tuple(sorted(set(requalification_missing))),
            missing_requirements=tuple(sorted(set(requalification_missing))),
        ),
        BarrierView(
            barrier_name=BarrierName.HANDOFF_READINESS.value,
            ready=handoff_ready,
            reason_codes=tuple(sorted(set(handoff_missing))),
            missing_requirements=tuple(sorted(set(handoff_missing))),
        ),
        BarrierView(
            barrier_name=BarrierName.PRODUCTION_SERIALIZATION.value,
            ready=production_ready,
            reason_codes=tuple(sorted(set(production_missing))),
            missing_requirements=tuple(sorted(set(production_missing))),
        ),
        BarrierView(
            barrier_name=BarrierName.REMOTE_EXECUTION_VERIFIABILITY.value,
            ready=remote_ready,
            reason_codes=tuple(sorted(set(remote_missing))),
            missing_requirements=tuple(sorted(set(remote_missing))),
        ),
        BarrierView(
            barrier_name=BarrierName.CANDIDATE_COMPLETION.value,
            ready=candidate_completion_ready,
            reason_codes=tuple(sorted(set(candidate_completion_missing))),
            missing_requirements=tuple(sorted(set(candidate_completion_missing))),
        ),
        BarrierView(
            barrier_name=BarrierName.CANDIDATE_JUDGEMENT.value,
            ready=judgement_ready,
            reason_codes=tuple(sorted(set(judgement_missing))),
            missing_requirements=tuple(sorted(set(judgement_missing))),
        ),
    )

    # ---------------- cycle / control-plane views ----------------
    cycle_view = CycleView(
        cycle_id=cycle.cycle_id,
        task_id=cycle.task_id,
        node_id=cycle.node_id,
        intent_digest=cycle.intent_digest,
        intent_revision=cycle.intent_revision,
        repository_id=cycle.repository_id,
        source_base_sha=cycle.base_sha,
        execution_epoch=cycle.execution_epoch,
        selected_strategy=cycle.selected_strategy.value,
        created_at=cycle.created_at,
        updated_at=cycle.updated_at,
    )
    handoff_view = HandoffView(
        present=handoff is not None,
        handoff_id=handoff.handoff_id if handoff else None,
        source_node_id=handoff.source_node_id if handoff else None,
        target_node_id=handoff.target_node_id if handoff else None,
        cycle_id=handoff.cycle_id if handoff else None,
        candidate_id=handoff.selected_candidate_id if handoff else None,
        evidence_refs_count=len(handoff.evidence_refs) if handoff else 0,
        readiness=handoff_ready,
        missing_requirements=tuple(sorted(set(handoff_missing))),
    )
    control_plane_view = ControlPlaneView(
        phase=cycle.phase.value,
        terminal=cycle.phase in _TERMINAL_PHASES,
        blocked=cycle.phase == ControlPlanePhase.BLOCKED or bool(cycle.blockers),
        requalification_required=cycle.requalification_required,
        handoff_ready=cycle.phase == ControlPlanePhase.READY_FOR_HANDOFF,
        blockers=tuple(sorted(set(cycle.blockers))),
        selected_candidate_id=cycle.selected_candidate_id,
    )

    # ---------------- projection status / health ----------------
    if flags.conflicts:
        status = ProjectionStatus.CONFLICTED
    elif flags.unverifiable or host_unverifiable:
        status = ProjectionStatus.UNVERIFIABLE
    elif flags.stale or any_stale_run:
        status = ProjectionStatus.STALE
    elif flags.incomplete or truncations:
        status = ProjectionStatus.PARTIAL
    else:
        status = ProjectionStatus.COMPLETE

    health = OperatorHealthState.OK
    if status == ProjectionStatus.CONFLICTED:
        health = OperatorHealthState.CONFLICTED
    elif status == ProjectionStatus.UNVERIFIABLE:
        health = OperatorHealthState.UNVERIFIABLE
    elif cycle.phase == ControlPlanePhase.BLOCKED or cycle.blockers:
        health = OperatorHealthState.BLOCKED
    elif status == ProjectionStatus.STALE:
        health = OperatorHealthState.STALE
    elif status == ProjectionStatus.PARTIAL:
        health = OperatorHealthState.DEGRADED

    # No false green: phases whose meaning depends on safety-critical
    # evidence must never render OK while that evidence is missing/stale.
    evidence_dependent_phases = {
        ControlPlanePhase.JUDGING,
        ControlPlanePhase.VALIDATING,
        ControlPlanePhase.REQUALIFYING,
        ControlPlanePhase.READY_FOR_HANDOFF,
    }
    if (
        health == OperatorHealthState.OK
        and cycle.phase in evidence_dependent_phases
        and (not validation_ready or not judgement_ready or not requalification_ready)
    ):
        health = OperatorHealthState.BLOCKED

    reason_codes = flags.all_codes()
    if truncations:
        reason_codes = tuple(
            sorted(set(reason_codes) | {ObservabilityReasonCode.OBSERVABILITY_OUTPUT_LIMIT_EXCEEDED.value})
        )

    # ---------------- provenance ----------------
    source_values = {
        OperatorSource.CONTROL_PLANE_STATE.value: 1,
        OperatorSource.CONTROL_PLANE_REGISTRY.value: 1 if registry is not None else 0,
        OperatorSource.TASK_INTENT.value: 1 if intent is not None else 0,
        OperatorSource.TASK_LINEAGE.value: 1 if lineage is not None else 0,
        OperatorSource.AUTHORITY_BOUNDARY.value: 1 if authority is not None else 0,
        OperatorSource.PARALLELIZATION_DECISION.value: 1 if parallelization_decision is not None else 0,
        OperatorSource.WORKSPACE_IDENTITIES.value: len(workspaces),
        OperatorSource.WORKTREE_LEASES.value: len(leases),
        OperatorSource.RUN_RECORDS.value: len(runs),
        OperatorSource.CANDIDATE_IDENTITIES.value: len(candidates),
        OperatorSource.CANDIDATE_RESULTS.value: len(results),
        OperatorSource.JUDGE_RESULT.value: 1 if judge_result is not None else 0,
        OperatorSource.VALIDATION_EVIDENCE.value: 1 if validation is not None else 0,
        OperatorSource.REQUALIFICATION_RESULT.value: 1 if requalification_result is not None else 0,
        OperatorSource.EXECUTION_HOST_RECORDS.value: len(hosts),
        OperatorSource.REMOTE_EXECUTION_LIFECYCLES.value: len(remote_map),
        OperatorSource.EVENT_LOG.value: len(timeline_source),
        OperatorSource.HANDOFF_RECORD.value: 1 if handoff is not None else 0,
        OperatorSource.PRODUCTION_SERIALIZATION_BARRIER.value: 1 if production_barrier is not None else 0,
        OperatorSource.CURRENT_MAIN_SHA.value: 1 if current_main_sha is not None else 0,
    }
    sources_present = tuple(sorted(name for name, count in source_values.items() if count))
    sources_absent = tuple(sorted(name for name, count in source_values.items() if not count))
    provenance = ProjectionProvenance(
        repository_id=cycle.repository_id,
        base_sha=cycle.base_sha,
        cycle_id=cycle.cycle_id,
        task_id=cycle.task_id,
        node_id=cycle.node_id,
        execution_epoch=cycle.execution_epoch,
        sources_present=sources_present,
        sources_absent=sources_absent,
        source_counts=tuple(sorted(source_values.items())),
    )

    return OperatorSnapshot(
        schema_version=OBSERVABILITY_SCHEMA_VERSION,
        generated_from=provenance,
        projection_status=status,
        projection_health=ProjectionHealth(
            status=status,
            health=health,
            reason_codes=reason_codes,
        ),
        cycle=cycle_view,
        control_plane=control_plane_view,
        task_intent=intent_view,
        lineage=lineage_view,
        authority=authority_view,
        parallelization=parallelization_view,
        workspaces=tuple(workspace_views),
        leases=tuple(lease_views),
        runs=tuple(run_views),
        execution_hosts=tuple(host_views),
        candidates=tuple(candidate_views),
        judgement=judgement_view,
        validation=validation_view,
        requalification=requalification_view,
        handoff=handoff_view,
        barriers=barriers,
        blockers=tuple(blocker_views),
        event_timeline=tuple(entries),
        artifacts=tuple(artifact_views),
        production_serialization=production_view,
        redactions=tuple(sorted(redaction_records, key=lambda r: r.field_path)),
        truncations=tuple(truncations),
    )
