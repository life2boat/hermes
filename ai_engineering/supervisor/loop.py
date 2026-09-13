"""SupervisorLoop - orchestrates the supervisor state machine."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

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
from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus
from ai_engineering.supervisor.policy.contracts import (
    AutonomyBudgetState,
    AutonomyLevel,
    AutonomyState,
    ExecutionTarget,
    PolicyReceipt,
    PolicyRequest,
    PolicyVerdict,
    WorkProfile,
    AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
    AUTONOMY_STATE_SCHEMA_VERSION,
    POLICY_REQUEST_SCHEMA_VERSION,
    compute_deterministic_digest,
)
from ai_engineering.supervisor.policy.work_profile import validate_work_profile
from ai_engineering.supervisor.policy.engine import evaluate_policy
from ai_engineering.supervisor.policy.budget import evaluate_budget_consumption
from ai_engineering.supervisor.policy.autonomy import evaluate_promotion, evaluate_demotion


def _compute_budget_digest(b: AutonomyBudgetState) -> str:
    payload = {
        "budget_id": b.budget_id,
        "child_tasks_used": b.child_tasks_used,
        "consecutive_failures": b.consecutive_failures,
        "decisions_used": b.decisions_used,
        "exhausted_dimensions": list(b.exhausted_dimensions),
        "fix_cycles_used": b.fix_cycles_used,
        "policy_denials": b.policy_denials,
        "provider_calls_used": b.provider_calls_used,
        "retries_used": b.retries_used,
        "schema_version": b.schema_version,
    }
    return compute_deterministic_digest(payload)


def _compute_autonomy_state_digest(a: AutonomyState) -> str:
    payload = {
        "budget_state_digest": a.budget_state_digest,
        "created_at_utc": a.created_at_utc,
        "critical_failures": a.critical_failures,
        "current_level": a.current_level.value if hasattr(a.current_level, "value") else str(a.current_level),
        "last_transition_receipt_id": a.last_transition_receipt_id,
        "maximum_allowed_level": a.maximum_allowed_level.value if hasattr(a.maximum_allowed_level, "value") else str(a.maximum_allowed_level),
        "profile_digest": a.profile_digest,
        "profile_id": a.profile_id,
        "promotion_sequence": a.promotion_sequence,
        "required_validators_status": dict(a.required_validators_status),
        "rollback_verified": a.rollback_verified,
        "run_id": a.run_id,
        "schema_version": a.schema_version,
        "successful_runs": a.successful_runs,
        "updated_at_utc": a.updated_at_utc,
    }
    return compute_deterministic_digest(payload)


class SupervisorLoop:
    """Orchestrates supervisor lifecycle: initialize -> ingest -> decide -> next."""

    def __init__(self, store: FileSupervisorStateStore) -> None:
        self._store = store
        self._validator = DecisionValidator()
        # Cache of verified results by result_id for digest computation
        self._vr_cache: dict[str, VerifiedResult] = {}
        self._profiles: dict[str, WorkProfile] = {}
        self._intents: dict[str, TaskIntent] = {}
        self._effective_policies: dict[str, EffectivePolicyReport] = {}
        self._latest_policy_receipt: dict[str, Any] = {}

    def get_latest_policy_receipt(self, run_id: str) -> Any | None:
        if run_id in self._latest_policy_receipt:
            return self._latest_policy_receipt[run_id]
        events = self._store.load_events(run_id)
        from ai_engineering.supervisor.policy.contracts import PolicyReceipt, PolicyVerdict
        for e in reversed(events):
            if getattr(e.event_type, "value", str(e.event_type)) == "POLICY_EVALUATED":
                if e.payload and e.payload.get("policy_receipt"):
                    pr_data = dict(e.payload["policy_receipt"])
                    if "verdict" in pr_data and isinstance(pr_data["verdict"], str):
                        pr_data["verdict"] = PolicyVerdict(pr_data["verdict"])
                    return PolicyReceipt(**pr_data)
        return None

    def initialize_run(
        self,
        intent: TaskIntent,
        lineage: TaskLineage,
        root_goal: str,
        root_goal_id: str,
        created_at_utc: str,
        attempt_id: str | None = None,
    ) -> SupervisorState:
        """Initialize a new supervisor run."""
        from ai_engineering.supervisor.state import (
            create_initial_state,
            SUPERVISOR_STATE_SCHEMA_VERSION,
        )

        run_id = root_goal_id
        idg = intent_digest(intent)
        attempt_id = attempt_id or f"{intent.task_id}-attempt-1"

        self._intents[intent.task_id] = intent
        self._intents[run_id] = intent

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
            payload={"root_goal": root_goal, "root_goal_id": root_goal_id, "task_intent": __import__("dataclasses").asdict(intent)},
            created_at_utc=created_at_utc,
        )
        self._store.save_event(event)

        # Replay to get actual state
        return replay_events_with_seed([event], seed)

    def bind_profile(
        self,
        run_id: str,
        profile: WorkProfile | dict,
        initial_level: AutonomyLevel | EffectivePolicyReport | None = None,
        created_at_utc: str | None = None,
        effective_policy: EffectivePolicyReport | None = None,
        policy: EffectivePolicyReport | None = None,
    ) -> SupervisorState:
        """Bind a work profile and initialize autonomy and budget state."""
        if isinstance(initial_level, EffectivePolicyReport):
            effective_policy = initial_level
            initial_level = None
        if policy is not None:
            effective_policy = policy

        if isinstance(profile, dict):
            work_profile = validate_work_profile(profile)
        elif isinstance(profile, WorkProfile):
            work_profile = profile
        else:
            _fail("WORK_PROFILE_INVALID")

        state = self._store.load_state(run_id)
        events = self._store.load_events(run_id)
        ts = created_at_utc or state.updated_at_utc

        level = initial_level or work_profile.maximum_autonomy_level or AutonomyLevel.LEVEL_0_OBSERVE

        # 1. Initialize budget state
        budget_id = f"budget-{run_id}"
        b_payload = {
            "budget_id": budget_id,
            "child_tasks_used": 0,
            "consecutive_failures": 0,
            "decisions_used": 0,
            "exhausted_dimensions": [],
            "fix_cycles_used": 0,
            "policy_denials": 0,
            "provider_calls_used": 0,
            "retries_used": 0,
            "schema_version": AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
        }
        b_digest = compute_deterministic_digest(b_payload)
        budget_state = AutonomyBudgetState(
            schema_version=AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
            budget_id=budget_id,
            budget_digest=b_digest,
            decisions_used=0,
            child_tasks_used=0,
            retries_used=0,
            fix_cycles_used=0,
            consecutive_failures=0,
            provider_calls_used=0,
            policy_denials=0,
            exhausted_dimensions=(),
        )

        # 2. Initialize autonomy state
        a_payload = {
            "budget_state_digest": b_digest,
            "created_at_utc": ts,
            "critical_failures": 0,
            "current_level": level.value,
            "last_transition_receipt_id": None,
            "maximum_allowed_level": work_profile.maximum_autonomy_level.value,
            "profile_digest": work_profile.profile_digest,
            "profile_id": work_profile.profile_id,
            "promotion_sequence": 0,
            "required_validators_status": {},
            "rollback_verified": False,
            "run_id": run_id,
            "schema_version": AUTONOMY_STATE_SCHEMA_VERSION,
            "successful_runs": 0,
            "updated_at_utc": ts,
        }
        a_digest = compute_deterministic_digest(a_payload)
        autonomy_state = AutonomyState(
            schema_version=AUTONOMY_STATE_SCHEMA_VERSION,
            run_id=run_id,
            profile_id=work_profile.profile_id,
            profile_digest=work_profile.profile_digest,
            current_level=level,
            maximum_allowed_level=work_profile.maximum_autonomy_level,
            successful_runs=0,
            critical_failures=0,
            rollback_verified=False,
            required_validators_status={},
            budget_state_digest=b_digest,
            promotion_sequence=0,
            last_transition_receipt_id=None,
            created_at_utc=ts,
            updated_at_utc=ts,
            state_digest=a_digest,
        )

        # Cache profile & effective policy
        self._profiles[run_id] = work_profile
        if effective_policy is not None:
            self._effective_policies[run_id] = effective_policy

        # Create WORK_PROFILE_BOUND event
        new_seq = len(events) + 1
        prev_digest = events[-1].event_digest if events else None

        event = create_event(
            run_id=run_id,
            sequence=new_seq,
            previous_event_digest=prev_digest,
            event_type=SupervisorEventType.WORK_PROFILE_BOUND,
            state_revision=state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={
                "profile_id": work_profile.profile_id,
                "profile_digest": work_profile.profile_digest,
                "initial_level": level.value,
                "budget_digest": b_digest,
                "autonomy_state_digest": a_digest,
                "work_profile": __import__('dataclasses').asdict(work_profile),
                "effective_policy": __import__('dataclasses').asdict(effective_policy) if effective_policy else None,
            },
            created_at_utc=ts,
        )
        self._store.save_event(event)

        # Update seed state so that replay starts with autonomy & budget state
        seed = self._store.load_seed_state(run_id)
        updated_seed = replace(seed, autonomy_state=autonomy_state, budget_state=budget_state)
        self._store.save_seed_state(updated_seed)

        all_events = self._store.load_events(run_id)
        new_state = replay_events_with_seed(all_events, updated_seed)
        return new_state

    def promote_level(
        self,
        run_id: str,
        operator_policy_allows: bool = True,
        updated_at_utc: str | None = None,
    ) -> SupervisorState:
        """Evaluate promotion and update autonomy level if conditions met."""
        state = self._store.load_state(run_id)
        if state.autonomy_state is None:
            _fail("NO_WORK_PROFILE_BOUND")
        profile = self._profiles.get(run_id)
        if profile is None:
            _fail("WORK_PROFILE_NOT_FOUND")

        new_level = evaluate_promotion(state.autonomy_state, profile, operator_policy_allows)
        if new_level == state.autonomy_state.current_level:
            return state

        ts = updated_at_utc or state.updated_at_utc
        new_autonomy = replace(
            state.autonomy_state,
            current_level=new_level,
            promotion_sequence=state.autonomy_state.promotion_sequence + 1,
            updated_at_utc=ts,
        )
        new_a_digest = _compute_autonomy_state_digest(new_autonomy)
        new_autonomy = replace(new_autonomy, state_digest=new_a_digest)

        events = self._store.load_events(run_id)
        event = create_event(
            run_id=run_id,
            sequence=len(events) + 1,
            previous_event_digest=events[-1].event_digest if events else None,
            event_type=SupervisorEventType.AUTONOMY_LEVEL_CHANGED,
            state_revision=state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={
                "previous_level": state.autonomy_state.current_level.value,
                "new_level": new_level.value,
                "reason": "PROMOTION",
            },
            created_at_utc=ts,
        )
        self._store.save_event(event)

        seed = self._store.load_seed_state(run_id)
        self._store.save_seed_state(replace(seed, autonomy_state=new_autonomy))
        all_events = self._store.load_events(run_id)
        return replay_events_with_seed(all_events, seed)

    def demote_level(
        self,
        run_id: str,
        critical_failure: bool = False,
        policy_invalidated: bool = False,
        updated_at_utc: str | None = None,
    ) -> SupervisorState:
        """Evaluate demotion and update autonomy level."""
        state = self._store.load_state(run_id)
        if state.autonomy_state is None:
            _fail("NO_WORK_PROFILE_BOUND")
        profile = self._profiles.get(run_id)
        if profile is None:
            _fail("WORK_PROFILE_NOT_FOUND")

        new_level = evaluate_demotion(state.autonomy_state, profile, critical_failure, policy_invalidated)
        if new_level == state.autonomy_state.current_level:
            return state

        ts = updated_at_utc or state.updated_at_utc
        new_autonomy = replace(
            state.autonomy_state,
            current_level=new_level,
            updated_at_utc=ts,
        )
        new_a_digest = _compute_autonomy_state_digest(new_autonomy)
        new_autonomy = replace(new_autonomy, state_digest=new_a_digest)

        events = self._store.load_events(run_id)
        event = create_event(
            run_id=run_id,
            sequence=len(events) + 1,
            previous_event_digest=events[-1].event_digest if events else None,
            event_type=SupervisorEventType.AUTONOMY_LEVEL_CHANGED,
            state_revision=state.state_revision + 1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={
                "previous_level": state.autonomy_state.current_level.value,
                "new_level": new_level.value,
                "reason": "DEMOTION",
            },
            created_at_utc=ts,
        )
        self._store.save_event(event)

        seed = self._store.load_seed_state(run_id)
        self._store.save_seed_state(replace(seed, autonomy_state=new_autonomy))
        all_events = self._store.load_events(run_id)
        return replay_events_with_seed(all_events, seed)

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

        from ai_engineering.supervisor.validator import canonical_serialize_verified_result
        vr_serialized = json.loads(canonical_serialize_verified_result(verified_result))

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
                "verified_result": vr_serialized,
            },
            created_at_utc=verified_result.verified_at_utc,
            verified_result_id=verified_result.result_id,
        )
        self._store.save_event(event)

        seed = self._store.load_seed_state(run_id)
        all_events = self._store.load_events(run_id)
        new_state = replay_events_with_seed(all_events, seed)

        # Patch latest_verified_result_digest since replay can't derive it
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
            autonomy_state=new_state.autonomy_state,
            budget_state=new_state.budget_state,
        )
        return new_state

    def get_verified_result(self, run_id: str, result_id: str) -> VerifiedResult | None:
        """Get VerifiedResult from in-memory cache or rehydrate from event log."""
        if result_id in self._vr_cache:
            return self._vr_cache[result_id]
        if f"id:{result_id}" in self._vr_cache:
            return self._vr_cache[f"id:{result_id}"]

        events = self._store.load_events(run_id)
        from ai_engineering.supervisor.events import SupervisorEventType
        from ai_engineering.supervisor.validator import deserialize_verified_result
        for ev in reversed(events):
            if (
                ev.event_type == SupervisorEventType.RESULT_INGESTED
                or getattr(ev.event_type, "value", str(ev.event_type)) == "RESULT_INGESTED"
            ):
                if ev.payload and ev.payload.get("result_id") == result_id:
                    vr_data = ev.payload.get("verified_result")
                    if vr_data:
                        vr_str = json.dumps(vr_data) if isinstance(vr_data, dict) else str(vr_data)
                        vr = deserialize_verified_result(vr_str)
                        self._vr_cache[result_id] = vr
                        if ev.payload.get("result_digest"):
                            self._vr_cache[f"digest:{result_id}"] = ev.payload["result_digest"]
                        return vr
        return None

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
            vr = self.get_verified_result(run_id, state.latest_verified_result_id)

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
            autonomy_state=new_state.autonomy_state,
            budget_state=new_state.budget_state,
        )
        return pack, new_state

    def accept_decision(
        self,
        run_id: str,
        decision: AstraDecision,
        context_pack_digest: str,
        verified_result_status: str,
        validated_at_utc: str,
        parent_intent: TaskIntent | None = None,
        effective_policy: EffectivePolicyReport | None = None,
        new_task_id: str | None = None,
        new_base_sha: str | None = None,
        new_attempt_id: str | None = None,
        execution_target: ExecutionTarget | None = None,
        requested_effect_classes: tuple[EffectClass, ...] | None = None,
        requested_stop_boundary: StopBoundary | None = None,
    ) -> tuple[DecisionReceipt, SupervisorState]:
        """Validate and accept a decision.
        If a work profile is bound, generates candidate child task,
        evaluates policy, and proceeds to NEXT_TASK_GENERATED only on ALLOW.
        If DENY, persists POLICY_EVALUATED and stops autonomous continuation.
        """
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

        accept_event = create_event(
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
        self._store.save_event(accept_event)

        # Legacy Task 2 mode (no work profile bound)
        if state.autonomy_state is None:
            seed = self._store.load_seed_state(run_id)
            all_events = self._store.load_events(run_id)
            new_state = replay_events_with_seed(all_events, seed)

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
                autonomy_state=new_state.autonomy_state,
                budget_state=new_state.budget_state,
            )
            return receipt, new_state

        # Work profile IS bound: generate candidate task, evaluate policy
        work_profile = self._profiles.get(run_id)
        if work_profile is None:
            events = self._store.load_events(run_id)
            for e in reversed(events):
                if "WORK_PROFILE_BOUND" in str(e.event_type):
                    from ai_engineering.supervisor.policy.work_profile import validate_work_profile
                    wp_data = dict(e.payload["work_profile"])
                    try:
                        work_profile = validate_work_profile(wp_data)
                    except Exception:
                        wp_data.pop("profile_digest", None)
                        work_profile = validate_work_profile(wp_data)
                    self._profiles[run_id] = work_profile
                    if e.payload.get("effective_policy") and run_id not in self._effective_policies:
                        ep_dict = e.payload["effective_policy"]
                        try:
                            from ai_engineering.effective_policy import deserialize_effective_policy_report
                            import json
                            ep_str = json.dumps(ep_dict) if isinstance(ep_dict, dict) else str(ep_dict)
                            self._effective_policies[run_id] = deserialize_effective_policy_report(ep_str)
                        except Exception:
                            from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus, TaskPolicyAttribution
                            d = dict(ep_dict) if isinstance(ep_dict, dict) else {}
                            tp = d.get("task_policy")
                            tpa = TaskPolicyAttribution(**tp) if isinstance(tp, dict) else tp
                            d["task_policy"] = tpa
                            if "status" in d and not isinstance(d["status"], EffectivePolicyStatus):
                                d["status"] = EffectivePolicyStatus(d["status"])
                            d["policy_sources"] = tuple(d.get("policy_sources", ()))
                            d["invariant_resolutions"] = tuple(d.get("invariant_resolutions", ()))
                            d["required_gate_resolutions"] = tuple(d.get("required_gate_resolutions", ()))
                            d["unresolved_references"] = tuple(d.get("unresolved_references", ()))
                            self._effective_policies[run_id] = EffectivePolicyReport(**d)
                    break
        if work_profile is None:
            _fail("WORK_PROFILE_NOT_FOUND")

        # Resolve parent intent
        p_intent = parent_intent or self._intents.get(state.current_task_id) or self._intents.get(run_id)
        if p_intent is None:
            events = self._store.load_events(run_id)
            from ai_engineering.task_intent import deserialize_intent
            import json
            for e in reversed(events):
                ev_type = getattr(e.event_type, "value", str(e.event_type))
                if ev_type in ("RUN_INITIALIZED", "NEXT_TASK_GENERATED", "ATTEMPT_INCREMENTED"):
                    if "task_intent" in e.payload:
                        p_intent = deserialize_intent(json.dumps(e.payload["task_intent"]))
                        self._intents[p_intent.task_id] = p_intent
                        self._intents[run_id] = p_intent
                        break
        if p_intent is None:
            _fail("PARENT_INTENT_NOT_FOUND")

        # Resolve effective policy
        eff_policy = effective_policy or self._effective_policies.get(run_id)
        if eff_policy is None:
            _fail("EFFECTIVE_POLICY_NOT_FOUND")

        # Generate candidate child task
        from ai_engineering.supervisor.next_task import NextTaskGenerator
        generator = NextTaskGenerator()
        vr = self._vr_cache.get(state.latest_verified_result_id or "")

        candidate_task_id = new_task_id or f"{p_intent.task_id}-candidate-child"
        candidate_base_sha = new_base_sha or state.current_base_sha
        candidate_attempt_id = new_attempt_id or f"{candidate_task_id}-attempt-1"

        if decision.action.value == "CREATE_PR":
            candidate_intent = p_intent
            lineage = None
        elif decision.action.value == "MERGE_IF_GREEN":
            candidate_intent = p_intent
            lineage = None
        elif decision.action.value == "CONTINUE":
            candidate_intent, lineage = generator.generate_continue(
                parent_intent=p_intent,
                decision=decision,
                receipt=receipt,
                new_task_id=candidate_task_id,
                new_base_sha=candidate_base_sha,
                created_at_utc=validated_at_utc,
            )
        elif decision.action.value == "FIX":
            failing_gates = [
                gr.gate_name for gr in (vr.gate_results if vr else ())
                if gr.status.value == "FAIL"
            ]
            candidate_intent, lineage = generator.generate_fix(
                parent_intent=p_intent,
                decision=decision,
                receipt=receipt,
                new_task_id=candidate_task_id,
                failing_gates=failing_gates,
                created_at_utc=validated_at_utc,
            )
        elif decision.action.value == "RETRY":
            candidate_intent, lineage = generator.generate_retry(
                parent_intent=p_intent,
                decision=decision,
                receipt=receipt,
                new_attempt_id=candidate_attempt_id,
                created_at_utc=validated_at_utc,
            )
        else:
            _fail(f"UNSUPPORTED_ACTION:{decision.action.value}")

        # Construct PolicyRequest
        req_id = compute_deterministic_digest({
            "run_id": run_id,
            "task_id": state.current_task_id,
            "attempt_id": state.current_attempt_id,
            "decision_id": decision.decision_id,
            "action": decision.action.value,
        })
        target = execution_target or (work_profile.allowed_targets[0] if work_profile.allowed_targets else ExecutionTarget.DEV)

        if requested_effect_classes is not None:
            final_effects = tuple(requested_effect_classes)
        elif decision.action.value == "CREATE_PR":
            final_effects = (EffectClass.PR_MUTATION,)
        elif decision.action.value == "MERGE_IF_GREEN":
            final_effects = (EffectClass.PR_MERGE,)
        else:
            req_effects = []
            for m in candidate_intent.allowed_mutations:
                try:
                    req_effects.append(EffectClass(m))
                except (ValueError, KeyError):
                    pass
            if not req_effects:
                req_effects = [EffectClass.READ_ONLY]
            final_effects = tuple(req_effects)

        if requested_stop_boundary is not None:
            final_stop_boundary = requested_stop_boundary
        elif decision.action.value == "CREATE_PR":
            final_stop_boundary = StopBoundary.READY_PR
        elif decision.action.value == "MERGE_IF_GREEN":
            final_stop_boundary = StopBoundary.MERGE
        else:
            final_stop_boundary = candidate_intent.stop_boundary

        policy_request = PolicyRequest(
            schema_version=POLICY_REQUEST_SCHEMA_VERSION,
            request_id=req_id,
            run_id=run_id,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            decision_id=decision.decision_id,
            decision_receipt_id=receipt.receipt_id,
            work_profile_id=work_profile.profile_id,
            work_profile_digest=work_profile.profile_digest,
            current_autonomy_level=state.autonomy_state.current_level,
            requested_action=decision.action.value,
            requested_effect_classes=final_effects,
            requested_stop_boundary=final_stop_boundary,
            execution_target=target,
            effective_policy_id=eff_policy.effective_policy_id,
            effective_policy_digest=eff_policy.effective_policy_id,
            budget_state_digest=state.budget_state.budget_digest,
        )

        policy_receipt = evaluate_policy(
            request=policy_request,
            task_intent=p_intent,
            effective_policy=eff_policy,
            work_profile=work_profile,
            autonomy_state=state.autonomy_state,
            budget_state=state.budget_state,
        )

        if policy_receipt.verdict == PolicyVerdict.ALLOW:
            # 1. Update budget
            c_inc = 1 if decision.action.value in ("CONTINUE", "FIX") else 0
            r_inc = 1 if decision.action.value == "RETRY" else 0
            f_inc = 1 if decision.action.value == "FIX" else 0
            updated_budget = evaluate_budget_consumption(
                state.budget_state,
                work_profile.budget_limits,
                decisions_increment=1,
                child_tasks_increment=c_inc,
                retries_increment=r_inc,
                fix_cycles_increment=f_inc,
            )
            b_dg = _compute_budget_digest(updated_budget)
            updated_budget = replace(updated_budget, budget_digest=b_dg)

            # 2. Update autonomy state
            updated_autonomy = replace(
                state.autonomy_state,
                budget_state_digest=b_dg,
                updated_at_utc=validated_at_utc,
            )
            a_dg = _compute_autonomy_state_digest(updated_autonomy)
            updated_autonomy = replace(updated_autonomy, state_digest=a_dg)

            # 3. Persist POLICY_EVALUATED event
            policy_event = create_event(
                run_id=run_id,
                sequence=new_seq + 1,
                previous_event_digest=accept_event.event_digest,
                event_type=SupervisorEventType.POLICY_EVALUATED,
                state_revision=state.state_revision + 2,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "receipt_id": policy_receipt.receipt_id,
                    "verdict": policy_receipt.verdict.value,
                    "reason_codes": list(policy_receipt.reason_codes),
                    "work_profile_id": work_profile.profile_id,
                    "current_level": state.autonomy_state.current_level.value,
                    "policy_receipt": __import__('dataclasses').asdict(policy_receipt),
                },
                created_at_utc=validated_at_utc,
                decision_id=decision.decision_id,
            )
            self._store.save_event(policy_event)

            self._latest_policy_receipt[run_id] = policy_receipt

            child_idg = intent_digest(candidate_intent)

            if decision.action.value not in ("CREATE_PR", "MERGE_IF_GREEN"):
                # 4. Proceed to NEXT_TASK_GENERATED or ATTEMPT_INCREMENTED (candidate task is authoritative)
                event_type = (
                    SupervisorEventType.ATTEMPT_INCREMENTED
                    if decision.action.value == "RETRY"
                    else SupervisorEventType.NEXT_TASK_GENERATED
                )
                task_event = create_event(
                    run_id=run_id,
                    sequence=new_seq + 2,
                    previous_event_digest=policy_event.event_digest,
                    event_type=event_type,
                    state_revision=state.state_revision + 3,
                    task_id=candidate_intent.task_id,
                    attempt_id=candidate_attempt_id,
                    intent_digest=child_idg,
                    payload={"action": decision.action.value, "new_task_id": candidate_intent.task_id, "task_intent": __import__('dataclasses').asdict(candidate_intent)},
                    created_at_utc=validated_at_utc,
                    decision_id=decision.decision_id,
                )
                self._store.save_event(task_event)

                self._intents[candidate_intent.task_id] = candidate_intent

            # Load seed state
            seed = self._store.load_seed_state(run_id)
            all_events = self._store.load_events(run_id)
            new_state = replay_events_with_seed(all_events, seed)

            new_state = SupervisorState(
                schema_version=new_state.schema_version,
                run_id=new_state.run_id,
                root_goal_id=new_state.root_goal_id,
                root_goal=new_state.root_goal,
                repository=new_state.repository,
                canonical_remote=new_state.canonical_remote,
                canonical_main_ref=new_state.canonical_main_ref,
                current_task_id=candidate_intent.task_id,
                current_intent_digest=child_idg,
                current_intent_revision=new_state.current_intent_revision,
                current_base_sha=new_state.current_base_sha,
                current_attempt_id=candidate_attempt_id,
                attempt_number=new_state.attempt_number,
                latest_verified_result_id=state.latest_verified_result_id,
                latest_verified_result_digest=vr_digest if vr_digest else None,
                latest_context_pack_digest=state.latest_context_pack_digest,
                latest_decision_id=decision.decision_id,
                latest_decision_digest=decision_dg,
                engineering_cycle_id=new_state.engineering_cycle_id,
                engineering_cycle_phase=new_state.engineering_cycle_phase,
                phase=SupervisorPhase.RUNNING,
                blockers=new_state.blockers,
                created_at_utc=new_state.created_at_utc,
                updated_at_utc=validated_at_utc,
                state_revision=new_state.state_revision,
                event_sequence=new_state.event_sequence,
                autonomy_state=updated_autonomy,
                budget_state=updated_budget,
            )
            return receipt, new_state

        else:
            # Policy Verdict == DENY
            # 1. Update budget consumption (policy denials increment)
            updated_budget = evaluate_budget_consumption(
                state.budget_state,
                work_profile.budget_limits,
                decisions_increment=1,
                policy_denials_increment=1,
            )
            b_dg = _compute_budget_digest(updated_budget)
            updated_budget = replace(updated_budget, budget_digest=b_dg)

            # 2. Update autonomy state
            updated_autonomy = replace(
                state.autonomy_state,
                budget_state_digest=b_dg,
                updated_at_utc=validated_at_utc,
            )
            a_dg = _compute_autonomy_state_digest(updated_autonomy)
            updated_autonomy = replace(updated_autonomy, state_digest=a_dg)

            # 3. Persist POLICY_EVALUATED event
            policy_event = create_event(
                run_id=run_id,
                sequence=new_seq + 1,
                previous_event_digest=accept_event.event_digest,
                event_type=SupervisorEventType.POLICY_EVALUATED,
                state_revision=state.state_revision + 2,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "receipt_id": policy_receipt.receipt_id,
                    "verdict": policy_receipt.verdict.value,
                    "reason_codes": list(policy_receipt.reason_codes),
                    "work_profile_id": work_profile.profile_id,
                    "current_level": state.autonomy_state.current_level.value,
                    "policy_receipt": __import__('dataclasses').asdict(policy_receipt),
                },
                created_at_utc=validated_at_utc,
                decision_id=decision.decision_id,
            )
            self._store.save_event(policy_event)
            self._latest_policy_receipt[run_id] = policy_receipt

            # 4. Record blocker and STOP autonomous continuation
            # Do NOT emit NEXT_TASK_GENERATED
            # Do NOT set candidate task as authoritative
            blocker_msg = f"POLICY_DENIED: {','.join(policy_receipt.reason_codes)}"
            blocker_event = create_event(
                run_id=run_id,
                sequence=new_seq + 2,
                previous_event_digest=policy_event.event_digest,
                event_type=SupervisorEventType.BLOCKER_RECORDED,
                state_revision=state.state_revision + 3,
                task_id=state.current_task_id,
                attempt_id=state.current_attempt_id,
                intent_digest=state.current_intent_digest,
                payload={
                    "blocker": blocker_msg,
                    "reason_codes": list(policy_receipt.reason_codes),
                },
                created_at_utc=validated_at_utc,
                decision_id=decision.decision_id,
            )
            self._store.save_event(blocker_event)

            # Load seed state
            seed = self._store.load_seed_state(run_id)
            all_events = self._store.load_events(run_id)
            new_state = replay_events_with_seed(all_events, seed)

            new_state = SupervisorState(
                schema_version=new_state.schema_version,
                run_id=new_state.run_id,
                root_goal_id=new_state.root_goal_id,
                root_goal=new_state.root_goal,
                repository=new_state.repository,
                canonical_remote=new_state.canonical_remote,
                canonical_main_ref=new_state.canonical_main_ref,
                current_task_id=state.current_task_id,  # KEEP ORIGINAL TASK_ID (NOT AUTHORITATIVE)
                current_intent_digest=state.current_intent_digest,
                current_intent_revision=state.current_intent_revision,
                current_base_sha=state.current_base_sha,
                current_attempt_id=state.current_attempt_id,
                attempt_number=state.attempt_number,
                latest_verified_result_id=state.latest_verified_result_id,
                latest_verified_result_digest=vr_digest if vr_digest else None,
                latest_context_pack_digest=state.latest_context_pack_digest,
                latest_decision_id=decision.decision_id,
                latest_decision_digest=decision_dg,
                engineering_cycle_id=new_state.engineering_cycle_id,
                engineering_cycle_phase=new_state.engineering_cycle_phase,
                phase=SupervisorPhase.BLOCKED,  # Autonomous continuation STOPPED
                blockers=tuple(list(new_state.blockers) + [blocker_msg]),
                created_at_utc=new_state.created_at_utc,
                updated_at_utc=validated_at_utc,
                state_revision=new_state.state_revision,
                event_sequence=new_state.event_sequence,
                autonomy_state=updated_autonomy,
                budget_state=updated_budget,
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
        created_at_utc: str,
        attempt_id: str | None = None,
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
            payload={"action": decision.action.value, "new_task_id": new_task_id, "task_intent": __import__('dataclasses').asdict(child_intent)},
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
