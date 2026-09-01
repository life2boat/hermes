"""Unit tests for AgentRunRecord and run state transitions."""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from ai_engineering.execution.run_contracts import (
    AgentRunIdentity,
    RunBlockingReason,
    RunState,
    RunStateError,
)
from ai_engineering.execution.run_state import AgentRunRecord


@pytest.fixture
def base_identity() -> AgentRunIdentity:
    return AgentRunIdentity(
        run_id="run-state-01",
        task_id="task-01",
        node_id="node-01",
        workspace_id="ws-01",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=datetime.now(timezone.utc),
    )


def test_valid_lifecycle_transitions(base_identity):
    now = datetime.now(timezone.utc)
    rec = AgentRunRecord(identity=base_identity, state=RunState.CREATED, updated_at=now)
    assert rec.is_active()
    assert not rec.is_terminal()

    # CREATED -> START_REQUESTED
    r2 = rec.transition(RunState.START_REQUESTED)
    assert r2.state == RunState.START_REQUESTED
    assert r2.is_active()

    # START_REQUESTED -> LIVE
    r3 = r2.transition(RunState.LIVE)
    assert r3.state == RunState.LIVE
    assert r3.is_active()

    # LIVE -> CANCEL_REQUESTED
    r4 = r3.transition(RunState.CANCEL_REQUESTED, cancellation_reason="User requested cancel")
    assert r4.state == RunState.CANCEL_REQUESTED
    assert r4.is_active()
    assert r4.cancellation_reason == "User requested cancel"

    # CANCEL_REQUESTED -> EXITED
    r5 = r4.transition(RunState.EXITED, exit_code=0)
    assert r5.state == RunState.EXITED
    assert not r5.is_active()
    assert r5.is_terminal()
    assert r5.exit_code == 0


def test_cancel_requested_does_not_equal_exited(base_identity):
    now = datetime.now(timezone.utc)
    rec = AgentRunRecord(identity=base_identity, state=RunState.LIVE, updated_at=now)
    cancelled = rec.transition(RunState.CANCEL_REQUESTED)
    assert cancelled.state == RunState.CANCEL_REQUESTED
    assert cancelled.is_active()
    assert not cancelled.is_terminal()


def test_terminal_state_resurrection_rejected(base_identity):
    now = datetime.now(timezone.utc)
    rec_exited = AgentRunRecord(identity=base_identity, state=RunState.EXITED, updated_at=now)

    with pytest.raises(RunStateError) as exc_info:
        rec_exited.transition(RunState.LIVE)
    assert exc_info.value.code == RunBlockingReason.RUN_STATE_TRANSITION_INVALID.value

    rec_failed = AgentRunRecord(identity=base_identity, state=RunState.FAILED, updated_at=now)
    with pytest.raises(RunStateError) as exc_info2:
        rec_failed.transition(RunState.LIVE)
    assert exc_info2.value.code == RunBlockingReason.RUN_STATE_TRANSITION_INVALID.value


def test_run_record_serialization_roundtrip(base_identity):
    now = datetime.now(timezone.utc)
    rec = AgentRunRecord(
        identity=base_identity,
        state=RunState.LIVE,
        updated_at=now,
        cancellation_reason=None,
        exit_code=None,
    )
    d = rec.to_dict()
    assert d["state"] == "LIVE"

    raw = rec.to_json()
    reconstructed = AgentRunRecord.from_json(raw)
    assert reconstructed.identity.run_id == base_identity.run_id
    assert reconstructed.state == RunState.LIVE
