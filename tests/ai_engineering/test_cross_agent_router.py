from __future__ import annotations
import os
import json
import uuid
import hashlib
import asyncio
import pytest
from typing import Any, Mapping, Sequence
from dataclasses import replace

from ai_engineering.contracts import EffectClass, StopBoundary, Status
from ai_engineering.task_intent import TaskIntent, deserialize_intent, intent_digest
from ai_engineering.supervisor.policy.contracts import PolicyReceipt, PolicyVerdict
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
    ReplayMessageInvalidError,
    StaleOrForeignAttemptError,
    PolicyDeniedError,
    AgentTransportUnavailableError,
    ComputerUseTransportUnavailableError,
    NON_RETRYABLE_EXCEPTIONS,
)

# ── Test Helpers ───────────────────────────────────────────────────────────

class FakeTransport:
    def __init__(self, available: bool = True, slow_seconds: float = 0.0) -> None:
        self.available = available
        self.slow_seconds = slow_seconds
        self.dispatch_count = 0
        self.cancelled_ops: list[str] = []
        self.last_request: dict[str, Any] | None = None

    def health(self) -> bool:
        return self.available

    def cancel(self, operation_id: str) -> None:
        self.cancelled_ops.append(operation_id)

    def dispatch(self, request: Mapping[str, Any], timeout: int) -> WorkerResultBundle:
        self.dispatch_count += 1
        self.last_request = dict(request)
        if self.slow_seconds > 0:
            import time
            time.sleep(self.slow_seconds)
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
    slow_transport = FakeTransport(True, slow_seconds=3.0)
    router, _, store = setup_test_router(tmp_path, intent, receipt, anti_transport=slow_transport)
    env = make_envelope({"cmd": "check"}, intent, receipt)

    with pytest.raises(asyncio.TimeoutError, match="WORK_TIMEOUT"):
        await router.dispatch(env)

    assert store.load_state(env.message_id) == "TIMED_OUT"
    assert len(slow_transport.cancelled_ops) > 0

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
        ReplayMessageInvalidError,
        StaleOrForeignAttemptError,
        IllegalStateTransitionError,
    ):
        assert exc_cls in NON_RETRYABLE_EXCEPTIONS

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
    res = await router.dispatch(env, fallback_candidate="codex")
    assert res.worker_id == "codex"
    assert healthy_codex.dispatch_count == 1
    assert unhealthy_anti.dispatch_count == 0

@pytest.mark.asyncio
async def test_26_stale_previous_attempt_result_rejected(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    env = make_envelope({"cmd": "check"}, intent, receipt)

    # Late result arrival where store is transitioned to TIMED_OUT mid-dispatch
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
    router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)
    env = make_envelope({"cmd": "commit_effect"}, intent, receipt)

    # First dispatch commits effect and succeeds
    res1 = await router.dispatch(env)
    assert transport.dispatch_count == 1

    # Re-dispatching the exact same envelope (after restart / replay) must return cached result
    # without re-dispatching to worker (idempotency guarantee)
    res2 = await router.dispatch(env)
    assert transport.dispatch_count == 1
    assert res2.result_id == res1.result_id

@pytest.mark.asyncio
async def test_29_result_collector_and_verified_result_integration(tmp_path: Any) -> None:
    intent = make_intent()
    receipt = make_receipt(intent=intent)
    transport = FakeTransport(True)
    router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)
    env = make_envelope({"cmd": "verify_claim"}, intent, receipt)

    worker_bundle = await router.dispatch(env)
    assert worker_bundle.task_id == intent.task_id

    # Pipeline integration: WorkerResultBundle -> ResultCollector -> validate_normalized_evidence -> VerifiedResult
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
    # Negative authority test:
    # TaskIntent is strictly bounded to LOCAL_DIFF
    intent = make_intent(stop_boundary=StopBoundary.LOCAL_DIFF)

    # Astra proposes DEPLOY -> PolicyEngine rejects and issues DENY verdict
    denied_receipt = make_receipt(intent=intent, verdict=PolicyVerdict.DENY)

    transport = FakeTransport(True)
    router, _, _ = setup_test_router(tmp_path, intent, denied_receipt, anti_transport=transport)
    env = make_envelope({"cmd": "deploy_to_production"}, intent, denied_receipt)

    # Must fail closed with PolicyDeniedError and zero worker dispatches
    with pytest.raises(PolicyDeniedError, match="POLICY_DENIED"):
        await router.dispatch(env)

    assert transport.dispatch_count == 0

@pytest.mark.asyncio
async def test_31_full_multi_agent_e2e_loop(tmp_path: Any) -> None:
    # 1. Authoritative TaskIntent
    intent = make_intent(task_id="task-e2e-100", stop_boundary=StopBoundary.LOCAL_DIFF)

    # 2. Astra proposal approved by policy
    receipt = make_receipt(receipt_id="rcpt-e2e-100", intent=intent, verdict=PolicyVerdict.ALLOW)

    # 3. AgentEnvelope constructed referencing authority
    env = make_envelope({"objective": "e2e flow"}, intent, receipt)

    # 4. CrossAgentRouter verifies authority and routes to Antigravity
    transport = FakeTransport(True)
    router, _, _ = setup_test_router(tmp_path, intent, receipt, anti_transport=transport)

    # 5. Worker execution
    worker_bundle = await router.dispatch(env)
    assert transport.dispatch_count == 1

    # 6. ResultCollector validates untrusted worker output
    evidence_dir = tmp_path / "e2e_evidence"
    evidence_dir.mkdir()
    bundle_json = canonical_serialize_worker_result(worker_bundle)
    ne = ResultCollector().collect(bundle_json, evidence_dir, intent)

    # 7. Deterministic verification to produce VerifiedResult
    vr = validate_normalized_evidence(ne, intent, verified_at_utc="2026-09-12T00:00:00Z")
    assert vr.status == Status.PASS
    assert vr.task_id == intent.task_id
    assert vr.intent_digest == intent_digest(intent)

@pytest.mark.asyncio
async def test_32_computer_use_requires_valid_policy_receipt(tmp_path: Any) -> None:
    intent = make_intent(allowed_mutations=("code", "os"))
    receipt = make_receipt(intent=intent)
    comp_transport = FakeTransport(True)
    router, resolver, _ = setup_test_router(tmp_path, intent, receipt, comp_transport=comp_transport)

    # Computer use without policy receipt ID must be blocked with PolicyDeniedError
    env_no_rcpt = make_envelope(
        {"cmd": "click"}, intent, receipt, recipient_agent="comp", recipient_capability="os"
    )
    object.__setattr__(env_no_rcpt, "policy_receipt_id", "")
    with pytest.raises(PolicyDeniedError, match="POLICY_DENIED"):
        await router.dispatch(env_no_rcpt)

    # ComputerUseAdapter direct dispatch with unavailable transport must raise ComputerUseTransportUnavailableError
    unhealthy_comp = FakeTransport(available=False)
    adapter = ComputerUseAdapter(unhealthy_comp)
    env_healthy = make_envelope(
        {"cmd": "click"}, intent, receipt, recipient_agent="comp", recipient_capability="os"
    )
    provenance = resolver.get_provenance(env_healthy.run_id, env_healthy.task_id)
    with pytest.raises(ComputerUseTransportUnavailableError, match="COMPUTER_USE_TRANSPORT_UNAVAILABLE"):
        adapter.dispatch(env_healthy, timeout=2, provenance=provenance)
