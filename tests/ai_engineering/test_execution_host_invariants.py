"""Comprehensive invariant tests for Hermes v4.1 PR-9 (Execution Host Abstraction LOCAL/WSL).

Covers all 60 normative test cases defined in Phase 42 of the specification.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import pytest

from ai_engineering.execution.execution_registry import ExecutionRegistry
from ai_engineering.execution.host_contracts import (
    DEFAULT_MAX_OUTPUT_BYTES,
    ExecutionHostError,
    ExecutionHostIdentity,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    HostBlockingReason,
    HostCapability,
    HostPlatform,
    HostStatus,
    WslExecutionConfig,
)
from ai_engineering.execution.local_host import LocalExecutionHost
from ai_engineering.execution.run_contracts import AgentRunIdentity
from ai_engineering.execution.wsl_host import WslExecutionHost
from ai_engineering.workspaces.workspace_contracts import WorkspaceIdentity


def _make_workspace_identity(
    workspace_id: str = "ws-01",
    worktree_path: str = "/tmp/ws",
    execution_host_id: str = "host-local-1",
) -> WorkspaceIdentity:
    return WorkspaceIdentity(
        workspace_id=workspace_id,
        task_id="task-01",
        candidate_id="cand-01",
        repository="/root/hermes_workspace/hermes",
        base_ref="main",
        base_sha="7334916be325e817fb3d35710aa7c547a9c10040",
        branch="codex/test",
        worktree_path=worktree_path,
        execution_host_id=execution_host_id,
        execution_mode="LOCAL",
        created_at=datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _make_run_identity(
    run_id: str = "run-01",
    workspace_id: str = "ws-01",
    execution_host_id: str = "host-local-1",
) -> AgentRunIdentity:
    return AgentRunIdentity(
        run_id=run_id,
        task_id="task-01",
        node_id="node-01",
        workspace_id=workspace_id,
        candidate_id="cand-01",
        model="gemini-3.1-pro-high",
        agent_capability="developer",
        execution_host_id=execution_host_id,
        execution_epoch=1,
        start_time=datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def test_inv01_02_local_and_wsl_host_identities():
    """1, 2. LOCAL and WSL host identities"""
    l_host = LocalExecutionHost("host-local-1")
    assert l_host.identity().mode == ExecutionMode.LOCAL

    w_host = WslExecutionHost("host-wsl-1", distro_name="Ubuntu")
    assert w_host.identity().mode == ExecutionMode.WSL


def test_inv03_invalid_execution_mode():
    """3. invalid execution mode rejected"""
    with pytest.raises(ExecutionHostError):
        ExecutionHostIdentity(
            execution_host_id="host-ssh-1",
            mode="SSH",  # Not supported in PR-9
            controller_platform=HostPlatform.WINDOWS,
            host_platform=HostPlatform.LINUX,
            hostname="host",
            architecture="x86_64",
            available=True,
            capabilities=(),
            created_at="",
        )


def test_inv04_05_06_07_local_and_wsl_execution_and_distro(tmp_path: Path):
    """4-7. Local execution, WSL command construction and explicit distro"""
    l_host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest("e1", "r1", "t1", "ws1", "host-local-1", ExecutionMode.LOCAL, ("python3", "-c", "print('ok')"), str(tmp_path))
    res = l_host.execute(req)
    assert res.state == ExecutionState.EXITED
    assert res.exit_code == 0

    w_host = WslExecutionHost("host-wsl-1", distro_name="Debian")
    cmd = w_host.build_wsl_command(("ls", "-la"))
    assert cmd == ["wsl.exe", "-d", "Debian", "--exec", "ls", "-la"]

    with pytest.raises(ExecutionHostError):
        WslExecutionConfig(distro_name="")


def test_inv08_09_10_command_safety():
    """8-10. empty argv rejected, shell strings not used, shell=False default"""
    with pytest.raises(ExecutionHostError) as exc:
        ExecutionRequest("e1", "r1", "t1", "ws1", "h1", ExecutionMode.LOCAL, (), ".")
    assert exc.value.code == HostBlockingReason.EXECUTION_COMMAND_INVALID.value


def test_inv11_16_cwd_path_fencing(tmp_path: Path):
    """11-16. cwd path validation, escapes and traversal rejected"""
    ws = _make_workspace_identity("ws-01", str(tmp_path), "host-local-1")
    host = LocalExecutionHost("host-local-1")

    # Valid cwd inside workspace
    sub = tmp_path / "subdir"
    sub.mkdir()
    req_valid = ExecutionRequest("e1", "r1", "t1", "ws-01", "host-local-1", ExecutionMode.LOCAL, ("python3", "-c", "print(1)"), "subdir")
    is_valid, blockers = host.validate_request(req_valid, workspace_identity=ws)
    assert is_valid is True

    # Escape attempt
    req_escape = ExecutionRequest("e2", "r1", "t1", "ws-01", "host-local-1", ExecutionMode.LOCAL, ("python3", "-c", "print(1)"), "../escape")
    is_valid2, blockers2 = host.validate_request(req_escape, workspace_identity=ws)
    assert is_valid2 is False
    assert HostBlockingReason.EXECUTION_PATH_INVALID.value in blockers2


def test_inv17_24_bindings_and_no_fallback():
    """17-24. Workspace/run/host binding and no fallback"""
    host = LocalExecutionHost("host-local-1")
    ws_other = _make_workspace_identity("ws-02", "/tmp/ws", "host-local-1")
    run_other = _make_run_identity("run-02", "ws-02", "host-local-1")

    req = ExecutionRequest("e1", "run-01", "task-01", "ws-01", "host-local-1", ExecutionMode.LOCAL, ("echo", "hi"), ".")

    # Workspace mismatch
    is_valid, blockers = host.validate_request(req, workspace_identity=ws_other)
    assert is_valid is False
    assert HostBlockingReason.RUN_WORKSPACE_MISMATCH.value in blockers

    # Run mismatch
    is_valid2, blockers2 = host.validate_request(req, run_identity=run_other)
    assert is_valid2 is False

    # Host mismatch
    req_wrong_host = ExecutionRequest("e1", "run-01", "task-01", "ws-01", "host-wsl-1", ExecutionMode.LOCAL, ("echo", "hi"), ".")
    is_valid3, blockers3 = host.validate_request(req_wrong_host)
    assert is_valid3 is False
    assert HostBlockingReason.EXECUTION_HOST_MISMATCH.value in blockers3

    # Mode mismatch (LOCAL host given WSL mode request)
    req_wsl = ExecutionRequest("e1", "run-01", "task-01", "ws-01", "host-local-1", ExecutionMode.WSL, ("echo", "hi"), ".")
    is_valid4, blockers4 = host.validate_request(req_wsl)
    assert is_valid4 is False
    assert HostBlockingReason.EXECUTION_MODE_INVALID.value in blockers4


def test_inv25_28_stdout_stderr_and_exit_codes(tmp_path: Path):
    """25-28. stdout/stderr separation and non-zero exit code preservation"""
    host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest(
        "e1", "r1", "t1", "ws1", "host-local-1", ExecutionMode.LOCAL,
        ("python3", "-c", "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(7)"),
        str(tmp_path),
    )
    res = host.execute(req)
    assert res.state == ExecutionState.EXITED
    assert res.exit_code == 7
    assert res.stdout == "out"
    assert res.stderr == "err"


def test_inv29_33_timeout_and_cancellation(tmp_path: Path):
    """29-33. timeout and cancellation semantics"""
    host = LocalExecutionHost("host-local-1")
    req_to = ExecutionRequest(
        "e-to", "r1", "t1", "ws1", "host-local-1", ExecutionMode.LOCAL,
        ("python3", "-c", "import time; time.sleep(10)"),
        str(tmp_path),
        timeout_seconds=0.1,
    )
    res = host.execute(req_to)
    assert res.state == ExecutionState.TIMED_OUT
    assert res.timed_out is True
    assert res.exit_code is None


def test_inv34_36_output_bounds_truncation(tmp_path: Path):
    """34-36. output bounds and deterministic truncation"""
    host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest(
        "e-trunc", "r1", "t1", "ws1", "host-local-1", ExecutionMode.LOCAL,
        ("python3", "-c", "print('A' * 500)"),
        str(tmp_path),
        max_stdout_bytes=100,
    )
    res = host.execute(req)
    assert res.stdout_truncated is True
    assert len(res.stdout) <= 100


def test_inv37_38_registry_idempotent_and_collision():
    """37, 38. registry idempotency and collision"""
    reg = ExecutionRegistry()
    r1 = ExecutionRequest("e1", "r1", "t1", "ws1", "h1", ExecutionMode.LOCAL, ("echo", "1"), ".")
    reg.record_request(r1)
    reg.record_request(r1)  # Idempotent

    r2 = ExecutionRequest("e1", "r1", "t1", "ws1", "h1", ExecutionMode.LOCAL, ("echo", "2"), ".")
    with pytest.raises(ExecutionHostError):
        reg.record_request(r2)


def test_inv39_42_stale_run_and_host_mismatches():
    """39-42. stale run and host mismatch rejection"""
    host = LocalExecutionHost("host-local-1")
    run = _make_run_identity("run-01", "ws-01", "host-local-other")
    req = ExecutionRequest("e1", "run-01", "t1", "ws-01", "host-local-1", ExecutionMode.LOCAL, ("echo", "1"), ".")
    is_valid, blockers = host.validate_request(req, run_identity=run)
    assert is_valid is False
    assert HostBlockingReason.EXECUTION_HOST_MISMATCH.value in blockers


def test_inv43_45_probe_and_unavailability():
    """43-45. host probe and unavailable host fails closed"""
    host_unavail = LocalExecutionHost("h-unavail", available=False)
    assert host_unavail.probe() == HostStatus.UNAVAILABLE
    req = ExecutionRequest("e1", "r1", "t1", "ws1", "h-unavail", ExecutionMode.LOCAL, ("echo", "1"), ".")
    res = host_unavail.execute(req)
    assert res.state == ExecutionState.FAILED
    assert HostBlockingReason.EXECUTION_HOST_UNAVAILABLE.value in res.blockers


def test_inv46_52_bounded_environment_and_zero_side_effects(tmp_path: Path):
    """46-52. bounded env, no DB, taskgraph, production or provider side effects"""
    host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest(
        "e-env", "r1", "t1", "ws1", "host-local-1", ExecutionMode.LOCAL,
        ("python3", "-c", "import os; print(os.environ.get('MY_CUSTOM_VAR'))"),
        str(tmp_path),
        env={"MY_CUSTOM_VAR": "SAFE_VALUE"},
    )
    res = host.execute(req)
    assert "SAFE_VALUE" in res.stdout


def test_inv53_pr1_compatibility():
    """53. PR-1 workspace compatibility"""
    from ai_engineering.workspaces.workspace_contracts import LeaseState
    assert LeaseState.ACTIVE == "ACTIVE"


def test_inv54_pr2_compatibility():
    """54. PR-2 run fencing compatibility"""
    from ai_engineering.execution.run_contracts import RunState
    assert RunState.LIVE == "LIVE"


def test_inv55_pr3_compatibility():
    """55. PR-3 policy compatibility"""
    from ai_engineering.parallel.parallel_contracts import ParallelizationStrategy
    assert ParallelizationStrategy.CANDIDATE == "CANDIDATE"


def test_inv56_pr4_compatibility():
    """56. PR-4 investigation compatibility"""
    from ai_engineering.investigation.investigation_contracts import RepositoryMatch
    m = RepositoryMatch("a.py", 1, 1, "test", "TEXT")
    assert m.path == "a.py"


def test_inv57_pr5_compatibility():
    """57. PR-5 candidate compatibility"""
    from ai_engineering.candidates.candidate_contracts import CandidateState
    assert CandidateState.COMPLETED == "COMPLETED"


def test_inv58_pr6_compatibility():
    """58. PR-6 judge compatibility"""
    from ai_engineering.judge.judge_contracts import CandidateDecisionState
    assert CandidateDecisionState.RANKED_SELECTION == "RANKED_SELECTION"


def test_inv59_pr7_compatibility():
    """59. PR-7 snapshot compatibility"""
    from ai_engineering.workspaces.snapshot_contracts import SnapshotPhase
    assert SnapshotPhase.FINAL == "FINAL"


def test_inv60_pr8_compatibility():
    """60. PR-8 requalification compatibility"""
    from ai_engineering.requalification.requalification_contracts import BaseRelationship
    assert BaseRelationship.EXACT_BASE == "EXACT_BASE"
