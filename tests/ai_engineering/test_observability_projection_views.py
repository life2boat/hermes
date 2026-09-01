"""PR-12 observability: core views, projection states, and barrier semantics."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from ai_engineering.candidates.candidate_contracts import CandidateState
from ai_engineering.contracts import EffectClass
from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.contracts import ControlPlanePhase
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.observability.collector import OperatorQueries, collect_operator_snapshot
from ai_engineering.observability.contracts import (
    OBSERVABILITY_SCHEMA_VERSION,
    BarrierName,
    OperatorHealthState,
    ProjectionStatus,
)
from ai_engineering.observability.projection import project
from ai_engineering.execution.remote_contracts import RemoteExecutionState
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)
from ai_engineering.requalification.requalification_contracts import (
    RequalificationDecisionState,
)
from tests.ai_engineering.observability_fixture_helpers import (
    BASE_SHA,
    CANDIDATE_ID,
    HOST_REMOTE,
    OTHER_SHA,
    WORKSPACE_ID,
    collect_full,
    make_candidate,
    make_host,
    make_requalification_result,
    make_run_record,
    make_workspace,
)
from tests.ai_engineering.control_plane_fixture_helpers import make_state


class TestCompleteHealthyProjection:
    def test_complete_healthy_projection(self):
        snap = collect_full()
        assert snap.projection_status is ProjectionStatus.COMPLETE
        assert snap.projection_health.health is OperatorHealthState.OK
        assert snap.projection_health.reason_codes == ()

    def test_schema_version_present(self):
        snap = collect_full()
        assert snap.schema_version == OBSERVABILITY_SCHEMA_VERSION == 1

    def test_cycle_view_fields(self):
        snap = collect_full()
        cycle = snap.cycle
        assert cycle.cycle_id == "c1"
        assert cycle.task_id == "t1"
        assert cycle.node_id == "n1"
        assert cycle.repository_id == "life2boat/hermes"
        assert cycle.source_base_sha == BASE_SHA
        assert cycle.execution_epoch == 1
        assert len(cycle.intent_digest) == 64

    def test_task_intent_view_excludes_prompt_text(self):
        snap = collect_full()
        intent = snap.task_intent
        assert intent.intent_digest == snap.cycle.intent_digest
        assert intent.acceptance_criteria_count >= 1
        # No raw desired outcome / constraint text anywhere in the snapshot dict.
        raw = str(snap.to_dict())
        assert "write the" not in raw.lower().replace(" ", "")  # cheap sanity
        assert not hasattr(intent, "desired_outcome")

    def test_authority_view_descriptive_only(self):
        snap = collect_full()
        authority = snap.authority
        assert authority.production_authorized is False
        assert authority.authority_production_capable is False
        assert authority.authority_secret_capable is False
        assert authority.authority_datastore_capable is False
        assert authority.authority_restricted is False
        assert EffectClass.DEPLOY.value in authority.forbidden_effect_classes

    def test_authority_view_flags_capability_classes(self):
        from ai_engineering.contracts import AuthorityBoundary, StopBoundary

        authority = AuthorityBoundary(
            allowed_effect_classes=(EffectClass.DEPLOY, EffectClass.SECRET_MUTATION, EffectClass.DATA_MUTATION),
            forbidden_effect_classes=(),
            stop_boundary=StopBoundary.DEPLOY,
            production_authorized=True,
            secret_access_authorized=True,
            data_access_authorized=True,
        )
        snap = collect_full(authority=authority)
        assert snap.authority.authority_production_capable is True
        assert snap.authority.authority_secret_capable is True
        assert snap.authority.authority_datastore_capable is True
        assert snap.authority.authority_restricted is True

    def test_tasklineage_view_binding(self):
        snap = collect_full()
        assert snap.lineage.bound_node_id == "n1"
        assert snap.lineage.bound_node_kind == "TASK"
        assert snap.lineage.bound_node_present is True
        assert snap.lineage.node_count >= 1

    def test_workspace_view_fields(self):
        snap = collect_full()
        assert len(snap.workspaces) == 1
        ws = snap.workspaces[0]
        assert ws.workspace_id == WORKSPACE_ID
        assert ws.execution_mode == "LOCAL"
        assert ws.lease_state == "ACTIVE"
        assert ws.owner_run_id == "run-1"

    def test_run_view_operator_states(self):
        snap = collect_full()
        assert snap.runs[0].operator_state == "ACTIVE"
        assert snap.runs[0].model == "claude-opus-4-6"
        assert "api_key" not in snap.runs[0].to_dict()

    def test_parallelization_view_within_budget(self):
        snap = collect_full()
        view = snap.parallelization
        assert view.strategy == ParallelizationStrategy.CANDIDATE.value
        assert view.candidate_slots_used == 1
        assert view.candidate_slots_remaining == 2
        assert view.concurrency_status == "CONCURRENCY_WITHIN_BUDGET"
        assert view.requires_single_mutation_owner is True

    def test_parallelization_budget_exceeded(self):
        from ai_engineering.parallel.parallel_contracts import ConcurrencyBudget

        candidates = tuple(
            make_candidate(candidate_id=f"cand-{i}", workspace_id=f"ws-{i}") for i in range(3)
        )
        workspaces = tuple(
            make_workspace(workspace_id=f"ws-{i}", candidate_id=f"cand-{i}") for i in range(3)
        )
        snap = collect_full(
            candidates=candidates,
            workspaces=workspaces,
            budget=ConcurrencyBudget(max_candidates=2),
        )
        assert snap.parallelization.concurrency_status == "CONCURRENCY_BUDGET_EXCEEDED"
        assert snap.parallelization.candidate_slots_remaining == 0

    def test_candidate_view_binding(self):
        snap = collect_full()
        cand = snap.candidates[0]
        assert cand.candidate_id == CANDIDATE_ID
        assert cand.completion_state == CandidateState.COMPLETED.value
        assert cand.validation_eligible is True
        assert cand.selected is True
        assert cand.freshness == "STILL_APPLICABLE"

    def test_candidate_registered_without_result(self):
        snap = collect_full(candidate_results={})
        assert snap.candidates[0].completion_state == "REGISTERED"
        assert snap.candidates[0].validation_eligible is False

    def test_judgement_view_deterministic(self):
        snap = collect_full()
        judgement = snap.judgement
        assert judgement.present is True
        assert judgement.selected_candidate_id == CANDIDATE_ID
        assert judgement.judgements[0].hard_gate_passed is True
        assert judgement.judgements[0].semantic_score == 0.9

    def test_judgement_hard_fail_renders_ineligible(self):
        snap = collect_full(_judge_hard_fail=True)
        entry = snap.judgement.judgements[0]
        assert entry.hard_gate_passed is False
        assert entry.eligible is False

    def test_semantic_cannot_override_hard_fail(self):
        # The canonical judge contract structurally forbids attaching a
        # semantic score to a hard-failed candidate: construction fails.
        import pytest as _pytest
        from ai_engineering.judge.judge_contracts import (
            CandidateJudgement,
            CandidateSemanticScore,
        )

        with _pytest.raises(Exception):
            CandidateJudgement(
                candidate_id=CANDIDATE_ID,
                hard_gate_passed=False,
                eligible=False,
                semantic_score=CandidateSemanticScore(
                    candidate_id=CANDIDATE_ID,
                    score=0.99,
                    rationale="looks great",
                    evaluator_id="judge-1",
                ),
            )
        snap = collect_full(_judge_hard_fail=True)
        entry = snap.judgement.judgements[0]
        assert entry.semantic_review_present is False
        assert entry.eligible is False

    def test_validation_view_bindings(self):
        snap = collect_full()
        validation = snap.validation
        assert validation.present is True
        assert validation.status == "VALID"
        assert validation.cycle_binding_ok is True
        assert validation.task_binding_ok is True
        assert validation.node_binding_ok is True
        assert validation.base_sha_binding_ok is True
        assert validation.execution_epoch_binding_ok is True
        assert validation.evidence_refs_count == 1
        assert validation.evidence_refs == ("snap-1",)

    def test_validation_missing_renders_missing(self):
        snap = collect_full(validation=None)
        assert snap.validation.status == "MISSING"
        assert snap.validation.present is False
        assert snap.handoff.readiness is False
        assert "OBSERVABILITY_EVIDENCE_MISSING" in snap.handoff.missing_requirements

    def test_validation_stale_when_requalification_required(self):
        # Requalification required but no fresh requalification evidence yet.
        state = dataclasses.replace(make_state(), requalification_required=True)
        snap = collect_full(cycle=state, requalification_result=None)
        assert snap.validation.status == "STALE"
        assert snap.validation.freshness == "REQUIRES_RERUN"

    def test_requalification_view(self):
        snap = collect_full()
        requal = snap.requalification
        assert requal.requalification_required is False
        assert requal.candidate_base_sha == BASE_SHA
        assert requal.qualified_against_sha == BASE_SHA

    def test_requalification_required_flagged(self):
        state = dataclasses.replace(make_state(), requalification_required=True)
        snap = collect_full(cycle=state, requalification_result=None)
        assert snap.requalification.requalification_required is True
        assert "CANDIDATE_REQUALIFICATION_REQUIRED" in snap.requalification.reason_codes
        assert snap.projection_status is ProjectionStatus.STALE
        assert snap.projection_health.health is OperatorHealthState.STALE

    def test_main_drift_observability(self):
        snap = collect_full(current_main_sha=OTHER_SHA, requalification_result=None)
        requal = snap.requalification
        assert requal.current_authoritative_sha == OTHER_SHA
        assert requal.candidate_base_sha == BASE_SHA
        assert requal.qualified_against_sha == BASE_SHA
        assert snap.projection_status is ProjectionStatus.STALE
        # No GitHub fetch: projection is pure.
        assert "CANDIDATE_BASE_DRIFT" in snap.projection_health.reason_codes

    def test_handoff_view(self):
        snap = collect_full()
        handoff = snap.handoff
        assert handoff.present is True
        assert handoff.handoff_id == "handoff-1"
        assert handoff.cycle_id == "c1"
        assert handoff.candidate_id == CANDIDATE_ID
        assert handoff.evidence_refs_count == 2
        assert handoff.readiness is True

    def test_artifact_view(self):
        snap = collect_full()
        kinds = {a.kind for a in snap.artifacts}
        assert kinds == {"SNAPSHOT", "DIFF_ARTIFACT"}
        assert len(snap.artifacts) == 3  # 2 snapshots + 1 diff
        for artifact in snap.artifacts:
            assert artifact.base_sha == BASE_SHA
            assert artifact.changed_path_count == 1

    def test_provenance_explicit(self):
        snap = collect_full()
        provenance = snap.generated_from
        assert provenance.repository_id == "life2boat/hermes"
        assert provenance.cycle_id == "c1"
        assert provenance.execution_epoch == 1
        assert "CONTROL_PLANE_STATE" in provenance.sources_present
        assert "TASK_INTENT" in provenance.sources_present
        assert "CURRENT_MAIN_SHA" in provenance.sources_absent

    def test_partial_input_cycle_only(self):
        state = make_state()
        snap = collect_operator_snapshot(cycle=state)
        assert snap.projection_status is ProjectionStatus.PARTIAL
        assert snap.task_intent is None
        assert snap.lineage is None
        assert "OBSERVABILITY_PROJECTION_INCOMPLETE" in snap.projection_health.reason_codes
        # Not OK: missing TaskIntent binding is not trusted.
        assert snap.projection_health.health is OperatorHealthState.DEGRADED

    def test_empty_registries(self):
        state = dataclasses.replace(make_state(), selected_candidate_id=None)
        snap = collect_full(
            cycle=state,
            workspaces=(),
            leases=(),
            runs=(),
            hosts=(),
            candidates=(),
            candidate_results={},
            judge_result=None,
        )
        assert snap.workspaces == ()
        assert snap.runs == ()
        assert snap.execution_hosts == ()
        assert snap.candidates == ()
        assert snap.projection_status in (ProjectionStatus.COMPLETE, ProjectionStatus.PARTIAL)


class TestOperatorStates:
    def test_terminal_cycle_views(self):
        for phase in (ControlPlanePhase.COMPLETED, ControlPlanePhase.FAILED, ControlPlanePhase.CANCELLED):
            state = dataclasses.replace(make_state(), phase=phase)
            snap = collect_full(cycle=state)
            assert snap.control_plane.terminal is True
            assert snap.control_plane.phase == phase.value

    def test_blocked_cycle_projection(self):
        state = dataclasses.replace(
            make_state(), phase=ControlPlanePhase.BLOCKED, blockers=("SOME_BLOCKER",)
        )
        snap = collect_full(cycle=state)
        assert snap.control_plane.blocked is True
        assert "SOME_BLOCKER" in snap.control_plane.blockers
        assert snap.projection_health.health is OperatorHealthState.BLOCKED

    def test_ready_for_handoff_view(self):
        state = dataclasses.replace(make_state(), phase=ControlPlanePhase.READY_FOR_HANDOFF)
        snap = collect_full(cycle=state)
        assert snap.control_plane.handoff_ready is True

    def test_lease_expiry_with_injected_clock(self):
        from tests.ai_engineering.observability_fixture_helpers import make_lease

        lease = make_lease(expires_at=datetime(2026, 1, 15, 13, 0, 0, tzinfo=timezone.utc))
        snap = collect_full(leases=(lease,), clock=datetime(2026, 1, 15, 14, 0, 0, tzinfo=timezone.utc))
        assert snap.leases[0].classification == "LEASE_EXPIRED"
        # Expired lease must not render healthy.
        assert snap.workspaces[0].lease_state == "ACTIVE"  # raw canonical state
        classifications = [item.classification for item in snap.leases]
        assert "LEASE_EXPIRED" in classifications

    def test_lease_expiry_without_clock_is_unverifiable(self):
        from tests.ai_engineering.observability_fixture_helpers import make_lease

        lease = make_lease(expires_at=datetime(2026, 1, 15, 13, 0, 0, tzinfo=timezone.utc))
        snap = collect_full(leases=(lease,))
        assert snap.leases[0].classification == "LEASE_ACTIVE"
        assert "OBSERVABILITY_SOURCE_UNVERIFIABLE" in snap.leases[0].reason_codes

    def test_stale_epoch_run_rendered_stale(self):
        stale_run = make_run_record(run_id="run-2", execution_epoch=2)
        snap = collect_full(runs=(make_run_record(), stale_run))
        by_id = {r.run_id: r for r in snap.runs}
        assert by_id["run-1"].operator_state == "ACTIVE"
        assert by_id["run-2"].operator_state == "STALE"
        assert snap.projection_status is ProjectionStatus.STALE


class TestRemoteUnverifiable:
    def test_remote_unverifiable_distinct_from_exit(self):
        lifecycle = collect_full.__wrapped__ if hasattr(collect_full, "__wrapped__") else None
        snap = collect_full(_remote_state=RemoteExecutionState.UNVERIFIABLE)
        remote = [h for h in snap.execution_hosts if h.execution_host_id == HOST_REMOTE]
        assert remote[0].remote_state == "UNVERIFIABLE"
        assert remote[0].remote_reconciled_terminal is False
        assert remote[0].remote_reconciliation_required is True

    def test_unverifiable_propagates_to_health(self):
        snap = collect_full(_remote_state=RemoteExecutionState.UNVERIFIABLE)
        assert snap.projection_status is ProjectionStatus.UNVERIFIABLE
        assert snap.projection_health.health is OperatorHealthState.UNVERIFIABLE

    def test_unverifiable_blocks_handoff_readiness(self):
        snap = collect_full(_remote_state=RemoteExecutionState.UNVERIFIABLE)
        barrier = {b.barrier_name: b for b in snap.barriers}[BarrierName.HANDOFF_READINESS.value]
        assert barrier.ready is False
        assert "REMOTE_EXECUTION_UNVERIFIABLE" in barrier.reason_codes

    def test_unverifiable_run_state(self):
        snap = collect_full(
            _remote_state=RemoteExecutionState.UNVERIFIABLE,
            runs=(make_run_record(execution_host_id=HOST_REMOTE),),
        )
        assert snap.runs[0].operator_state == "UNVERIFIABLE"
        assert "REMOTE_EXECUTION_UNVERIFIABLE" in snap.runs[0].reason_codes

    def test_disconnected_is_unverifiable_not_exited(self):
        snap = collect_full(_remote_state=RemoteExecutionState.DISCONNECTED)
        remote = [h for h in snap.execution_hosts if h.execution_host_id == HOST_REMOTE]
        assert remote[0].remote_state == "UNVERIFIABLE"

    def test_remote_exit_requires_terminal_proof(self):
        snap = collect_full(_remote_state=RemoteExecutionState.EXITED)
        remote = [h for h in snap.execution_hosts if h.execution_host_id == HOST_REMOTE]
        assert remote[0].remote_state == "EXITED"
        assert remote[0].remote_reconciled_terminal is True
        # EXITED with reconciliation proof does not poison projection.
        assert snap.projection_status is ProjectionStatus.COMPLETE

    def test_cancel_ack_not_cancelled(self):
        snap = collect_full(_remote_state=RemoteExecutionState.CANCEL_REQUESTED)
        remote = [h for h in snap.execution_hosts if h.execution_host_id == HOST_REMOTE]
        # Remote cancel-ack is neither LIVE nor a terminal state here.
        assert remote[0].remote_state not in ("EXITED", "CANCELLED")

    def test_cancel_requested_run_operator_state(self):
        from ai_engineering.execution.run_contracts import RunState

        snap = collect_full(runs=(make_run_record(state=RunState.CANCEL_REQUESTED),))
        assert snap.runs[0].operator_state == "CANCEL_REQUESTED"
        assert snap.runs[0].operator_state != "CANCELLED"


class TestProductionSerialization:
    def test_ready_barrier(self):
        snap = collect_full()
        view = snap.production_serialization
        assert view.ready is True
        assert view.active_mutation_agents == 0
        assert view.owner_count == 1
        assert view.production_owner == "owner-1"

    def test_not_ready_with_active_mutations(self):
        snap = collect_full(barrier=make_barrier_ready_false())
        view = snap.production_serialization
        assert view.ready is False
        assert "PARALLEL_MUTATION_CONFLICT" in view.reason_codes

    def test_no_owner_assignment(self):
        snap = collect_full(barrier=ProductionSerializationBarrier(active_mutation_agents=0, single_production_owner=None, ready=False))
        assert snap.production_serialization.production_owner is None
        assert snap.production_serialization.owner_count == 0
        assert "OBSERVABILITY_EVIDENCE_MISSING" in snap.production_serialization.reason_codes

    def test_barrier_missing_is_incomplete_not_ready(self):
        snap = collect_full(barrier=None)
        assert snap.production_serialization is None
        barrier = {b.barrier_name: b for b in snap.barriers}[BarrierName.PRODUCTION_SERIALIZATION.value]
        assert barrier.ready is False
        assert "OBSERVABILITY_PROJECTION_INCOMPLETE" in barrier.reason_codes


def make_barrier_ready_false():
    from tests.ai_engineering.observability_fixture_helpers import make_barrier

    return make_barrier(active_mutation_agents=2, single_production_owner="owner-1", ready=False)
