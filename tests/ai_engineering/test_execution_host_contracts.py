"""Unit tests for ExecutionHost contracts and serialization."""

from __future__ import annotations

import pytest

from ai_engineering.execution.host_contracts import (
    DEFAULT_MAX_OUTPUT_BYTES,
    EXECUTION_HOST_CONTRACT_VERSION,
    ExecutionHostError,
    ExecutionHostIdentity,
    ExecutionMode,
    ExecutionRequest,
    ExecutionResult,
    ExecutionState,
    HostBlockingReason,
    HostCapability,
    HostPlatform,
    WslExecutionConfig,
)
from ai_engineering.workspaces.workspace_contracts import ExecutionMode as WsExecutionMode


def test_execution_mode_compatibility():
    assert ExecutionMode.LOCAL == "LOCAL"
    assert ExecutionMode.WSL == "WSL"
    assert WsExecutionMode.WSL == "WSL"


def test_execution_host_identity_serialization():
    ident = ExecutionHostIdentity(
        execution_host_id="host-01",
        mode=ExecutionMode.LOCAL,
        controller_platform=HostPlatform.WINDOWS,
        host_platform=HostPlatform.LINUX,
        hostname="node-1",
        architecture="x86_64",
        available=True,
        capabilities=(HostCapability.CAN_RUN_COMMANDS, HostCapability.CAN_CAPTURE_STDOUT),
        created_at="2026-09-01T00:00:00Z",
    )
    d = ident.to_dict()
    assert d["execution_host_id"] == "host-01"
    assert d["mode"] == "LOCAL"
    restored = ExecutionHostIdentity.from_dict(d)
    assert restored == ident


def test_execution_request_and_result_serialization():
    req = ExecutionRequest(
        execution_id="exec-01",
        run_id="run-01",
        task_id="task-01",
        workspace_id="ws-01",
        execution_host_id="host-01",
        mode=ExecutionMode.LOCAL,
        argv=("python3", "--version"),
        cwd="src",
    )
    d_req = req.to_dict()
    assert d_req["argv"] == ["python3", "--version"]

    res = ExecutionResult(
        execution_id="exec-01",
        run_id="run-01",
        workspace_id="ws-01",
        execution_host_id="host-01",
        state=ExecutionState.EXITED,
        exit_code=0,
        stdout="Python 3.14.4\n",
        stderr="",
        started_at="2026-09-01T00:00:00Z",
        completed_at="2026-09-01T00:00:01Z",
        timed_out=False,
        cancelled=False,
    )
    d_res = res.to_dict()
    assert d_res["schema_version"] == EXECUTION_HOST_CONTRACT_VERSION
    assert d_res["exit_code"] == 0
    restored = ExecutionResult.from_dict(d_res)
    assert restored == res
    assert ExecutionResult.from_json(res.to_json()) == res


def test_invalid_execution_mode_in_request_rejected():
    with pytest.raises(ExecutionHostError) as exc:
        ExecutionRequest(
            execution_id="exec-01",
            run_id="run-01",
            task_id="task-01",
            workspace_id="ws-01",
            execution_host_id="host-01",
            mode=ExecutionMode.CONTAINER,  # Not LOCAL or WSL
            argv=("echo", "hi"),
            cwd="src",
        )
    assert exc.value.code == HostBlockingReason.EXECUTION_MODE_INVALID.value
