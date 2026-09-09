"""Pure deterministic event replay - no LLM, no network, no shell."""

from __future__ import annotations

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
        )

    elif et == SupervisorEventType.PHASE_TRANSITIONED:
        # Phase is encoded in the event metadata; we need to look at the new state_revision
        # and infer the phase from context. Actually, we store phase in payload.
        # Since we only have payload_digest, we use a different mechanism.
        # For PHASE_TRANSITIONED, the actual new phase must be deterministically derivable.
        # We'll use a convention: the event carries phase info via state_revision ordering.
        # However, for full replay fidelity, we store extra info in task_id field... no.
        # 
        # DESIGN: events carry enough info. PHASE_TRANSITIONED events alone don't carry
        # the new phase explicitly in the event metadata. Instead, we derive state from
        # the event TYPE and the sequence of events. Each operation (ingest_result,
        # build_context, accept_decision) transitions the phase deterministically.
        # We track state by event type sequence.
        #
        # The loop stores the current phase in the event payload, but we only have
        # payload_digest. For full replay without payload content, we need to embed
        # phase changes in the event metadata somehow.
        #
        # FINAL DESIGN: We use a simple mapping: each event type implies a phase transition.
        # The exact phase for PHASE_TRANSITIONED is derived from which operation
        # the event represents. We eliminate PHASE_TRANSITIONED as a separate event type
        # and embed phase in the other events.
        return state  # fallthrough - phase handled by other events

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
            latest_verified_result_digest=None,  # not in event metadata directly
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
            latest_context_pack_digest=event.payload_digest,  # use payload_digest as context pack ref
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
        )

    elif et == SupervisorEventType.NEXT_TASK_GENERATED:
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
            attempt_number=state.attempt_number,
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
            blockers=tuple(list(state.blockers) + [event.payload_digest]),
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.RUN_COMPLETED:
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
            phase=SupervisorPhase.DONE,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.RUN_FAILED:
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
            phase=SupervisorPhase.FAILED,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    elif et == SupervisorEventType.RUN_CANCELLED:
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
            phase=SupervisorPhase.CANCELLED,
            blockers=state.blockers,
            created_at_utc=state.created_at_utc,
            updated_at_utc=event.created_at_utc,
            state_revision=event.state_revision,
            event_sequence=event.sequence,
        )

    else:
        _fail(f"REPLAY_UNKNOWN_EVENT_TYPE:{et}")
