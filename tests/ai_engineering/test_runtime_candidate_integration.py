"""PR-13 candidate-result integration and control-plane barrier preservation tests."""

from __future__ import annotations

import sys

import pytest

from ai_engineering.candidates.candidate_contracts import CandidateState
from ai_engineering.control_plane.barriers import ProductionSerializationBarrier
from ai_engineering.control_plane.orchestrator import EngineeringCycleOrchestrator
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)
from ai_engineering.runtime.runtime_contracts import RuntimeBlockingReason
from ai_engineering.runtime.runtime_evidence import build_candidate_result_from_evidence
from tests.ai_engineering.control_plane_fixture_helpers import make_lineage, make_orchestrator
from tests.ai_engineering.runtime_fixture_helpers import (
    CANDIDATE_ID,
    LOCAL_HOST_ID,
    RUN_ID,
    TASK_ID,
    WORKSPACE_ID,
    execute_default,
    make_local_fixture,
    make_request,
)

_DECISION = ParallelizationDecision(
    allowed=True,
    strategy=ParallelizationStrategy.CANDIDATE,
    max_candidates=2,
    max_agents=1,
    requires_single_mutation_owner=True,
    requires_serialization_barrier=True,
    reason="PR-13 controlled runtime integration",
)


def _orchestrator(fx) -> EngineeringCycleOrchestrator:
    return make_orchestrator(
        task_id=TASK_ID,
        node_id="node-1",
        intent=fx.intent,
        lineage=make_lineage(task_node_id="node-1", target_node_id=None),
    )


def _drive_to_implementing(orch: EngineeringCycleOrchestrator) -> None:
    orch.qualify(_DECISION)
    orch.prepare_workspaces([WORKSPACE_ID])
    orch.register_execution_host(LOCAL_HOST_ID)
    orch.register_run(RUN_ID, WORKSPACE_ID)
    orch.start_investigation()
    orch.record_investigation_results(["docs/pr13-investigation.md"])


class TestCandidateResultFromEvidence:
    def test_successful_execution_builds_completed_candidate(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", "open('feature.py', 'w').write('x = 1')"),
            execution_id="exec-c1",
        )
        artifacts = fx.runtime.get_artifacts(evidence.execution_id)
        result = build_candidate_result_from_evidence(
            evidence,
            branch="codex/candidate/task-1/cand-1",
            pre_execution_snapshot=artifacts.pre_execution_snapshot,
            post_execution_snapshot=artifacts.post_execution_snapshot,
            diff_artifact=artifacts.diff_artifact,
        )
        assert result.state == CandidateState.COMPLETED
        assert result.success is True
        assert result.validation_results == ()
        assert result.post_execution_snapshot is not None
        assert "feature.py" in result.post_execution_snapshot.changed_paths

    def test_missing_post_snapshot_blocks_completion(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(fx, execution_id="exec-c2")
        result = build_candidate_result_from_evidence(
            evidence,
            branch="b",
            post_execution_snapshot=None,
            diff_artifact=None,
        )
        assert result.state == CandidateState.FAILED
        assert result.success is False
        assert RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value in result.blockers

    def test_nonzero_exit_builds_failed_candidate(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", "import sys; sys.exit(1)"),
            execution_id="exec-c3",
        )
        artifacts = fx.runtime.get_artifacts(evidence.execution_id)
        result = build_candidate_result_from_evidence(
            evidence,
            branch="b",
            post_execution_snapshot=artifacts.post_execution_snapshot,
            diff_artifact=artifacts.diff_artifact,
        )
        assert result.state == CandidateState.FAILED
        assert result.success is False

    def test_cancel_terminal_builds_cancelled_candidate(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        from tests.ai_engineering.test_runtime_idempotency_concurrency import _evidence_for

        evidence = _evidence_for(request := make_request(fx), cancelled=True, cancel_terminal=True)
        result = build_candidate_result_from_evidence(
            evidence,
            branch="b",
            post_execution_snapshot=None,
            diff_artifact=None,
        )
        assert result.state == CandidateState.CANCELLED
        assert result.success is False

    def test_timeout_evidence_never_completes_candidate(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        from tests.ai_engineering.test_runtime_idempotency_concurrency import _evidence_for

        evidence = _evidence_for(
            make_request(fx),
            state="TIMED_OUT",
            exit_code=None,
            exit_proven=False,
            timed_out=True,
        )
        result = build_candidate_result_from_evidence(
            evidence,
            branch="b",
            post_execution_snapshot=None,
            diff_artifact=None,
        )
        assert result.state == CandidateState.FAILED
        assert result.success is False

    def test_snapshot_foreign_workspace_not_accepted(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(fx, execution_id="exec-c4")
        artifacts = fx.runtime.get_artifacts(evidence.execution_id)
        foreign = artifacts.post_execution_snapshot
        from dataclasses import replace

        foreign = replace(foreign, workspace_id="ws-other")
        result = build_candidate_result_from_evidence(
            evidence,
            branch="b",
            post_execution_snapshot=foreign,
            diff_artifact=artifacts.diff_artifact,
        )
        assert result.state == CandidateState.FAILED
        assert RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value in result.blockers


class TestValidationBarrierPreserved:
    def test_runtime_evidence_never_yields_handoff_ready(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        orch = _orchestrator(fx)
        _drive_to_implementing(orch)
        orch.record_candidate_results([fx.candidate])
        request, evidence = execute_default(
            fx,
            argv=(sys.executable, "-c", "print('implemented')"),
            execution_id="exec-v1",
        )
        orch.record_candidate_completed(CANDIDATE_ID)
        assert orch.state.phase.value == "JUDGING"
        assert orch.state.phase.value != "READY_FOR_HANDOFF"

    def test_validation_without_judge_fails_closed(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        orch = _orchestrator(fx)
        _drive_to_implementing(orch)
        orch.record_candidate_results([fx.candidate])
        orch.record_candidate_completed(CANDIDATE_ID)
        from ai_engineering.control_plane.contracts import ControlPlaneError
        from tests.ai_engineering.observability_fixture_helpers import make_validation_evidence

        with pytest.raises(ControlPlaneError):
            orch.record_validation(make_validation_evidence())

    def test_requalification_blocks_handoff(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        orch = _orchestrator(fx)
        _drive_to_implementing(orch)
        orch.record_candidate_results([fx.candidate])
        orch.record_candidate_completed(CANDIDATE_ID)
        orch.trigger_requalification()
        assert orch.state.phase.value == "REQUALIFYING"
        assert orch.state.phase.value != "READY_FOR_HANDOFF"

    def test_candidate_binding_enforced_for_runtime_candidate(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        orch = _orchestrator(fx)
        _drive_to_implementing(orch)
        from dataclasses import replace

        stale_base = replace(fx.candidate, base_sha="b" * 40)
        from ai_engineering.control_plane.contracts import ControlPlaneError

        with pytest.raises(ControlPlaneError):
            orch.record_candidate_results([stale_base])


class TestProductionSerializationBarrierView:
    def test_no_runtime_mutation_of_production_owner(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request, evidence = execute_default(fx, execution_id="exec-p1")
        barrier = ProductionSerializationBarrier(
            active_mutation_agents=0,
            single_production_owner="cycle-owner",
        )
        assert barrier.ready is True
        # The runtime never assigns ownership; the barrier view is read-only.
        assert evidence.success is True

    def test_active_mutation_agents_block_ready(self):
        barrier = ProductionSerializationBarrier(
            active_mutation_agents=2,
            single_production_owner=None,
        )
        assert barrier.ready is False

    def test_single_owner_required_for_ready(self):
        barrier = ProductionSerializationBarrier(
            active_mutation_agents=0,
            single_production_owner=None,
        )
        assert barrier.ready is False
