"""Unit tests for LocalExecutionHost."""

from __future__ import annotations

from pathlib import Path
import pytest

from ai_engineering.execution.host_contracts import (
    ExecutionMode,
    ExecutionRequest,
    ExecutionState,
    HostBlockingReason,
    HostStatus,
)
from ai_engineering.execution.local_host import LocalExecutionHost


def test_local_host_identity_and_probe():
    host = LocalExecutionHost("host-local-1")
    ident = host.identity()
    assert ident.execution_host_id == "host-local-1"
    assert ident.mode == ExecutionMode.LOCAL
    assert host.probe() == HostStatus.AVAILABLE


def test_local_host_simple_execution(tmp_path: Path):
    host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest(
        execution_id="exec-01",
        run_id="run-01",
        task_id="task-01",
        workspace_id="ws-01",
        execution_host_id="host-local-1",
        mode=ExecutionMode.LOCAL,
        argv=("python3", "-c", "print('hello local execution')"),
        cwd=str(tmp_path),
    )
    res = host.execute(req)
    assert res.state == ExecutionState.EXITED
    assert res.exit_code == 0
    assert "hello local execution" in res.stdout


def test_local_host_nonzero_exit(tmp_path: Path):
    host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest(
        execution_id="exec-02",
        run_id="run-01",
        task_id="task-01",
        workspace_id="ws-01",
        execution_host_id="host-local-1",
        mode=ExecutionMode.LOCAL,
        argv=("python3", "-c", "import sys; sys.exit(42)"),
        cwd=str(tmp_path),
    )
    res = host.execute(req)
    assert res.state == ExecutionState.EXITED
    assert res.exit_code == 42


def test_local_host_timeout(tmp_path: Path):
    host = LocalExecutionHost("host-local-1")
    req = ExecutionRequest(
        execution_id="exec-03",
        run_id="run-01",
        task_id="task-01",
        workspace_id="ws-01",
        execution_host_id="host-local-1",
        mode=ExecutionMode.LOCAL,
        argv=("python3", "-c", "import time; time.sleep(10)"),
        cwd=str(tmp_path),
        timeout_seconds=0.1,
    )
    res = host.execute(req)
    assert res.state == ExecutionState.TIMED_OUT
    assert res.timed_out is True
    assert res.exit_code is None
    assert HostBlockingReason.EXECUTION_TIMEOUT.value in res.blockers
