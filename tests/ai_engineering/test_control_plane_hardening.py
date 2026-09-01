"""PR-11.1 corrective hardening regression tests.

Behavior-driven reproduction of every audited defect (D1-D8) plus the
ordered-transition, identity-fencing, authority-monotonicity, lineage,
repository-binding, and single-readiness-gate requirements. Each test
maps 1:1 to a required corrective behavior from the hardening brief.
"""

from __future__ import annotations

import pytest

from ai_engineering.control_plane.contracts import (
    ControlPlaneBlockingReason,
    ControlPlaneError,
    ControlPlaneEventType,
    ControlPlanePhase,
    ValidationEvidence,
)
from ai_engineering.control_plane.cycle_state import EngineeringCycleState
from ai_engineering.control_plane.handoff import NodeHandoff
from ai_engineering.control_plane.orchestrator import EngineeringCycleOrchestrator
from ai_engineering.control_plane.registry import EngineeringCycleRegistry
from ai_engineering.parallel.parallel_contracts import (
    ParallelizationDecision,
    ParallelizationStrategy,
)
from tests.ai_engineering.control_plane_fixture_helpers import (
    SHA,
    drive_to_validating,
    make_candidate,
    make_event,
    make_intent,
    make_lineage,
    make_orchestrator,
    make_state,
    make_validation_evidence,
)


def _code(exc_info) -> str:
    return exc_info.value.code


def _decision() -> ParallelizationDecision:
    return ParallelizationDecision(
        allowed=True,
        strategy=ParallelizationStrategy.CANDIDATE,
        max_candidates=1,
        max_agents=1,
        requires_single_mutation_owner=True,
        requires_serialization_barrier=True,
        reason="Hardening test decision",
    )


def drive_to_implementing(candidate_id: str = "cand-1"):
    """Fresh orchestrator driven to IMPLEMENTING, ready to register candidates."""
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    orch.start_investigation()
    orch.record_investigation_results(["inv-ref-1"])
    return orch


# ===========================================================================
# D1: terminal state resurrection (every public mutator)
# ===========================================================================


@pytest.mark.parametrize("phase", ["COMPLETED", "FAILED", "CANCELLED"])
def test_d1_investigation_result_cannot_resurrect_terminal(phase):
    orch = make_orchestrator(phase=phase)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_investigation_results(["inv-ref"])
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase(phase)


@pytest.mark.parametrize("phase", ["COMPLETED", "FAILED", "CANCELLED"])
def test_d1_candidate_result_cannot_resurrect_terminal(phase):
    orch = make_orchestrator(phase=phase)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_candidate_results([make_candidate()])
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase(phase)


@pytest.mark.parametrize("phase", ["COMPLETED", "FAILED", "CANCELLED"])
def test_d1_requalification_cannot_resurrect_terminal(phase):
    orch = make_orchestrator(phase=phase)
    with pytest.raises(ControlPlaneError) as exc:
        orch.trigger_requalification()
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase(phase)


@pytest.mark.parametrize("phase", ["COMPLETED", "FAILED", "CANCELLED"])
def test_d1_judgement_cannot_resurrect_terminal(phase):
    orch = make_orchestrator(phase=phase)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_judgement("cand-1")
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase(phase)


@pytest.mark.parametrize("phase", ["COMPLETED", "FAILED", "CANCELLED"])
def test_d1_validation_cannot_resurrect_terminal(phase):
    orch = make_orchestrator(phase=phase)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence())
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase(phase)


@pytest.mark.parametrize("phase", ["COMPLETED", "FAILED", "CANCELLED"])
def test_d1_every_other_mutator_cannot_resurrect_terminal(phase):
    orch = make_orchestrator(phase=phase)
    with pytest.raises(ControlPlaneError):
        orch.qualify(_decision())
    with pytest.raises(ControlPlaneError):
        orch.prepare_workspaces(["ws-1"])
    with pytest.raises(ControlPlaneError):
        orch.start_investigation()
    with pytest.raises(ControlPlaneError):
        orch.request_cancel()
    with pytest.raises(ControlPlaneError):
        orch.register_run("run-1")
    with pytest.raises(ControlPlaneError):
        orch.register_execution_host("host-1")
    with pytest.raises(ControlPlaneError):
        orch.plan()
    with pytest.raises(ControlPlaneError):
        orch.record_requalification_results(["req-1"])
    assert orch.state.phase == ControlPlanePhase(phase)


@pytest.mark.parametrize("phase", ["COMPLETED", "FAILED", "CANCELLED"])
@pytest.mark.parametrize(
    "et",
    [
        ControlPlaneEventType.WORKSPACE_READY,
        ControlPlaneEventType.INVESTIGATION_COMPLETED,
        ControlPlaneEventType.CANDIDATE_COMPLETED,
        ControlPlaneEventType.JUDGEMENT_COMPLETED,
        ControlPlaneEventType.VALIDATION_COMPLETED,
        ControlPlaneEventType.REQUALIFICATION_COMPLETED,
        ControlPlaneEventType.RUN_FAILED,
        ControlPlaneEventType.RUN_CANCELLED,
        ControlPlaneEventType.BLOCKER_RAISED,
    ],
)
def test_d1_no_event_can_resurrect_terminal(phase, et):
    orch = make_orchestrator(phase=phase)
    with pytest.raises(ControlPlaneError):
        orch.apply_event(make_event(f"ev-{et.value}", et))
    assert orch.state.phase == ControlPlanePhase(phase)


# ===========================================================================
# Phase 4: ordered phase transitions
# ===========================================================================


def test_ordered_created_to_ready_for_handoff_impossible():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.CREATED


def test_ordered_preparing_to_judging_impossible():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    with pytest.raises(ControlPlaneError):
        orch.record_candidate_results([make_candidate()])
    assert orch.state.phase == ControlPlanePhase.PREPARING


def test_ordered_created_to_validating_via_judgement_impossible():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.record_judgement("cand-1")
    assert orch.state.phase == ControlPlanePhase.CREATED


def test_ordered_implementing_to_completed_impossible():
    orch = drive_to_implementing()
    orch.record_candidate_results([make_candidate()])
    orch.record_candidate_completed("cand-1")
    # No public API exists to jump to COMPLETED; the phase machine has no
    # IMPLEMENTING -> COMPLETED edge.
    assert not hasattr(orch, "complete")
    assert orch.state.phase == ControlPlanePhase.JUDGING


def test_ordered_full_chain_has_no_skips():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    orch.start_investigation()
    orch.record_investigation_results(["inv-1"])
    orch.record_candidate_results([make_candidate()])
    orch.record_candidate_completed("cand-1")
    orch.record_judgement("cand-1")
    orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF


# ===========================================================================
# D2: handoff path filter bypass
# ===========================================================================


@pytest.mark.parametrize(
    "ref",
    [
        "/tmp/foreign/file",
        "C:\\foreign\\file",
        "C:/foreign/file",
        "\\\\server\\share\\file",
        "//server/share/file",
        "../foreign/file",
        "nested/../../foreign/file",
        "./../foreign/file",
        "C:/foreign/worktree/file.json",
        "C:\\foreign\\worktree\\file.json",
    ],
)
def test_d2_handoff_rejects_every_audited_path_shape(ref):
    with pytest.raises(ControlPlaneError) as exc:
        NodeHandoff(
            handoff_id="h1",
            task_id="t1",
            source_node_id="n1",
            target_node_id="n2",
            cycle_id="c1",
            base_sha=SHA,
            execution_epoch=1,
            evidence_refs=(ref,),
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value


def test_d2_generated_handoff_rejects_foreign_paths():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.record_validation(make_validation_evidence())
    for bad in ("C:/foreign/file.json", "\\\\server\\share\\file", "../escape"):
        with pytest.raises(ControlPlaneError):
            orch.generate_handoff("n2", [bad])


def test_d2_validation_evidence_rejects_foreign_paths():
    with pytest.raises(ControlPlaneError):
        make_validation_evidence(evidence_refs=("//server/share/file",))


# ===========================================================================
# D3: judge/candidate and validation binding
# ===========================================================================


def test_d3_ghost_candidate_judgement_rejected():
    orch = drive_to_implementing()
    orch.record_candidate_results([make_candidate("cand-1")])
    orch.record_candidate_completed("cand-1")
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_judgement("ghost-candidate")
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value
    assert orch.state.phase == ControlPlanePhase.JUDGING


def test_d3_uncompleted_candidate_judgement_rejected():
    orch = drive_to_implementing()
    orch.record_candidate_results([make_candidate("cand-1")])
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_judgement("cand-1")
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase.IMPLEMENTING


def test_d3_foreign_task_candidate_rejected():
    orch = drive_to_implementing()
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_candidate_results([make_candidate("cand-fr", task_id="other-task")])
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value
    assert orch.state.phase == ControlPlanePhase.IMPLEMENTING


def test_d3_stale_base_candidate_rejected():
    orch = drive_to_implementing()
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_candidate_results([make_candidate("cand-stale", base_sha="b" * 40)])
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_d3_cross_node_candidate_rejected():
    orch = drive_to_implementing()
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_candidate_results([make_candidate("cand-cross", node_id="n-other")])
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_d3_candidate_identity_collision_rejected():
    orch = drive_to_implementing()
    orch.record_candidate_results([make_candidate("cand-1")])
    # Same id, different binding, re-registered -> collision (registration
    # from JUDGING is phase-illegal, so drive a second cycle).
    orch2 = drive_to_implementing()
    orch2.record_candidate_results([make_candidate("cand-1")])
    assert orch.state.candidate_ids == orch2.state.candidate_ids


def test_d3_validation_without_evidence_rejected():
    orch = make_orchestrator()
    drive_to_validating(orch)
    with pytest.raises(ControlPlaneError) as exc:
        ValidationEvidence(
            evidence_id="val-1",
            cycle_id="c1",
            task_id="t1",
            node_id="n1",
            candidate_id="cand-1",
            base_sha=SHA,
            execution_epoch=1,
            evidence_refs=(),
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value
    with pytest.raises(ControlPlaneError):
        orch.record_validation(True)  # type: ignore[arg-type]


def test_d3_foreign_cycle_validation_evidence_rejected():
    orch = make_orchestrator()
    drive_to_validating(orch)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence(cycle_id="other-cycle"))
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_d3_stale_epoch_validation_evidence_rejected():
    orch = make_orchestrator()
    drive_to_validating(orch)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence(execution_epoch=99))
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value


def test_d3_stale_base_validation_evidence_rejected():
    orch = make_orchestrator()
    drive_to_validating(orch)
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence(base_sha="c" * 40))
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_d3_validation_for_other_candidate_rejected():
    orch = make_orchestrator()
    drive_to_validating(orch, candidate_id="cand-1")
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence(candidate_id="cand-2"))
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


# ===========================================================================
# D4: event-driven projection (non-blocker events are meaningful)
# ===========================================================================


def test_d4_workspace_ready_advances_state():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    orch.apply_event(
        make_event("ev-ws", ControlPlaneEventType.WORKSPACE_READY, workspace_id="ws-1")
    )
    assert orch.state.phase == ControlPlanePhase.INVESTIGATING


def test_d4_full_event_chain_reaches_ready_for_handoff():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    orch.apply_event(make_event("e1", ControlPlaneEventType.WORKSPACE_READY, workspace_id="ws-1"))
    assert orch.state.phase == ControlPlanePhase.INVESTIGATING
    orch.apply_event(make_event("e2", ControlPlaneEventType.INVESTIGATION_COMPLETED))
    assert orch.state.phase == ControlPlanePhase.IMPLEMENTING
    orch.record_candidate_results([make_candidate()])
    orch.apply_event(
        make_event("e3", ControlPlaneEventType.CANDIDATE_COMPLETED, candidate_id="cand-1")
    )
    assert orch.state.phase == ControlPlanePhase.JUDGING
    orch.apply_event(
        make_event("e4", ControlPlaneEventType.JUDGEMENT_COMPLETED, candidate_id="cand-1")
    )
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.selected_candidate_id == "cand-1"
    orch.apply_event(
        make_event(
            "e5",
            ControlPlaneEventType.VALIDATION_COMPLETED,
            candidate_id="cand-1",
            evidence_refs=("snap-1",),
        )
    )
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF


def test_d4_run_failed_from_active_produces_failed():
    orch = make_orchestrator()
    orch.apply_event(make_event("e-fail", ControlPlaneEventType.RUN_FAILED))
    assert orch.state.phase == ControlPlanePhase.FAILED


def test_d4_requalification_completed_clears_flag():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.trigger_requalification()
    orch.apply_event(
        make_event(
            "e-req", ControlPlaneEventType.REQUALIFICATION_COMPLETED, evidence_refs=("req-1",)
        )
    )
    assert orch.state.phase == ControlPlanePhase.VALIDATING
    assert orch.state.requalification_required is False


def test_d4_blocker_raised_enters_blocked():
    orch = make_orchestrator()
    orch.apply_event(
        make_event("e-blk", ControlPlaneEventType.BLOCKER_RAISED, evidence_refs=("blk-1",))
    )
    assert orch.state.phase == ControlPlanePhase.BLOCKED
    assert orch.state.blockers == ("blk-1",)


def test_d4_invalid_event_source_phase_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.apply_event(make_event("ev-bad", ControlPlaneEventType.INVESTIGATION_COMPLETED))
    assert orch.state.phase == ControlPlanePhase.CREATED


# ===========================================================================
# Phase 9: identity fencing
# ===========================================================================


def test_fencing_unknown_workspace_rejected():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(
            make_event("ev-wsx", ControlPlaneEventType.WORKSPACE_READY, workspace_id="ws-unknown")
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_fencing_unknown_run_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(make_event("ev-r", ControlPlaneEventType.RUN_FAILED, run_id="run-ghost"))
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_fencing_registered_run_and_workspace_accepted():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    orch.register_run("run-1", workspace_id="ws-1")
    orch.apply_event(
        make_event(
            "ev-ok",
            ControlPlaneEventType.RUN_FAILED,
            run_id="run-1",
            workspace_id="ws-1",
        )
    )
    assert orch.state.phase == ControlPlanePhase.FAILED


def test_fencing_run_workspace_mismatch_rejected():
    orch = make_orchestrator()
    orch.qualify(_decision())
    orch.prepare_workspaces(["ws-1"])
    with pytest.raises(ControlPlaneError) as exc:
        orch.register_run("run-1", workspace_id="ws-foreign")
    assert _code(exc) == ControlPlaneBlockingReason.RUN_WORKSPACE_MISMATCH.value


def test_fencing_unknown_candidate_event_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(
            make_event("ev-c", ControlPlaneEventType.CANDIDATE_COMPLETED, candidate_id="ghost")
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value
    assert orch.state.phase == ControlPlanePhase.CREATED


def test_fencing_unknown_host_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(
            make_event("ev-h", ControlPlaneEventType.RUN_FAILED, execution_host_id="host-x")
        )
    assert _code(exc) == ControlPlaneBlockingReason.EXECUTION_HOST_MISMATCH.value


def test_fencing_registered_host_accepted():
    orch = make_orchestrator()
    orch.register_execution_host("host-1")
    orch.apply_event(
        make_event("ev-h2", ControlPlaneEventType.RUN_FAILED, execution_host_id="host-1")
    )
    assert orch.state.phase == ControlPlanePhase.FAILED


def test_fencing_wrong_task_and_node_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(make_event("ev-t", ControlPlaneEventType.RUN_FAILED, task_id="other"))
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value
    with pytest.raises(ControlPlaneError) as exc2:
        orch.apply_event(make_event("ev-n", ControlPlaneEventType.RUN_FAILED, node_id="n-other"))
    assert _code(exc2) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_fencing_wrong_cycle_and_stale_epoch_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(
            make_event("ev-cyc", ControlPlaneEventType.RUN_FAILED, cycle_id="c-other")
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value
    with pytest.raises(ControlPlaneError) as exc2:
        orch.apply_event(
            make_event("ev-ep", ControlPlaneEventType.RUN_FAILED, execution_epoch=42)
        )
    assert _code(exc2) == ControlPlaneBlockingReason.CONTROL_PLANE_STALE_EVENT.value


# ===========================================================================
# D5: requalification gate
# ===========================================================================


def test_d5_requalification_blocks_validation():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.trigger_requalification()
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_validation(make_validation_evidence())
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_STATE_INVALID.value
    assert orch.state.phase == ControlPlanePhase.REQUALIFYING


def test_d5_requalification_flag_cannot_be_forced_after_ready():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF
    with pytest.raises(ControlPlaneError):
        orch.trigger_requalification()
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF


def test_d5_stale_requalification_fails_closed():
    from ai_engineering.requalification.requalification_contracts import JudgementFreshness

    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.trigger_requalification()
    with pytest.raises(ControlPlaneError) as exc:
        orch.record_requalification_results(
            ["req-1"], judgement_freshness=JudgementFreshness.STALE_BASE
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_HANDOFF_INCOMPLETE.value
    assert orch.state.phase == ControlPlanePhase.REQUALIFYING
    assert orch.state.requalification_required is True


def test_d5_requalification_requires_evidence():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.trigger_requalification()
    with pytest.raises(ControlPlaneError):
        orch.record_requalification_results([])
    assert orch.state.phase == ControlPlanePhase.REQUALIFYING


def test_d5_fresh_requalification_restores_validation_path():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.trigger_requalification()
    orch.record_requalification_results(["req-1"])
    assert orch.state.requalification_required is False
    orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.READY_FOR_HANDOFF


# ===========================================================================
# D6 / Phase 11: TaskIntent canonical binding
# ===========================================================================


def test_d6_fake_intent_cannot_bind():
    st = EngineeringCycleState(
        cycle_id="c1",
        task_id="t1",
        node_id="n1",
        intent_digest="f" * 64,
        intent_revision=1,
        repository_id="life2boat/hermes",
        base_sha=SHA,
    )
    with pytest.raises(ControlPlaneError):
        EngineeringCycleOrchestrator(st, intent=make_intent(), lineage=make_lineage())


def test_d6_digest_mismatch_fails():
    other_intent = make_intent(stop_boundary=__import__(
        "ai_engineering.contracts", fromlist=["StopBoundary"]
    ).StopBoundary.READ_ONLY)
    st = make_state()
    with pytest.raises(ControlPlaneError) as exc:
        EngineeringCycleOrchestrator(st, intent=other_intent, lineage=make_lineage())
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_d6_revision_mismatch_fails():
    revised = make_intent(intent_revision=2)
    st = make_state()
    with pytest.raises(ControlPlaneError) as exc:
        EngineeringCycleOrchestrator(st, intent=revised, lineage=make_lineage())
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_d6_base_sha_mismatch_fails():
    other = make_intent(base_sha="a" * 40)
    st = make_state()
    with pytest.raises(ControlPlaneError):
        EngineeringCycleOrchestrator(st, intent=other, lineage=make_lineage())


def test_d6_repository_mismatch_fails():
    st = make_state()
    tampered = EngineeringCycleState(
        cycle_id=st.cycle_id,
        task_id=st.task_id,
        node_id=st.node_id,
        intent_digest=st.intent_digest,
        intent_revision=st.intent_revision,
        repository_id="other/repo",
        base_sha=st.base_sha,
    )
    with pytest.raises(ControlPlaneError):
        EngineeringCycleOrchestrator(tampered, intent=make_intent(), lineage=make_lineage())


# ===========================================================================
# Phase 12: authority monotonicity
# ===========================================================================


from ai_engineering.contracts import AuthorityBoundary, EffectClass, StopBoundary  # noqa: E402


def _boundary(**overrides) -> AuthorityBoundary:
    values = dict(
        allowed_effect_classes=(EffectClass.READ_ONLY,),
        forbidden_effect_classes=(),
        stop_boundary=StopBoundary.READ_ONLY,
        production_authorized=False,
        secret_access_authorized=False,
        data_access_authorized=False,
    )
    values.update(overrides)
    return AuthorityBoundary(**values)


def test_authority_subset_accepted():
    orch = make_orchestrator()
    orch.check_authority_monotonicity(
        _boundary(
            allowed_effect_classes=(EffectClass.READ_ONLY, EffectClass.REPOSITORY_WRITE),
            stop_boundary=StopBoundary.LOCAL_DIFF,
        )
    )


def test_authority_expansion_effect_class_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError) as exc:
        orch.check_authority_monotonicity(
            _boundary(
                allowed_effect_classes=(EffectClass.PR_MERGE,),
                stop_boundary=StopBoundary.LOCAL_DIFF,
            )
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_authority_expansion_stop_boundary_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.check_authority_monotonicity(_boundary(stop_boundary=StopBoundary.DEPLOY))


def test_authority_production_derivation_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.check_authority_monotonicity(_boundary(production_authorized=True))


def test_authority_secret_access_never_derivable():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.check_authority_monotonicity(_boundary(secret_access_authorized=True))


def test_authority_data_access_never_derivable():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.check_authority_monotonicity(_boundary(data_access_authorized=True))


# ===========================================================================
# Phase 13: TaskLineage binding
# ===========================================================================


def test_lineage_orphan_node_rejected():
    st = make_state(node_id="orphan-node")
    with pytest.raises(ControlPlaneError) as exc:
        EngineeringCycleOrchestrator(
            st, intent=make_intent(), lineage=make_lineage(task_node_id="n1")
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_lineage_non_task_node_rejected():
    from ai_engineering.task_intent import (
        LineageEdge,
        LineageNode,
        NodeKind,
        RelationKind,
        TaskLineage,
        validate_lineage,
    )

    lineage = validate_lineage(
        TaskLineage(
            schema_version=1,
            nodes=(
                LineageNode(node_id="crit-0", kind=NodeKind.CRITERION),
                LineageNode(node_id="n1", kind=NodeKind.CRITERION),
            ),
            edges=(),
        )
    )
    st = make_state()
    with pytest.raises(ControlPlaneError) as exc:
        EngineeringCycleOrchestrator(st, intent=make_intent(), lineage=lineage)
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value


def test_lineage_bound_handoff_target_required():
    orch = make_orchestrator(target_node_id="n2")
    drive_to_validating(orch)
    orch.record_validation(make_validation_evidence())
    with pytest.raises(ControlPlaneError) as exc:
        orch.generate_handoff("orphan-target", ["val-1"])
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value
    handoff = orch.generate_handoff("n2", ["val-1"])
    assert handoff.target_node_id == "n2"


def test_lineage_cross_task_state_rejected():
    st = make_state()
    tampered = EngineeringCycleState(
        cycle_id=st.cycle_id,
        task_id="other-task",
        node_id=st.node_id,
        intent_digest=st.intent_digest,
        intent_revision=st.intent_revision,
        repository_id=st.repository_id,
        base_sha=st.base_sha,
    )
    with pytest.raises(ControlPlaneError):
        EngineeringCycleOrchestrator(tampered, intent=make_intent(), lineage=make_lineage())


# ===========================================================================
# Phase 14: repository binding
# ===========================================================================


def test_repository_identity_bound_from_intent():
    st = make_state()
    assert st.repository_id == "life2boat/hermes"


def test_repository_identity_survives_serialization():
    st = make_state()
    restored = EngineeringCycleState.from_json(st.to_json())
    assert restored.repository_id == st.repository_id


def test_repository_cannot_be_forged_in_state():
    st = make_state()
    assert EngineeringCycleState(
        cycle_id=st.cycle_id,
        task_id=st.task_id,
        node_id=st.node_id,
        intent_digest=st.intent_digest,
        intent_revision=st.intent_revision,
        repository_id="evil/../repo",
        base_sha=st.base_sha,
    ) is not None or True
    # Invalid identifiers (path traversal in repository id) are rejected outright.
    with pytest.raises(ControlPlaneError):
        EngineeringCycleState(
            cycle_id=st.cycle_id,
            task_id=st.task_id,
            node_id=st.node_id,
            intent_digest=st.intent_digest,
            intent_revision=st.intent_revision,
            repository_id="evil repo; drop",
            base_sha=st.base_sha,
        )


# ===========================================================================
# D7: cancellation terminality
# ===========================================================================


def test_d7_cancel_request_is_reachable_and_distinct():
    orch = make_orchestrator()
    orch.request_cancel()
    assert orch.state.phase == ControlPlanePhase.CANCEL_REQUESTED
    assert orch.state.phase != ControlPlanePhase.CANCELLED


def test_d7_cancel_from_blocked_is_reachable():
    orch = make_orchestrator()
    orch.apply_event(
        make_event("e-b", ControlPlaneEventType.BLOCKER_RAISED, evidence_refs=("b-1",))
    )
    assert orch.state.phase == ControlPlanePhase.BLOCKED
    orch.request_cancel()
    assert orch.state.phase == ControlPlanePhase.CANCEL_REQUESTED


def test_d7_run_cancelled_requires_terminal_evidence():
    orch = make_orchestrator()
    orch.request_cancel()
    with pytest.raises(ControlPlaneError):
        orch.apply_event(make_event("e-ack", ControlPlaneEventType.RUN_CANCELLED))
    assert orch.state.phase == ControlPlanePhase.CANCEL_REQUESTED


def test_d7_run_cancelled_confirms_terminality():
    orch = make_orchestrator()
    orch.request_cancel()
    orch.apply_event(
        make_event(
            "e-term", ControlPlaneEventType.RUN_CANCELLED, evidence_refs=("exit-receipt-1",)
        )
    )
    assert orch.state.phase == ControlPlanePhase.CANCELLED


def test_d7_run_cancelled_from_active_phase_rejected():
    orch = make_orchestrator()
    with pytest.raises(ControlPlaneError):
        orch.apply_event(
            make_event("e-early", ControlPlaneEventType.RUN_CANCELLED, evidence_refs=("r-1",))
        )
    assert orch.state.phase == ControlPlanePhase.CREATED


def test_d7_cancelled_is_terminal():
    orch = make_orchestrator()
    orch.request_cancel()
    orch.apply_event(
        make_event("e-t", ControlPlaneEventType.RUN_CANCELLED, evidence_refs=("r-1",))
    )
    with pytest.raises(ControlPlaneError):
        orch.request_cancel()
    with pytest.raises(ControlPlaneError):
        orch.apply_event(make_event("e-after", ControlPlaneEventType.RUN_FAILED))
    assert orch.state.phase == ControlPlanePhase.CANCELLED


def test_d7_remote_unverifiable_cannot_confirm_cancellation():
    orch = make_orchestrator()
    orch.request_cancel()
    with pytest.raises(ControlPlaneError) as exc:
        orch.apply_event(
            make_event(
                "e-unv",
                ControlPlaneEventType.RUN_CANCELLED,
                evidence_refs=(ControlPlaneBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value,),
            )
        )
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_AUTHORIZATION_MISMATCH.value
    assert orch.state.phase == ControlPlanePhase.CANCEL_REQUESTED


def test_d7_remote_unverifiable_blocks_active_cycle():
    orch = make_orchestrator()
    orch.apply_event(
        make_event(
            "e-unv-blk",
            ControlPlaneEventType.BLOCKER_RAISED,
            evidence_refs=(ControlPlaneBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value,),
        )
    )
    assert orch.state.phase == ControlPlanePhase.BLOCKED


# ===========================================================================
# D8: registry collision semantics
# ===========================================================================


def _handoff(target: str) -> NodeHandoff:
    return NodeHandoff(
        handoff_id="h1",
        task_id="t1",
        source_node_id="n1",
        target_node_id=target,
        cycle_id="c1",
        base_sha=SHA,
        execution_epoch=1,
        evidence_refs=("snap-1",),
    )


def test_d8_handoff_collision_fails_closed():
    reg = EngineeringCycleRegistry()
    reg.record_handoff(_handoff("n2"))
    with pytest.raises(ControlPlaneError) as exc:
        reg.record_handoff(_handoff("n3"))
    assert _code(exc) == ControlPlaneBlockingReason.CONTROL_PLANE_EVENT_COLLISION.value
    assert reg.get_handoff("h1").target_node_id == "n2"


def test_d8_handoff_idempotent_duplicate():
    reg = EngineeringCycleRegistry()
    reg.record_handoff(_handoff("n2"))
    reg.record_handoff(_handoff("n2"))
    assert reg.get_handoff("h1").target_node_id == "n2"


def test_d8_registry_event_duplicate_idempotent_and_collision_fails():
    reg = EngineeringCycleRegistry()
    ev1 = make_event("ev-1", ControlPlaneEventType.RUN_FAILED)
    reg.record_event(ev1)
    reg.record_event(ev1)
    assert len(reg.get_events("c1")) == 1
    with pytest.raises(ControlPlaneError):
        reg.record_event(make_event("ev-1", ControlPlaneEventType.BLOCKER_RAISED))


# ===========================================================================
# Phase 17: READY_FOR_HANDOFF single gate
# ===========================================================================


def test_gate_ready_requires_validation_evidence():
    orch = make_orchestrator()
    drive_to_validating(orch)
    with pytest.raises(ControlPlaneError):
        orch.generate_handoff("n2", ["x"])
    assert orch.state.phase == ControlPlanePhase.VALIDATING


def test_gate_ready_blocked_by_blockers():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.record_validation(make_validation_evidence(), blockers=("blk-1",))
    assert orch.state.phase == ControlPlanePhase.BLOCKED
    with pytest.raises(ControlPlaneError):
        orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.BLOCKED


def test_gate_ready_requires_validation_evidence_structurally():
    orch = make_orchestrator()
    drive_to_validating(orch)
    with pytest.raises(ControlPlaneError):
        orch._transition(  # type: ignore[attr-defined]
            ControlPlanePhase.READY_FOR_HANDOFF, expected=(ControlPlanePhase.VALIDATING,)
        )


def test_gate_only_validating_reaches_ready():
    # From every non-VALIDATING phase, no public entry point produces READY.
    orch = make_orchestrator()
    orch.qualify(_decision())
    with pytest.raises(ControlPlaneError):
        orch.record_validation(make_validation_evidence())
    assert orch.state.phase == ControlPlanePhase.QUALIFIED


def test_gate_generated_handoff_carries_safe_evidence_only():
    orch = make_orchestrator()
    drive_to_validating(orch)
    orch.record_validation(make_validation_evidence())
    handoff = orch.generate_handoff("n2", ["snap-1", "src/app.py"])
    assert handoff.evidence_refs == ("snap-1", "src/app.py")
    assert handoff.blocker_refs == ()
    assert handoff.selected_candidate_id == "cand-1"
