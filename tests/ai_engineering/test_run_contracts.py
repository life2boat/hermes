"""Unit tests for AgentRunIdentity, RunEventEnvelope, and related contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import pytest

from ai_engineering.execution.run_contracts import (
    AGENT_RUN_CONTRACT_VERSION,
    RUN_EVENT_SCHEMA_VERSION,
    AgentRunIdentity,
    RunBlockingReason,
    RunEventEnvelope,
    RunEventType,
    RunIdentityError,
    StaleEventError,
)


def test_agent_run_identity_valid_creation():
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-001",
        task_id="task-100",
        node_id="node-1",
        workspace_id="ws-100-1",
        candidate_id="cand-01",
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="host-local-1",
        execution_epoch=1,
        start_time=now,
    )
    assert ident.run_id == "run-001"
    assert ident.task_id == "task-100"
    assert ident.node_id == "node-1"
    assert ident.execution_epoch == 1

    # Serialization roundtrip
    d = ident.to_dict()
    assert d["schema_version"] == AGENT_RUN_CONTRACT_VERSION
    assert d["run_id"] == "run-001"

    raw = ident.to_json()
    reconstructed = AgentRunIdentity.from_json(raw)
    assert reconstructed.run_id == ident.run_id
    assert reconstructed.execution_epoch == ident.execution_epoch
    assert reconstructed.start_time == ident.start_time


def test_agent_run_identity_invalid_epoch():
    now = datetime.now(timezone.utc)
    with pytest.raises(RunIdentityError) as exc_info:
        AgentRunIdentity(
            run_id="run-001",
            task_id="task-100",
            node_id="node-1",
            workspace_id="ws-100-1",
            candidate_id=None,
            model="deepseek-chat",
            agent_capability="CODE_GENERATION",
            execution_host_id="host-local-1",
            execution_epoch=0,  # Invalid epoch <= 0
            start_time=now,
        )
    assert exc_info.value.code == RunBlockingReason.INVALID_EPOCH.value

    with pytest.raises(RunIdentityError) as exc_info2:
        AgentRunIdentity(
            run_id="run-001",
            task_id="task-100",
            node_id="node-1",
            workspace_id="ws-100-1",
            candidate_id=None,
            model="deepseek-chat",
            agent_capability="CODE_GENERATION",
            execution_host_id="host-local-1",
            execution_epoch=-5,  # Negative epoch
            start_time=now,
        )
    assert exc_info2.value.code == RunBlockingReason.INVALID_EPOCH.value


def test_agent_run_identity_missing_required_fields():
    now = datetime.now(timezone.utc)
    with pytest.raises(RunIdentityError):
        AgentRunIdentity(
            run_id="",
            task_id="task-100",
            node_id="node-1",
            workspace_id="ws-1",
            candidate_id=None,
            model="deepseek-chat",
            agent_capability="CODE_GENERATION",
            execution_host_id="host-1",
            execution_epoch=1,
            start_time=now,
        )


def test_run_event_envelope_valid_and_roundtrip():
    now = datetime.now(timezone.utc)
    envelope = RunEventEnvelope(
        event_id="evt-001",
        run_id="run-001",
        execution_epoch=1,
        event_type=RunEventType.AGENT_RUN_LIVE,
        payload={"status": "ready"},
        timestamp=now,
        task_id="task-1",
    )
    assert envelope.event_id == "evt-001"
    assert envelope.event_type == RunEventType.AGENT_RUN_LIVE

    d = envelope.to_dict()
    assert d["schema_version"] == RUN_EVENT_SCHEMA_VERSION
    assert d["event_type"] == "AGENT_RUN_LIVE"

    raw = envelope.to_json()
    reconstructed = RunEventEnvelope.from_json(raw)
    assert reconstructed.event_id == envelope.event_id
    assert reconstructed.event_type == RunEventType.AGENT_RUN_LIVE
    assert reconstructed.payload == {"status": "ready"}


def test_run_event_envelope_invalid_epoch():
    now = datetime.now(timezone.utc)
    with pytest.raises(StaleEventError) as exc_info:
        RunEventEnvelope(
            event_id="evt-001",
            run_id="run-001",
            execution_epoch=0,
            event_type=RunEventType.AGENT_RUN_LIVE,
            payload={},
            timestamp=now,
        )
    assert exc_info.value.code == RunBlockingReason.INVALID_EPOCH.value
