"""Read-only collector facade and operator query API (PR-12).

The collector accepts explicit authoritative objects/registries only.
There is no hidden network fetch, subprocess, git command, filesystem
crawl, or provider call. Every method is a pure read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from ai_engineering.candidates.candidate_contracts import CandidateIdentity, CandidateResult
from ai_engineering.contracts import AuthorityBoundary
from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import ValidationEvidence
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.control_plane.registry import EngineeringCycleRegistry
from ai_engineering.execution.host_contracts import ExecutionHostIdentity
from ai_engineering.execution.remote_state import RemoteExecutionLifecycle
from ai_engineering.execution.run_state import AgentRunRecord
from ai_engineering.judge.judge_contracts import CandidateJudgeResult
from ai_engineering.observability.contracts import ProjectionLimits
from ai_engineering.observability.projection import OperatorSnapshot, project
from ai_engineering.observability.views import (
    BarrierView,
    BlockerView,
    CandidateView,
    ExecutionHostView,
    EventTimelineEntry,
    RunView,
    WorkspaceView,
)
from ai_engineering.parallel.parallel_contracts import ConcurrencyBudget, ParallelizationDecision
from ai_engineering.requalification.requalification_contracts import (
    CandidateRequalificationResult,
)
from ai_engineering.task_intent import TaskIntent, TaskLineage
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity, WorktreeLease

_ACTIVE_RUN_STATES = {"ACTIVE", "CANCEL_REQUESTED", "UNVERIFIABLE"}


def collect_operator_snapshot(
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
    """Collect the deterministic, read-only operator snapshot.

    Inputs must be explicit authoritative objects. The collector never
    constructs control-plane state and never mutates any input.
    """

    return project(
        cycle=cycle,
        intent=intent,
        lineage=lineage,
        authority=authority,
        parallelization_decision=parallelization_decision,
        budget=budget,
        workspaces=workspaces,
        leases=leases,
        runs=runs,
        hosts=hosts,
        remote_lifecycles=remote_lifecycles,
        candidates=candidates,
        candidate_results=candidate_results,
        judge_result=judge_result,
        validation=validation,
        requalification_result=requalification_result,
        current_main_sha=current_main_sha,
        handoff=handoff,
        registry=registry,
        raw_events=raw_events,
        production_barrier=production_barrier,
        clock=clock,
        limits=limits,
    )


class OperatorQueries:
    """Pure read-only query facade over a collected OperatorSnapshot."""

    def __init__(self, snapshot: OperatorSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def snapshot(self) -> OperatorSnapshot:
        return self._snapshot

    def get_cycle_summary(self) -> dict[str, object]:
        cycle = self._snapshot.cycle
        control_plane = self._snapshot.control_plane
        if cycle is None or control_plane is None:
            return {}
        return {
            "cycle_id": cycle.cycle_id,
            "task_id": cycle.task_id,
            "node_id": cycle.node_id,
            "phase": control_plane.phase,
            "blocked": control_plane.blocked,
            "selected_candidate_id": control_plane.selected_candidate_id,
            "projection_status": self._snapshot.projection_status.value,
            "health": self._snapshot.projection_health.health.value,
        }

    def get_active_runs(self) -> tuple[RunView, ...]:
        return tuple(run for run in self._snapshot.runs if run.operator_state in _ACTIVE_RUN_STATES)

    def get_active_workspaces(self) -> tuple[WorkspaceView, ...]:
        active_lease_states = {"ACTIVE", "RESERVED", "RELEASE_PENDING"}
        return tuple(
            ws
            for ws in self._snapshot.workspaces
            if ws.lease_state in active_lease_states or ws.lease_state is None
        )

    def get_candidate_statuses(self) -> tuple[CandidateView, ...]:
        return self._snapshot.candidates

    def get_blockers(self) -> tuple[BlockerView, ...]:
        return self._snapshot.blockers

    def get_barrier_status(self) -> tuple[BarrierView, ...]:
        return self._snapshot.barriers

    def get_event_timeline(self) -> tuple[EventTimelineEntry, ...]:
        return self._snapshot.event_timeline

    def get_execution_hosts(self) -> tuple[ExecutionHostView, ...]:
        return self._snapshot.execution_hosts

    def get_handoff_status(self) -> dict[str, object]:
        handoff = self._snapshot.handoff
        if handoff is None:
            return {"present": False, "readiness": False}
        return {
            "present": handoff.present,
            "handoff_id": handoff.handoff_id,
            "readiness": handoff.readiness,
            "missing_requirements": list(handoff.missing_requirements),
        }

    def get_projection_health(self) -> dict[str, object]:
        return self._snapshot.projection_health.to_dict()
