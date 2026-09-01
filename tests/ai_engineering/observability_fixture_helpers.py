"""Shared fixtures for PR-12 observability tests.

Builds synthetic authoritative records (PR-1..PR-11.1 contracts) with
deterministic values. All timestamps are fixed constants — no wall
clock, no randomness.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Sequence

from ai_engineering.candidates.candidate_contracts import (
    CandidateIdentity,
    CandidateResult,
    CandidateState,
)
from ai_engineering.contracts import AuthorityBoundary, EffectClass, StopBoundary
from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import (
    ControlPlanePhase,
    ValidationEvidence,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.events import ControlPlaneEvent, ControlPlaneEventType
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.control_plane.registry import EngineeringCycleRegistry
from ai_engineering.execution.host_contracts import (
    ExecutionHostIdentity,
    ExecutionMode,
    HostCapability,
    HostPlatform,
)
from ai_engineering.execution.remote_contracts import RemoteProcessIdentity, RemoteExecutionState
from ai_engineering.execution.remote_state import RemoteExecutionLifecycle
from ai_engineering.execution.run_contracts import RunState
from ai_engineering.execution.run_state import AgentRunIdentity, AgentRunRecord
from ai_engineering.judge.judge_contracts import (
    CandidateDecisionState,
    CandidateJudgeResult,
    CandidateJudgement,
    CandidateSemanticScore,
)
from ai_engineering.observability.contracts import ProjectionLimits
from ai_engineering.observability.projection import OperatorSnapshot
from ai_engineering.observability.collector import collect_operator_snapshot
from ai_engineering.parallel.parallel_contracts import (
    ConcurrencyBudget,
    ParallelizationDecision,
    ParallelizationStrategy,
)
from ai_engineering.requalification.requalification_contracts import (
    BaseRelationship,
    CandidateRequalificationResult,
    RequalificationDecisionState,
    RequalificationEvidence,
    ValidationFreshness,
)
from ai_engineering.task_intent import (
    AcceptanceCriterion,
    IntentUnknown,
    LineageEdge,
    LineageNode,
    TaskIntent,
    TaskLineage,
    intent_digest,
    validate_intent,
    validate_lineage,
)
from ai_engineering.workspaces.snapshot_contracts import (
    DiffArtifact,
    SnapshotPhase,
    WorkspaceSnapshot,
)
from ai_engineering.workspaces.workspace_contracts import (
    LeaseState,
    WorkspaceIdentity,
    WorktreeLease,
)
from tests.ai_engineering.control_plane_fixture_helpers import make_intent, make_lineage, make_state

T0 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
BASE_SHA = "e3a4f268d68786728e88e6ae8953e79a6f694ada"
HEAD_SHA = "4ef1cfd43870b252d8b79864712ca2c919ac58cb"
OTHER_SHA = "1111111111111111111111111111111111111111"
TASK_ID = "t1"
NODE_ID = "n1"
CYCLE_ID = "c1"
REPO = "life2boat/hermes"
HOST_LOCAL = "host-local"
HOST_REMOTE = "host-remote"
WORKSPACE_ID = "ws-1"
CANDIDATE_ID = "cand-1"
RUN_ID = "run-1"
SECRET_SENTINEL = "HERMES_OBSERVABILITY_SECRET_SENTINEL_DO_NOT_EXPOSE"


def make_workspace(
    *,
    workspace_id: str = WORKSPACE_ID,
    task_id: str = TASK_ID,
    candidate_id: str | None = CANDIDATE_ID,
    base_sha: str = BASE_SHA,
    execution_host_id: str = HOST_LOCAL,
    execution_mode: str = "LOCAL",
    created_at: datetime = T0,
) -> WorkspaceIdentity:
    return WorkspaceIdentity(
        workspace_id=workspace_id,
        task_id=task_id,
        candidate_id=candidate_id,
        repository=REPO,
        base_ref="main",
        base_sha=base_sha,
        branch=f"agent/{workspace_id}",
        worktree_path=f".hermes/worktrees/{workspace_id}",
        execution_host_id=execution_host_id,
        execution_mode=execution_mode,
        created_at=created_at,
    )


def make_lease(
    *,
    workspace_id: str = WORKSPACE_ID,
    owner_run_id: str = RUN_ID,
    task_id: str = TASK_ID,
    state: LeaseState = LeaseState.ACTIVE,
    acquired_at: datetime = T0,
    expires_at: datetime | None = None,
) -> WorktreeLease:
    return WorktreeLease(
        workspace_id=workspace_id,
        owner_run_id=owner_run_id,
        task_id=task_id,
        state=state,
        acquired_at=acquired_at,
        expires_at=expires_at,
    )


def make_run_record(
    *,
    run_id: str = RUN_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    workspace_id: str = WORKSPACE_ID,
    candidate_id: str | None = CANDIDATE_ID,
    execution_host_id: str = HOST_LOCAL,
    execution_epoch: int = 1,
    state: RunState = RunState.LIVE,
    start_time: datetime = T0,
    exit_code: int | None = None,
    error_message: str | None = None,
) -> AgentRunRecord:
    return AgentRunRecord(
        identity=AgentRunIdentity(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            workspace_id=workspace_id,
            candidate_id=candidate_id,
            model="claude-opus-4-6",
            agent_capability="IMPLEMENTATION",
            execution_host_id=execution_host_id,
            execution_epoch=execution_epoch,
            start_time=start_time,
        ),
        state=state,
        updated_at=start_time + timedelta(minutes=1),
        exit_code=exit_code,
        error_message=error_message,
    )


def make_host(
    *,
    execution_host_id: str = HOST_LOCAL,
    mode: ExecutionMode = ExecutionMode.LOCAL,
    available: bool = True,
    capabilities: Sequence[HostCapability] = (
        HostCapability.CAN_RUN_COMMANDS,
        HostCapability.CAN_ACCESS_REPOSITORY,
    ),
) -> ExecutionHostIdentity:
    return ExecutionHostIdentity(
        execution_host_id=execution_host_id,
        mode=mode,
        controller_platform=HostPlatform.WINDOWS,
        host_platform=HostPlatform.WINDOWS,
        hostname="hermes-local",
        architecture="x86_64",
        available=available,
        capabilities=tuple(capabilities),
        created_at="2026-01-15T12:00:00+00:00",
    )


def make_remote_lifecycle(
    *,
    run_id: str = RUN_ID,
    workspace_id: str = WORKSPACE_ID,
    execution_host_id: str = HOST_REMOTE,
    state: RemoteExecutionState = RemoteExecutionState.LIVE,
    execution_epoch: int = 1,
) -> RemoteExecutionLifecycle:
    identity = RemoteProcessIdentity(
        execution_id="exec-1",
        run_id=run_id,
        workspace_id=workspace_id,
        execution_host_id=execution_host_id,
        session_id="sess-1",
        remote_process_id="12345",
        execution_epoch=execution_epoch,
    )
    return RemoteExecutionLifecycle(process_identity=identity, initial_state=state)


def make_snapshot(
    *,
    snapshot_id: str = "snap-1",
    workspace_id: str = WORKSPACE_ID,
    candidate_id: str = CANDIDATE_ID,
    phase: SnapshotPhase = SnapshotPhase.POST_EXECUTION,
    changed_paths: tuple[str, ...] = ("src/feature.py",),
) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        snapshot_id=snapshot_id,
        workspace_id=workspace_id,
        task_id=TASK_ID,
        candidate_id=candidate_id,
        run_id=RUN_ID,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        branch=f"agent/{workspace_id}",
        worktree_path=f".hermes/worktrees/{workspace_id}",
        execution_epoch=1,
        phase=phase,
        captured_at="2026-01-15T12:30:00+00:00",
        git_status="M src/feature.py",
        changed_paths=changed_paths,
        diff_stat="1 file changed",
        diff_digest="d" * 64,
        clean=False,
    )


def make_diff_artifact(
    *,
    artifact_id: str = "diff-1",
    workspace_id: str = WORKSPACE_ID,
    candidate_id: str = CANDIDATE_ID,
) -> DiffArtifact:
    return DiffArtifact(
        artifact_id=artifact_id,
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        changed_paths=("src/feature.py",),
        diff_stat="1 file changed",
        diff_digest="e" * 64,
        patch_size_bytes=120,
        binary_files=(),
        generated_at="2026-01-15T12:31:00+00:00",
    )


def make_candidate(
    *,
    candidate_id: str = CANDIDATE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    workspace_id: str = WORKSPACE_ID,
    base_sha: str = BASE_SHA,
) -> CandidateIdentity:
    return CandidateIdentity(
        candidate_id=candidate_id,
        task_id=task_id,
        node_id=node_id,
        workspace_id=workspace_id,
        run_id=RUN_ID,
        base_sha=base_sha,
    )


def make_candidate_result(
    *,
    candidate_id: str = CANDIDATE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    base_sha: str = BASE_SHA,
    state: CandidateState = CandidateState.COMPLETED,
    success: bool = True,
    blockers: tuple[str, ...] = (),
    pre_snapshot: WorkspaceSnapshot | None = None,
    post_snapshot: WorkspaceSnapshot | None = None,
    diff_artifact: DiffArtifact | None = None,
) -> CandidateResult:
    return CandidateResult(
        candidate_id=candidate_id,
        task_id=task_id,
        node_id=node_id,
        workspace_id=WORKSPACE_ID,
        run_id=RUN_ID,
        base_sha=base_sha,
        branch=f"agent/{candidate_id}",
        changed_paths=("src/feature.py",),
        diff_summary="adds feature",
        validation_results=(),
        state=state,
        blockers=blockers,
        completed_at="2026-01-15T12:40:00+00:00",
        success=success,
        candidate_head_sha=HEAD_SHA,
        pre_execution_snapshot=pre_snapshot,
        post_execution_snapshot=post_snapshot,
        diff_artifact=diff_artifact,
    )


def make_judge_result(
    *,
    candidate_ids: tuple[str, ...] = (CANDIDATE_ID,),
    selected_candidate_id: str | None = CANDIDATE_ID,
    hard_gate_passed: bool = True,
    eligible: bool = True,
    decision_state: CandidateDecisionState = CandidateDecisionState.SINGLE_ELIGIBLE,
    base_sha: str = BASE_SHA,
) -> CandidateJudgeResult:
    judgements = tuple(
        CandidateJudgement(
            candidate_id=cid,
            hard_gate_passed=hard_gate_passed,
            eligible=eligible,
            semantic_score=(
                CandidateSemanticScore(
                    candidate_id=cid,
                    score=0.9,
                    rationale="clean diff",
                    evaluator_id="judge-1",
                )
                if hard_gate_passed
                else None
            ),
            rank=1,
            blockers=(),
            rationale="ok",
            hard_gate_results=(),
        )
        for cid in candidate_ids
    )
    return CandidateJudgeResult(
        judge_id="judge-1",
        task_id=TASK_ID,
        node_id=NODE_ID,
        base_sha=base_sha,
        judgements=judgements,
        selected_candidate_id=selected_candidate_id,
        decision_state=decision_state,
        completed_at="2026-01-15T12:50:00+00:00",
    )


def make_validation_evidence(
    *,
    cycle_id: str = CYCLE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    candidate_id: str = CANDIDATE_ID,
    base_sha: str = BASE_SHA,
    execution_epoch: int = 1,
    evidence_refs: tuple[str, ...] = ("snap-1",),
) -> ValidationEvidence:
    return ValidationEvidence(
        evidence_id=f"val-{candidate_id}",
        cycle_id=cycle_id,
        task_id=task_id,
        node_id=node_id,
        candidate_id=candidate_id,
        base_sha=base_sha,
        execution_epoch=execution_epoch,
        evidence_refs=evidence_refs,
    )


def make_requalification_result(
    *,
    candidate_id: str = CANDIDATE_ID,
    candidate_base_sha: str = BASE_SHA,
    current_main_sha: str = BASE_SHA,
    relationship: BaseRelationship = BaseRelationship.EXACT_BASE,
    decision_state: RequalificationDecisionState = RequalificationDecisionState.NO_REQUALIFICATION_REQUIRED,
    evidence: RequalificationEvidence | None = None,
) -> CandidateRequalificationResult:
    return CandidateRequalificationResult(
        requalification_id=f"req-{candidate_id}",
        candidate_id=candidate_id,
        candidate_base_sha=candidate_base_sha,
        current_main_sha=current_main_sha,
        relationship=relationship,
        decision_state=decision_state,
        eligible=True,
        requires_new_candidate=False,
        blockers=(),
        evidence=evidence,
        completed_at="2026-01-15T13:00:00+00:00",
    )


def make_requalification_evidence(
    *,
    candidate_base_sha: str = BASE_SHA,
    current_main_sha: str = BASE_SHA,
    validation_status: ValidationFreshness = ValidationFreshness.STILL_APPLICABLE,
) -> RequalificationEvidence:
    return RequalificationEvidence(
        candidate_base_sha=candidate_base_sha,
        current_main_sha=current_main_sha,
        drift_changed_paths=(),
        candidate_changed_paths=("src/feature.py",),
        overlapping_paths=(),
        drift_diff_digest="f" * 64,
        candidate_diff_digest="a" * 64,
        validation_status=validation_status,
    )


def make_handoff(
    *,
    handoff_id: str = "handoff-1",
    cycle_id: str = CYCLE_ID,
    selected_candidate_id: str = CANDIDATE_ID,
) -> NodeHandoff:
    return NodeHandoff(
        handoff_id=handoff_id,
        cycle_id=cycle_id,
        task_id=TASK_ID,
        source_node_id=NODE_ID,
        target_node_id="node-2",
        base_sha=BASE_SHA,
        execution_epoch=1,
        evidence_refs=("snap-1", "diff-1"),
        selected_candidate_id=selected_candidate_id,
        created_at="2026-01-15T13:10:00+00:00",
    )


def make_barrier(
    *,
    active_mutation_agents: int = 0,
    single_production_owner: str | None = "owner-1",
    ready: bool = True,
) -> ProductionSerializationBarrier:
    return ProductionSerializationBarrier(
        active_mutation_agents=active_mutation_agents,
        single_production_owner=single_production_owner,
        ready=ready,
    )


def make_event(
    *,
    event_id: str = "evt-1",
    event_type: ControlPlaneEventType = ControlPlaneEventType.WORKSPACE_READY,
    cycle_id: str = CYCLE_ID,
    task_id: str = TASK_ID,
    node_id: str = NODE_ID,
    execution_epoch: int = 1,
    created_at: str = "2026-01-15T12:05:00+00:00",
    run_id: str | None = None,
    workspace_id: str | None = WORKSPACE_ID,
    candidate_id: str | None = None,
    execution_host_id: str | None = None,
) -> ControlPlaneEvent:
    return ControlPlaneEvent(
        event_id=event_id,
        event_type=event_type,
        cycle_id=cycle_id,
        task_id=task_id,
        node_id=node_id,
        execution_epoch=execution_epoch,
        source_kind="OPERATOR",
        source_id="operator-test",
        created_at=created_at,
        run_id=run_id,
        workspace_id=workspace_id,
        candidate_id=candidate_id,
        execution_host_id=execution_host_id,
    )


def make_parallelization_decision(
    *,
    strategy: ParallelizationStrategy = ParallelizationStrategy.CANDIDATE,
) -> ParallelizationDecision:
    return ParallelizationDecision(
        allowed=True,
        strategy=strategy,
        max_candidates=3,
        max_agents=3,
        requires_single_mutation_owner=True,
        requires_serialization_barrier=True,
        reason="candidate parallelism permitted",
    )


def make_budget(max_candidates: int = 3) -> ConcurrencyBudget:
    return ConcurrencyBudget(max_candidates=max_candidates)


def make_authority() -> AuthorityBoundary:
    return AuthorityBoundary(
        allowed_effect_classes=(
            EffectClass.READ_ONLY,
            EffectClass.REPOSITORY_WRITE,
        ),
        forbidden_effect_classes=(
            EffectClass.DEPLOY,
            EffectClass.SECRET_MUTATION,
            EffectClass.PR_MERGE,
        ),
        stop_boundary=StopBoundary.COMMIT,
        production_authorized=False,
        secret_access_authorized=False,
        data_access_authorized=False,
    )


def build_full_state():
    """Build a fully coherent, healthy synthetic state."""

    import dataclasses

    intent = make_intent()
    lineage = make_lineage()
    state = dataclasses.replace(
        make_state(),
        selected_candidate_id=CANDIDATE_ID,
        selected_strategy=ParallelizationStrategy.CANDIDATE,
    )
    registry = EngineeringCycleRegistry()
    registry.register_cycle(state)
    workspaces = (make_workspace(),)
    leases = (make_lease(),)
    hosts = (
        make_host(execution_host_id=HOST_LOCAL, mode=ExecutionMode.LOCAL),
        make_host(execution_host_id=HOST_REMOTE, mode=ExecutionMode.SSH),
    )
    runs = (make_run_record(),)
    candidates = (make_candidate(),)
    pre_snapshot = make_snapshot(snapshot_id="snap-pre", phase=SnapshotPhase.PRE_EXECUTION)
    post_snapshot = make_snapshot(snapshot_id="snap-1", phase=SnapshotPhase.POST_EXECUTION)
    candidate_results = {
        CANDIDATE_ID: make_candidate_result(
            pre_snapshot=pre_snapshot,
            post_snapshot=post_snapshot,
            diff_artifact=make_diff_artifact(),
        )
    }
    judge_result = make_judge_result()
    validation = make_validation_evidence()
    requalification_result = make_requalification_result(
        evidence=make_requalification_evidence()
    )
    handoff = make_handoff()
    registry.record_handoff(handoff)
    events = (
        make_event(event_id="evt-1", created_at="2026-01-15T12:05:00+00:00"),
        make_event(event_id="evt-2", created_at="2026-01-15T12:06:00+00:00"),
    )
    barrier = make_barrier()
    return {
        "intent": intent,
        "lineage": lineage,
        "cycle": state,
        "registry": registry,
        "workspaces": workspaces,
        "leases": leases,
        "hosts": hosts,
        "runs": runs,
        "candidates": candidates,
        "candidate_results": candidate_results,
        "judge_result": judge_result,
        "validation": validation,
        "requalification_result": requalification_result,
        "handoff": handoff,
        "events": events,
        "raw_events": events,
        "barrier": barrier,
        "authority": make_authority(),
        "parallelization_decision": make_parallelization_decision(),
        "budget": make_budget(),
    }


def collect_full(**overrides) -> OperatorSnapshot:
    state = build_full_state()
    remote_state = overrides.pop("_remote_state", None)
    judge_hard_fail = overrides.pop("_judge_hard_fail", False)
    state.update(overrides)
    if remote_state is not None:
        state["remote_lifecycles"] = {
            HOST_REMOTE: make_remote_lifecycle(state=remote_state),
        }
    if judge_hard_fail:
        state["judge_result"] = make_judge_result(
            hard_gate_passed=False,
            eligible=False,
            selected_candidate_id=None,
            decision_state=CandidateDecisionState.NO_ELIGIBLE_CANDIDATES,
        )
    return collect_operator_snapshot(
        cycle=state["cycle"],
        intent=state["intent"],
        lineage=state["lineage"],
        authority=state["authority"],
        parallelization_decision=state["parallelization_decision"],
        budget=state["budget"],
        workspaces=state["workspaces"],
        leases=state["leases"],
        runs=state["runs"],
        hosts=state["hosts"],
        remote_lifecycles=state.get("remote_lifecycles"),
        candidates=state["candidates"],
        candidate_results=state["candidate_results"],
        judge_result=state["judge_result"],
        validation=state["validation"],
        requalification_result=state["requalification_result"],
        current_main_sha=state.get("current_main_sha"),
        handoff=state["handoff"],
        registry=state["registry"],
        raw_events=state.get("raw_events"),
        production_barrier=state["barrier"],
        clock=state.get("clock"),
        limits=state.get("limits"),
    )
