"""Deterministic EngineeringCycleOrchestrator and EngineeringCycleResult (PR-11.1).

Hardening invariants enforced structurally:

- D1: terminal states (COMPLETED / CANCELLED / FAILED) can never be left;
  the guard lives centrally in ``_transition`` so every public mutator and
  every event path inherits it.
- D4: control state advances only through one validated transition
  mechanism; every non-blocker event type performs a meaningful,
  phase-checked transition instead of being recorded as a no-op.
- D3: judgement requires a registered, identity-bound candidate;
  validation requires a :class:`ValidationEvidence` bound to the cycle,
  the judged candidate, the base SHA, and the execution epoch.
- D5: ``requalification_required`` gates both validation and handoff.
- D6/D12/D13/D14: the cycle is bound to a canonical ``TaskIntent``
  (digest + revision + repository) and a canonical ``TaskLineage`` node;
  child authority must be a subset of intent authority; candidate and
  evidence identities are rejected across tasks/nodes/repositories.
- D7: cancellation is reachable and two-staged (CANCEL_REQUESTED then,
  only with proven terminal execution evidence, CANCELLED).
- Phase 17: exactly one logical readiness barrier guards
  READY_FOR_HANDOFF, enforced centrally in ``_transition``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
import threading
from typing import Any

from ai_engineering.candidates.candidate_contracts import CandidateIdentity
from ai_engineering.contracts import AuthorityBoundary, EffectClass, StopBoundary
from ai_engineering.control_plane._evidence_refs import validate_evidence_ref
from ai_engineering.control_plane.contracts import (
    ValidationEvidence,
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.parallel.parallel_contracts import ParallelizationDecision, ParallelizationStrategy
from ai_engineering.requalification.requalification_contracts import (
    JudgementFreshness,
    ValidationFreshness,
)
from ai_engineering.task_intent import TaskIntent, intent_digest, validate_intent
from ai_engineering.task_intent import NodeKind, TaskLineage, validate_lineage

_TERMINAL_PHASES = frozenset(
    {
        ControlPlanePhase.COMPLETED,
        ControlPlanePhase.CANCELLED,
        ControlPlanePhase.FAILED,
    }
)

_P = ControlPlanePhase

# Ordered state machine (Phase 4). Every phase change -- direct helper or
# event-driven -- must be a legal edge in this table.
_ALLOWED_TRANSITIONS: dict[_P, frozenset[_P]] = {
    _P.CREATED: frozenset({_P.QUALIFIED, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.QUALIFIED: frozenset({_P.PREPARING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.PLANNED: frozenset({_P.PREPARING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.PREPARING: frozenset({_P.INVESTIGATING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.INVESTIGATING: frozenset({_P.IMPLEMENTING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.IMPLEMENTING: frozenset({_P.JUDGING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.JUDGING: frozenset(
        {_P.VALIDATING, _P.REQUALIFYING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}
    ),
    _P.VALIDATING: frozenset(
        {_P.READY_FOR_HANDOFF, _P.REQUALIFYING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}
    ),
    _P.REQUALIFYING: frozenset({_P.VALIDATING, _P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.READY_FOR_HANDOFF: frozenset({_P.BLOCKED, _P.CANCEL_REQUESTED, _P.FAILED}),
    _P.BLOCKED: frozenset({_P.CANCEL_REQUESTED, _P.FAILED}),
    _P.CANCEL_REQUESTED: frozenset({_P.CANCELLED, _P.FAILED}),
    _P.COMPLETED: frozenset(),
    _P.CANCELLED: frozenset(),
    _P.FAILED: frozenset(),
}

_ANY_ACTIVE = (
    _P.CREATED,
    _P.QUALIFIED,
    _P.PLANNED,
    _P.PREPARING,
    _P.INVESTIGATING,
    _P.IMPLEMENTING,
    _P.JUDGING,
    _P.VALIDATING,
    _P.REQUALIFYING,
    _P.READY_FOR_HANDOFF,
    _P.BLOCKED,
)

# Authority ceiling per stop boundary (Phase 12): the maximum effect
# classes a child boundary may be granted for a given intent boundary.
_READ_ONLY = frozenset({EffectClass.READ_ONLY})
_REPO_WRITE = _READ_ONLY | {EffectClass.REPOSITORY_WRITE}
_COMMIT = _REPO_WRITE | {EffectClass.GIT_COMMIT}
_PR = _COMMIT | {EffectClass.GIT_PUSH, EffectClass.PR_MUTATION}
_MERGE = _PR | {EffectClass.PR_MERGE}

_STOP_BOUNDARY_EFFECT_CEILING: dict[StopBoundary, frozenset[EffectClass]] = {
    StopBoundary.READ_ONLY: _READ_ONLY,
    StopBoundary.LOCAL_DIFF: _REPO_WRITE,
    StopBoundary.COMMIT: _COMMIT,
    StopBoundary.DRAFT_PR: _PR,
    StopBoundary.READY_PR: _PR,
    StopBoundary.MERGE: _MERGE,
    StopBoundary.BUILD: _COMMIT | {EffectClass.BUILD},
    StopBoundary.DEPLOY: _MERGE | {EffectClass.BUILD, EffectClass.DEPLOY, EffectClass.RUNTIME_MUTATION},
    StopBoundary.LIVE_SMOKE: _MERGE
    | {EffectClass.BUILD, EffectClass.DEPLOY, EffectClass.RUNTIME_MUTATION},
}

_STOP_BOUNDARY_RANK: dict[StopBoundary, int] = {
    StopBoundary.READ_ONLY: 0,
    StopBoundary.LOCAL_DIFF: 1,
    StopBoundary.COMMIT: 2,
    StopBoundary.BUILD: 3,
    StopBoundary.DRAFT_PR: 4,
    StopBoundary.READY_PR: 4,
    StopBoundary.MERGE: 5,
    StopBoundary.DEPLOY: 6,
    StopBoundary.LIVE_SMOKE: 7,
}

_PRODUCTION_BOUNDARIES = frozenset({StopBoundary.DEPLOY, StopBoundary.LIVE_SMOKE})


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

    def __init__(
        self,
        initial_state: EngineeringCycleState,
        *,
        intent: TaskIntent,
        lineage: TaskLineage,
    ) -> None:
        validated_intent = validate_intent(intent)
        validated_lineage = validate_lineage(lineage)

        state = initial_state
        digest = intent_digest(validated_intent)
        if state.intent_digest != digest:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Cycle intent_digest does not match the canonical TaskIntent digest",
            )
        if state.intent_revision != validated_intent.intent_revision:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Cycle intent_revision does not match the canonical TaskIntent revision",
            )
        if state.task_id != validated_intent.task_id:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Cycle task_id does not match the canonical TaskIntent task identity",
            )
        if state.repository_id != validated_intent.source_repository:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Cycle repository_id does not match the TaskIntent source repository",
            )
        if state.base_sha != validated_intent.source_base_sha:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Cycle base_sha does not match the TaskIntent source base",
            )

        lineage_kinds = {node.node_id: node.kind for node in validated_lineage.nodes}
        bound_kind = lineage_kinds.get(state.node_id)
        if bound_kind is None:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Cycle node_id {state.node_id!r} is not bound to the canonical TaskLineage "
                "(orphan execution identity)",
            )
        if bound_kind != NodeKind.TASK:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Cycle node_id {state.node_id!r} is a {bound_kind.value} lineage node, "
                "not a TASK node",
            )

        self._intent = validated_intent
        self._lineage = validated_lineage
        self._state = state
        self._lock = threading.Lock()
        self._processed_events: dict[str, ControlPlaneEvent] = {}
        self._candidates: dict[str, CandidateIdentity] = {}
        self._completed_candidates: set[str] = set()
        self._known_execution_hosts: set[str] = set()
        self._validation_evidence: ValidationEvidence | None = None

    @property
    def state(self) -> EngineeringCycleState:
        with self._lock:
            return self._state

    # ------------------------------------------------------------------
    # Central transition authority (D1 + ordered state machine + Phase 17)
    # ------------------------------------------------------------------

    def _transition(
        self,
        new_phase: ControlPlanePhase,
        *,
        expected: tuple[ControlPlanePhase, ...],
        validation_evidence: ValidationEvidence | None = None,
        **state_overrides: Any,
    ) -> None:
        current = self._state
        if current.phase in _TERMINAL_PHASES:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Cycle is terminal ({current.phase.value}); terminal states can never "
                "be left (no resurrection)",
            )
        if current.phase not in expected:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Illegal transition: expected source phase(s) "
                f"{[p.value for p in expected]}, current phase is {current.phase.value}",
            )
        if new_phase not in _ALLOWED_TRANSITIONS[current.phase]:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Illegal transition {current.phase.value} -> {new_phase.value}: "
                "not permitted by the ordered phase state machine",
            )
        if new_phase == _P.READY_FOR_HANDOFF:
            self._verify_readiness_gate(validation_evidence)
        self._state = replace(current, phase=new_phase, **state_overrides)

    def _verify_readiness_gate(self, validation_evidence: ValidationEvidence | None) -> None:
        """Single READY_FOR_HANDOFF barrier (Phase 17). No bypass exists."""
        if validation_evidence is None:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                "READY_FOR_HANDOFF requires bound validation evidence; a bare boolean "
                "or phase jump can never satisfy the readiness barrier",
            )
        if self._state.selected_candidate_id is None:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                "READY_FOR_HANDOFF requires a judge-selected candidate",
            )
        if self._state.requalification_required:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                "READY_FOR_HANDOFF blocked: requalification is required and no fresh "
                "requalification evidence has been recorded (no auto-rebase)",
            )
        if self._state.blockers:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                f"READY_FOR_HANDOFF blocked by active blockers: {self._state.blockers}",
            )

    def _identity_override(self, **kwargs: Any) -> dict[str, Any]:
        return {"updated_at": "2026-09-01T00:00:00Z", **kwargs}

    def _ensure_active(self) -> None:
        """Uniform terminal guard for every public mutator (D1).

        Terminal states (COMPLETED / CANCELLED / FAILED) can never be left;
        every direct helper API inherits this check before any other logic.
        """
        if self._state.phase in _TERMINAL_PHASES:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Cycle is terminal ({self._state.phase.value}); terminal states can "
                "never be left (no resurrection)",
            )

    # ------------------------------------------------------------------
    # Event-driven projection (D4) with identity fencing (Phase 9)
    # ------------------------------------------------------------------

    def apply_event(self, event: ControlPlaneEvent) -> None:
        """Apply a validated event and transition state through the central authority."""
        with self._lock:
            if event.event_id in self._processed_events:
                existing = self._processed_events[event.event_id]
                if existing == event:
                    return  # Idempotent duplicate
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_EVENT_COLLISION.value,
                    f"Event collision on event_id: {event.event_id}",
                )

            if event.cycle_id != self._state.cycle_id:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                    f"Event cycle_id {event.cycle_id!r} != {self._state.cycle_id!r}",
                )
            if event.execution_epoch != self._state.execution_epoch:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                    f"Event epoch {event.execution_epoch} != active epoch "
                    f"{self._state.execution_epoch}",
                )
            if event.task_id != self._state.task_id:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Event task_id {event.task_id!r} does not match cycle task "
                    f"{self._state.task_id!r}",
                )
            if event.node_id != self._state.node_id:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Event node_id {event.node_id!r} does not match cycle node "
                    f"{self._state.node_id!r}",
                )
            if (
                event.workspace_id is not None
                and event.workspace_id not in self._state.active_workspace_ids
            ):
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Event workspace_id {event.workspace_id!r} was not authorized "
                    "by this cycle",
                )
            if event.run_id is not None and event.run_id not in self._state.active_run_ids:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Event run_id {event.run_id!r} was not authorized by this cycle",
                )
            if (
                event.candidate_id is not None
                and event.candidate_id not in self._candidates
            ):
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Event candidate_id {event.candidate_id!r} was not registered "
                    "with this cycle",
                )
            if (
                event.execution_host_id is not None
                and event.execution_host_id not in self._known_execution_hosts
            ):
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.EXECUTION_HOST_MISMATCH.value,
                    f"Event execution_host_id {event.execution_host_id!r} was not "
                    "registered with this cycle",
                )

            self._processed_events[event.event_id] = event
            self._dispatch_event(event)

    def _dispatch_event(self, event: ControlPlaneEvent) -> None:
        et = event.event_type
        if et == ControlPlaneEventType.WORKSPACE_READY:
            self._transition(
                _P.INVESTIGATING,
                expected=(_P.PREPARING,),
                **self._identity_override(),
            )
        elif et == ControlPlaneEventType.INVESTIGATION_COMPLETED:
            self._transition(
                _P.IMPLEMENTING,
                expected=(_P.INVESTIGATING,),
                **self._identity_override(),
            )
        elif et == ControlPlaneEventType.CANDIDATE_COMPLETED:
            if event.candidate_id is None:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    "CANDIDATE_COMPLETED requires a candidate_id",
                )
            if event.candidate_id not in self._candidates:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"CANDIDATE_COMPLETED for candidate {event.candidate_id!r} that was "
                    "never registered with a full CandidateIdentity binding",
                )
            self._completed_candidates.add(event.candidate_id)
            self._transition(
                _P.JUDGING,
                expected=(_P.IMPLEMENTING,),
                **self._identity_override(),
            )
        elif et == ControlPlaneEventType.JUDGEMENT_COMPLETED:
            if event.candidate_id is None:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    "JUDGEMENT_COMPLETED requires a candidate_id",
                )
            self._verify_judgeable_candidate(event.candidate_id)
            self._transition(
                _P.VALIDATING,
                expected=(_P.JUDGING,),
                selected_candidate_id=event.candidate_id,
                **self._identity_override(),
            )
        elif et == ControlPlaneEventType.VALIDATION_COMPLETED:
            if event.candidate_id is None or not event.evidence_refs:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    "VALIDATION_COMPLETED requires a candidate_id and concrete "
                    "evidence references",
                )
            evidence = ValidationEvidence(
                evidence_id=event.event_id,
                cycle_id=event.cycle_id,
                task_id=event.task_id,
                node_id=event.node_id,
                candidate_id=event.candidate_id,
                base_sha=self._state.base_sha,
                execution_epoch=event.execution_epoch,
                evidence_refs=tuple(event.evidence_refs),
            )
            self._apply_validation(evidence)
        elif et == ControlPlaneEventType.REQUALIFICATION_COMPLETED:
            if not event.evidence_refs:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    "REQUALIFICATION_COMPLETED requires fresh requalification "
                    "evidence references",
                )
            self._transition(
                _P.VALIDATING,
                expected=(_P.REQUALIFYING,),
                requalification_required=False,
                **self._identity_override(),
            )
        elif et == ControlPlaneEventType.RUN_FAILED:
            self._transition(_P.FAILED, expected=_ANY_ACTIVE, **self._identity_override())
        elif et == ControlPlaneEventType.RUN_CANCELLED:
            self._apply_cancel_confirmation(event)
        elif et == ControlPlaneEventType.BLOCKER_RAISED:
            self._apply_blockers(tuple(event.evidence_refs))
        else:  # pragma: no cover - exhaustive enum handled above
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Unhandled event type {et!r}",
            )

    def _apply_blockers(self, blockers: Sequence[str]) -> None:
        new_blockers = list(self._state.blockers)
        for b in blockers:
            if b not in new_blockers:
                new_blockers.append(b)
        self._transition(
            _P.BLOCKED,
            expected=_ANY_ACTIVE,
            blockers=tuple(new_blockers),
            **self._identity_override(),
        )

    def _apply_cancel_confirmation(self, event: ControlPlaneEvent) -> None:
        if not event.evidence_refs:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                "RUN_CANCELLED requires proven terminal execution evidence "
                "references; a bare cancellation acknowledgement cannot confirm "
                "terminality",
            )
        for ref in event.evidence_refs:
            if ref == ControlPlaneBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    "Remote UNVERIFIABLE evidence cannot produce a cancellation "
                    "completion; the cycle stays blocked/unverified (fail closed)",
                )
        self._transition(_P.CANCELLED, expected=(_P.CANCEL_REQUESTED,), **self._identity_override())

    # ------------------------------------------------------------------
    # Direct helper APIs -- every one delegates into the same validated
    # transition mechanism; none can bypass the state machine.
    # ------------------------------------------------------------------

    def qualify(self, decision: ParallelizationDecision) -> None:
        """Apply parallelization policy decision (CREATED -> QUALIFIED)."""
        with self._lock:
            self._ensure_active()
            self._transition(
                _P.QUALIFIED,
                expected=(_P.CREATED,),
                selected_strategy=decision.strategy,
                **self._identity_override(),
            )

    def plan(self) -> None:
        """QUALIFIED -> PLANNED (explicit planning step, optional)."""
        with self._lock:
            self._ensure_active()
            self._transition(_P.PLANNED, expected=(_P.QUALIFIED,), **self._identity_override())

    def prepare_workspaces(self, workspace_ids: Sequence[str]) -> None:
        with self._lock:
            self._ensure_active()
            ids = tuple(workspace_ids)
            for ws in ids:
                if not isinstance(ws, str) or not ws:
                    raise ControlPlaneError(
                        ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                        f"Invalid workspace id: {ws!r}",
                    )
            self._transition(
                _P.PREPARING,
                expected=(_P.QUALIFIED, _P.PLANNED),
                active_workspace_ids=ids,
                **self._identity_override(),
            )

    def register_run(self, run_id: str, workspace_id: str | None = None) -> None:
        """Authorize an execution run for fencing (run identity known to the cycle)."""
        with self._lock:
            self._ensure_active()
            if workspace_id is not None and workspace_id not in self._state.active_workspace_ids:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.RUN_WORKSPACE_MISMATCH.value,
                    f"Run workspace {workspace_id!r} is not an active workspace "
                    "of this cycle",
                )
            if run_id in self._state.active_run_ids:
                return
            self._state = replace(
                self._state,
                active_run_ids=tuple([*self._state.active_run_ids, run_id]),
            )

    def register_execution_host(self, execution_host_id: str) -> None:
        """Authorize an execution host identity for event fencing."""
        with self._lock:
            self._ensure_active()
            self._known_execution_hosts.add(execution_host_id)

    def start_investigation(self) -> None:
        """PREPARING -> INVESTIGATING (direct-API complement of WORKSPACE_READY)."""
        with self._lock:
            self._ensure_active()
            self._transition(_P.INVESTIGATING, expected=(_P.PREPARING,), **self._identity_override())

    def record_investigation_results(
        self,
        evidence_refs: Sequence[str],
        blockers: Sequence[str] = (),
    ) -> None:
        """INVESTIGATING -> IMPLEMENTING, or BLOCKED when blockers are raised."""
        with self._lock:
            self._ensure_active()
            for ref in evidence_refs:
                validate_evidence_ref(ref, ControlPlaneError)
            if blockers:
                self._apply_blockers(tuple(blockers))
                return
            self._transition(
                _P.IMPLEMENTING,
                expected=(_P.INVESTIGATING,),
                **self._identity_override(),
            )

    def record_candidate_results(
        self,
        candidates: Sequence[CandidateIdentity],
        blockers: Sequence[str] = (),
    ) -> None:
        """Register identity-bound candidates (no phase change).

        Registration requires full :class:`CandidateIdentity` binding and is
        only legal from IMPLEMENTING. Judging starts only when candidate
        completion evidence arrives (CANDIDATE_COMPLETED event or
        ``record_candidate_completed``).
        """
        with self._lock:
            self._ensure_active()
            if self._state.phase != _P.IMPLEMENTING:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Candidate registration is only legal from IMPLEMENTING; current "
                    f"phase is {self._state.phase.value}",
                )
            if blockers:
                self._apply_blockers(tuple(blockers))
                return
            for candidate in candidates:
                self._verify_candidate_binding(candidate)
                existing = self._candidates.get(candidate.candidate_id)
                if existing is not None and existing != candidate:
                    raise ControlPlaneError(
                        ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                        f"Candidate identity collision on candidate_id: "
                        f"{candidate.candidate_id}",
                    )
                self._candidates[candidate.candidate_id] = candidate
            new_ids = tuple(
                [*self._state.candidate_ids, *[c.candidate_id for c in candidates
                                               if c.candidate_id not in self._state.candidate_ids]]
            )
            self._state = replace(
                self._state,
                candidate_ids=new_ids,
                updated_at="2026-09-01T00:00:00Z",
            )

    def record_candidate_completed(self, candidate_id: str) -> None:
        """Record candidate completion evidence (IMPLEMENTING -> JUDGING)."""
        with self._lock:
            self._ensure_active()
            if candidate_id not in self._candidates:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Candidate {candidate_id!r} was never registered with this cycle",
                )
            self._completed_candidates.add(candidate_id)
            self._transition(
                _P.JUDGING,
                expected=(_P.IMPLEMENTING,),
                **self._identity_override(),
            )

    def _verify_candidate_binding(self, candidate: CandidateIdentity) -> None:
        if not isinstance(candidate, CandidateIdentity):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Candidates must be canonical CandidateIdentity contracts",
            )
        if candidate.task_id != self._state.task_id:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Candidate {candidate.candidate_id!r} belongs to foreign task "
                f"{candidate.task_id!r} (expected {self._state.task_id!r})",
            )
        if candidate.node_id != self._state.node_id:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Candidate {candidate.candidate_id!r} belongs to foreign node "
                f"{candidate.node_id!r} (expected {self._state.node_id!r})",
            )
        if candidate.base_sha != self._state.base_sha:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Candidate {candidate.candidate_id!r} is bound to stale/foreign base "
                f"{candidate.base_sha!r} (expected {self._state.base_sha!r})",
            )

    def _verify_judgeable_candidate(self, candidate_id: str) -> None:
        """A candidate can only be judged if registered AND proven completed."""
        candidate = self._candidates.get(candidate_id)
        if candidate is None:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Selected candidate {candidate_id!r} was never registered "
                "with this cycle; unknown/ghost candidates cannot be judged",
            )
        self._verify_candidate_binding(candidate)
        if candidate_id not in self._completed_candidates:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                f"Candidate {candidate_id!r} has no recorded completion evidence; "
                "a bare candidate id cannot prove execution completion",
            )

    def record_judgement(
        self,
        selected_candidate_id: str,
        blockers: Sequence[str] = (),
    ) -> None:
        """JUDGING -> VALIDATING (or BLOCKED). The candidate must be registered,
        identity-bound, and proven completed; arbitrary IDs can never be selected."""
        with self._lock:
            self._ensure_active()
            self._verify_judgeable_candidate(selected_candidate_id)
            if blockers:
                self._apply_blockers(tuple(blockers))
                return
            self._transition(
                _P.VALIDATING,
                expected=(_P.JUDGING,),
                selected_candidate_id=selected_candidate_id,
                **self._identity_override(),
            )

    def record_validation(
        self,
        evidence: ValidationEvidence,
        blockers: Sequence[str] = (),
    ) -> None:
        """VALIDATING -> READY_FOR_HANDOFF (or BLOCKED), gated on bound evidence."""
        with self._lock:
            self._ensure_active()
            if self._state.phase != _P.VALIDATING:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value,
                    f"Validation is only legal from VALIDATING; current phase is "
                    f"{self._state.phase.value}",
                )
            self._verify_validation_evidence(evidence)
            if blockers:
                self._apply_blockers(tuple(blockers))
                return
            self._apply_validation(evidence)

    def _verify_validation_evidence(self, evidence: ValidationEvidence) -> None:
        if not isinstance(evidence, ValidationEvidence):
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                "Validation requires a canonical ValidationEvidence record; a bare "
                "boolean is never sufficient",
            )
        if evidence.cycle_id != self._state.cycle_id:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Validation evidence belongs to a foreign cycle",
            )
        if evidence.task_id != self._state.task_id:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Validation evidence belongs to a foreign task",
            )
        if evidence.node_id != self._state.node_id:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Validation evidence belongs to a foreign node",
            )
        if evidence.candidate_id != self._state.selected_candidate_id:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Validation evidence does not bind to the judged candidate",
            )
        if evidence.base_sha != self._state.base_sha:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Validation evidence is bound to a stale/foreign base SHA",
            )
        if evidence.execution_epoch != self._state.execution_epoch:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value,
                "Validation evidence belongs to a stale execution epoch",
            )

    def _apply_validation(self, evidence: ValidationEvidence) -> None:
        if self._state.requalification_required:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                "Validation cannot produce READY_FOR_HANDOFF while requalification "
                "is required and no fresh requalification evidence exists",
            )
        self._validation_evidence = evidence
        self._transition(
            _P.READY_FOR_HANDOFF,
            expected=(_P.VALIDATING,),
            validation_evidence=evidence,
            **self._identity_override(),
        )

    def trigger_requalification(self) -> None:
        """JUDGING/VALIDATING -> REQUALIFYING (main drift or stale evidence)."""
        with self._lock:
            self._ensure_active()
            self._transition(
                _P.REQUALIFYING,
                expected=(_P.JUDGING, _P.VALIDATING),
                requalification_required=True,
                **self._identity_override(),
            )

    def record_requalification_results(
        self,
        evidence_refs: Sequence[str],
        validation_freshness: ValidationFreshness = ValidationFreshness.STILL_APPLICABLE,
        judgement_freshness: JudgementFreshness = JudgementFreshness.CURRENT,
    ) -> None:
        """REQUALIFYING -> VALIDATING with fresh requalification evidence (no auto-rebase)."""
        with self._lock:
            self._ensure_active()
            refs = tuple(evidence_refs)
            if not refs:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    "Requalification completion requires concrete evidence references",
                )
            for ref in refs:
                validate_evidence_ref(ref, ControlPlaneError)
            if validation_freshness in (ValidationFreshness.REQUIRES_RERUN, ValidationFreshness.INVALID) or judgement_freshness in (
                JudgementFreshness.STALE_BASE,
                JudgementFreshness.INVALID,
            ):
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    "Requalification evidence is stale (validation="
                    f"{validation_freshness.value}, judgement={judgement_freshness.value}); "
                    "stale evidence can never produce a fresh requalification completion",
                )
            self._transition(
                _P.VALIDATING,
                expected=(_P.REQUALIFYING,),
                requalification_required=False,
                **self._identity_override(),
            )

    def request_cancel(self) -> None:
        """Any active phase -> CANCEL_REQUESTED (cancellation is reachable, D7)."""
        with self._lock:
            self._ensure_active()
            self._transition(
                _P.CANCEL_REQUESTED,
                expected=_ANY_ACTIVE,
                **self._identity_override(),
            )

    # ------------------------------------------------------------------
    # Authority monotonicity (Phase 12)
    # ------------------------------------------------------------------

    def check_authority_monotonicity(self, boundary: AuthorityBoundary) -> None:
        """A child authority boundary must be a strict subset of intent authority.

        Execution authority can never expand beyond what the canonical
        TaskIntent grants. Secrets and production data stores are never
        authorized by an engineering intent.
        """
        ceiling = _STOP_BOUNDARY_EFFECT_CEILING.get(self._intent.stop_boundary)
        if ceiling is None:  # pragma: no cover - exhaustive enum
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Unknown stop boundary {self._intent.stop_boundary!r}",
            )
        for effect in boundary.allowed_effect_classes:
            if effect not in ceiling:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Authority expansion: effect class {effect.value} is not granted "
                    f"by intent stop boundary {self._intent.stop_boundary.value}",
                )
        child_rank = _STOP_BOUNDARY_RANK.get(boundary.stop_boundary)
        intent_rank = _STOP_BOUNDARY_RANK[self._intent.stop_boundary]
        if child_rank is None or child_rank > intent_rank:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                f"Authority expansion: child stop boundary {boundary.stop_boundary.value} "
                f"exceeds intent stop boundary {self._intent.stop_boundary.value}",
            )
        if boundary.production_authorized and self._intent.stop_boundary not in _PRODUCTION_BOUNDARIES:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Production authority cannot be derived from a non-production "
                "TaskIntent stop boundary",
            )
        if boundary.secret_access_authorized:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Secret access authority can never be derived from a TaskIntent",
            )
        if boundary.data_access_authorized:
            raise ControlPlaneError(
                ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                "Production data-store access authority can never be derived from "
                "a TaskIntent",
            )

    # ------------------------------------------------------------------
    # Handoff
    # ------------------------------------------------------------------

    def generate_handoff(
        self,
        target_node_id: str,
        evidence_refs: Sequence[str],
    ) -> NodeHandoff:
        with self._lock:
            if self._state.phase != _P.READY_FOR_HANDOFF:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    f"Cannot generate handoff while phase is {self._state.phase.value}",
                )
            if self._state.blockers:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    f"Cannot generate handoff with active blockers: {self._state.blockers}",
                )
            if self._state.requalification_required:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    "Cannot generate handoff while requalification is required",
                )
            if self._validation_evidence is None:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value,
                    "Cannot generate handoff without recorded validation evidence",
                )
            lineage_kinds = {node.node_id: node.kind for node in self._lineage.nodes}
            target_kind = lineage_kinds.get(target_node_id)
            if target_kind is None:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Handoff target node {target_node_id!r} is not bound to the "
                    "canonical TaskLineage (orphan handoff target)",
                )
            if target_kind != NodeKind.TASK:
                raise ControlPlaneError(
                    ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value,
                    f"Handoff target {target_node_id!r} is a {target_kind.value} "
                    "lineage node, not a TASK node",
                )
            for ref in evidence_refs:
                validate_evidence_ref(ref, ControlPlaneError)
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
