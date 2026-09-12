"""Pure deterministic event replay - no LLM, no network, no shell."""

from __future__ import annotations

from dataclasses import replace

from ai_engineering.supervisor.events import (
    SupervisorEvent,
    SupervisorEventType,
    validate_event_chain,
)
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorPhase,
    SupervisorState,
    _fail,
)

REPLAY_EVENTS_MAX = 10_000


def replay_events(events: list[SupervisorEvent]) -> SupervisorState:
    """Pure deterministic reconstruction of supervisor state from events.

    No LLM, no network, no shell. Same events => same state.
    """
    if len(events) > REPLAY_EVENTS_MAX:
        _fail("REPLAY_EVENTS_EXCEEDED")

    validate_event_chain(events)

    if not events:
        _fail("REPLAY_NO_EVENTS")

    # Build state from the first event (RUN_INITIALIZED)
    first = events[0]
    if first.event_type != SupervisorEventType.RUN_INITIALIZED:
        _fail("REPLAY_FIRST_EVENT_NOT_INITIALIZED")

    # Extract initial state from RUN_INITIALIZED event payload is stored in
    # fields on the event itself, but we need to reconstruct from the embedded
    # payload. The payload_digest alone isn't enough. We rely on the event
    # fields + the attached payload stored on the event. Since events only
    # store payload_digest (not payload content), we reconstruct state by
    # applying each event's metadata to a running state object.
    #
    # For RUN_INITIALIZED, the initial state fields are carried in the event metadata.
    # We require a special "init_payload" approach: store initial fields in the first event.
    # Since we only have payload_digest, not payload content, state reconstruction
    # must be done from event metadata only.
    #
    # We use a convention: RUN_INITIALIZED events carry init metadata in task_id,
    # attempt_id, intent_digest, and the run_id. The root_goal, repository, etc.
    # are NOT stored in the event itself - they need to be passed in or stored separately.
    #
    # DESIGN DECISION: The store keeps a state snapshot alongside the event journal.
    # replay_events() is called AFTER loading events, and the initial state seed
    # is reconstructed from the RUN_INITIALIZED event + a state seed file.
    #
    # For simplicity in replay, the FileSupervisorStateStore also saves the initial
    # state as a seed, and replay_events reads it. However, replay_events() as a pure
    # function only gets events. So we use a different approach:
    #
    # The events carry all state-changing deltas. The initial state is reconstructed
    # from the RUN_INITIALIZED event's fields (task_id, attempt_id, intent_digest).
    # Fields like root_goal, repository etc. are embedded in the payload of
    # RUN_INITIALIZED. Since payload content is NOT stored in events (only digest),
    # we require the store to also persist a "seed state" which is the state after
    # applying the first event.
    #
    # PRACTICAL APPROACH: replay_events() takes events and reconstructs state
    # by applying each event's known delta to a running state. The initial state
    # must be bootstrapped from the first event. We embed essential fields in
    # a special way: the store will call replay_events with an initial_state seed
    # derived from the persisted seed file.
    #
    # For the pure replay function, we accept that some fields (root_goal, repository,
    # canonical_remote, canonical_main_ref, root_goal_id) come from a seed state that
    # is stored separately by the FileSupervisorStateStore.
    #
    # This function is called with a seed_state to bootstrap the replay.
    raise NotImplementedError("Use replay_events_with_seed instead")


def replay_events_with_seed(
    events: list[SupervisorEvent],
    seed_state: SupervisorState,
) -> SupervisorState:
    """Replay events onto a seed state to reconstruct current state.

    Pure and deterministic. No LLM, no network, no shell.
    seed_state is the state BEFORE any events (e.g., zero-revision state
    or the state at the point of the last snapshot).
    """
    if len(events) > REPLAY_EVENTS_MAX:
        _fail("REPLAY_EVENTS_EXCEEDED")

    validate_event_chain(events)

    state = seed_state

    for event in events:
        state = _apply_event(state, event)

    return state


def _bump_budget(b: Any, decisions: int = 0, child_tasks: int = 0, retries: int = 0, fix_cycles: int = 0, denials: int = 0) -> Any:
    if b is None:
        return None
    try:
        from dataclasses import replace
        from ai_engineering.supervisor.policy.contracts import compute_deterministic_digest
        new_b = replace(
            b,
            decisions_used=b.decisions_used + decisions,
            child_tasks_used=b.child_tasks_used + child_tasks,
            retries_used=b.retries_used + retries,
            fix_cycles_used=b.fix_cycles_used + fix_cycles,
            policy_denials=b.policy_denials + denials,
        )
        payload = {
            "budget_id": new_b.budget_id,
            "child_tasks_used": new_b.child_tasks_used,
            "consecutive_failures": new_b.consecutive_failures,
            "decisions_used": new_b.decisions_used,
            "exhausted_dimensions": list(new_b.exhausted_dimensions),
            "fix_cycles_used": new_b.fix_cycles_used,
            "policy_denials": new_b.policy_denials,
            "provider_calls_used": new_b.provider_calls_used,
            "retries_used": new_b.retries_used,
            "schema_version": new_b.schema_version,
        }
        b_dg = compute_deterministic_digest(payload)
        return replace(new_b, budget_digest=b_dg)
    except Exception:
        return b


def _apply_event(state: SupervisorState, event: SupervisorEvent) -> SupervisorState:
    """Apply a single event to the state, returning the new state."""
    et = event.event_type

    if et == SupervisorEventType.RUN_INITIALIZED:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=None,
            latest_verified_result_digest=None,
            latest_context_pack_digest=None,
            latest_decision_id=None,
            latest_decision_digest=None,
            engineering_cycle_id=None,
            engineering_cycle_phase=None,
            phase=SupervisorPhase.RUNNING,
            blockers=(),
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=state.budget_state,
        )

    elif et == SupervisorEventType.WORK_PROFILE_BOUND:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=state.phase,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=state.budget_state,
        )

    elif et == SupervisorEventType.POLICY_EVALUATED:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=event.decision_id or state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=state.phase,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=state.budget_state,
        )

    elif et == SupervisorEventType.AUTONOMY_LEVEL_CHANGED:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=state.phase,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=state.budget_state,
        )

    elif et == SupervisorEventType.PHASE_TRANSITIONED:
        return state

    elif et == SupervisorEventType.RESULT_INGESTED:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=event.verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=SupervisorPhase.VERIFYING,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=state.budget_state,
        )

    elif et == SupervisorEventType.CONTEXT_PACK_BUILT:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=event.payload_digest,
            latest_decision_id=state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=SupervisorPhase.DECIDING,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=state.budget_state,
        )

    elif et == SupervisorEventType.DECISION_ACCEPTED:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=event.verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=event.decision_id,
            latest_decision_digest=None,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=SupervisorPhase.READY_FOR_NEXT_TASK,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=_bump_budget(state.budget_state, decisions=1),
        )

    elif et == SupervisorEventType.NEXT_TASK_GENERATED:
        is_fix = "-fix-" in event.task_id
        is_retry = (event.task_id == state.current_task_id and event.attempt_id != state.current_attempt_id)
        new_attempt = state.attempt_number + 1 if is_retry else state.attempt_number
        c_inc = 1 if not is_retry else 0
        r_inc = 1 if is_retry else 0
        f_inc = 1 if is_fix else 0
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision + 1,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=new_attempt,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=SupervisorPhase.RUNNING,
            blockers=(),
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=_bump_budget(
                state.budget_state,
                child_tasks=c_inc,
                retries=r_inc,
                fix_cycles=f_inc,
            ),
        )

    elif et == SupervisorEventType.ATTEMPT_INCREMENTED:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number + 1,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=SupervisorPhase.RUNNING,
            blockers=(),
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=_bump_budget(state.budget_state, retries=1),
        )

    elif et == SupervisorEventType.BLOCKER_RECORDED:
        return SupervisorState(
            schema_version=state.schema_version,
            run_id=state.run_id,
            root_goal_id=state.root_goal_id,
            root_goal=state.root_goal,
            repository=state.repository,
            canonical_remote=state.canonical_remote,
            canonical_main_ref=state.canonical_main_ref,
            current_task_id=event.task_id,
            current_intent_digest=event.intent_digest,
            current_intent_revision=state.current_intent_revision,
            current_base_sha=state.current_base_sha,
            current_attempt_id=event.attempt_id,
            attempt_number=state.attempt_number,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=state.latest_decision_id,
            latest_decision_digest=state.latest_decision_digest,
            engineering_cycle_id=state.engineering_cycle_id,
            engineering_cycle_phase=state.engineering_cycle_phase,
            phase=SupervisorPhase.BLOCKED,
            blockers=tuple(list(state.blockers) + [f"POLICY_DENIED: {event.payload_digest}"]),
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
            autonomy_state=state.autonomy_state,
            budget_state=_bump_budget(state.budget_state, denials=1),
        )

    elif et == SupervisorEventType.RUN_COMPLETED:
        return replace(
            state,
            phase=SupervisorPhase.DONE,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.RUN_FAILED:
        return replace(
            state,
            phase=SupervisorPhase.FAILED,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.RUN_CANCELLED:
        return replace(
            state,
            phase=SupervisorPhase.CANCELLED,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.DISPATCH_SENT:
        from ai_engineering.supervisor.dispatch.contracts import compute_dispatch_id
        dispatch_id = event.decision_id or compute_dispatch_id(
            event.run_id, event.task_id, event.attempt_id, event.intent_digest
        )
        return replace(
            state,
            active_dispatch_id=dispatch_id,
            active_dispatch_digest=event.payload_digest,
            phase=SupervisorPhase.RUNNING,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.WORKER_RESULT_RECEIVED:
        return replace(
            state,
            latest_verified_result_id=event.verified_result_id or state.latest_verified_result_id,
            phase=SupervisorPhase.VERIFYING,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.PR_BOUND:
        pr_num = state.pr_number
        if event.decision_id:
            try:
                pr_num = int(event.decision_id.removeprefix("pr-"))
            except ValueError:
                pass
        pr_sha = event.verified_result_id or state.pr_head_sha
        return replace(
            state,
            pr_number=pr_num,
            pr_head_sha=pr_sha,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.CI_PASSED:
        return replace(
            state,
            ci_state="PASS",
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.CI_FAILED:
        return replace(
            state,
            ci_state="FAIL",
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.SOURCE_RECONCILED:
        new_base = event.verified_result_id or state.current_base_sha
        return replace(
            state,
            current_base_sha=new_base,
            active_dispatch_id=None,
            active_dispatch_digest=None,
            phase=SupervisorPhase.DONE,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.ASTRA_PROPOSAL_CREATED:
        return replace(
            state,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )
    else:
        _fail(f"REPLAY_UNKNOWN_EVENT_TYPE:{et}")
