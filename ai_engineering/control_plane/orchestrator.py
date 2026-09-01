"""Deterministic EngineeringCycleOrchestrator and EngineeringCycleResult."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import threading
from typing import Any

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.parallel.parallel_contracts import ParallelizationDecision, ParallelizationStrategy


@dataclass(frozen=True, slots=True)
class EngineeringCycleResult:
    """Final typed result of an Engineering Cycle."""

    cycle_id: str
    task_id: str
    final_phase: ControlPlanePhase
    selected_candidate_id: str | None
    handoff: NodeHandoff | None
    blockers: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "task_id": self.task_id,
            "final_phase": self.final_phase.value,
            "selected_candidate_id": self.selected_candidate_id,
            "handoff": self.handoff.to_dict() if self.handoff else None,
            "blockers": list(self.blockers),
            "evidence_refs": list(self.evidence_refs),
        }


class EngineeringCycleOrchestrator:
    """Deterministic orchestrator managing control-plane cycle progression and barriers."""

    def __init__(self, initial_state: EngineeringCycleState) -> None:
        self._state = initial_state
        self._lock = threading.Lock()
        self._processed_events: dict[str, ControlPlaneEvent] = {}

    @property
    def state(self) -> EngineeringCycleState:
        with self._lock:
            return self._state

    def apply_event(self, event: ControlPlaneEvent) -> None:
        """Apply a validated event and transition state."""
        with self._lock:
            if event.event_id in self._processed_events:
                existing = self._processed_events[event.event_id]
                if existing == event:
                    return  # Idempotent duplicate
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_EVENT_COLLISION.value,
                    f"Event collision on event_id: {event.event_id}",
                )

            # Stale checks
            if event.cycle_id != self._state.cycle_id:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                    f"Event cycle_id {event.cycle_id!r} != {self._state.cycle_id!r}",
                )
            if event.execution_epoch != self._state.execution_epoch:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                    f"Event epoch {event.execution_epoch} != active epoch {self._state.execution_epoch}",
                )

            # Terminal state check
            if self._state.phase in (
                ControlPlanePhase.COMPLETED,
                ControlPlanePhase.CANCELLED,
                ControlPlanePhase.FAILED,
            ):
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Cannot apply events to terminal phase {self._state.phase}",
                )

            self._processed_events[event.event_id] = event

            # Handle event transitions
            if event.event_type == ControlPlaneEventType.BLOCKER_RAISED:
                new_blockers = list(self._state.blockers)
                for b in event.evidence_refs:
                    if b not in new_blockers:
                        new_blockers.append(b)
                self._state = EngineeringCycleState(
                    cycle_id=self._state.cycle_id,
                    task_id=self._state.task_id,
                    node_id=self._state.node_id,
                    intent_id=self._state.intent_id,
                    base_sha=self._state.base_sha,
                    phase=ControlPlanePhase.BLOCKED,
                    execution_epoch=self._state.execution_epoch,
                    selected_strategy=self._state.selected_strategy,
                    active_workspace_ids=self._state.active_workspace_ids,
                    active_run_ids=self._state.active_run_ids,
                    candidate_ids=self._state.candidate_ids,
                    selected_candidate_id=self._state.selected_candidate_id,
                    requalification_required=self._state.requalification_required,
                    blockers=tuple(new_blockers),
                    created_at=self._state.created_at,
                    updated_at=event.created_at,
                )

    def qualify(self, decision: ParallelizationDecision) -> None:
        """Apply parallelization policy decision."""
        with self._lock:
            if self._state.phase != ControlPlanePhase.CREATED:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Cannot qualify from phase {self._state.phase}",
                )
            self._state = EngineeringCycleState(
                cycle_id=self._state.cycle_id,
                task_id=self._state.task_id,
                node_id=self._state.node_id,
                intent_id=self._state.intent_id,
                base_sha=self._state.base_sha,
                phase=ControlPlanePhase.QUALIFIED,
                execution_epoch=self._state.execution_epoch,
                selected_strategy=decision.strategy,
                active_workspace_ids=self._state.active_workspace_ids,
                active_run_ids=self._state.active_run_ids,
                candidate_ids=self._state.candidate_ids,
                selected_candidate_id=self._state.selected_candidate_id,
                requalification_required=self._state.requalification_required,
                blockers=self._state.blockers,
                created_at=self._state.created_at,
                updated_at="2026-09-01T00:00:00Z",
            )

    def prepare_workspaces(self, workspace_ids: Sequence[str]) -> None:
        with self._lock:
            if self._state.phase not in (ControlPlanePhase.QUALIFIED, ControlPlanePhase.PLANNED):
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Cannot prepare workspaces from phase {self._state.phase}",
                )
            self._state = EngineeringCycleState(
                cycle_id=self._state.cycle_id,
                task_id=self._state.task_id,
                node_id=self._state.node_id,
                intent_id=self._state.intent_id,
                base_sha=self._state.base_sha,
                phase=ControlPlanePhase.PREPARING,
                execution_epoch=self._state.execution_epoch,
                selected_strategy=self._state.selected_strategy,
                active_workspace_ids=tuple(workspace_ids),
                active_run_ids=self._state.active_run_ids,
                candidate_ids=self._state.candidate_ids,
                selected_candidate_id=self._state.selected_candidate_id,
                requalification_required=self._state.requalification_required,
                blockers=self._state.blockers,
                created_at=self._state.created_at,
                updated_at="2026-09-01T00:00:00Z",
            )

    def record_investigation_results(
        self,
        evidence_refs: Sequence[str],
        blockers: Sequence[str] = (),
    ) -> None:
        with self._lock:
            phase = ControlPlanePhase.BLOCKED if blockers else ControlPlanePhase.READY_FOR_HANDOFF
            self._state = EngineeringCycleState(
                cycle_id=self._state.cycle_id,
                task_id=self._state.task_id,
                node_id=self._state.node_id,
                intent_id=self._state.intent_id,
                base_sha=self._state.base_sha,
                phase=phase,
                execution_epoch=self._state.execution_epoch,
                selected_strategy=self._state.selected_strategy,
                active_workspace_ids=self._state.active_workspace_ids,
                active_run_ids=self._state.active_run_ids,
                candidate_ids=self._state.candidate_ids,
                selected_candidate_id=self._state.selected_candidate_id,
                requalification_required=self._state.requalification_required,
                blockers=tuple(blockers),
                created_at=self._state.created_at,
                updated_at="2026-09-01T00:00:00Z",
            )

    def record_candidate_results(
        self,
        candidate_ids: Sequence[str],
        blockers: Sequence[str] = (),
    ) -> None:
        with self._lock:
            phase = ControlPlanePhase.BLOCKED if blockers else ControlPlanePhase.JUDGING
            self._state = EngineeringCycleState(
                cycle_id=self._state.cycle_id,
                task_id=self._state.task_id,
                node_id=self._state.node_id,
                intent_id=self._state.intent_id,
                base_sha=self._state.base_sha,
                phase=phase,
                execution_epoch=self._state.execution_epoch,
                selected_strategy=self._state.selected_strategy,
                active_workspace_ids=self._state.active_workspace_ids,
                active_run_ids=self._state.active_run_ids,
                candidate_ids=tuple(candidate_ids),
                selected_candidate_id=self._state.selected_candidate_id,
                requalification_required=self._state.requalification_required,
                blockers=tuple(blockers),
                created_at=self._state.created_at,
                updated_at="2026-09-01T00:00:00Z",
            )

    def record_judgement(
        self,
        selected_candidate_id: str,
        blockers: Sequence[str] = (),
    ) -> None:
        with self._lock:
            if self._state.phase != ControlPlanePhase.JUDGING:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Cannot record judgement from phase {self._state.phase}",
                )
            phase = ControlPlanePhase.BLOCKED if blockers else ControlPlanePhase.VALIDATING
            self._state = EngineeringCycleState(
                cycle_id=self._state.cycle_id,
                task_id=self._state.task_id,
                node_id=self._state.node_id,
                intent_id=self._state.intent_id,
                base_sha=self._state.base_sha,
                phase=phase,
                execution_epoch=self._state.execution_epoch,
                selected_strategy=self._state.selected_strategy,
                active_workspace_ids=self._state.active_workspace_ids,
                active_run_ids=self._state.active_run_ids,
                candidate_ids=self._state.candidate_ids,
                selected_candidate_id=selected_candidate_id,
                requalification_required=self._state.requalification_required,
                blockers=tuple(blockers),
                created_at=self._state.created_at,
                updated_at="2026-09-01T00:00:00Z",
            )

    def record_validation(
        self,
        validation_passed: bool,
        evidence_refs: Sequence[str] = (),
        blockers: Sequence[str] = (),
    ) -> None:
        with self._lock:
            if self._state.phase != ControlPlanePhase.VALIDATING:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Cannot record validation from phase {self._state.phase}",
                )
            if not validation_passed or blockers:
                phase = ControlPlanePhase.BLOCKED
                b_list = list(blockers)
                if not b_list:
                    b_list.append("VALIDATION_FAILED")
            else:
                phase = ControlPlanePhase.READY_FOR_HANDOFF
                b_list = []

            self._state = EngineeringCycleState(
                cycle_id=self._state.cycle_id,
                task_id=self._state.task_id,
                node_id=self._state.node_id,
                intent_id=self._state.intent_id,
                base_sha=self._state.base_sha,
                phase=phase,
                execution_epoch=self._state.execution_epoch,
                selected_strategy=self._state.selected_strategy,
                active_workspace_ids=self._state.active_workspace_ids,
                active_run_ids=self._state.active_run_ids,
                candidate_ids=self._state.candidate_ids,
                selected_candidate_id=self._state.selected_candidate_id,
                requalification_required=self._state.requalification_required,
                blockers=tuple(b_list),
                created_at=self._state.created_at,
                updated_at="2026-09-01T00:00:00Z",
            )

    def trigger_requalification(self) -> None:
        with self._lock:
            self._state = EngineeringCycleState(
                cycle_id=self._state.cycle_id,
                task_id=self._state.task_id,
                node_id=self._state.node_id,
                intent_id=self._state.intent_id,
                base_sha=self._state.base_sha,
                phase=ControlPlanePhase.REQUALIFYING,
                execution_epoch=self._state.execution_epoch,
                selected_strategy=self._state.selected_strategy,
                active_workspace_ids=self._state.active_workspace_ids,
                active_run_ids=self._state.active_run_ids,
                candidate_ids=self._state.candidate_ids,
                selected_candidate_id=self._state.selected_candidate_id,
                requalification_required=True,
                blockers=self._state.blockers,
                created_at=self._state.created_at,
                updated_at="2026-09-01T00:00:00Z",
            )

    def generate_handoff(
        self,
        target_node_id: str,
        evidence_refs: Sequence[str],
    ) -> NodeHandoff:
        with self._lock:
            if self._state.phase != ControlPlanePhase.READY_FOR_HANDOFF:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    f"Cannot generate handoff while phase is {self._state.phase}",
                )
            if self._state.blockers:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    f"Cannot generate handoff with active blockers: {self._state.blockers}",
                )
            return NodeHandoff(
                handoff_id=f"handoff-{self._state.cycle_id}",
                task_id=self._state.task_id,
                source_node_id=self._state.node_id,
                target_node_id=target_node_id,
                cycle_id=self._state.cycle_id,
                base_sha=self._state.base_sha,
                execution_epoch=self._state.execution_epoch,
                evidence_refs=tuple(evidence_refs),
                blocker_refs=(),
                selected_candidate_id=self._state.selected_candidate_id,
            )
