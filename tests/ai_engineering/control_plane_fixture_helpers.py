"""Shared deterministic fixtures for control-plane (PR-11.1) tests."""

from __future__ import annotations

from ai_engineering.candidates.candidate_contracts import CandidateIdentity
from ai_engineering.contracts import StopBoundary, TaskClass
from ai_engineering.control_plane.contracts import ControlPlanePhase, ValidationEvidence
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent
from ai_engineering.control_plane.orchestrator import EngineeringCycleOrchestrator
from ai_engineering.task_intent import (
    AcceptanceCriterion,
    IntentStatus,
    NodeKind,
    RelationKind,
    LineageEdge,
    LineageNode,
    TaskIntent,
    TaskLineage,
    intent_digest,
    validate_intent,
    validate_lineage,
)

SHA = "e3a4f268d68786728e88e6ae8953e79a6f694ada"
TASK_ID = "t1"
NODE_ID = "n1"
CYCLE_ID = "c1"


def make_intent(
    task_id: str = TASK_ID,
    base_sha: str = SHA,
    stop_boundary: StopBoundary = StopBoundary.DRAFT_PR,
    intent_revision: int = 1,
) -> TaskIntent:
    return validate_intent(
        TaskIntent(
            schema_version=1,
            task_id=task_id,
            intent_revision=intent_revision,
            status=IntentStatus.READY,
            task_class=TaskClass.BOUNDED_IMPLEMENTATION,
            desired_outcome="Deterministic control-plane hardening fixture intent",
            source_repository="life2boat/hermes",
            source_main_ref="main",
            source_base_sha=base_sha,
            constraints=("Offline deterministic execution only",),
            allowed_mutations=("ai_engineering/",),
            forbidden_mutations=("production/", "deploy/"),
            stop_boundary=stop_boundary,
            acceptance_criteria=(AcceptanceCriterion("AC1", "Cycle completes"),),
            unknowns=(),
            applicable_invariants=("CP1",),
            required_gates=("CODE_GATE",),
            parent_intent_digest=None,
        )
    )


def make_lineage(task_node_id: str = NODE_ID, target_node_id: str | None = None) -> TaskLineage:
    nodes = [
        LineageNode(node_id="intent-1", kind=NodeKind.INTENT),
        LineageNode(node_id="crit-1", kind=NodeKind.CRITERION),
        LineageNode(node_id=task_node_id, kind=NodeKind.TASK),
    ]
    edges = [
        LineageEdge(
            source_id=task_node_id, target_id="crit-1", relation=RelationKind.IMPLEMENTS
        ),
    ]
    if target_node_id is not None:
        nodes.append(LineageNode(node_id=target_node_id, kind=NodeKind.TASK))
        edges.append(
            LineageEdge(
                source_id=target_node_id, target_id="crit-1", relation=RelationKind.IMPLEMENTS
            )
        )
    return validate_lineage(
        TaskLineage(schema_version=1, nodes=tuple(nodes), edges=tuple(edges))
    )


def make_state(
    *,
    cycle_id: str = CYCLE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    intent: TaskIntent | None = None,
    phase: str = "CREATED",
    execution_epoch: int = 1,
    base_sha: str | None = None,
) -> EngineeringCycleState:
    intent_obj = intent if intent is not None else make_intent(task_id=task_id, base_sha=base_sha or SHA)
    state = EngineeringCycleState.from_task_intent(
        intent_obj, cycle_id=cycle_id, node_id=node_id
    )
    if phase != "CREATED" or execution_epoch != 1:
        from dataclasses import replace

        state = replace(
            state,
            phase=ControlPlanePhase(phase),
            execution_epoch=execution_epoch,
        )
    return state


def make_orchestrator(
    *,
    cycle_id: str = CYCLE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    intent: TaskIntent | None = None,
    lineage: TaskLineage | None = None,
    phase: str = "CREATED",
    execution_epoch: int = 1,
    target_node_id: str | None = "n2",
) -> EngineeringCycleOrchestrator:
    intent_obj = intent if intent is not None else make_intent(task_id=task_id)
    lineage_obj = (
        lineage
        if lineage is not None
        else make_lineage(task_node_id=node_id, target_node_id=target_node_id)
    )
    state = make_state(
        cycle_id=cycle_id,
        task_id=task_id,
        node_id=node_id,
        intent=intent_obj,
        phase=phase,
        execution_epoch=execution_epoch,
    )
    return EngineeringCycleOrchestrator(state, intent=intent_obj, lineage=lineage_obj)


def make_candidate(
    candidate_id: str = "cand-1",
    *,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    base_sha: str = SHA,
    workspace_id: str = "ws-1",
    run_id: str = "run-1",
) -> CandidateIdentity:
    return CandidateIdentity(
        candidate_id=candidate_id,
        task_id=task_id,
        node_id=node_id,
        base_sha=base_sha,
        workspace_id=workspace_id,
        run_id=run_id,
    )


def make_event(
    event_id: str,
    event_type,
    *,
    cycle_id: str = CYCLE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    execution_epoch: int = 1,
    source_kind: str = "TEST",
    source_id: str = "src-1",
    evidence_refs: tuple[str, ...] = (),
    run_id: str | None = None,
    workspace_id: str | None = None,
    candidate_id: str | None = None,
    execution_host_id: str | None = None,
) -> ControlPlaneEvent:
    return ControlPlaneEvent(
        event_id=event_id,
        cycle_id=cycle_id,
        task_id=task_id,
        node_id=node_id,
        execution_epoch=execution_epoch,
        event_type=event_type,
        source_kind=source_kind,
        source_id=source_id,
        evidence_refs=evidence_refs,
        run_id=run_id,
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        execution_host_id=execution_host_id,
    )


def make_validation_evidence(
    candidate_id: str = "cand-1",
    *,
    evidence_id: str = "val-1",
    cycle_id: str = CYCLE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    base_sha: str = SHA,
    execution_epoch: int = 1,
    evidence_refs: tuple[str, ...] = ("snap-1",),
) -> ValidationEvidence:
    return ValidationEvidence(
        evidence_id=evidence_id,
        cycle_id=cycle_id,
        task_id=task_id,
        node_id=node_id,
        candidate_id=candidate_id,
        base_sha=base_sha,
        execution_epoch=execution_epoch,
        evidence_refs=evidence_refs,
    )


def drive_to_validating(
    orch: EngineeringCycleOrchestrator,
    *,
    candidate_id: str = "cand-1",
) -> None:
    """Drive a fresh orchestrator deterministically to the VALIDATING phase."""
    orch.qualify(_candidate_decision())
    orch.prepare_workspaces(["ws-1"])
    orch.start_investigation()
    orch.record_investigation_results(["inv-ref-1"])
    orch.record_candidate_results([make_candidate(candidate_id)])
    orch.record_candidate_completed(candidate_id)
    orch.record_judgement(candidate_id)


def _candidate_decision():
    from ai_engineering.parallel.parallel_contracts import (
        ParallelizationDecision,
        ParallelizationStrategy,
    )

    return ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=3,
        max_agents=3,
        requires_single_mutation_owner=True,
        requires_serialization_barrier=True,
        reason="Test candidate strategy",
    )


def digest_of(intent: TaskIntent) -> str:
    return intent_digest(intent)
