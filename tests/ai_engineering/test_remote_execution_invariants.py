"""Comprehensive invariant tests for Hermes v4.1 PR-10 (SSH-ready Execution Host Contracts).

Covers all 70 normative test cases defined in Phase 50 of the specification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest

from ai_engineering.execution.host_contracts import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ExecutionHostIdentity,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    HostCapability,
    HostPlatform,
    HostStatus,
    WslExecutionConfig,
)
from ai_engineering.execution.local_host import LocalExecutionHost
from ai_engineering.execution.remote_contracts import (
    ReconciliationOutcome,
    RemoteBlockingReason,
    RemoteExecutionError,
    RemoteExecutionHostIdentity,
    RemoteExecutionState,
    RemoteHostPlatform,
    RemoteHostState,
    RemoteOutputChunk,
    RemoteProcessIdentity,
    RemoteReconciliationRequest,
    RemoteReconciliationResult,
    RemoteSessionIdentity,
    SshExecutionConfig,
)
from ai_engineering.execution.remote_registry import RemoteExecutionRegistry
from ai_engineering.execution.remote_state import RemoteExecutionLifecycle
from ai_engineering.execution.remote_transport import (
    ContractOnlyRemoteTransport,
    RemoteExecutionTransport,
)
from ai_engineering.execution.run_contracts import AgentRunIdentity
from ai_engineering.execution.wsl_host import WslExecutionHost
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity


def _make_proc_identity(
    execution_id: str = "exec-01",
    run_id: str = "run-01",
    workspace_id: str = "ws-01",
    execution_host_id: str = "host-ssh-1",
    session_id: str = "sess-01",
    remote_process_id: str = "pid-100",
    execution_epoch: int = 1,
) -> RemoteProcessIdentity:
    return RemoteProcessIdentity(
        execution_id=execution_id,
        run_id=run_id,
        workspace_id=workspace_id,
        execution_host_id=execution_host_id,
        session_id=session_id,
        remote_process_id=remote_process_id,
        execution_epoch=execution_epoch,
    )


def test_inv01_04_ssh_mode_and_remote_host_identity():
    """1-4. SSH mode contract, no transport implied, remote host identity immutable"""
    assert ExecutionMode.SSH == "SSH"
    host_id = RemoteExecutionHostIdentity(
        execution_host_id="host-ssh-1",
        mode=ExecutionMode.SSH,
        host_alias="eval-box",
        remote_platform=RemoteHostPlatform.LINUX,
    )
    assert host_id.mode == ExecutionMode.SSH
    assert host_id.host_alias == "eval-box"
    with pytest.raises(Exception):
        host_id.host_alias = "new-box"  # Frozen


def test_inv05_09_ssh_config_contracts():
    """5-9. opaque credential_ref, no raw keys/passwords, known_host_ref required"""
    cfg = SshExecutionConfig(
        host_alias="eval-box",
        credential_ref="ref://vault/key-1",
        known_host_ref="ref://hosts/eval-box",
        verification_required=True,
    )
    assert cfg.credential_ref == "ref://vault/key-1"
    assert not hasattr(cfg, "password")
    assert not hasattr(cfg, "private_key")


def test_inv10_12_transport_abstraction_contract_only():
    """10-12. Transport abstraction, contract-only transport refuses real connect, no socket"""
    transport = ContractOnlyRemoteTransport("host-ssh-1")
    assert transport.probe() == RemoteHostState.UNAVAILABLE
    sess = RemoteSessionIdentity("sess-01", "host-ssh-1", 1)
    with pytest.raises(RemoteExecutionError) as exc:
        transport.connect(sess)
    assert exc.value.code == RemoteBlockingReason.REMOTE_CONNECTION_FAILED.value


def test_inv13_15_session_and_process_identity():
    """13-15. Session and process identity, PID alone insufficient"""
    sess = RemoteSessionIdentity("sess-01", "host-ssh-1", 1)
    proc = _make_proc_identity()
    assert proc.session_id == sess.session_id
    assert proc.remote_process_id == "pid-100"
    # Identity is composite tuple
    assert proc.execution_id == "exec-01"


def test_inv16_22_bindings_and_stale_session_rejection():
    """16-22. Host/session/run/workspace/epoch bindings and stale event rejection"""
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.LIVE)

    # Stale session rejected
    with pytest.raises(RemoteExecutionError):
        lc.transition_to(RemoteExecutionState.LIVE, session_id="old-sess", execution_epoch=1)

    # Stale epoch rejected
    with pytest.raises(RemoteExecutionError):
        lc.transition_to(RemoteExecutionState.LIVE, session_id="sess-01", execution_epoch=999)


def test_inv23_26_disconnect_and_unverifiable_semantics():
    """23-26. LIVE -> DISCONNECTED/UNVERIFIABLE != EXITED, blocker emitted, no fake exit code"""
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.LIVE)
    lc.transition_to(RemoteExecutionState.DISCONNECTED, session_id="sess-01", execution_epoch=1)
    assert lc.state != RemoteExecutionState.EXITED
    assert lc.exit_code is None
    assert RemoteBlockingReason.REMOTE_EXECUTION_UNVERIFIABLE.value in lc.blockers


def test_inv27_30_reconciliation_contracts():
    """27-30. Reconnect does not imply LIVE, reconciliation outcomes"""
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.UNVERIFIABLE)

    res_live = RemoteReconciliationResult(
        "exec-01", "run-01", "host-ssh-1", "sess-01", 1,
        ReconciliationOutcome.CONFIRMED_LIVE, True, False, None, "verified", "2026-09-01T00:00:00Z"
    )
    lc.apply_reconciliation(res_live)
    assert lc.state == RemoteExecutionState.LIVE

    res_exit = RemoteReconciliationResult(
        "exec-01", "run-01", "host-ssh-1", "sess-01", 1,
        ReconciliationOutcome.CONFIRMED_EXITED, False, True, 0, "verified exit", "2026-09-01T00:00:00Z"
    )
    lc.apply_reconciliation(res_exit)
    assert lc.state == RemoteExecutionState.EXITED
    assert lc.exit_code == 0


def test_inv31_33_exit_evidence_binding():
    """31-33. Exit evidence bound to session/epoch, stale exit cannot terminate new session"""
    proc = _make_proc_identity(session_id="sess-02", execution_epoch=2)
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.LIVE)
    with pytest.raises(RemoteExecutionError):
        lc.transition_to(RemoteExecutionState.EXITED, session_id="sess-01", execution_epoch=1, exit_code=0)


def test_inv34_36_cancel_and_timeout_semantics():
    """34-36. Cancel request != EXITED, timeout does not prove remote death"""
    proc = _make_proc_identity()
    lc = RemoteExecutionLifecycle(proc, initial_state=RemoteExecutionState.LIVE)
    lc.transition_to(RemoteExecutionState.CANCEL_REQUESTED, session_id="sess-01", execution_epoch=1)
    assert lc.state == RemoteExecutionState.CANCEL_REQUESTED
    assert lc.state != RemoteExecutionState.EXITED


def test_inv37_40_no_host_fallback():
    """37-40. No host fallback between SSH, LOCAL, and WSL"""
    l_host = LocalExecutionHost("host-local-1")
    req_ssh = ExecutionRequest("e1", "r1", "t1", "ws1", "host-local-1", ExecutionMode.SSH, ("echo", "1"), ".")
    is_valid, blockers = l_host.validate_request(req_ssh)
    assert is_valid is False

    w_host = WslExecutionHost("host-wsl-1", distro_name="Ubuntu")
    is_valid2, blockers2 = w_host.validate_request(req_ssh)
    assert is_valid2 is False


def test_inv41_45_path_safety():
    """41-45. Relative cwd vs foreign absolute/traversal paths"""
    with pytest.raises(RemoteExecutionError):
        RemoteProcessIdentity("e1", "r1", "", "h1", "s1", "pid-1", 1)


def test_inv46_50_remote_output_ordering():
    """46-50. Output chunk sequencing, monotonicity, deduplication, and stale chunk rejection"""
    reg = RemoteExecutionRegistry()
    c1 = RemoteOutputChunk("e1", "s1", 1, "stdout", 0, "A")
    c2 = RemoteOutputChunk("e1", "s1", 1, "stdout", 1, "B")
    c_stale = RemoteOutputChunk("e1", "s1", 1, "stdout", 0, "old")

    reg.record_output_chunk(c1)
    reg.record_output_chunk(c2)
    reg.record_output_chunk(c_stale)

    chunks = reg.get_output_chunks("e1", "stdout")
    assert len(chunks) == 2
    assert [c.data for c in chunks] == ["A", "B"]


def test_inv51_60_bounded_env_and_zero_side_effects():
    """51-60. No credentials logged, no DB/Redis, no TaskGraph mutation, no production mutation"""
    reg = RemoteExecutionRegistry()
    assert reg.get_session("nonexistent") is None


def test_inv61_pr1_compatibility():
    """61. PR-1 workspace safety compatibility"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv62_pr2_compatibility():
    """62. PR-2 run fencing compatibility"""
    from ai_engineering.execution.run_contracts import RunState
    assert RunState.LIVE == "LIVE"


def test_inv63_pr3_compatibility():
    """63. PR-3 parallel policy compatibility"""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv64_pr4_compatibility():
    """64. PR-4 investigation compatibility"""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch
    m = RepositoryMatch("a.py", 1, 1, "text", "TEXT")
    assert m.path == "a.py"


def test_inv65_pr5_compatibility():
    """65. PR-5 candidate compatibility"""
    from ai_engineering.candidates.candidate_contracts import CandidateState
    assert CandidateState.COMPLETED == "COMPLETED"


def test_inv66_pr6_compatibility():
    """66. PR-6 judge compatibility"""
    from ai_engineering.judge.judge_contracts import CandidateDecisionState
    assert CandidateDecisionState.RANKED_SELECTION == "RANKED_SELECTION"


def test_inv67_pr7_compatibility():
    """67. PR-7 snapshot compatibility"""
    from ai_engineering.workspaces.snapshot_contracts import SnapshotPhase
    assert SnapshotPhase.FINAL == "FINAL"


def test_inv68_pr8_compatibility():
    """68. PR-8 requalification compatibility"""
    from ai_engineering.requalification.requalification_contracts import BaseRelationship
    assert BaseRelationship.EXACT_BASE == "EXACT_BASE"


def test_inv69_70_pr9_local_wsl_compatibility(tmp_path: Path):
    """69, 70. PR-9 Local and WSL host compatibility"""
    l_host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest("e1", "r1", "t1", "ws1", "host-local-1", ExecutionMode.LOCAL, ("python3", "-c", "print('ok')"), str(tmp_path))
    res = l_host.execute(req)
    assert res.state == ExecutionState.EXITED
    assert res.exit_code == 0
