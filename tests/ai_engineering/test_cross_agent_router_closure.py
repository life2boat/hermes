import pytest
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock
from ai_engineering.supervisor.router import (
    AgentEnvelope, AgentRegistry, CrossAgentRouter, FilePersistentStore, AgentAdapter
)
from ai_engineering.supervisor.router.router import ROUTER_CAN_GRANT_AUTHORITY, ASTRA_CAN_AUTHORIZE, PolicyDeniedError
from ai_engineering.supervisor.router.registry import AgentCapability
from ai_engineering.supervisor.worker_result import WorkerResultBundle

def test_envelope_integrity():
    payload = {"command": "deploy", "args": ["--force"]}
    envelope = AgentEnvelope.create(
        message_id="msg-1",
        correlation_id="corr-1",
        target_agent="antigravity",
        reply_to="user",
        payload=payload,
        timestamp_utc="2026-09-11T00:00:00Z",
        task_intent="mock_intent",
        policy_receipt="mock_receipt",
        effect_class="mock_effect",
        stop_boundary="mock_boundary"
    )
    assert envelope.verify_integrity()
    assert envelope.schema_version == "hermes.agent-envelope.v1"
    
def test_explicit_flags():
    assert ROUTER_CAN_GRANT_AUTHORITY is False
    assert ASTRA_CAN_AUTHORIZE is False

def test_registry_capabilities():
    registry = AgentRegistry.create_default()
    cap = registry.get_capability("antigravity")
    assert cap.agent_id == "antigravity"
    assert cap.timeout_seconds == 60

def test_router_deduplication(tmp_path: Path):
    store = FilePersistentStore(tmp_path)
    registry = AgentRegistry.create_default()
    
    router = CrossAgentRouter(registry, store)
    envelope = AgentEnvelope.create(
        message_id="msg-2",
        correlation_id="corr-2",
        target_agent="codex",
        reply_to="user",
        payload={"test": "data"},
        timestamp_utc="2026-09-11T00:00:00Z"
    )
    
    result1 = router.route(envelope)
    result2 = router.route(envelope)
    assert result1 == result2

def test_router_timeout(tmp_path: Path):
    store = FilePersistentStore(tmp_path)
    registry = AgentRegistry()
    mock_adapter = Mock(spec=AgentAdapter)
    mock_adapter.dispatch.side_effect = lambda e: time.sleep(0.02)
    registry.register("mock", mock_adapter, AgentCapability("mock", (), 0))
    
    router = CrossAgentRouter(registry, store, default_timeout=0)
    envelope = AgentEnvelope.create("msg-3", "corr-3", "mock", "user", {}, "utc")
    with pytest.raises(TimeoutError):
        router.route(envelope)

def test_router_cancellation(tmp_path: Path):
    store = FilePersistentStore(tmp_path)
    registry = AgentRegistry.create_default()
    router = CrossAgentRouter(registry, store)
    envelope = AgentEnvelope.create("msg-4", "corr-4", "codex", "user", {}, "utc")
    
    router.cancel("corr-4")
    with pytest.raises(RuntimeError, match="canceled"):
        router.route(envelope)

def test_policy_denied(tmp_path: Path):
    store = FilePersistentStore(tmp_path)
    registry = AgentRegistry()
    mock_adapter = Mock(spec=AgentAdapter)
    mock_adapter.dispatch.side_effect = PolicyDeniedError("Denied")
    registry.register("mock", mock_adapter)
    
    router = CrossAgentRouter(registry, store)
    envelope = AgentEnvelope.create("msg-5", "corr-5", "mock", "user", {}, "utc")
    with pytest.raises(PolicyDeniedError):
        router.route(envelope)

def test_safe_persistence(tmp_path: Path):
    store = FilePersistentStore(tmp_path)
    with pytest.raises(ValueError):
        store.has_processed("../evil")
    with pytest.raises(ValueError):
        store.store_result("../evil", None)

def test_router_unvailable_transport(tmp_path):
    store = FilePersistentStore(tmp_path)
    registry = AgentRegistry.create_default()
    router = CrossAgentRouter(registry, store)
    # simulate transport unvailable
    registry.get_adapter('antigravity').transport._available = False
    
    envelope = AgentEnvelope.create('msg-99', 'corr-99', 'antigravity', 'user', {}, 'utc')
    from ai_engineering.supervisor.router.adapters import AgentTransportUnavailableError
    with pytest.raises((AgentTransportUnavailableError, RuntimeError)):
        router.route(envelope)

def test_envelope_canonical_contracts():
    envelope = AgentEnvelope.create(
        'msg-c1', 'corr-c1', 'astra', 'user', {}, 'utc',
        task_intent={'intent': 'test'},
        policy_receipt={'policy': 'test'},
        effect_class='read_only',
        stop_boundary='auto'
    )
    assert envelope.task_intent == {'intent': 'test'}
    assert envelope.policy_receipt == {'policy': 'test'}
    assert envelope.effect_class == 'read_only'
    assert envelope.stop_boundary == 'auto'
