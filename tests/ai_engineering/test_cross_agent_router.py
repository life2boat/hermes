import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock
from ai_engineering.supervisor.router import (
    AgentEnvelope, AgentRegistry, CrossAgentRouter, FilePersistentStore, AgentAdapter
)
from ai_engineering.supervisor.worker_result import WorkerResultBundle, GateClaim

def test_envelope_integrity():
    payload = {"command": "deploy", "args": ["--force"]}
    envelope = AgentEnvelope.create(
        message_id="msg-1",
        correlation_id="corr-1",
        target_agent="antigravity",
        reply_to="user",
        payload=payload,
        timestamp_utc="2026-09-11T00:00:00Z"
    )
    assert envelope.verify_integrity()
    
    # Tamper payload
    tampered_envelope = AgentEnvelope(
        message_id=envelope.message_id,
        correlation_id=envelope.correlation_id,
        target_agent=envelope.target_agent,
        reply_to=envelope.reply_to,
        payload={"command": "delete_all"},
        payload_digest=envelope.payload_digest,
        timestamp_utc=envelope.timestamp_utc
    )
    assert not tampered_envelope.verify_integrity()

def test_agent_registry():
    registry = AgentRegistry.create_default()
    adapter = registry.get_adapter("antigravity")
    assert adapter is not None
    
    with pytest.raises(KeyError):
        registry.get_adapter("unknown")

def test_cross_agent_router_deduplication(tmp_path: Path):
    store = FilePersistentStore(tmp_path)
    registry = AgentRegistry.create_default()
    
    mock_adapter = Mock(spec=AgentAdapter)
    # mock result bundle
    bundle = WorkerResultBundle(
        schema_version="hermes.worker-result.v1",
        result_id="res-1",
        task_id="task-1",
        attempt_id="att-1",
        worker_id="worker-1",
        base_sha="sha",
        head_sha="sha",
        canonical_remote="remote",
        repository="repo",
        intent_digest="digest",
        produced_at_utc="utc",
        artifacts=(),
        gate_claims=()
    )
    mock_adapter.dispatch.return_value = bundle
    registry.register("mock_agent", mock_adapter)
    
    router = CrossAgentRouter(registry, store)
    envelope = AgentEnvelope.create(
        message_id="msg-2",
        correlation_id="corr-2",
        target_agent="mock_agent",
        reply_to="user",
        payload={"test": "data"},
        timestamp_utc="2026-09-11T00:00:00Z"
    )
    
    # First route
    result1 = router.route(envelope)
    assert result1 == bundle
    mock_adapter.dispatch.assert_called_once_with(envelope)
    
    # Second route (deduplication)
    result2 = router.route(envelope)
    assert result2 == bundle
    mock_adapter.dispatch.assert_called_once() # still called once

def test_cross_agent_router_retry(tmp_path: Path):
    store = FilePersistentStore(tmp_path)
    registry = AgentRegistry()
    mock_adapter = Mock(spec=AgentAdapter)
    
    bundle = WorkerResultBundle(
        schema_version="hermes.worker-result.v1",
        result_id="res-1",
        task_id="task-1",
        attempt_id="att-1",
        worker_id="worker-1",
        base_sha="sha",
        head_sha="sha",
        canonical_remote="remote",
        repository="repo",
        intent_digest="digest",
        produced_at_utc="utc",
        artifacts=(),
        gate_claims=()
    )
    mock_adapter.dispatch.side_effect = [Exception("fail"), bundle]
    registry.register("mock_agent", mock_adapter)
    
    router = CrossAgentRouter(registry, store, max_retries=3)
    envelope = AgentEnvelope.create(
        message_id="msg-3",
        correlation_id="corr-3",
        target_agent="mock_agent",
        reply_to="user",
        payload={"test": "data"},
        timestamp_utc="2026-09-11T00:00:00Z"
    )
    
    result = router.route(envelope)
    assert result == bundle
    assert mock_adapter.dispatch.call_count == 2
