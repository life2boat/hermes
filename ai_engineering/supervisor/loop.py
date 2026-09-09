"""SupervisorLoop - orchestrates the supervisor state machine."""

from __future__ import annotations

import hashlib

from ai_engineering.supervisor.context_pack import (
    ContextPack,
    build_context_pack,
    context_pack_digest,
)
from ai_engineering.supervisor.decision import (
    AstraDecision,
    DecisionReceipt,
    DecisionValidator,
    decision_content_digest,
)
from ai_engineering.supervisor.events import (
    SupervisorEvent,
    SupervisorEventType,
    create_event,
)
from ai_engineering.supervisor.replay import replay_events_with_seed
from ai_engineering.supervisor.state import (
    SupervisorError,
    SupervisorPhase,
    SupervisorState,
    _fail,
    canonical_serialize_state,
    state_digest,
)
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.validator import VerifiedResult, canonical_serialize_verified_result
from ai_engineering.task_intent import TaskIntent, TaskLineage, intent_digest


class SupervisorLoop:
    """Orchestrates supervisor lifecycle: initialize -> ingest -> decide -> next."""

    def __init__(self, store: FileSupervisorStateStore) -> None:
        self._store = store
        self._validator = DecisionValidator()
        # Cache of verified results by result_id for digest computation
        self._vr_cache: dict[str, VerifiedResult] = {}

    def initialize_run(
        self,
        intent: TaskIntent,
        lineage: TaskLineage,
        root_goal: str,
        root_goal_id: str,
        created_at_utc: str, attempt_id: str | None = None,
    ) -> SupervisorState:
        """Initialize a new supervisor run."""
        from ai_engineering.supervisor.state import (
            create_initial_state,
            SUPERVISOR_STATE_SCHEMA_VERSION,
        )

        run_id = root_goal_id
        idg = intent_digest(intent)
        attempt_id = attempt_id or f"{intent.task_id}-attempt-1"

        # Create zero-revision seed state (before events)
        seed = create_initial_state(
            run_id=run_id,
            root_goal_id=root_goal_id,
            root_goal=root_goal,
            repository=intent.source_repository,
            canonical_remote=intent.source_repository,
            canonical_main_ref=intent.source_main_ref,
            task_id=intent.task_id,
            intent_digest_val=idg,
            intent_revision=intent.intent_revision,
            base_sha=intent.source_base_sha,
            attempt_id=attempt_id,
            created_at_utc=created_at_utc,
        )

        # Save seed state for replay bootstrapping
        self._store.save_seed_state(seed)

        # Create RUN_INITIALIZED event
        event = create_event(
            run_id=run_id,
            sequence=1,
            previous_event_digest=None,
            event_type=SupervisorEventType.RUN_INITIALIZED,
            state_revision=1,
            task_id=intent.task_id,
            attempt_id=attempt_id,
            intent_digest=idg,
            payload={"root_goal": root_goal, "root_goal_id": root_goal_id},
            created_at_utc=created_at_utc,
        )
        self._store.save_event(event)

        # Replay to get actual state
        return replay_events_with_seed([event], seed)

    def ingest_verified_result(
        self,
        run_id: str,
        verified_result: VerifiedResult,
        verified_result_digest: str,
    ) -> SupervisorState:
        """Ingest a verified result and transition to VERIFYING phase."""
        state = self._store.load_state(run_id)
        events = self._store.load_events(run_id)

        # Validate result binding
        if verified_result.task_id != state.current_task_id:
            _fail("RESULT_TASK_MISMATCH")
        if verified_result.attempt_id != state.current_attempt_id:
            _fail("RESULT_ATTEMPT_MISMATCH")
        if verified_result.intent_digest != state.current_intent_digest:
            _fail("RESULT_INTENT_MISMATCH")

        # Cache for later
        self._vr_cache[verified_result.result_id] = verified_result
        # Store digest mapping
        self._vr_cache[f"digest:{verified_result.result_id}"] = verified_result_digest

        new_seq = len(events) + 1
        prev_digest = events[-1].event_digest if events else None

        event = create_event(
            run_id=run_id,
            sequence=new_seq,
            previous_event_digest=prev_digest,
            event_type=SupervisorEventType.RESULT_INGESTED,
            state_revision=state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={
                "result_id": verified_result.result_id,
                "result_digest": verified_result_digest,
                "status": verified_result.status.value,
            },
            created_at_utc=verified_result.verified_at_utc,
            verified_result_id=verified_result.result_id,
        )
        self._store.save_event(event)

        seed = self._store.load_seed_state(run_id)
        all_events = self._store.load_events(run_id)
        new_state = replay_events_with_seed(all_events, seed)

        # Patch latest_verified_result_digest since replay can't derive it
        # We store it in the cache and patch the state post-replay
        new_state = SupervisorState(
            schema_version=new_state.schema_version,
            run_id=new_state.run_id,
            root_goal_id=new_state.root_goal_id,
            root_goal=new_state.root_goal,
            repository=new_state.repository,
            canonical_remote=new_state.canonical_remote,
            canonical_main_ref=new_state.canonical_main_ref,
            current_task_id=new_state.current_task_id,
            current_intent_digest=new_state.current_intent_digest,
            current_intent_revision=new_state.current_intent_revision,
            current_base_sha=new_state.current_base_sha,
            current_attempt_id=new_state.current_attempt_id,
            attempt_number=new_state.attempt_number,
            latest_verified_result_id=verified_result.result_id,
            latest_verified_result_digest=verified_result_digest,
            latest_context_pack_digest=new_state.latest_context_pack_digest,
            latest_decision_id=new_state.latest_decision_id,
            latest_decision_digest=new_state.latest_decision_digest,
            engineering_cycle_id=new_state.engineering_cycle_id,
            engineering_cycle_phase=new_state.engineering_cycle_phase,
            phase=SupervisorPhase.VERIFYING,
            blockers=new_state.blockers,
            created_at_utc=new_state.created_at_utc,
            updated_at_utc=new_state.updated_at_utc,
            state_revision=new_state.state_revision,
            event_sequence=new_state.event_sequence,
        )
        return new_state

    def build_context_pack(
        self,
        run_id: str,
        intent: TaskIntent,
    ) -> tuple[ContextPack, SupervisorState]:
        """Build a ContextPack for decision making."""
        state = self._store.load_state(run_id)
        events = self._store.load_events(run_id)

        # Get verified result if available
        vr: VerifiedResult | None = None
        if state.latest_verified_result_id:
            vr = self._vr_cache.get(state.latest_verified_result_id)

        pack = build_context_pack(state, intent, vr)
        pack_digest = context_pack_digest(pack)

        new_seq = len(events) + 1
        prev_digest = events[-1].event_digest if events else None

        event = create_event(
            run_id=run_id,
            sequence=new_seq,
            previous_event_digest=prev_digest,
            event_type=SupervisorEventType.CONTEXT_PACK_BUILT,
            state_revision=state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={"pack_digest": pack_digest},
            created_at_utc=state.updated_at_utc,
        )
        self._store.save_event(event)

        seed = self._store.load_seed_state(run_id)
        all_events = self._store.load_events(run_id)
        new_state = replay_events_with_seed(all_events, seed)

        # Patch context pack digest and verified result digest
        new_state = SupervisorState(
            schema_version=new_state.schema_version,
            run_id=new_state.run_id,
            root_goal_id=new_state.root_goal_id,
            root_goal=new_state.root_goal,
            repository=new_state.repository,
            canonical_remote=new_state.canonical_remote,
            canonical_main_ref=new_state.canonical_main_ref,
            current_task_id=new_state.current_task_id,
            current_intent_digest=new_state.current_intent_digest,
            current_intent_revision=new_state.current_intent_revision,
            current_base_sha=new_state.current_base_sha,
            current_attempt_id=new_state.current_attempt_id,
            attempt_number=new_state.attempt_number,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=state.latest_verified_result_digest,
            latest_context_pack_digest=pack_digest,
            latest_decision_id=new_state.latest_decision_id,
            latest_decision_digest=new_state.latest_decision_digest,
            engineering_cycle_id=new_state.engineering_cycle_id,
            engineering_cycle_phase=new_state.engineering_cycle_phase,
            phase=SupervisorPhase.DECIDING,
            blockers=new_state.blockers,
            created_at_utc=new_state.created_at_utc,
            updated_at_utc=new_state.updated_at_utc,
            state_revision=new_state.state_revision,
            event_sequence=new_state.event_sequence,
        )
        return pack, new_state

    def accept_decision(
        self,
        run_id: str,
        decision: AstraDecision,
        context_pack_digest: str,
        verified_result_status: str,
        validated_at_utc: str,
    ) -> tuple[DecisionReceipt, SupervisorState]:
        """Validate and accept a decision."""
        state = self._store.load_state(run_id)
        events = self._store.load_events(run_id)

        vr_digest = state.latest_verified_result_digest or ""
        if not vr_digest and state.latest_verified_result_id:
            vr_digest = self._vr_cache.get(f"digest:{state.latest_verified_result_id}", "")

        receipt = self._validator.validate(
            decision=decision,
            state=state,
            verified_result_digest=vr_digest,
            context_pack_digest=context_pack_digest,
            verified_result_status=verified_result_status,
        )

        decision_dg = decision_content_digest(decision)

        new_seq = len(events) + 1
        prev_digest = events[-1].event_digest if events else None

        event = create_event(
            run_id=run_id,
            sequence=new_seq,
            previous_event_digest=prev_digest,
            event_type=SupervisorEventType.DECISION_ACCEPTED,
            state_revision=state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={
                "decision_id": decision.decision_id,
                "decision_digest": decision_dg,
                "action": decision.action.value,
            },
            created_at_utc=validated_at_utc,
            verified_result_id=state.latest_verified_result_id,
            decision_id=decision.decision_id,
        )
        self._store.save_event(event)

        seed = self._store.load_seed_state(run_id)
        all_events = self._store.load_events(run_id)
        new_state = replay_events_with_seed(all_events, seed)

        # Patch decision and verified result digests
        new_state = SupervisorState(
            schema_version=new_state.schema_version,
            run_id=new_state.run_id,
            root_goal_id=new_state.root_goal_id,
            root_goal=new_state.root_goal,
            repository=new_state.repository,
            canonical_remote=new_state.canonical_remote,
            canonical_main_ref=new_state.canonical_main_ref,
            current_task_id=new_state.current_task_id,
            current_intent_digest=new_state.current_intent_digest,
            current_intent_revision=new_state.current_intent_revision,
            current_base_sha=new_state.current_base_sha,
            current_attempt_id=new_state.current_attempt_id,
            attempt_number=new_state.attempt_number,
            latest_verified_result_id=state.latest_verified_result_id,
            latest_verified_result_digest=vr_digest if vr_digest else None,
            latest_context_pack_digest=state.latest_context_pack_digest,
            latest_decision_id=decision.decision_id,
            latest_decision_digest=decision_dg,
            engineering_cycle_id=new_state.engineering_cycle_id,
            engineering_cycle_phase=new_state.engineering_cycle_phase,
            phase=SupervisorPhase.READY_FOR_NEXT_TASK,
            blockers=new_state.blockers,
            created_at_utc=new_state.created_at_utc,
            updated_at_utc=validated_at_utc,
            state_revision=new_state.state_revision,
            event_sequence=new_state.event_sequence,
        )
        return receipt, new_state

    def generate_next_task(
        self,
        run_id: str,
        parent_intent: TaskIntent,
        decision: AstraDecision,
        receipt: DecisionReceipt,
        new_base_sha: str,
        new_task_id: str,
        new_attempt_id: str,
        created_at_utc: str, attempt_id: str | None = None,
    ) -> tuple[TaskIntent, TaskLineage, SupervisorState]:
        """Generate the next task intent based on the decision."""
        from ai_engineering.supervisor.next_task import NextTaskGenerator
        state = self._store.load_state(run_id)
        events = self._store.load_events(run_id)

        generator = NextTaskGenerator()
        vr = self._vr_cache.get(state.latest_verified_result_id or "")

        if decision.action.value == "CONTINUE":
            child_intent, lineage = generator.generate_continue(
                parent_intent=parent_intent,
                decision=decision,
                receipt=receipt,
                new_task_id=new_task_id,
                new_base_sha=new_base_sha,
                created_at_utc=created_at_utc,
            )
        elif decision.action.value == "FIX":
            failing_gates = [
                gr.gate_name for gr in (vr.gate_results if vr else ())
                if gr.status.value == "FAIL"
            ]
            child_intent, lineage = generator.generate_fix(
                parent_intent=parent_intent,
                decision=decision,
                receipt=receipt,
                new_task_id=new_task_id,
                failing_gates=failing_gates,
                created_at_utc=created_at_utc,
            )
        elif decision.action.value == "RETRY":
            child_intent, lineage = generator.generate_retry(
                parent_intent=parent_intent,
                decision=decision,
                receipt=receipt,
                new_attempt_id=new_attempt_id,
                created_at_utc=created_at_utc,
            )
        else:
            _fail(f"UNSUPPORTED_ACTION:{decision.action.value}")

        child_idg = intent_digest(child_intent)
        new_seq = len(events) + 1
        prev_digest = events[-1].event_digest if events else None

        event = create_event(
            run_id=run_id,
            sequence=new_seq,
            previous_event_digest=prev_digest,
            event_type=SupervisorEventType.NEXT_TASK_GENERATED,
            state_revision=state.state_revision + 1,
            task_id=child_intent.task_id,
            attempt_id=new_attempt_id,
            intent_digest=child_idg,
            payload={"action": decision.action.value, "new_task_id": new_task_id},
            created_at_utc=created_at_utc,
        )
        self._store.save_event(event)

        seed = self._store.load_seed_state(run_id)
        all_events = self._store.load_events(run_id)
        new_state = replay_events_with_seed(all_events, seed)

        return child_intent, lineage, new_state

    def replay(self, run_id: str) -> SupervisorState:
        """Replay events to reconstruct state. No LLM, no network, no shell."""
        return self._store.load_state(run_id)
