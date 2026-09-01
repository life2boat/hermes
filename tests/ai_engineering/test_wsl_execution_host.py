"""Unit tests for WslExecutionHost."""

from __future__ import annotations

import subprocess
import pytest

from ai_engineering.execution.host_contracts import (
    ExecutionMode,
    ExecutionRequest,
    ExecutionState,
    HostBlockingReason,
    HostPlatform,
    HostStatus,
    WslExecutionConfig,
)
from ai_engineering.execution.wsl_host import WslExecutionHost


def test_wsl_host_identity_and_command_construction():
    cfg = WslExecutionConfig(distro_name="Ubuntu-24.04")
    host = WslExecutionHost("host-wsl-1", config=cfg)
    ident = host.identity()
    assert ident.execution_host_id == "host-wsl-1"
    assert ident.mode == ExecutionMode.WSL
    assert ident.host_platform == HostPlatform.LINUX

    cmd = host.build_wsl_command(("pytest", "-v"), cwd="/root/repo")
    assert cmd == ["wsl.exe", "-d", "Ubuntu-24.04", "--cd", "/root/repo", "--exec", "pytest", "-v"]


def test_wsl_host_mock_execution():
    def mock_launcher(cmd: list[str], **kwargs):
        class MockProc:
            returncode = 0
            def communicate(self, timeout=None):
                return b"mocked wsl output", b""
            def poll(self):
                return 0
        return MockProc()

    host = WslExecutionHost("host-wsl-1", distro_name="Ubuntu", process_launcher=mock_launcher)
    req = ExecutionRequest(
        execution_id="exec-01",
        run_id="run-01",
        task_id="task-01",
        workspace_id="ws-01",
        execution_host_id="host-wsl-1",
        mode=ExecutionMode.WSL,
        argv=("echo", "hi"),
        cwd=".",
    )
    res = host.execute(req)
    assert res.state == ExecutionState.EXITED
    assert res.exit_code == 0
    assert res.stdout == "mocked wsl output"
