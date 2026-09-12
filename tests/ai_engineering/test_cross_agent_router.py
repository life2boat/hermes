from __future__ import annotations
import os
import json
import uuid
import hashlib
import asyncio
import pytest
from typing import Any, Mapping, Sequence
from dataclasses import replace, asdict
from pathlib import Path

from ai_engineering.contracts import EffectClass, StopBoundary, Status
from ai_engineering.task_intent import TaskIntent, TaskLineage, LineageNode, LINEAGE_SCHEMA_VERSION, NodeKind, RelationKind, deserialize_intent, intent_digest
from ai_engineering.supervisor.policy.contracts import (
    PolicyReceipt,
    PolicyVerdict,
    PolicyRequest,
    AutonomyState,
    AutonomyBudgetState,
    AutonomyLevel,
    ExecutionTarget,
    PromotionThresholds,
    BudgetLimits,
    WORK_PROFILE_SCHEMA_VERSION,
    AUTONOMY_STATE_SCHEMA_VERSION,
    AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
    compute_deterministic_digest,
)
from ai_engineering.supervisor.policy.engine import evaluate_policy
from ai_engineering.supervisor.policy.work_profile import validate_work_profile
from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus, TaskPolicyAttribution
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.events import SupervisorEventType, create_event
from ai_engineering.supervisor.worker_result import (
    WorkerResultBundle,
    canonical_serialize_worker_result,
)
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.validator import validate_normalized_evidence, VerifiedResult
from ai_engineering.supervisor.router import (
    AgentEnvelope,
    MessageType,
    CrossAgentRouter,
    AgentRegistry,
    AgentDefinition,
    AntigravityAdapter,
    CodexAdapter,
    ComputerUseAdapter,
    AuthorityResolver,
    PersistentStore,
    compute_policy_receipt_digest,
    AuthorityInvalidError,
    EffectClassEscalationError,
    StopBoundaryEscalationError,
    MessageTamperedError,
    CrossRunViolationError,
    UnsupportedMessageTypeError,
    StaleResultError,
    WorkerResultIdentityMismatchError,
    IllegalStateTransitionError,
    CapabilityNotAuthorizedError,
    SourceProvenanceUnresolvedError,
    SourceProvenanceMismatchError,
    TimeoutCancellationUnconfirmedError,
    ReplayMessageInvalidError,
    StaleOrForeignAttemptError,
    PolicyDeniedError,
    AgentTransportUnavailableError,
    ComputerUseTransportUnavailableError,
    NON_RETRYABLE_EXCEPTIONS,
)

# ── Test Helpers ───────────────────────────────────────────────────────────

class FakeTransport:
    def __init__(
        self,
        available: bool = True,
        slow_seconds: float = 0.0,
        cancel_confirmation: bool = True,
    ) -> None:
        self.available = available
        self.slow_seconds = slow_seconds
        self.cancel_confirmation = cancel_confirmation
        self.dispatch_count = 0
        self.cancelled_ops: list[str] = []
        self.last_request: dict[str, Any] | None = None
        self._is_running = False

    def health(self) -> bool:
        return self.available

    def cancel(self, operation_id: str) -> bool:
        self.cancelled_ops.append(operation_id)
        if self.cancel_confirmation:
            self._is_running = False
            return True
        return False

    def is_running(self, operation_id: str) -> bool:
        return self._is_running

    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
        self.dispatch_count += 1
        self.last_request = dict(request)
        if self.slow_seconds > 0:
            import time
            self._is_running = True
            time.sleep(self.slow_seconds)
            self._is_running = False
        return WorkerResultBundle(
            schema_version="hermes.worker-result.v1",
            result_id=f"res-{uuid.uuid4().hex[:8]}",
            task_id=request["task_id"],
            attempt_id=request["attempt_id"],
            worker_id=request.get("worker_id", "anti"),
            base_sha=request["base_sha"],
            head_sha="head_sha_123",
            canonical_remote=request["canonical_remote"],
            repository=request["repository"],
            intent_digest=request["task_intent_digest"],
            produced_at_utc="2026-09-12T00:00:00Z",
            artifacts=(),
            gate_claims=(),
        )
def make_intent(
    task_id: str = "task-001",
    stop_boundary: StopBoundary = StopBoundary.LOCAL_DIFF,
    allowed_mutations: Sequence[str] = ("code", "os"),
    base_sha: str = "54e404eab378b6a5a8ca7c9d3bf8bed01810ec35",
    source_repository: str = "github",
    required_gates: Sequence[str] = (),
) -> TaskIntent:
    d = {
        "schema_version": 1,
        "task_id": task_id,
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Verify cross agent routing",
        "source_repository": source_repository,
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": base_sha,
        "constraints": [],
        "allowed_mutations": list(allowed_mutations),
        "forbidden_mutations": [],
        "stop_boundary": stop_boundary.value if hasattr(stop_boundary, "value") else str(stop_boundary),
        "acceptance_criteria": [{"criterion_id": "c1", "statement": "verify"}],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": list(required_gates),
        "parent_intent_digest": None,
    }
    return deserialize_intent(json.dumps(d))

def make_receipt(
    receipt_id: str = "rcpt-001",
    intent: TaskIntent | None = None,
    verdict: PolicyVerdict = PolicyVerdict.ALLOW,
) -> PolicyReceipt:
    i_digest = intent_digest(intent) if intent else "0" * 64
    return PolicyReceipt(
        schema_version="hermes.supervisor-policy-receipt.v1",
        receipt_id=receipt_id,
        request_id="req-001",
        task_intent_digest=i_digest,
        decision_id="dec-001",
        decision_receipt_id="drec-001",
        effective_policy_id="pol-001",
        work_profile_id="prof-001",
        work_profile_digest="prof_dig",
        autonomy_state_digest="auto_dig",
        budget_state_digest="bud_dig",
        verdict=verdict,
        reason_codes=(),
        created_at_utc="2026-09-12T00:00:00Z",
    )

def make_envelope(
    payload: dict[str, Any],
    intent: TaskIntent,
    receipt: PolicyReceipt,
    run_id: str = "run-001",
    attempt_id: str = "att-001",
    recipient_agent: str = "anti",
    recipient_capability: str = "code",
    message_type: MessageType = MessageType.WORK_REQUEST,
    effect_class: EffectClass = EffectClass.READ_ONLY,
    stop_boundary: StopBoundary = StopBoundary.LOCAL_DIFF,
    message_id: str | None = None,
) -> AgentEnvelope:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    p_digest = hashlib.sha256(canonical).hexdigest()
    return AgentEnvelope(
        schema_version="hermes.agent-envelope.v1",
        message_id=message_id or str(uuid.uuid4()),
        correlation_id=f"corr-{uuid.uuid4().hex[:6]}",
        causation_id=f"caus-{uuid.uuid4().hex[:6]}",
        run_id=run_id,
        task_id=intent.task_id,
        attempt_id=attempt_id,
        sender_agent="astra",
        recipient_agent=recipient_agent,
        recipient_capability=recipient_capability,
        message_type=message_type,
        payload=payload,
        payload_digest=p_digest,
        task_intent_id=intent.task_id,
        task_intent_digest=intent_digest(intent),
        policy_receipt_id=receipt.receipt_id,
        policy_receipt_digest=compute_policy_receipt_digest(receipt),
        effect_class=effect_class,
        stop_boundary=stop_boundary,
        created_at_utc="2026-09-12T00:00:00Z",
        expires_at_utc="2026-09-12T01:00:00Z",
    )

def setup_test_router(
    tmp_path: Any,
    intent: TaskIntent,
    receipt: PolicyReceipt,
    anti_transport: FakeTransport | None = None,
    codex_transport: FakeTransport | None = None,
    comp_transport: FakeTransport | None = None,
    provenance: dict[str, str] | None = None,
    permitted_capabilities: Sequence[str] | None = None,
) -> tuple[CrossAgentRouter, AuthorityResolver, PersistentStore]:
    registry = AgentRegistry()
    anti_t = anti_transport or FakeTransport(True)
    registry.register(
        AgentDefinition(
            agent_id="anti",
            capabilities=["code", "test"],
            supported_message_types=[MessageType.WORK_REQUEST, MessageType.CANCEL_REQUEST],
            allowed_effect_classes=[EffectClass.READ_ONLY, EffectClass.REPOSITORY_WRITE],
            timeout_seconds=2,
        ),
        AntigravityAdapter(anti_t),
    )
    if codex_transport:
        registry.register(
            AgentDefinition(
                agent_id="codex",
                capabilities=["code", "test"],
                supported_message_types=[MessageType.WORK_REQUEST],
                allowed_effect_classes=[EffectClass.READ_ONLY, EffectClass.REPOSITORY_WRITE],
                timeout_seconds=2,
            ),
            CodexAdapter(codex_transport),
        )
    if comp_transport:
        registry.register(
            AgentDefinition(
                agent_id="comp",
                capabilities=["os"],
                supported_message_types=[MessageType.WORK_REQUEST],
                allowed_effect_classes=[EffectClass.READ_ONLY],
                timeout_seconds=2,
            ),
            ComputerUseAdapter(comp_transport),
        )

    resolver = AuthorityResolver()
    resolver.register_authority(
        run_id="run-001",
        task_id=intent.task_id,
        intent=intent,
        receipt=receipt,
        provenance=provenance,
        permitted_capabilities=permitted_capabilities,
    )
    store = PersistentStore(str(tmp_path))
    router = CrossAgentRouter(registry, resolver, store)
    return router, resolver, store

# ── Substantive Test Matrix ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_01_forged_task_intent_id(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    object.__setattr__(env, "task_intent_id", "forged-task-id")
    with pytest.raises(AuthorityInvalidError, match="AUTHORITY_INVALID"):
        router.route(env)

@pytest.mark.asyncio
async def test_02_forged_task_intent_digest(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    object.__setattr__(env, "task_intent_digest", "0" * 64)
    with pytest.raises(AuthorityInvalidError, match="AUTHORITY_INVALID: TaskIntent digest mismatch"):
        router.route(env)

@pytest.mark.asyncio
async def test_03_forged_policy_receipt_id(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    object.__setattr__(env, "policy_receipt_id", "forged-rcpt-id")
    with pytest.raises(AuthorityInvalidError, match="AUTHORITY_INVALID"):
        router.route(env)

@pytest.mark.asyncio
async def test_04_forged_policy_receipt_digest(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    object.__setattr__(env, "policy_receipt_digest", "f" * 64)
    with pytest.raises(AuthorityInvalidError, match="AUTHORITY_INVALID: PolicyReceipt digest mismatch"):
        router.route(env)

@pytest.mark.asyncio
async def test_05_payload_tampered(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    object.__setattr__(env, "payload", {"cmd": "rm -rf /"})
    with pytest.raises(MessageTamperedError, match="AGENT_MESSAGE_PAYLOAD_TAMPERED"):
        router.route(env)

@pytest.mark.asyncio
async def test_06_unsupported_message_type(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    object.__setattr__(env, "message_type", "UNKNOWN_TYPE")
    with pytest.raises(UnsupportedMessageTypeError, match="AGENT_MESSAGE_TYPE_UNSUPPORTED"):
        router.route(env)

@pytest.mark.asyncio
async def test_07_effect_class_escalation(tmp_path: Any) -> None:
    intent = make_intent(stop_boundary=StopBoundary.LOCAL_DIFF)
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt, effect_class=EffectClass.DEPLOY)
    with pytest.raises(EffectClassEscalationError, match="EFFECT_CLASS_ESCALATION"):
        router.route(env)

@pytest.mark.asyncio
async def test_08_stop_boundary_escalation(tmp_path: Any) -> None:
    intent = make_intent(stop_boundary=StopBoundary.LOCAL_DIFF)
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt, stop_boundary=StopBoundary.DEPLOY)
    with pytest.raises(StopBoundaryEscalationError, match="STOP_BOUNDARY_ESCALATION"):
        router.route(env)

@pytest.mark.asyncio
async def test_09_unknown_agent(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt, recipient_agent="non_existent")
    receipt_out = router.route(env)
    assert receipt_out.decision == "BLOCK"
    assert receipt_out.reason == "UNKNOWN_AGENT"

@pytest.mark.asyncio
async def test_10_unavailable_agent_defers(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    unhealthy_transport = FakeTransport(available=False)
    router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=unhealthy_transport)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    receipt_out = router.route(env)
    assert receipt_out.decision == "DEFER"
    assert receipt_out.reason == "WORKER_UNAVAILABLE"

@pytest.mark.asyncio
async def test_11_path_traversal_message_id(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt, message_id="../../etc/passwd")
    with pytest.raises(ValueError, match="Path traversal"):
        router.route(env)

@pytest.mark.asyncio
async def test_12_capability_denied_by_agent(tmp_path: Any) -> None:
    intent = make_intent(allowed_mutations=("code", "os"))
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt, recipient_capability="unsupported_cap")
    receipt_out = router.route(env)
    assert receipt_out.decision == "BLOCK"
    assert "not in agent capabilities" in receipt_out.reason

@pytest.mark.asyncio
async def test_13_capability_denied_by_task_intent(tmp_path: Any) -> None:
    intent = make_intent(allowed_mutations=("code",))
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(tmp_path, intent, receipt)
    # Agent supports "test", but TaskIntent only allows ("code",)
    env = make_envelope({"cmd": "check"}, intent, receipt, recipient_capability="test")
    receipt_out = router.route(env)
    assert receipt_out.decision == "BLOCK"
    assert "denied by TaskIntent" in receipt_out.reason

@pytest.mark.asyncio
async def test_14_capability_denied_by_policy_receipt(tmp_path: Any) -> None:
    intent = make_intent(allowed_mutations=("code", "test"))
    receipt = make_receipt(intent=intent)
    router, _, _ = setup_test_router(
        tmp_path, intent, receipt, permitted_capabilities=["code"]
    )
    # PolicyReceipt explicitly permits only ["code"], envelope requests "test"
    env = make_envelope({"cmd": "check"}, intent, receipt, recipient_capability="test")
    receipt_out = router.route(env)
    assert receipt_out.decision == "BLOCK"
    assert "denied by PolicyReceipt" in receipt_out.reason

@pytest.mark.asyncio
async def test_15_missing_source_provenance(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, resolver, _ = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    # Set explicit incomplete provenance missing required keys
    resolver.provenance_store[env.run_id] = {"repository": "", "canonical_remote": "", "base_sha": ""}
    receipt_out = router.route(env)
    assert receipt_out.decision == "BLOCK"
    assert receipt_out.reason == "SOURCE_PROVENANCE_UNRESOLVED"

@pytest.mark.asyncio
async def test_16_correct_dynamic_provenance(tmp_path: Any) -> None:
    custom_sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    intent = make_intent(base_sha=custom_sha)
    receipt = make_receipt(intent=intent)
    transport = FakeTransport(True)
    router, _, _ = setup_test_router(
        tmp_path,
        intent,
        receipt,
        anti_transport=transport,
        provenance={"repository": "custom/repo", "canonical_remote": "origin", "base_sha": custom_sha},
    )
    env = make_envelope({"cmd": "check"}, intent, receipt)
    res = await router.dispatch(env)
    assert res.base_sha == custom_sha
    assert res.repository == "custom/repo"
    assert transport.last_request is not None
    assert transport.last_request["base_sha"] == custom_sha

@pytest.mark.asyncio
async def test_17_illegal_state_transition(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, store = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    store.save(env, "SUCCEEDED")
    # Transition from terminal SUCCEEDED back to RUNNING must fail
    with pytest.raises(IllegalStateTransitionError, match="ILLEGAL_STATE_TRANSITION"):
        router._transition(env, "RUNNING")

@pytest.mark.asyncio
async def test_18_persistence_restart_and_reload(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    _, _, store1 = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    store1.save(env, "ROUTED")

    # Recreate store on same directory (simulating process restart)
    store2 = PersistentStore(str(tmp_path))
    loaded_env = store2.load_envelope(env.message_id)
    assert loaded_env.message_id == env.message_id
    assert loaded_env.payload == env.payload
    assert store2.load_state(env.message_id) == "ROUTED"
    all_msgs = store2.load_messages()
    assert len(all_msgs) == 1
    assert all_msgs[0]["message_id"] == env.message_id

@pytest.mark.asyncio
async def test_19_persisted_payload_tamper_detected_on_replay(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    _, _, store = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt)
    store.save(env, "ROUTED")

    # Manually tamper with the stored file on disk
    file_path = os.path.join(str(tmp_path), f"{env.message_id}.json")
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["payload"]["cmd"] = "tampered_cmd"
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Reloading must detect payload digest mismatch
    with pytest.raises(ReplayMessageInvalidError, match="REPLAY_MESSAGE_INVALID"):
        store.load_envelope(env.message_id)
    with pytest.raises(ReplayMessageInvalidError, match="REPLAY_MESSAGE_INVALID"):
        store.load_messages()

@pytest.mark.asyncio
async def test_20_successful_cancellation(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    transport = FakeTransport(True)
    router, _, store = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)
    env = make_envelope({"cmd": "check"}, intent, receipt)

    router.route(env)
    router._transition(env, "DISPATCHED", operation_id="op-123")
    router._transition(env, "RUNNING", operation_id="op-123")

    router.cancel(env.run_id, env.task_id, env.attempt_id, env.correlation_id)
    assert store.load_state(env.message_id) == "CANCELLED"
    assert "op-123" in transport.cancelled_ops

@pytest.mark.asyncio
async def test_21_cross_run_cancellation_rejected(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, store = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt, run_id="run-A")
    store.save(env, "RUNNING")
    with pytest.raises(CrossRunViolationError, match="CROSS_RUN_CANCELLATION"):
        router.cancel("run-B", env.task_id, env.attempt_id, env.correlation_id)

@pytest.mark.asyncio
async def test_22_wrong_attempt_cancellation_rejected(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    router, _, store = setup_test_router(tmp_path, intent, receipt)
    env = make_envelope({"cmd": "check"}, intent, receipt, attempt_id="att-original")
    store.save(env, "RUNNING")
    with pytest.raises(StaleOrForeignAttemptError, match="STALE_OR_FOREIGN_ATTEMPT"):
        router.cancel(env.run_id, env.task_id, "att-wrong", env.correlation_id)

@pytest.mark.asyncio
async def test_23_real_timeout_terminates_and_cancels(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    slow_transport = FakeTransport(True, slow_seconds=3.0, cancel_confirmation=True)
    router, _, store = setup_test_router(tmp_path, intent, receipt, anti_transport=slow_transport)
    env = make_envelope({"cmd": "check"}, intent, receipt)

    with pytest.raises(asyncio.TimeoutError, match="WORK_TIMEOUT"):
        await router.dispatch(env)

    assert store.load_state(env.message_id) == "TIMED_OUT"
    assert len(slow_transport.cancelled_ops) > 0

@pytest.mark.asyncio
async def test_23b_timeout_unconfirmed_cancellation_blocks(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    # Transport that cannot confirm termination (orphan execution danger)
    unconfirmed_transport = FakeTransport(True, slow_seconds=3.0, cancel_confirmation=False)
    router, _, store = setup_test_router(tmp_path, intent, receipt, anti_transport=unconfirmed_transport)
    env = make_envelope({"cmd": "check"}, intent, receipt)

    with pytest.raises(TimeoutCancellationUnconfirmedError, match="TIMEOUT_CANCELLATION_UNCONFIRMED"):
        await router.dispatch(env)

    assert store.load_state(env.message_id) == "BLOCKED"

def test_24_retry_classification_non_retryable() -> None:
    for exc_cls in (
        AuthorityInvalidError,
        PolicyDeniedError,
        MessageTamperedError,
        UnsupportedMessageTypeError,
        EffectClassEscalationError,
        StopBoundaryEscalationError,
        CrossRunViolationError,
        WorkerResultIdentityMismatchError,
        CapabilityNotAuthorizedError,
        SourceProvenanceUnresolvedError,
        SourceProvenanceMismatchError,
        TimeoutCancellationUnconfirmedError,
        ReplayMessageInvalidError,
        StaleOrForeignAttemptError,
        IllegalStateTransitionError,
    ):
        assert exc_cls in NON_RETRYABLE_EXCEPTIONS

@pytest.mark.asyncio
async def test_24b_retry_loop_with_budget_and_attempt_increment(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)

    class FlakyTransport(FakeTransport):
        def __init__(self, available: bool = True) -> None:
            super().__init__(available)
            self.attempt_calls = 0

        def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
            self.attempt_calls += 1
            if self.attempt_calls == 1:
                raise RuntimeError("temporary transport blip")
            return super().dispatch(request, timeout)

    flaky_t = FlakyTransport(True)
    router, _, store = setup_test_router(tmp_path, intent, receipt, anti_transport=flaky_t)
    env = make_envelope({"cmd": "retry_me"}, intent, receipt, attempt_id="att-orig")
    object.__setattr__(env, "max_retries", 2)
    object.__setattr__(env, "retry_count", 0)

    def mock_eval(envelope, next_attempt_id):
        new_receipt = make_receipt(intent=intent)
        router.authority_resolver.receipt_store[(envelope.run_id, envelope.task_id)] = new_receipt
        return new_receipt
    router.authority_resolver.evaluate_fresh_policy = mock_eval

    res = await router.dispatch(env)
    assert res is not None
    assert flaky_t.attempt_calls == 2
    assert "retry-1" in res.attempt_id

    # Exhausted retry budget fails closed
    flaky_t2 = FlakyTransport(True)
    router2, _, _ = setup_test_router(tmp_path / "t2", intent, receipt, anti_transport=flaky_t2)
    env2 = make_envelope({"cmd": "fail_fast"}, intent, receipt)
    object.__setattr__(env2, "max_retries", 0)
    object.__setattr__(env2, "retry_count", 0)
    with pytest.raises(RuntimeError, match="temporary transport blip"):
        await router2.dispatch(env2)
    assert flaky_t2.attempt_calls == 1

@pytest.mark.asyncio
async def test_25_safe_fallback_with_policy_recheck(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    unhealthy_anti = FakeTransport(available=False)
    healthy_codex = FakeTransport(available=True)
    router, _, _ = setup_test_router(
        tmp_path,
        intent,
        receipt,
        anti_transport=unhealthy_anti,
        codex_transport=healthy_codex,
    )
    env = make_envelope({"cmd": "check"}, intent, receipt, recipient_agent="anti")

    def mock_eval(envelope, next_attempt_id):
        new_receipt = make_receipt(intent=intent)
        router.authority_resolver.receipt_store[(envelope.run_id, envelope.task_id)] = new_receipt
        return new_receipt
    router.authority_resolver.evaluate_fresh_policy = mock_eval

    res = await router.dispatch(env, fallback_candidate="codex")
    assert res.worker_id == "codex"
    assert healthy_codex.dispatch_count == 1
    assert unhealthy_anti.dispatch_count == 0

@pytest.mark.asyncio
async def test_26_stale_previous_attempt_result_rejected(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    env = make_envelope({"cmd": "check"}, intent, receipt)

    class LateTransport(FakeTransport):
        def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
            store.save(env, "TIMED_OUT")
            return super().dispatch(request, timeout)

    late_t = LateTransport(True)
    router, _, store = setup_test_router(tmp_path, intent, receipt, anti_transport=late_t)

    with pytest.raises(StaleResultError, match="STALE_RESULT"):
        await router.dispatch(env)

@pytest.mark.asyncio
async def test_27_worker_result_identity_mismatches(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)

    class CorruptTransport(FakeTransport):
        def __init__(self, field_to_corrupt: str) -> None:
            super().__init__(True)
            self.field = field_to_corrupt

        def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
            bundle = super().dispatch(request, timeout)
            if self.field == "task_id":
                return replace(bundle, task_id="mismatched-task")
            if self.field == "attempt_id":
                return replace(bundle, attempt_id="mismatched-att")
            if self.field == "intent_digest":
                return replace(bundle, intent_digest="mismatched-digest")
            if self.field == "worker_id":
                return replace(bundle, worker_id="wrong_worker")
            if self.field == "base_sha":
                return replace(bundle, base_sha="mismatched-sha")
            if self.field == "repository":
                return replace(bundle, repository="mismatched-repo")
            return bundle

    for field_name in ("task_id", "attempt_id", "intent_digest", "worker_id", "base_sha", "repository"):
        corrupt_t = CorruptTransport(field_name)
        router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=corrupt_t)
        env = make_envelope({"cmd": "check"}, intent, receipt)
        with pytest.raises(WorkerResultIdentityMismatchError, match="WORKER_RESULT_IDENTITY_MISMATCH"):
            await router.dispatch(env)

@pytest.mark.asyncio
async def test_28_crash_safe_effect_replay(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    transport = FakeTransport(True)
    router, _, store = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)
    env = make_envelope({"cmd": "commit_effect"}, intent, receipt)

    # Inject real crash right after effect receipt is committed
    original_save = store.save_effect_receipt
    def crashing_save(op_id, data):
        original_save(op_id, data)
        raise RuntimeError("SIMULATED_HARD_CRASH")
    store.save_effect_receipt = crashing_save

    # 1. First dispatch crashes immediately after saving effect receipt
    with pytest.raises(RuntimeError, match="SIMULATED_HARD_CRASH"):
        await router.dispatch(env)

    assert transport.dispatch_count == 1

    # State in store is STILL "RUNNING" because crash happened before _transition("SUCCEEDED")
    assert store.load_state(env.message_id) == "RUNNING"

    # 3. Simulate restart with new router on same persistent store
    # Unpatch the crash for the replay
    router2, _, store2 = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)

    # 4. Re-dispatch reconciles effect receipt without re-executing effect!
    res2 = await router2.dispatch(env)
    # INVARIANT: WORKER_EFFECT_EXECUTION_COUNT == 1
    assert transport.dispatch_count == 1
    # res2 should be the fully deserialized result from the receipt
    assert res2.worker_id == "anti"
    assert store2.load_state(env.message_id) == "SUCCEEDED"

@pytest.mark.asyncio
async def test_29_result_collector_and_verified_result_integration(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    transport = FakeTransport(True)
    router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)
    env = make_envelope({"cmd": "verify_claim"}, intent, receipt)

    worker_bundle = await router.dispatch(env)
    assert worker_bundle.task_id == intent.task_id

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    bundle_json = canonical_serialize_worker_result(worker_bundle)

    collector = ResultCollector()
    ne = collector.collect(bundle_json, evidence_root, intent)
    assert ne.task_id == intent.task_id
    assert ne.integrity_verified is True

    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-12T00:00:00Z")
    assert isinstance(vr, VerifiedResult)
    assert vr.task_id == intent.task_id
    assert vr.status == Status.PASS

@pytest.mark.asyncio
async def test_30_astra_proposal_loop_and_negative_escalation(tmp_path: Any) -> None:
    intent = make_intent(
        task_id="task-astra-30",
        stop_boundary=StopBoundary.LOCAL_DIFF,
        allowed_mutations=("code", "REPOSITORY_WRITE"),
    )
    i_dig = intent_digest(intent)

    eff_pol = EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="eff-pol-30",
        task_id=intent.task_id,
        intent_digest=i_dig,
        intent_revision=1,
        source_base_sha=intent.source_base_sha,
        subject_sha="subj-30",
        status=EffectivePolicyStatus.COMPLETE,
        policy_sources=(),
        task_policy=TaskPolicyAttribution(
            task_id=intent.task_id,
            intent_revision=1,
            intent_digest=i_dig,
            source_base_sha=intent.source_base_sha,
            constraints=(),
            allowed_mutations=intent.allowed_mutations,
            forbidden_mutations=intent.forbidden_mutations,
            stop_boundary="LOCAL_DIFF",
            source_id="src-30",
        ),
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="src-30",
    )

    wp_dict = {
        "schema_version": WORK_PROFILE_SCHEMA_VERSION,
        "profile_id": "wp-30",
        "profile_version": 1,
        "preferred_worker_model": "flash",
        "preferred_verifier_model": "pro",
        "preferred_supervisor_model": "pro",
        "escalation_model": "pro",
        "allowed_task_classes": ["BOUNDED_IMPLEMENTATION"],
        "maximum_autonomy_level": "LEVEL_2_DEV_AUTONOMY",
        "allowed_effect_classes": ["READ_ONLY", "REPOSITORY_WRITE"],
        "forbidden_effect_classes": ["DEPLOY"],
        "allowed_targets": ["LOCAL"],
        "required_validators": ["tests"],
        "promotion_thresholds": {
            "required_successful_runs": 3,
            "allowed_critical_failures": 0,
            "require_rollback_verified": False,
        },
        "budget_limits": {
            "max_supervisor_decisions": 10,
            "max_child_tasks": 5,
            "max_retries_per_task": 2,
            "max_fix_cycles_per_task": 2,
            "max_consecutive_failures": 2,
            "max_provider_calls": 20,
            "max_policy_denials": 3,
        },
        "production_execution_allowed": False,
        "vector_mutation_allowed": False,
        "secret_mutation_allowed": False,
        "external_send_allowed": False,
    }
    work_profile = validate_work_profile(wp_dict)

    bs_dict = {
        "budget_id": "bud-30",
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
    b_dig = compute_deterministic_digest(bs_dict)
    budget_state = AutonomyBudgetState(
        schema_version=AUTONOMY_BUDGET_STATE_SCHEMA_VERSION,
        budget_id="bud-30",
        budget_digest=b_dig,
        decisions_used=0,
        child_tasks_used=0,
        retries_used=0,
        fix_cycles_used=0,
        consecutive_failures=0,
        provider_calls_used=0,
        policy_denials=0,
        exhausted_dimensions=(),
    )

    auto_state = AutonomyState(
        schema_version=AUTONOMY_STATE_SCHEMA_VERSION,
        run_id="run-30",
        profile_id=work_profile.profile_id,
        profile_digest=work_profile.profile_digest,
        current_level=AutonomyLevel.LEVEL_1_LOCAL_WRITE,
        maximum_allowed_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY,
        successful_runs=0,
        critical_failures=0,
        rollback_verified=False,
        required_validators_status={},
        budget_state_digest=b_dig,
        promotion_sequence=0,
        last_transition_receipt_id=None,
        created_at_utc="2026-09-12T00:00:00Z",
        updated_at_utc="2026-09-12T00:00:00Z",
        state_digest="a-dig-30",
    )

    # Negative case: Astra proposes DEPLOY -> PolicyEngine DENY
    req_deny = PolicyRequest(
        schema_version="hermes.policy-request.v1",
        request_id="req-deny-30",
        run_id="run-30",
        task_id=intent.task_id,
        attempt_id="att-30",
        intent_digest=i_dig,
        decision_id="dec-deny-30",
        decision_receipt_id="drec-deny-30",
        work_profile_id=work_profile.profile_id,
        work_profile_digest=work_profile.profile_digest,
        current_autonomy_level=auto_state.current_level,
        requested_action="DEPLOY",
        requested_effect_classes=(EffectClass.DEPLOY,),
        requested_stop_boundary=StopBoundary.DEPLOY,
        execution_target=ExecutionTarget.LOCAL,
        effective_policy_id=eff_pol.effective_policy_id,
        effective_policy_digest=eff_pol.effective_policy_id,
        budget_state_digest=budget_state.budget_digest,
    )
    receipt_deny = evaluate_policy(req_deny, intent, eff_pol, work_profile, auto_state, budget_state)
    assert receipt_deny.verdict == PolicyVerdict.DENY

    transport = FakeTransport(True)
    router, _, _ = setup_test_router(tmp_path, intent, receipt_deny, anti_transport=transport)
    env_deny = make_envelope({"cmd": "deploy"}, intent, receipt_deny, effect_class=EffectClass.REPOSITORY_WRITE)

    with pytest.raises(PolicyDeniedError, match="POLICY_DENIED"):
        await router.dispatch(env_deny)
    assert transport.dispatch_count == 0

    # Positive case: Astra proposes REPOSITORY_WRITE within bounds -> PolicyEngine ALLOW
    req_allow = PolicyRequest(
        schema_version="hermes.policy-request.v1",
        request_id="req-allow-30",
        run_id="run-30",
        task_id=intent.task_id,
        attempt_id="att-30",
        intent_digest=i_dig,
        decision_id="dec-allow-30",
        decision_receipt_id="drec-allow-30",
        work_profile_id=work_profile.profile_id,
        work_profile_digest=work_profile.profile_digest,
        current_autonomy_level=auto_state.current_level,
        requested_action="WRITE",
        requested_effect_classes=(EffectClass.REPOSITORY_WRITE,),
        requested_stop_boundary=StopBoundary.LOCAL_DIFF,
        execution_target=ExecutionTarget.LOCAL,
        effective_policy_id=eff_pol.effective_policy_id,
        effective_policy_digest=eff_pol.effective_policy_id,
        budget_state_digest=budget_state.budget_digest,
    )
    receipt_allow = evaluate_policy(req_allow, intent, eff_pol, work_profile, auto_state, budget_state)
    assert receipt_allow.verdict == PolicyVerdict.ALLOW

    router_allow, _, _ = setup_test_router(tmp_path / "allow", intent, receipt_allow, anti_transport=transport)
    env_allow = make_envelope({"cmd": "write"}, intent, receipt_allow, effect_class=EffectClass.REPOSITORY_WRITE)
    res_allow = await router_allow.dispatch(env_allow)
    assert res_allow is not None
    assert transport.dispatch_count == 1

@pytest.mark.asyncio
async def test_31_full_multi_agent_e2e_loop(tmp_path: Any) -> None:
    # Full repository-component path:
    # TaskIntent -> Persistent Supervisor -> PolicyEngine -> persisted PolicyReceipt ->
    # trusted AuthorityResolver (reading from store) -> AgentEnvelope -> CrossAgentRouter ->
    # FakeTransport -> WorkerResultBundle -> ResultCollector -> VerifiedResult -> SupervisorLoop.ingest_verified_result
    sup_store = FileSupervisorStateStore(tmp_path / "supervisor_state")
    sup_loop = SupervisorLoop(sup_store)

    intent = make_intent(
        task_id="task-e2e-real",
        stop_boundary=StopBoundary.LOCAL_DIFF,
        allowed_mutations=("code", "REPOSITORY_WRITE"),
    )
    lineage = TaskLineage(
        schema_version=LINEAGE_SCHEMA_VERSION,
        nodes=(LineageNode(node_id=intent.task_id, kind=NodeKind.TASK),),
        edges=(),
    )

    state = sup_loop.initialize_run(
        intent=intent,
        lineage=lineage,
        root_goal="E2E test run",
        root_goal_id="run-e2e-real",
        created_at_utc="2026-09-12T00:00:00Z",
    )

    from ai_engineering.supervisor.policy.work_profile import WorkProfile
    from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus
    from ai_engineering.supervisor.policy.work_profile import validate_work_profile
    wp = validate_work_profile({
        "schema_version": "hermes.work-profile.v1",
        "profile_id": "wp-123",
        "profile_version": 1,
        "allowed_targets": ["LOCAL"],
        "allowed_effect_classes": ["READ_ONLY", "REPOSITORY_WRITE"],
        "forbidden_effect_classes": [],
        "preferred_worker_model": "test-model",
        "preferred_verifier_model": "test-model",
        "preferred_supervisor_model": "test-model",
        "escalation_model": "test-model",
        "allowed_task_classes": ["test"],
        "maximum_autonomy_level": "LEVEL_2_DEV_AUTONOMY",
        "required_validators": [],
        "promotion_thresholds": {
            "required_successful_runs": 0,
            "allowed_critical_failures": 0,
            "require_rollback_verified": False
        },
        "budget_limits": {
            "max_supervisor_decisions": None,
            "max_child_tasks": None,
            "max_retries_per_task": None,
            "max_fix_cycles_per_task": None,
            "max_consecutive_failures": None,
            "max_provider_calls": None,
            "max_policy_denials": None
        },
        "production_execution_allowed": False,
        "vector_mutation_allowed": False,
        "secret_mutation_allowed": False,
        "external_send_allowed": False,
    })
    from ai_engineering.effective_policy import resolve_effective_policy, CANONICAL_SOURCE_MAP_PATH, CANONICAL_INVARIANTS_PATH, CANONICAL_RELEASE_GATES_PATH, CANONICAL_RELEASE_GATE_MODULE_PATH, EffectivePolicyValidationError
    def make_mock_git_reader():
        store = {
            CANONICAL_SOURCE_MAP_PATH: b"map",
            CANONICAL_INVARIANTS_PATH: b"inv",
            CANONICAL_RELEASE_GATES_PATH: b"gates",
            CANONICAL_RELEASE_GATE_MODULE_PATH: b"module",
        }
        def reader(subject_sha: str, path: str) -> bytes:
            if path in store:
                return store[path]
            raise EffectivePolicyValidationError("SOURCE_NOT_FOUND")
        return reader

    ep = resolve_effective_policy(intent, str(tmp_path), "1234567890abcdef1234567890abcdef12345678", git_reader=make_mock_git_reader())
    from ai_engineering.supervisor.policy.contracts import AutonomyLevel
    state = sup_loop.bind_profile("run-e2e-real", profile=wp, effective_policy=ep, created_at_utc="2026-09-12T00:00:00Z", initial_level=AutonomyLevel.LEVEL_2_DEV_AUTONOMY)

    trusted_resolver = AuthorityResolver(
        supervisor_store=sup_store,
        supervisor_loop=sup_loop,
    )
    # Using the standard loop logic to evaluate policy and generate the event
    trusted_resolver.intent_store[("run-e2e-real", intent.task_id)] = intent

    # We create a dummy receipt just to initialize the envelope
    dummy_receipt = make_receipt(intent=intent)
    dummy_env = make_envelope(
        {"cmd": "e2e"},
        intent,
        dummy_receipt,
        run_id="run-e2e-real",
        attempt_id=state.current_attempt_id,
        recipient_agent="anti",
        recipient_capability="code",
        effect_class=EffectClass.REPOSITORY_WRITE
    )
    receipt = trusted_resolver.evaluate_fresh_policy(dummy_env, state.current_attempt_id)
    if receipt.verdict != "ALLOW":
        print(f"Receipt DENY: {receipt.reason_codes}")


    transport = FakeTransport(True)
    registry = AgentRegistry()
    registry.register(
        AgentDefinition(
            agent_id="anti",
            capabilities=["code", "test"],
            supported_message_types=[MessageType.WORK_REQUEST],
            allowed_effect_classes=[EffectClass.READ_ONLY, EffectClass.REPOSITORY_WRITE],
            timeout_seconds=2,
        ),
        AntigravityAdapter(transport),
    )
    router_store = PersistentStore(str(tmp_path / "router_store"))
    router = CrossAgentRouter(registry, trusted_resolver, router_store)

    env = make_envelope(
        {"cmd": "e2e_execute"},
        intent,
        receipt,
        run_id="run-e2e-real",
        attempt_id=state.current_attempt_id,
        recipient_agent="anti",
        recipient_capability="code",
        effect_class=EffectClass.REPOSITORY_WRITE,
    )

    # Dispatch through CrossAgentRouter
    worker_bundle = await router.dispatch(env)
    assert transport.dispatch_count == 1
    assert worker_bundle.task_id == intent.task_id

    # Pipeline: WorkerResultBundle -> ResultCollector -> VerifiedResult
    evidence_dir = tmp_path / "evidence_e2e"
    evidence_dir.mkdir()
    bundle_json = canonical_serialize_worker_result(worker_bundle)
    ne = ResultCollector().collect(bundle_json, evidence_dir, intent)
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-12T00:02:00Z")
    assert vr.status == Status.PASS

    # Ingest VerifiedResult into SupervisorLoop
    vr_digest = hashlib.sha256(json.dumps(asdict(vr), sort_keys=True).encode("utf-8")).hexdigest()
    updated_state = sup_loop.ingest_verified_result("run-e2e-real", vr, vr_digest)
    assert updated_state.latest_verified_result_id == vr.result_id
    assert updated_state.phase.value == "VERIFYING"

@pytest.mark.asyncio
async def test_32_computer_use_requires_valid_policy_receipt(tmp_path: Any) -> None:
    intent = make_intent(allowed_mutations=("code", "os"))
    receipt = make_receipt(intent=intent)
    comp_transport = FakeTransport(True)
    router, resolver, _ = setup_test_router(tmp_path, intent, receipt, comp_transport=comp_transport)

    env_no_rcpt = make_envelope(
        {"cmd": "click"}, intent, receipt, recipient_agent="comp", recipient_capability="os"
    )
    object.__setattr__(env_no_rcpt, "policy_receipt_id", "")
    with pytest.raises(PolicyDeniedError, match="POLICY_DENIED"):
        await router.dispatch(env_no_rcpt)

    unhealthy_comp = FakeTransport(available=False)
    adapter = ComputerUseAdapter(unhealthy_comp)
    env_healthy = make_envelope(
        {"cmd": "click"}, intent, receipt, recipient_agent="comp", recipient_capability="os"
    )
    provenance = resolver.get_provenance(env_healthy.run_id, env_healthy.task_id)
    with pytest.raises(ComputerUseTransportUnavailableError, match="COMPUTER_USE_TRANSPORT_UNAVAILABLE"):
        adapter.dispatch(env_healthy, timeout=2, provenance=provenance, operation_id="op-1")

@pytest.mark.asyncio
async def test_33_provenance_mismatch_fails_closed(tmp_path: Any) -> None:
    intent = make_intent(source_repository="life2boat/hermes", base_sha="54e404eab378b6a5a8ca7c9d3bf8bed01810ec35")
    receipt = make_receipt(intent=intent)
    transport = FakeTransport(True)
    router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)
    env = make_envelope({"cmd": "check"}, intent, receipt)

    # 1. Caller attempts to override repository
    with pytest.raises(SourceProvenanceMismatchError, match="SOURCE_PROVENANCE_MISMATCH"):
        await router.dispatch(env, caller_provenance={"repository": "attacker/repo"})

    # 2. Caller attempts to override base_sha
    with pytest.raises(SourceProvenanceMismatchError, match="SOURCE_PROVENANCE_MISMATCH"):
        await router.dispatch(env, caller_provenance={"base_sha": "0000000000000000000000000000000000000000"})

    # 3. Payload specifies mismatched repository
    env_tampered_payload = make_envelope({"cmd": "check", "repository": "attacker/repo"}, intent, receipt)
    with pytest.raises(SourceProvenanceMismatchError, match="SOURCE_PROVENANCE_MISMATCH"):
        await router.dispatch(env_tampered_payload)

    assert transport.dispatch_count == 0

@pytest.mark.asyncio
async def test_34_effect_class_and_stop_boundary_independence(tmp_path: Any) -> None:
    intent = make_intent(
        stop_boundary=StopBoundary.LOCAL_DIFF,
        allowed_mutations=("code", "REPOSITORY_WRITE"),
    )
    receipt = make_receipt(intent=intent)
    transport = FakeTransport(True)
    router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)

    # Case 1: Stop boundary is compliant (LOCAL_DIFF <= LOCAL_DIFF), but effect class is escalated (DEPLOY not allowed)
    env_escalated_effect = make_envelope(
        {"cmd": "deploy"},
        intent,
        receipt,
        effect_class=EffectClass.DEPLOY,
        stop_boundary=StopBoundary.LOCAL_DIFF,
    )
    with pytest.raises(EffectClassEscalationError, match="EFFECT_CLASS_ESCALATION"):
        router.route(env_escalated_effect)

    # Case 2: Effect class is compliant (READ_ONLY), but stop boundary is escalated (DEPLOY > LOCAL_DIFF)
    env_escalated_stop = make_envelope(
        {"cmd": "read"},
        intent,
        receipt,
        effect_class=EffectClass.READ_ONLY,
        stop_boundary=StopBoundary.DEPLOY,
    )
    with pytest.raises(StopBoundaryEscalationError, match="STOP_BOUNDARY_ESCALATION"):
        router.route(env_escalated_stop)

    # Case 3: Effect class is explicitly forbidden
    intent_forbidden = make_intent(
        stop_boundary=StopBoundary.LOCAL_DIFF,
        allowed_mutations=("REPOSITORY_WRITE",),
    )
    object.__setattr__(intent_forbidden, "forbidden_mutations", ("GIT_COMMIT",))
    receipt_f = make_receipt(intent=intent_forbidden)
    router_f, _, _ = setup_test_router(tmp_path / "f", intent_forbidden, receipt_f, anti_transport=transport)
    env_forbidden = make_envelope(
        {"cmd": "commit"},
        intent_forbidden,
        receipt_f,
        effect_class=EffectClass.GIT_COMMIT,
        stop_boundary=StopBoundary.LOCAL_DIFF,
    )
    with pytest.raises(EffectClassEscalationError, match="EFFECT_CLASS_ESCALATION"):
        router_f.route(env_forbidden)

    # Case 4: Both effect class and stop boundary compliant -> succeeds
    env_valid = make_envelope(
        {"cmd": "write"},
        intent,
        receipt,
        effect_class=EffectClass.REPOSITORY_WRITE,
        stop_boundary=StopBoundary.LOCAL_DIFF,
    )
    receipt_out = router.route(env_valid)
    assert receipt_out.decision == "ROUTE"

@pytest.mark.asyncio
async def test_35_trusted_supervisor_store_authority_lookup(tmp_path: Any) -> None:
    sup_store = FileSupervisorStateStore(tmp_path / "sup_store")
    sup_loop = SupervisorLoop(sup_store)

    intent = make_intent(task_id="task-store-35", base_sha="54e404eab378b6a5a8ca7c9d3bf8bed01810ec35")
    lineage = TaskLineage(
        schema_version=LINEAGE_SCHEMA_VERSION,
        nodes=(LineageNode(node_id=intent.task_id, kind=NodeKind.TASK),),
        edges=(),
    )

    state = sup_loop.initialize_run(
        intent=intent,
        lineage=lineage,
        root_goal="Store authority test",
        root_goal_id="run-store-35",
        created_at_utc="2026-09-12T00:00:00Z",
    )

    receipt = make_receipt(receipt_id="rcpt-store-35", intent=intent, verdict=PolicyVerdict.ALLOW)
    event = create_event(
        run_id="run-store-35",
        sequence=2,
        previous_event_digest=sup_store.load_events("run-store-35")[-1].event_digest,
        event_type=SupervisorEventType.POLICY_EVALUATED,
        state_revision=state.state_revision + 1,
        task_id=intent.task_id,
        attempt_id=state.current_attempt_id,
        intent_digest=intent_digest(intent),
        payload={
            "receipt_id": receipt.receipt_id,
            "verdict": receipt.verdict.value,
            "reason_codes": list(receipt.reason_codes),
            "policy_receipt": asdict(receipt),
        },
        created_at_utc="2026-09-12T00:01:00Z",
    )
    sup_store.save_event(event)

    resolver = AuthorityResolver(supervisor_store=sup_store, supervisor_loop=sup_loop)

    # Resolves TaskIntent from supervisor loop / store
    res_intent = resolver.resolve_task_intent(
        intent.task_id, intent_digest(intent), "run-store-35", intent.task_id
    )
    assert res_intent.task_id == intent.task_id

    # Resolves PolicyReceipt from supervisor events
    res_receipt = resolver.resolve_policy_receipt(
        receipt.receipt_id, compute_policy_receipt_digest(receipt), "run-store-35", intent.task_id
    )
    assert res_receipt.receipt_id == receipt.receipt_id
    assert res_receipt.verdict == PolicyVerdict.ALLOW

    # Resolves Provenance from supervisor state
    prov = resolver.get_provenance("run-store-35", intent.task_id)
    assert prov["repository"] == intent.source_repository
    assert prov["base_sha"] == intent.source_base_sha
