"""PR-12 observability: identity conflict handling (fail closed, no reconciliation)."""

from __future__ import annotations

import dataclasses

import pytest

from ai_engineering.observability.contracts import (
    ObservabilityReasonCode,
    OperatorHealthState,
    ProjectionStatus,
)
from tests.ai_engineering.observability_fixture_helpers import (
    BASE_SHA,
    CANDIDATE_ID,
    HOST_REMOTE,
    OTHER_SHA,
    TASK_ID,
    WORKSPACE_ID,
    collect_full,
    make_candidate,
    make_intent,
    make_lineage,
    make_run_record,
    make_state,
    make_workspace,
)
from tests.ai_engineering.control_plane_fixture_helpers import make_state as base_make_state


def _task_intent():
    return make_intent()


class TestIntentConflicts:
    def test_task_intent_task_mismatch(self):
        intent = make_intent()
        other = dataclasses.replace(intent, task_id="other-task")
        snap = collect_full(intent=other)
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert (
            ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value
            in snap.projection_health.reason_codes
        )
        assert snap.projection_health.health is OperatorHealthState.CONFLICTED

    def test_intent_digest_mismatch(self):
        intent = make_intent()
        other = dataclasses.replace(intent, intent_revision=99)
        snap = collect_full(intent=other)
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_intent_repository_mismatch(self):
        intent = make_intent()
        other = dataclasses.replace(intent, source_repository="other/repo")
        snap = collect_full(intent=other)
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_intent_base_sha_mismatch(self):
        intent = make_intent()
        other = dataclasses.replace(intent, source_base_sha=OTHER_SHA)
        snap = collect_full(intent=other)
        assert snap.projection_status is ProjectionStatus.CONFLICTED


class TestLineageConflicts:
    def test_lineage_missing_node_is_conflict(self):
        lineage = make_lineage()
        # Remove the bound task node: keep only criterion nodes.
        from ai_engineering.task_intent import LineageNode, NodeKind

        orphan = dataclasses.replace(
            lineage,
            nodes=tuple(n for n in lineage.nodes if n.node_id != "n1"),
            edges=tuple(
                e
                for e in lineage.edges
                if e.source_id != "n1" and e.target_id != "n1"
            ),
        )
        snap = collect_full(lineage=orphan)
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert snap.lineage.bound_node_present is False


class TestWorkspaceConflicts:
    def test_workspace_task_mismatch(self):
        ws = make_workspace(task_id="task-B")
        snap = collect_full(workspaces=(ws,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert (
            ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value
            in snap.projection_health.reason_codes
        )

    def test_workspace_repository_mismatch(self):
        ws = make_workspace()
        other = dataclasses.replace(ws, repository="other/repo")
        snap = collect_full(workspaces=(other,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_workspace_base_sha_mismatch(self):
        ws = make_workspace()
        other = dataclasses.replace(ws, base_sha=OTHER_SHA)
        snap = collect_full(workspaces=(other,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_workspace_unknown_host_mismatch(self):
        ws = make_workspace(execution_host_id="ghost-host")
        snap = collect_full(workspaces=(ws,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert "EXECUTION_HOST_MISMATCH" in snap.projection_health.reason_codes


class TestRunConflicts:
    def test_run_task_mismatch(self):
        from ai_engineering.execution.run_contracts import RunState

        run = make_run_record(run_id="run-x", task_id="task-B", state=RunState.LIVE)
        snap = collect_full(runs=(run,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_run_node_mismatch(self):
        from ai_engineering.execution.run_contracts import RunState

        run = make_run_record(run_id="run-x", node_id="node-B", state=RunState.LIVE)
        snap = collect_full(runs=(run,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_run_workspace_mismatch(self):
        from ai_engineering.execution.run_contracts import RunState

        run = make_run_record(run_id="run-x", workspace_id="ws-ghost", state=RunState.LIVE)
        snap = collect_full(runs=(run,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert "RUN_WORKSPACE_MISMATCH" in snap.projection_health.reason_codes

    def test_run_host_mismatch(self):
        from ai_engineering.execution.run_contracts import RunState

        run = make_run_record(run_id="run-x", execution_host_id="ghost-host", state=RunState.LIVE)
        snap = collect_full(runs=(run,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert "EXECUTION_HOST_MISMATCH" in snap.projection_health.reason_codes

    def test_run_epoch_mismatch_is_stale_not_conflict(self):
        from ai_engineering.execution.run_contracts import RunState

        run = make_run_record(run_id="run-x", execution_epoch=7, state=RunState.LIVE)
        snap = collect_full(runs=(run,))
        assert snap.projection_status is ProjectionStatus.STALE
        assert snap.runs[0].operator_state == "STALE"


class TestCandidateConflicts:
    def test_candidate_task_mismatch(self):
        candidate = make_candidate(candidate_id="cand-x", task_id="task-B")
        snap = collect_full(candidates=(candidate,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_candidate_node_mismatch(self):
        candidate = make_candidate(candidate_id="cand-x", node_id="node-B")
        snap = collect_full(candidates=(candidate,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_candidate_base_sha_mismatch(self):
        candidate = make_candidate(candidate_id="cand-x", base_sha=OTHER_SHA)
        snap = collect_full(candidates=(candidate,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_candidate_unknown_workspace(self):
        candidate = make_candidate(candidate_id="cand-x", workspace_id="ws-ghost")
        snap = collect_full(candidates=(candidate,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert "WORKTREE_IDENTITY_MISMATCH" in snap.projection_health.reason_codes

    def test_candidate_result_base_mismatch(self):
        candidate = make_candidate()
        from tests.ai_engineering.observability_fixture_helpers import make_candidate_result

        result = make_candidate_result(base_sha=OTHER_SHA)
        snap = collect_full(
            candidates=(candidate,), candidate_results={"cand-1": result}
        )
        assert snap.projection_status is ProjectionStatus.CONFLICTED


class TestJudgeConflicts:
    def test_judge_task_mismatch(self):
        from tests.ai_engineering.observability_fixture_helpers import make_judge_result

        judge = make_judge_result()
        other = dataclasses.replace(judge, task_id="task-B")
        snap = collect_full(judge_result=other)
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_judge_base_mismatch(self):
        from tests.ai_engineering.observability_fixture_helpers import make_judge_result

        judge = make_judge_result(base_sha=OTHER_SHA)
        snap = collect_full(judge_result=judge)
        assert snap.projection_status is ProjectionStatus.CONFLICTED

    def test_judge_ghost_selection(self):
        # The canonical judge contract structurally rejects a selection
        # that is not among the eligible judgements (binding invariant).
        from tests.ai_engineering.observability_fixture_helpers import make_judge_result

        with pytest.raises(Exception):
            make_judge_result(selected_candidate_id="ghost-candidate")


class TestConflictProminence:
    def test_conflict_survives_into_health_and_dict(self):
        ws = make_workspace(task_id="task-B")
        snap = collect_full(workspaces=(ws,))
        data = snap.to_dict()
        assert data["projection_health"]["health"] == "CONFLICTED"
        assert data["projection_status"] == "CONFLICTED"
        assert (
            ObservabilityReasonCode.OBSERVABILITY_IDENTITY_CONFLICT.value
            in data["projection_health"]["reason_codes"]
        )

    def test_conflict_overrides_stale_in_precedence(self):
        from ai_engineering.execution.run_contracts import RunState

        stale_run = make_run_record(run_id="run-x", execution_epoch=9, state=RunState.LIVE)
        bad_ws = make_workspace(task_id="task-B")
        snap = collect_full(workspaces=(bad_ws,), runs=(stale_run,))
        assert snap.projection_status is ProjectionStatus.CONFLICTED
        assert snap.projection_health.health is OperatorHealthState.CONFLICTED
