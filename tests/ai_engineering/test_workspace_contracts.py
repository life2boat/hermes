"""Unit tests for WorkspaceIdentity and WorktreeLease contracts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
import pytest

from ai_engineering.workspaces.workspace_contracts import (
    WORKSPACE_CONTRACT_VERSION,
    WORKTREE_LEASE_VERSION,
    ExecutionMode,
    LeaseState,
    LeaseTransitionError,
    WorkspaceBlockingReason,
    WorkspaceIdentity,
    WorkspaceSecurityError,
    WorktreeLease,
)

VALID_SHA = "8fdd22acb62be46f3b738c7bfecbd0c616df8fbe"


def test_workspace_identity_valid_creation():
    created = datetime.now(timezone.utc)
    ident = WorkspaceIdentity(
        workspace_id="ws-task-123-1",
        task_id="task-123",
        candidate_id="cand-001",
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=VALID_SHA,
        branch="codex/hermes-v4-pr1-workspace-safety",
        worktree_path="/tmp/workspaces/ws-task-123-1",
        execution_host_id="local-host-1",
        execution_mode=ExecutionMode.ISOLATED.value,
        created_at=created,
    )
    assert ident.workspace_id == "ws-task-123-1"
    assert ident.task_id == "task-123"
    assert ident.candidate_id == "cand-001"
    assert ident.base_sha == VALID_SHA

    # Serialization roundtrip
    d = ident.to_dict()
    assert d["schema_version"] == WORKSPACE_CONTRACT_VERSION
    assert d["base_sha"] == VALID_SHA

    raw_json = ident.to_json()
    reconstructed = WorkspaceIdentity.from_json(raw_json)
    assert reconstructed.workspace_id == ident.workspace_id
    assert reconstructed.base_sha == ident.base_sha
    assert reconstructed.created_at == ident.created_at


def test_workspace_identity_invalid_sha():
    with pytest.raises(WorkspaceSecurityError) as exc_info:
        WorkspaceIdentity(
            workspace_id="ws-1",
            task_id="task-1",
            candidate_id=None,
            repository="repo",
            base_ref="main",
            base_sha="invalid-sha-too-short",
            branch="branch-1",
            worktree_path="/tmp/path",
            execution_host_id="host",
            execution_mode="ISOLATED",
            created_at=datetime.now(timezone.utc),
        )
    assert exc_info.value.code == WorkspaceBlockingReason.WORKTREE_BASE_SHA_MISMATCH.value


def test_workspace_identity_empty_fields_fail():
    with pytest.raises(WorkspaceSecurityError):
        WorkspaceIdentity(
            workspace_id="",
            task_id="task-1",
            candidate_id=None,
            repository="repo",
            base_ref="main",
            base_sha=VALID_SHA,
            branch="branch-1",
            worktree_path="/tmp/path",
            execution_host_id="host",
            execution_mode="ISOLATED",
            created_at=datetime.now(timezone.utc),
        )


def test_worktree_lease_state_machine():
    now = datetime.now(timezone.utc)
    lease = WorktreeLease(
        workspace_id="ws-1",
        owner_run_id="run-1",
        task_id="task-1",
        acquired_at=now,
        expires_at=None,
        state=LeaseState.RESERVED,
    )
    assert not lease.is_active()

    # RESERVED -> ACTIVE (allowed)
    active_lease = lease.transition(LeaseState.ACTIVE, actor_run_id="run-1")
    assert active_lease.state == LeaseState.ACTIVE
    assert active_lease.is_active()

    # ACTIVE -> RELEASE_PENDING (allowed)
    pending_lease = active_lease.transition(LeaseState.RELEASE_PENDING, actor_run_id="run-1")
    assert pending_lease.state == LeaseState.RELEASE_PENDING
    assert not pending_lease.is_active()

    # RELEASE_PENDING -> RELEASED (allowed)
    released_lease = pending_lease.transition(LeaseState.RELEASED, actor_run_id="run-1")
    assert released_lease.state == LeaseState.RELEASED
    assert not released_lease.is_active()

    # RELEASED -> ACTIVE (forbidden!)
    with pytest.raises(LeaseTransitionError) as exc_info:
        released_lease.transition(LeaseState.ACTIVE, actor_run_id="run-1")
    assert exc_info.value.code == WorkspaceBlockingReason.LEASE_TRANSITION_INVALID.value


def test_worktree_lease_quarantine_terminal():
    now = datetime.now(timezone.utc)
    lease = WorktreeLease(
        workspace_id="ws-1",
        owner_run_id="run-1",
        task_id="task-1",
        acquired_at=now,
        expires_at=None,
        state=LeaseState.ACTIVE,
    )
    quarantined = lease.transition(LeaseState.QUARANTINED, actor_run_id="run-1")
    assert quarantined.state == LeaseState.QUARANTINED

    # QUARANTINED -> ACTIVE (forbidden!)
    with pytest.raises(LeaseTransitionError) as exc_info:
        quarantined.transition(LeaseState.ACTIVE, actor_run_id="run-1")
    assert exc_info.value.code == WorkspaceBlockingReason.LEASE_TRANSITION_INVALID.value


def test_worktree_lease_foreign_actor_rejected():
    now = datetime.now(timezone.utc)
    lease = WorktreeLease(
        workspace_id="ws-1",
        owner_run_id="run-1",
        task_id="task-1",
        acquired_at=now,
        expires_at=None,
        state=LeaseState.RESERVED,
    )
    with pytest.raises(LeaseTransitionError) as exc_info:
        lease.transition(LeaseState.ACTIVE, actor_run_id="foreign-run-2")
    assert exc_info.value.code == WorkspaceBlockingReason.LEASE_OWNERSHIP_MISMATCH.value


def test_worktree_lease_serialization_roundtrip():
    now = datetime.now(timezone.utc)
    lease = WorktreeLease(
        workspace_id="ws-1",
        owner_run_id="run-1",
        task_id="task-1",
        acquired_at=now,
        expires_at=None,
        state=LeaseState.ACTIVE,
    )
    payload = lease.to_dict()
    assert payload["schema_version"] == WORKTREE_LEASE_VERSION
    assert payload["state"] == "ACTIVE"

    reconstructed = WorktreeLease.from_dict(payload)
    assert reconstructed.workspace_id == lease.workspace_id
    assert reconstructed.state == LeaseState.ACTIVE
