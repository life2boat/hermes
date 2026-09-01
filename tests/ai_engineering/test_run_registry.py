"""Unit and integration tests for ActiveRunRegistry and idempotent spawn."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest

from ai_engineering.execution.run_contracts import (
    AgentRunIdentity,
    RunBlockingReason,
    RunEventEnvelope,
    RunEventType,
    RunIdentityError,
    RunState,
    RunStateError,
    SpawnStatus,
    StaleEventError,
)
from ai_engineering.execution.run_registry import ActiveRunRegistry
from ai_engineering.workspaces.workspace_contracts import (
    ExecutionMode,
    LeaseState,
    WorkspaceIdentity,
    WorktreeLease,
)
from ai_engineering.workspaces.workspace_manager import WorkspaceManager


def test_active_run_registry_register_and_lookup():
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-reg-01",
        task_id="task-1",
        node_id="node-1",
        workspace_id="ws-1",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    rec = registry.register_run(ident)
    assert rec.identity == ident
    assert rec.state == RunState.CREATED
    assert registry.get_run("run-reg-01") == rec

    # Idempotent re-registration with same identity
    rec2 = registry.register_run(ident)
    assert rec2 == rec


def test_run_identity_collision_fails():
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident1 = AgentRunIdentity(
        run_id="run-collision",
        task_id="task-1",
        node_id="node-1",
        workspace_id="ws-1",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    registry.register_run(ident1)

    # Different workspace for same run_id
    ident2 = AgentRunIdentity(
        run_id="run-collision",
        task_id="task-1",
        node_id="node-1",
        workspace_id="ws-different",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    with pytest.raises(RunIdentityError) as exc_info:
        registry.register_run(ident2)
    assert exc_info.value.code == RunBlockingReason.RUN_IDENTITY_COLLISION.value


def test_idempotent_spawn():
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-spawn-01",
        task_id="task-1",
        node_id="node-1",
        workspace_id="ws-1",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )

    # First spawn
    rec1, status1 = registry.spawn_agent(ident)
    assert status1 == SpawnStatus.SPAWNED
    assert rec1.state == RunState.LIVE
    assert registry.get_active_run_for_slot("task-1", "node-1", "ws-1") == rec1

    # Second spawn with same identity -> ALREADY_ACTIVE (idempotent)
    rec2, status2 = registry.spawn_agent(ident)
    assert status2 == SpawnStatus.ALREADY_ACTIVE
    assert rec2 == rec1
    assert len(registry.list_runs()) == 1


def test_duplicate_active_run_in_slot_rejected():
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident1 = AgentRunIdentity(
        run_id="run-slot-01",
        task_id="task-1",
        node_id="node-1",
        workspace_id="ws-1",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    registry.spawn_agent(ident1)

    # Different run_id trying to occupy same slot with same epoch
    ident2 = AgentRunIdentity(
        run_id="run-slot-02",
        task_id="task-1",
        node_id="node-1",
        workspace_id="ws-1",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    with pytest.raises(RunIdentityError) as exc_info:
        registry.spawn_agent(ident2)
    assert exc_info.value.code == RunBlockingReason.DUPLICATE_ACTIVE_RUN.value
