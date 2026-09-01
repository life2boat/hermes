"""Comprehensive invariant and fencing tests for Hermes v4.1 PR-2.

Covers all 24 normative test cases defined in Phase 19 of the specification.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
import pytest

from ai_engineering.execution.run_contracts import (
    AGENT_RUN_CONTRACT_VERSION,
    RUN_EVENT_SCHEMA_VERSION,
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
from ai_engineering.execution.run_state import AgentRunRecord
from ai_engineering.workspaces.workspace_contracts import (
    ExecutionMode,
    LeaseState,
    WorkspaceIdentity,
    WorktreeLease,
)
from ai_engineering.workspaces.workspace_manager import WorkspaceManager


@pytest.fixture
def mock_workspace_env(tmp_path: Path):
    """Fixture providing a initialized WorkspaceManager with a test workspace and lease."""
    repo_dir = tmp_path / "canonical_hermes"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test Hermes"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "hermes@example.com"], check=True, capture_output=True)
    (repo_dir / "README.md").write_text("# Canonical")
    subprocess.run(["git", "-C", str(repo_dir), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "Init"], check=True, capture_output=True)
    proc = subprocess.run(["git", "-C", str(repo_dir), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    base_sha = proc.stdout.strip()

    ws_manager = WorkspaceManager(repo_dir)
    wt_dir = tmp_path / "worktrees" / "ws-task-1"

    ws_ident, lease = ws_manager.create_isolated_workspace(
        workspace_id="ws-task-1",
        task_id="task-1",
        candidate_id="cand-1",
        repository="life2boat/hermes",
        base_ref="refs/heads/main",
        base_sha=base_sha,
        branch="codex/task-1-cand-1",
        worktree_path=wt_dir,
        owner_run_id="run-001",
        auto_acquire_lease=True,
    )

    return {
        "repo_dir": repo_dir,
        "base_sha": base_sha,
        "ws_manager": ws_manager,
        "workspace_id": "ws-task-1",
        "task_id": "task-1",
        "candidate_id": "cand-1",
        "owner_run_id": "run-001",
    }


def test_inv01_valid_agent_run_identity():
    """1. valid AgentRunIdentity -> PASS"""
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-01",
        task_id="task-01",
        node_id="node-01",
        workspace_id="ws-01",
        candidate_id="cand-01",
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    assert ident.run_id == "run-inv-01"
    assert ident.execution_epoch == 1


def test_inv02_invalid_execution_epoch():
    """2. invalid execution_epoch <= 0 -> FAIL"""
    now = datetime.now(timezone.utc)
    with pytest.raises(RunIdentityError) as exc_info:
        AgentRunIdentity(
            run_id="run-inv-02",
            task_id="task-02",
            node_id="node-02",
            workspace_id="ws-02",
            candidate_id=None,
            model="deepseek-chat",
            agent_capability="CODE_GENERATION",
            execution_host_id="local",
            execution_epoch=0,
            start_time=now,
        )
    assert exc_info.value.code == RunBlockingReason.INVALID_EPOCH.value


def test_inv03_duplicate_identical_run_replay():
    """3. duplicate identical run replay -> idempotent PASS"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-03",
        task_id="task-03",
        node_id="node-03",
        workspace_id="ws-03",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    r1 = registry.register_run(ident)
    r2 = registry.register_run(ident)
    assert r1 == r2


def test_inv04_same_run_id_different_workspace():
    """4. same run_id different workspace -> RUN_IDENTITY_COLLISION"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident1 = AgentRunIdentity(
        run_id="run-inv-04",
        task_id="task-04",
        node_id="node-04",
        workspace_id="ws-04a",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    ident2 = AgentRunIdentity(
        run_id="run-inv-04",
        task_id="task-04",
        node_id="node-04",
        workspace_id="ws-04b",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    registry.register_run(ident1)
    with pytest.raises(RunIdentityError) as exc_info:
        registry.register_run(ident2)
    assert exc_info.value.code == RunBlockingReason.RUN_IDENTITY_COLLISION.value


def test_inv05_unknown_workspace(mock_workspace_env):
    """5. unknown workspace -> FAIL"""
    registry = ActiveRunRegistry(workspace_manager=mock_workspace_env["ws_manager"])
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-05",
        task_id="task-1",
        node_id="node-1",
        workspace_id="unknown-workspace-id",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    with pytest.raises(RunIdentityError) as exc_info:
        registry.register_run(ident)
    assert exc_info.value.code == RunBlockingReason.UNKNOWN_WORKSPACE.value


def test_inv06_workspace_task_mismatch(mock_workspace_env):
    """6. workspace task mismatch -> RUN_WORKSPACE_MISMATCH"""
    registry = ActiveRunRegistry(workspace_manager=mock_workspace_env["ws_manager"])
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-06",
        task_id="foreign-task-99",  # Workspace is bound to task-1
        node_id="node-1",
        workspace_id=mock_workspace_env["workspace_id"],
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    with pytest.raises(RunIdentityError) as exc_info:
        registry.register_run(ident)
    assert exc_info.value.code == RunBlockingReason.RUN_WORKSPACE_MISMATCH.value


def test_inv07_lease_owner_mismatch(mock_workspace_env):
    """7. lease owner != run_id -> FAIL"""
    registry = ActiveRunRegistry(workspace_manager=mock_workspace_env["ws_manager"])
    now = datetime.now(timezone.utc)
    # Workspace lease is owned by 'run-001'
    ident = AgentRunIdentity(
        run_id="unauthorized-run-99",
        task_id=mock_workspace_env["task_id"],
        node_id="node-1",
        workspace_id=mock_workspace_env["workspace_id"],
        candidate_id=mock_workspace_env["candidate_id"],
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    with pytest.raises(RunIdentityError) as exc_info:
        registry.register_run(ident)
    assert exc_info.value.code in (
        RunBlockingReason.RUN_LEASE_OWNERSHIP_MISMATCH.value,
        RunBlockingReason.LEASE_OWNERSHIP_MISMATCH.value,
    )


def test_inv08_valid_created_start_requested_live():
    """8. valid CREATED -> START_REQUESTED -> LIVE -> PASS"""
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-08",
        task_id="task-08",
        node_id="node-08",
        workspace_id="ws-08",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    r1 = AgentRunRecord(identity=ident, state=RunState.CREATED, updated_at=now)
    r2 = r1.transition(RunState.START_REQUESTED)
    r3 = r2.transition(RunState.LIVE)
    assert r3.state == RunState.LIVE
    assert r3.is_active()


def test_inv09_exited_to_live_fails():
    """9. EXITED -> LIVE -> FAIL"""
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-09",
        task_id="task-09",
        node_id="node-09",
        workspace_id="ws-09",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    r_exited = AgentRunRecord(identity=ident, state=RunState.EXITED, updated_at=now)
    with pytest.raises(RunStateError) as exc_info:
        r_exited.transition(RunState.LIVE)
    assert exc_info.value.code == RunBlockingReason.RUN_STATE_TRANSITION_INVALID.value


def test_inv10_failed_to_live_fails():
    """10. FAILED -> LIVE -> FAIL"""
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-10",
        task_id="task-10",
        node_id="node-10",
        workspace_id="ws-10",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    r_failed = AgentRunRecord(identity=ident, state=RunState.FAILED, updated_at=now)
    with pytest.raises(RunStateError) as exc_info:
        r_failed.transition(RunState.LIVE)
    assert exc_info.value.code == RunBlockingReason.RUN_STATE_TRANSITION_INVALID.value


def test_inv11_live_to_cancel_requested():
    """11. LIVE -> CANCEL_REQUESTED -> PASS"""
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-11",
        task_id="task-11",
        node_id="node-11",
        workspace_id="ws-11",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    r_live = AgentRunRecord(identity=ident, state=RunState.LIVE, updated_at=now)
    r_cancel = r_live.transition(RunState.CANCEL_REQUESTED, cancellation_reason="Timeout")
    assert r_cancel.state == RunState.CANCEL_REQUESTED
    assert r_cancel.cancellation_reason == "Timeout"


def test_inv12_cancel_requested_does_not_equal_exited():
    """12. CANCEL_REQUESTED does not equal EXITED -> PASS"""
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-12",
        task_id="task-12",
        node_id="node-12",
        workspace_id="ws-12",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    r_cancel = AgentRunRecord(identity=ident, state=RunState.CANCEL_REQUESTED, updated_at=now)
    assert r_cancel.state != RunState.EXITED
    assert r_cancel.is_active() is True
    assert r_cancel.is_terminal() is False


def test_inv13_cancel_confirmation_same_run_epoch():
    """13. cancel confirmation same run/epoch -> EXITED"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-13",
        task_id="task-13",
        node_id="node-13",
        workspace_id="ws-13",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    registry.spawn_agent(ident)
    registry.request_cancel("run-inv-13", reason="User cancel")

    # Inbound AGENT_RUN_EXITED event confirming termination
    event = RunEventEnvelope(
        event_id="evt-13",
        run_id="run-inv-13",
        execution_epoch=1,
        event_type=RunEventType.AGENT_RUN_EXITED,
        payload={"exit_code": 130},
        timestamp=now,
    )
    success, _, rec = registry.process_event(event)
    assert success is True
    assert rec.state == RunState.EXITED
    assert rec.exit_code == 130
    assert not rec.is_active()


def test_inv14_stale_run_id_event():
    """14. stale run_id event -> STALE_RUN_EVENT"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident1 = AgentRunIdentity(
        run_id="run-old-14",
        task_id="task-14",
        node_id="node-14",
        workspace_id="ws-14",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    ident2 = AgentRunIdentity(
        run_id="run-new-14",
        task_id="task-14",
        node_id="node-14",
        workspace_id="ws-14",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=2,
        start_time=now,
    )
    registry.spawn_agent(ident1)
    registry.spawn_agent(ident2)  # Supersedes ident1 for the slot

    stale_event = RunEventEnvelope(
        event_id="evt-stale",
        run_id="run-old-14",
        execution_epoch=1,
        event_type=RunEventType.VALIDATION_COMPLETED,
        payload={},
        timestamp=now,
    )
    with pytest.raises(StaleEventError) as exc_info:
        registry.process_event(stale_event)
    assert exc_info.value.code == RunBlockingReason.STALE_RUN_EVENT.value


def test_inv15_stale_epoch_event():
    """15. stale epoch event -> STALE_RUN_MUTATION"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-inv-15",
        task_id="task-15",
        node_id="node-15",
        workspace_id="ws-15",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=3,  # Current active epoch is 3
        start_time=now,
    )
    registry.spawn_agent(ident)

    # Event arriving with epoch 2 (stale epoch)
    stale_epoch_event = RunEventEnvelope(
        event_id="evt-stale-epoch",
        run_id="run-inv-15",
        execution_epoch=2,
        event_type=RunEventType.VALIDATION_COMPLETED,
        payload={},
        timestamp=now,
    )
    with pytest.raises(StaleEventError) as exc_info:
        registry.process_event(stale_epoch_event)
    assert exc_info.value.code == RunBlockingReason.STALE_RUN_MUTATION.value


def test_inv16_old_agent_exited_cannot_terminate_new_run():
    """16. old AGENT_EXITED cannot terminate new active run -> PASS"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    run_a = AgentRunIdentity(
        run_id="run-a",
        task_id="task-16",
        node_id="node-16",
        workspace_id="ws-16",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    run_b = AgentRunIdentity(
        run_id="run-b",
        task_id="task-16",
        node_id="node-16",
        workspace_id="ws-16",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=2,
        start_time=now,
    )
    registry.spawn_agent(run_a)
    registry.spawn_agent(run_b)

    # Late exit event from Run A arrives
    late_exit_event = RunEventEnvelope(
        event_id="evt-late-exit",
        run_id="run-a",
        execution_epoch=1,
        event_type=RunEventType.AGENT_RUN_EXITED,
        payload={"exit_code": 0},
        timestamp=now,
    )
    with pytest.raises(StaleEventError) as exc_info:
        registry.process_event(late_exit_event)
    assert exc_info.value.code == RunBlockingReason.STALE_RUN_EVENT.value

    # Run B must remain active and LIVE!
    active_b = registry.get_active_run_for_slot("task-16", "node-16", "ws-16")
    assert active_b.identity.run_id == "run-b"
    assert active_b.state == RunState.LIVE


def test_inv17_old_validation_callback_cannot_mutate_current_run():
    """17. old validation callback cannot mutate current run -> PASS"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    run_a = AgentRunIdentity(
        run_id="run-a-17",
        task_id="task-17",
        node_id="node-17",
        workspace_id="ws-17",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    run_b = AgentRunIdentity(
        run_id="run-b-17",
        task_id="task-17",
        node_id="node-17",
        workspace_id="ws-17",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=2,
        start_time=now,
    )
    registry.spawn_agent(run_a)
    registry.spawn_agent(run_b)

    late_val_event = RunEventEnvelope(
        event_id="evt-val-17",
        run_id="run-a-17",
        execution_epoch=1,
        event_type=RunEventType.VALIDATION_COMPLETED,
        payload={"verdict": "PASS"},
        timestamp=now,
    )
    with pytest.raises(StaleEventError):
        registry.process_event(late_val_event)

    active_b = registry.get_active_run_for_slot("task-17", "node-17", "ws-17")
    assert active_b.identity.run_id == "run-b-17"


def test_inv18_duplicate_spawn_same_run_id():
    """18. duplicate spawn request same run_id -> one logical spawn"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-dup-18",
        task_id="task-18",
        node_id="node-18",
        workspace_id="ws-18",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    rec1, s1 = registry.spawn_agent(ident)
    rec2, s2 = registry.spawn_agent(ident)
    assert s1 == SpawnStatus.SPAWNED
    assert s2 == SpawnStatus.ALREADY_ACTIVE
    assert rec1 == rec2
    assert len(registry.list_runs()) == 1


def test_inv19_different_run_tries_same_active_slot():
    """19. different run tries same active slot -> DUPLICATE_ACTIVE_RUN"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident1 = AgentRunIdentity(
        run_id="run-19a",
        task_id="task-19",
        node_id="node-19",
        workspace_id="ws-19",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    ident2 = AgentRunIdentity(
        run_id="run-19b",
        task_id="task-19",
        node_id="node-19",
        workspace_id="ws-19",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    registry.spawn_agent(ident1)
    with pytest.raises(RunIdentityError) as exc_info:
        registry.spawn_agent(ident2)
    assert exc_info.value.code == RunBlockingReason.DUPLICATE_ACTIVE_RUN.value


def test_inv20_run_cannot_change_workspace_silently():
    """20. run cannot silently change workspace -> PASS"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-20",
        task_id="task-20",
        node_id="node-20",
        workspace_id="ws-20a",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    registry.register_run(ident)

    # Replay with modified workspace
    ident_mutated = AgentRunIdentity(
        run_id="run-20",
        task_id="task-20",
        node_id="node-20",
        workspace_id="ws-20b",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="local",
        execution_epoch=1,
        start_time=now,
    )
    with pytest.raises(RunIdentityError) as exc_info:
        registry.register_run(ident_mutated)
    assert exc_info.value.code == RunBlockingReason.RUN_IDENTITY_COLLISION.value


def test_inv21_run_cannot_change_execution_host_id_silently():
    """21. run cannot silently change execution_host_id -> PASS"""
    registry = ActiveRunRegistry()
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-21",
        task_id="task-21",
        node_id="node-21",
        workspace_id="ws-21",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="host-alpha",
        execution_epoch=1,
        start_time=now,
    )
    registry.register_run(ident)

    ident_mutated = AgentRunIdentity(
        run_id="run-21",
        task_id="task-21",
        node_id="node-21",
        workspace_id="ws-21",
        candidate_id=None,
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="host-beta",
        execution_epoch=1,
        start_time=now,
    )
    with pytest.raises(RunIdentityError) as exc_info:
        registry.register_run(ident_mutated)
    assert exc_info.value.code == RunBlockingReason.RUN_IDENTITY_COLLISION.value


def test_inv22_serialization_round_trip():
    """22. serialization round-trip -> PASS"""
    now = datetime.now(timezone.utc)
    ident = AgentRunIdentity(
        run_id="run-22",
        task_id="task-22",
        node_id="node-22",
        workspace_id="ws-22",
        candidate_id="cand-22",
        model="deepseek-chat",
        agent_capability="CODE_GENERATION",
        execution_host_id="host-22",
        execution_epoch=5,
        start_time=now,
    )
    record = AgentRunRecord(identity=ident, state=RunState.LIVE, updated_at=now)

    rec_json = record.to_json()
    reconstructed = AgentRunRecord.from_json(rec_json)
    assert reconstructed.identity.run_id == ident.run_id
    assert reconstructed.identity.execution_epoch == 5
    assert reconstructed.state == RunState.LIVE


def test_inv23_pr1_worktree_safety_tests_remain_pass(mock_workspace_env):
    """23. PR-1 worktree safety tests remain PASS"""
    ws_mgr = mock_workspace_env["ws_manager"]
    ws_id = mock_workspace_env["workspace_id"]
    # Path validation from PR-1 must still function and block escapes
    with pytest.raises(Exception):
        ws_mgr.validate_workspace_path(ws_id, "../../escaped.txt")


def test_inv24_canonical_checkout_remains_untouched(mock_workspace_env):
    """24. canonical checkout remains untouched -> PASS"""
    repo_dir = mock_workspace_env["repo_dir"]
    proc = subprocess.run(["git", "-C", str(repo_dir), "status", "--porcelain"], check=True, capture_output=True, text=True)
    assert proc.stdout.strip() == ""
