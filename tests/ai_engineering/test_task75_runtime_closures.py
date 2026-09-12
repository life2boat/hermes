from ai_engineering.supervisor.router.router import compute_policy_receipt_digest
from ai_engineering.task_intent import intent_digest
import pytest
import asyncio
import os
import json
import uuid
import datetime
from dataclasses import replace
from typing import Any

from ai_engineering.supervisor.router.envelope import AgentEnvelope, MessageType, EffectClass, StopBoundary
from ai_engineering.supervisor.router.router import CrossAgentRouter, AuthorityResolver, PolicyDeniedError, TimeoutCancellationUnconfirmedError, AuthorityInvalidError
from ai_engineering.supervisor.router.adapters import BaseAgentAdapter, AgentTransport
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.state import SupervisorState
from ai_engineering.supervisor.policy.contracts import AutonomyState, AutonomyBudgetState, AutonomyLevel
from ai_engineering.task_intent import TaskIntent, NodeKind, RelationKind, validate_intent
from ai_engineering.supervisor.events import create_event, SupervisorEventType
from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus

from ai_engineering.supervisor.router.router import PolicyReceipt
from ai_engineering.supervisor.policy.contracts import PolicyVerdict
from ai_engineering.supervisor.router.registry import AgentRegistry, AgentDefinition

def make_registry(adapter) -> AgentRegistry:
    reg = AgentRegistry()
    reg.register(AgentDefinition(
        agent_id="codex", capabilities=["test"], supported_message_types=[MessageType.WORK_REQUEST],
        allowed_effect_classes=[EffectClass.READ_ONLY], timeout_seconds=30
    ), adapter)
    return reg

def make_receipt(task_intent_digest: str = "dig") -> PolicyReceipt:
    return PolicyReceipt(
        schema_version="1", receipt_id="rec-1", request_id="req-1", task_intent_digest=task_intent_digest,
        decision_id="dec-1", decision_receipt_id="drec-1", effective_policy_id="ep-1",
        work_profile_id="wp-1", work_profile_digest="wpdig", autonomy_state_digest="adig",
        budget_state_digest="bdig", verdict=PolicyVerdict.ALLOW, reason_codes=(),
        created_at_utc="now"
    )

def make_env(run_id: str, intent_digest: str = "dig", receipt_digest: str = "dig2") -> AgentEnvelope:
    from ai_engineering.supervisor.router.envelope import compute_payload_digest
    return AgentEnvelope(
        message_id="msg-1", correlation_id="cor-1", causation_id="cause", run_id=run_id, task_id="task-01", attempt_id="att-01",
        sender_agent="supervisor", recipient_agent="codex", recipient_capability="test", message_type=MessageType.WORK_REQUEST,
        task_intent_id="task-01", task_intent_digest=intent_digest, policy_receipt_id="rec-1", policy_receipt_digest=receipt_digest,
        effect_class=EffectClass.READ_ONLY, stop_boundary=StopBoundary.DRAFT_PR, retry_count=0, max_retries=3, payload={}, payload_digest=compute_payload_digest({}),
        created_at_utc="now", expires_at_utc="later"
    )

def make_intent(task_id: str = "task-runtime-01") -> TaskIntent:
    from ai_engineering.task_intent import IntentStatus, TaskClass
    return validate_intent(TaskIntent(
        schema_version=1,
        task_id=task_id,
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Test runtime closures",
        source_repository="github.com/life2boat/hermes",
        source_main_ref="main",
        source_base_sha="a8c8b9d619e169c172de2c91496d79717be0dd20",
        constraints=(),
        allowed_mutations=("*", "test"),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.DRAFT_PR,
        acceptance_criteria=(),
        unknowns=(),
        applicable_invariants=(),
        required_gates=(),
        parent_intent_digest=None,
    ))

class FlakyTransport:
    def __init__(self, behavior: str = "timeout_no_is_running"):
        self.behavior = behavior
        self.running = False
        self.dispatch_count = 0

    def dispatch(self, request, timeout):
        self.dispatch_count += 1
        self.running = True
        self.last_operation_id = request.get("operation_id", "op-unknown")
        # Simulating work timeout...
        raise asyncio.TimeoutError("WORK_TIMEOUT")

    def cancel(self, operation_id):
        if self.behavior == "timeout_no_is_running":
            return None # no boolean returned
        elif self.behavior == "timeout_unconfirmed":
            self.running = True # still running
            return False
        elif self.behavior == "timeout_confirmed":
            self.running = False
            return True
        return True

    def is_running(self, operation_id):
        if self.behavior == "timeout_no_is_running":
            raise AttributeError("no is_running")
        return self.running

    def health(self):
        return True

from ai_engineering.supervisor.state import create_initial_state

def make_autonomy_state(run_id="run-1"):
    return AutonomyState(
        schema_version="1", run_id=run_id, profile_id="p-1", profile_digest="dig",
        current_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY, maximum_allowed_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY,
        successful_runs=0, critical_failures=0, rollback_verified=False,
        required_validators_status={}, budget_state_digest="bdig", promotion_sequence=0,
        last_transition_receipt_id="", created_at_utc="", updated_at_utc="", state_digest="adig"
    )

def make_budget_state(run_id="run-1"):
    return AutonomyBudgetState(
        schema_version="1", budget_id="b-1", budget_digest="bdig",
        decisions_used=0, child_tasks_used=0, retries_used=0, fix_cycles_used=0,
        consecutive_failures=0, provider_calls_used=0, policy_denials=0, exhausted_dimensions=()
    )

def make_state(run_id, no_auth=False):
    s = create_initial_state(
        run_id=run_id, root_goal_id="g1", root_goal="g", repository="repo",
        canonical_remote="github", canonical_main_ref="main", task_id="task-01",
        intent_digest_val="dig", intent_revision=1, base_sha="sha", attempt_id="att-01",
        created_at_utc="now"
    )
    if not no_auth:
        s = replace(s, autonomy_state=make_autonomy_state(run_id), budget_state=make_budget_state(run_id))
    return s

@pytest.mark.asyncio
async def test_cancellation_unconfirmed_blocks_execution(tmp_path):
    # - cancel() возвращает None, API подтверждения остановки отсутствует: результат BLOCKED / TIMEOUT_CANCELLATION_UNCONFIRMED;
    store = FileSupervisorStateStore(tmp_path)
    run_id = "run-test-cancel"

    # Setup initial state
    state = make_state(run_id)
    # mock save state
    with open(os.path.join(tmp_path, "state.json"), "w") as f:
        from ai_engineering.supervisor.state import _state_to_dict
        json.dump(_state_to_dict(state), f)

    resolver = AuthorityResolver(store)
    intent = make_intent("task-01")
    resolver.intent_store[(run_id, "task-01")] = intent
    from ai_engineering.supervisor.router.router import compute_policy_receipt_digest
    receipt = make_receipt(intent_digest(intent))
    resolver.receipt_store[(run_id, "task-01")] = receipt

    transport = FlakyTransport("timeout_no_is_running")
    # Wrap transport so it acts like real transport but lacks is_running
    class NoIsRunningTransport:
        def dispatch(self, r, t): return transport.dispatch(r, t)
        def cancel(self, op): return transport.cancel(op)
        def health(self): return True

    adapter = BaseAgentAdapter(NoIsRunningTransport())
    from ai_engineering.supervisor.router.router import PersistentStore
    router = CrossAgentRouter(registry=make_registry(adapter), authority_resolver=resolver, store=PersistentStore(str(tmp_path)))


    receipt = make_receipt(intent_digest(intent))
    env = make_env(run_id, intent_digest(intent), compute_policy_receipt_digest(receipt))

    with pytest.raises(TimeoutCancellationUnconfirmedError):
        await router.dispatch(env)

@pytest.mark.asyncio
async def test_cancellation_confirmed(tmp_path):
    # - подтверждённая остановка реального тестового worker: после TIMED_OUT нет продолжающегося исполнения и позднего контрольного эффекта;
    store = FileSupervisorStateStore(tmp_path)
    run_id = "run-test-cancel-conf"

    state = make_state(run_id)
    with open(os.path.join(tmp_path, "state.json"), "w") as f:
        from ai_engineering.supervisor.state import _state_to_dict
        json.dump(_state_to_dict(state), f)

    resolver = AuthorityResolver(store)
    intent = make_intent("task-01")
    resolver.intent_store[(run_id, "task-01")] = intent
    from ai_engineering.supervisor.router.router import compute_policy_receipt_digest
    receipt = make_receipt(intent_digest(intent))
    resolver.receipt_store[(run_id, "task-01")] = receipt
    transport = FlakyTransport("timeout_confirmed")
    adapter = BaseAgentAdapter(transport)
    from ai_engineering.supervisor.router.router import PersistentStore
    router = CrossAgentRouter(registry=make_registry(adapter), authority_resolver=resolver, store=PersistentStore(str(tmp_path)))
    receipt = make_receipt(intent_digest(intent))
    env = make_env(run_id, intent_digest(intent), compute_policy_receipt_digest(receipt))
    env = replace(env, max_retries=0)

    with pytest.raises(asyncio.TimeoutError):
        await router.dispatch(env)

    assert transport.running == False

@pytest.mark.asyncio
async def test_authority_missing_blocks_dispatch(tmp_path):
    # - отсутствие WorkProfile, EffectivePolicy, autonomy state или budget не создаёт синтетическую authority и не допускает dispatch;
    store = FileSupervisorStateStore(tmp_path)
    run_id = "run-no-auth"

    # State has NO autonomy/budget
    state = make_state(run_id, no_auth=True)
    with open(os.path.join(tmp_path, "state.json"), "w") as f:
        from ai_engineering.supervisor.state import _state_to_dict
        json.dump(_state_to_dict(state), f)

    resolver = AuthorityResolver(store)
    intent = make_intent("task-01")
    resolver.intent_store[(run_id, "task-01")] = intent

    resolver.receipt_store[(run_id, "task-01")] = make_receipt(intent_digest(intent))

    # In evaluate_fresh_policy, it should raise PolicyDeniedError if authority is missing

    receipt = make_receipt(intent_digest(intent))
    env = make_env(run_id, intent_digest(intent), compute_policy_receipt_digest(receipt))

    with pytest.raises(PolicyDeniedError, match="Authority unresolved"):
        resolver.evaluate_fresh_policy(env, "att-retry-1")

@pytest.mark.asyncio
async def test_subclass_exceptions_block_retry(tmp_path):
    # - подклассы security/policy/integrity ошибок не запускают retry или fallback.
    class SubclassedAuthorityError(AuthorityInvalidError):
        pass

    class ErrorTransport:
        def dispatch(self, request, timeout):
            raise SubclassedAuthorityError("Should not retry")
        def cancel(self, op): return True
        def health(self): return True
        def is_running(self, op): return False

    store = FileSupervisorStateStore(tmp_path)
    run_id = "run-subclass"

    state = make_state(run_id)
    with open(os.path.join(tmp_path, "state.json"), "w") as f:
        from ai_engineering.supervisor.state import _state_to_dict
        json.dump(_state_to_dict(state), f)

    resolver = AuthorityResolver(store)
    intent = make_intent("task-01")
    resolver.intent_store[(run_id, "task-01")] = intent

    resolver.receipt_store[(run_id, "task-01")] = make_receipt(intent_digest(intent))

    from ai_engineering.supervisor.router.router import PersistentStore
    router = CrossAgentRouter(registry=make_registry(BaseAgentAdapter(ErrorTransport())), authority_resolver=resolver, store=PersistentStore(str(tmp_path)))

    receipt = make_receipt(intent_digest(intent))
    env = make_env(run_id, intent_digest(intent), compute_policy_receipt_digest(receipt))

    # Should raise SubclassedAuthorityError without hitting retry logic (which would complain about missing policy)
    with pytest.raises(SubclassedAuthorityError):
        await router.dispatch(env)
