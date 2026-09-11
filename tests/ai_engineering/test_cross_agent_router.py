import pytest
import json
import hashlib
import uuid
import datetime
from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.supervisor.router import (
    AgentEnvelope, MessageType, CrossAgentRouter, AgentRegistry, AgentDefinition,
    AntigravityAdapter, ComputerUseAdapter, AuthorityResolver, PersistentStore,
    MessageTamperedError, AuthorityInvalidError, EffectClassEscalationError,
    StopBoundaryEscalationError, AgentTransportUnavailableError, PolicyDeniedError,
    CrossRunViolationError, UnsupportedMessageTypeError
)

class FakeTransport:
    def __init__(self, available=True):
        self.available = available
    def health(self):
        return self.available
    def cancel(self, op_id):
        pass
    def dispatch(self, req, timeout):
        from ai_engineering.supervisor.worker_result import WorkerResultBundle
        return WorkerResultBundle(
            schema_version="v", result_id="r", task_id="t", attempt_id="a",
            worker_id="w", base_sha="b", head_sha="h", canonical_remote="c",
            repository="r", intent_digest="i", produced_at_utc="u", artifacts=(), gate_claims=()
        )

def make_envelope(payload, effect_class=EffectClass.READ_ONLY, stop_boundary=StopBoundary.LOCAL_DIFF, msg_type=MessageType.WORK_REQUEST):
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    digest = hashlib.sha256(canonical).hexdigest()
    return AgentEnvelope(
        message_id="msg1", correlation_id="cor1", causation_id="caus1",
        run_id="valid_run", task_id="task1", attempt_id="att1",
        sender_agent="astra", recipient_agent="anti", recipient_capability="code",
        message_type=msg_type, payload=payload, payload_digest=digest,
        task_intent_id="ti", task_intent_digest="ti_d",
        policy_receipt_id="pr", policy_receipt_digest="pr_d",
        effect_class=effect_class, stop_boundary=stop_boundary,
        created_at_utc="utc", expires_at_utc="utc"
    )

def test_payload_tampered():
    env = make_envelope({"cmd": "ls"})
    object.__setattr__(env, "payload", {"cmd": "rm -rf /"})
    router = CrossAgentRouter(AgentRegistry(), AuthorityResolver(), PersistentStore("/tmp/store"))
    with pytest.raises(MessageTamperedError):
        router.route(env)

def test_unsupported_message_type():
    env = make_envelope({"cmd": "ls"}, msg_type="INVALID")
    router = CrossAgentRouter(AgentRegistry(), AuthorityResolver(), PersistentStore("/tmp/store"))
    with pytest.raises(UnsupportedMessageTypeError):
        router.route(env)

def test_effect_escalation():
    env = make_envelope({"cmd": "ls"}, effect_class=EffectClass.DEPLOY)
    router = CrossAgentRouter(AgentRegistry(), AuthorityResolver(), PersistentStore("/tmp/store"))
    with pytest.raises(EffectClassEscalationError):
        router.route(env)

def test_stop_boundary_escalation():
    env = make_envelope({"cmd": "ls"}, stop_boundary=StopBoundary.DEPLOY)
    router = CrossAgentRouter(AgentRegistry(), AuthorityResolver(), PersistentStore("/tmp/store"))
    with pytest.raises(StopBoundaryEscalationError):
        router.route(env)

def test_unknown_agent():
    env = make_envelope({"cmd": "ls"})
    router = CrossAgentRouter(AgentRegistry(), AuthorityResolver(), PersistentStore("/tmp/store"))
    receipt = router.route(env)
    assert receipt.decision == "BLOCK"
    assert receipt.reason == "UNKNOWN_AGENT"

def test_unavailable_adapter():
    reg = AgentRegistry()
    reg.register(
        AgentDefinition("anti", ["code"], ["WORK_REQUEST"], [EffectClass.READ_ONLY], 30, 1),
        AntigravityAdapter(FakeTransport(False))
    )
    env = make_envelope({"cmd": "ls"})
    router = CrossAgentRouter(reg, AuthorityResolver(), PersistentStore("/tmp/store"))
    receipt = router.route(env)
    assert receipt.decision == "DEFER"
    assert receipt.reason == "WORKER_UNAVAILABLE"

def test_path_traversal_message_id():
    env = make_envelope({"cmd": "ls"})
    object.__setattr__(env, "message_id", "../../etc/passwd")
    reg = AgentRegistry()
    reg.register(
        AgentDefinition("anti", ["code"], ["WORK_REQUEST"], [EffectClass.READ_ONLY], 30, 1),
        AntigravityAdapter(FakeTransport(True))
    )
    router = CrossAgentRouter(reg, AuthorityResolver(), PersistentStore("/tmp/store"))
    with pytest.raises(ValueError, match="Path traversal"):
        router.route(env)

def test_computer_use_no_policy():
    env = make_envelope({"cmd": "ls"})
    object.__setattr__(env, "policy_receipt_id", "")
    adapter = ComputerUseAdapter(FakeTransport(True))
    with pytest.raises(PolicyDeniedError):
        adapter.dispatch(env, 30)

def test_cross_run_cancellation():
    router = CrossAgentRouter(AgentRegistry(), AuthorityResolver(), PersistentStore("/tmp/store"))
    with pytest.raises(CrossRunViolationError):
        router.cancel("invalid_run", "cor1")

def test_successful_dispatch():
    reg = AgentRegistry()
    reg.register(
        AgentDefinition("anti", ["code"], ["WORK_REQUEST"], [EffectClass.READ_ONLY], 30, 1),
        AntigravityAdapter(FakeTransport(True))
    )
    env = make_envelope({"cmd": "ls"})
    router = CrossAgentRouter(reg, AuthorityResolver(), PersistentStore("/tmp/store"))
    res = router.dispatch(env)
    assert res.task_id == "task1"
