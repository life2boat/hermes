"""PR-13.1 corrective tests: WSL transport vs agent-child environment separation.

Covers C7/C8:

- TRANSPORT_ENVIRONMENT != AGENT_CHILD_ENVIRONMENT: the environment used
  to launch the ``wsl.exe`` transport comes from a controller-side
  allowlist and is never replaced by the sanitized agent child
  environment (a poisoned controller child PATH cannot break transport
  launch);
- the agent child inside WSL receives a deny-by-default environment
  injected via ``env -i`` (no secrets, POSIX-flavored, default Linux PATH);
- launch failures surface as FAILED evidence, never silent skips
  (WSL_SKIP_FALSE_GREEN_RISK).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from ai_engineering.execution.host_contracts import ExecutionMode, ExecutionRequest
from ai_engineering.execution.wsl_host import (
    WslExecutionHost,
    build_wsl_transport_environment,
)
from ai_engineering.runtime.runtime_policy import build_child_environment

WSL_HOST = "host-wsl"


def _wsl_request(
    execution_id: str,
    argv: tuple[str, ...] = ("python3", "-c", "print('x')"),
    *,
    env: dict | None = None,
    inherit_environment: bool = False,
    timeout: float = 10.0,
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=execution_id,
        run_id="run-w1",
        task_id="task-w1",
        workspace_id="ws-w1",
        execution_host_id=WSL_HOST,
        mode=ExecutionMode.WSL,
        argv=argv,
        cwd="/tmp",
        env=env if env is not None else {},
        inherit_environment=inherit_environment,
        timeout_seconds=timeout,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        created_at="2026-09-02T00:00:00Z",
    )


class _CapturingLauncher:
    """Mock launcher recording the command and environment it was given."""

    def __init__(self, proc_factory=None):
        self.calls: list[tuple[list[str], dict | None]] = []
        self._proc_factory = proc_factory

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs.get("env")))
        if self._proc_factory is not None:
            return self._proc_factory(cmd, **kwargs)
        return subprocess.Popen(
            (sys_executable(), "-c", "pass"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )


def sys_executable() -> str:
    import sys

    return sys.executable


def _host_with(launcher: _CapturingLauncher) -> WslExecutionHost:
    return WslExecutionHost(
        execution_host_id=WSL_HOST,
        distro_name="Ubuntu",
        process_launcher=launcher,
    )


class TestTransportChildEnvironmentSeparation:
    def test_transport_env_is_controller_allowlist_not_child_env(self):
        """Launcher env must come from the controller allowlist, not request.env."""
        launcher = _CapturingLauncher()
        host = _host_with(launcher)
        host.execute(_wsl_request("exec-w1", env={"HOME": "/home/agent"}))
        _, launcher_env = launcher.calls[0]
        transport = build_wsl_transport_environment()
        # The transport env is exactly the controller-side allowlist.
        assert launcher_env == transport
        assert "HOME" not in launcher_env  # child env var must not leak to transport

    def test_poisoned_child_env_cannot_break_transport_discovery(self):
        """A sanitized child PATH is never used to launch wsl.exe (C7 root cause)."""
        launcher = _CapturingLauncher()
        host = _host_with(launcher)
        host.execute(_wsl_request("exec-w2", env={"PATH": "C:\\fake-path"}))
        cmd, launcher_env = launcher.calls[0]
        assert cmd[0] == host.config.wsl_binary
        # wsl.exe is resolved through the real controller PATH, not the
        # poisoned child PATH.
        assert launcher_env.get("PATH") == os.environ.get("PATH")
        assert launcher_env.get("PATH") != "C:\\fake-path"

    def test_agent_child_env_injected_via_env_i(self):
        launcher = _CapturingLauncher()
        host = _host_with(launcher)
        child_env = {"HOME": "/home/agent", "LANG": "C.UTF-8"}
        host.execute(_wsl_request("exec-w3", env=child_env))
        cmd, _ = launcher.calls[0]
        # ... --exec env -i KEY=VALUE ... <argv>
        exec_idx = cmd.index("--exec")
        assert cmd[exec_idx + 1] == "env"
        assert cmd[exec_idx + 2] == "-i"
        assert "HOME=/home/agent" in cmd
        assert "LANG=C.UTF-8" in cmd
        assert cmd[-3:] == ["python3", "-c", "print('x')"]

    def test_default_linux_path_injected_when_child_env_has_none(self):
        launcher = _CapturingLauncher()
        host = _host_with(launcher)
        host.execute(_wsl_request("exec-w4", env={"HOME": "/home/agent"}))
        cmd, _ = launcher.calls[0]
        path_entries = [c for c in cmd if c.startswith("PATH=")]
        assert len(path_entries) == 1
        assert path_entries[0].startswith("PATH=/usr")

    def test_legacy_inherit_environment_unchanged(self):
        """PR-9 behavior: inherit_environment=True keeps full controller env."""
        launcher = _CapturingLauncher()
        host = _host_with(launcher)
        host.execute(_wsl_request("exec-w5", inherit_environment=True))
        cmd, launcher_env = launcher.calls[0]
        assert launcher_env is not None
        for key, value in os.environ.items():
            assert launcher_env.get(key) == value
        assert "env" not in cmd[cmd.index("--exec") + 1 :][:2]

    def test_no_secrets_in_child_env(self):
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/home/agent",
            "GITHUB_TOKEN": "ghs_fake_token",
            "TELEGRAM_BOT_TOKEN": "123:fake",
            "DATABASE_URL": "postgres://fake",
        }
        child = build_child_environment(parent, extra={}, target_platform="posix")
        assert "GITHUB_TOKEN" not in child
        assert "TELEGRAM_BOT_TOKEN" not in child
        assert "DATABASE_URL" not in child

    def test_runner_wsl_child_env_is_posix_and_drops_controller_path(self, tmp_path):
        """The runner builds a POSIX-flavored child env for WSL requests."""
        from pathlib import Path

        from ai_engineering.runtime.process_runner import AgentProcessRunner
        from ai_engineering.runtime.runtime_contracts import AgentExecutionRequest

        host = WslExecutionHost(execution_host_id=WSL_HOST, distro_name="Ubuntu")
        runner = AgentProcessRunner(
            local_host=None,
            wsl_host=host,
            parent_env={"PATH": "C:\\fake-path", "SYSTEMROOT": "C:\\Windows", "HOME": "/h"},
        )
        request = AgentExecutionRequest(
            execution_id="exec-w6",
            run_id="run-w1",
            task_id="task-w1",
            node_id="node-1",
            cycle_id="cycle-1",
            workspace_id="ws-w1",
            candidate_id="cand-1",
            repository_id="life2boat/hermes",
            base_sha="a" * 40,
            execution_epoch=1,
            execution_host_id=WSL_HOST,
            agent_capability="CANDIDATE_IMPLEMENTATION",
            command_argv=("python3", "-c", "print('x')"),
            working_directory=".",
            timeout_seconds=10.0,
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
            authority_digest="d" * 64,
        )
        execution_request = runner.build_execution_request(
            request,
            workspace_root=Path(tmp_path),
            resolved_working_directory=Path(tmp_path),
        )
        assert execution_request.mode == ExecutionMode.WSL
        assert execution_request.inherit_environment is False
        # Windows controller PATH and SYSTEMROOT never reach the Linux child.
        assert "PATH" not in execution_request.env
        assert "SYSTEMROOT" not in execution_request.env


class TestWslLifecycleAndSkipPolicy:
    def test_launch_failure_is_failed_not_skip(self):
        """Runtime-caused launch failures must surface as FAILED (C8)."""

        def broken_launcher(cmd, **kwargs):
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{cmd[0]}'")

        host = WslExecutionHost(
            execution_host_id=WSL_HOST,
            distro_name="Ubuntu",
            process_launcher=broken_launcher,
        )
        result = host.execute(_wsl_request("exec-w7"))
        assert result.state.value == "FAILED"
        assert result.exit_code is None
        assert "WSL process launch failed" in (result.error_message or "")
        assert host.lifecycle_state("exec-w7") == "FAILED"

    def test_wsl_timeout_reaps_transport_process(self):
        class _HangingProc:
            pid = 999

            def __init__(self):
                self.killed = False

            def poll(self):
                return 999 if self.killed else None

            def terminate(self):
                self.killed = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired(cmd="wsl", timeout=timeout or 0)
                return -15

            def communicate(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired(cmd="wsl", timeout=timeout or 0)
                return (b"", b"")

        proc = _HangingProc()
        launcher = _CapturingLauncher(proc_factory=lambda cmd, **kwargs: proc)
        host = WslExecutionHost(
            execution_host_id=WSL_HOST,
            distro_name="Ubuntu",
            process_launcher=launcher,
        )
        result = host.execute(_wsl_request("exec-w8", timeout=0.5))
        assert result.timed_out is True
        assert result.exit_code is None
        assert proc.killed is True
        assert host.lifecycle_state("exec-w8") == "TIMED_OUT"

    def test_wsl_cancel_before_registration_is_consumed(self):
        class _CancellableProc:
            pid = 998
            returncode = -15

            def __init__(self):
                self.terminated = False

            def poll(self):
                return -15 if self.terminated else None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.terminated = True

            def wait(self, timeout=None):
                return -15

            def communicate(self, timeout=None):
                return (b"", b"")

        host = WslExecutionHost(execution_host_id=WSL_HOST, distro_name="Ubuntu")
        # Record the cancel before the process exists (spawn in flight).
        assert host.request_cancel("exec-w9") is True
        proc = _CancellableProc()
        host._process_launcher = lambda cmd, **kwargs: proc
        result = host.execute(_wsl_request("exec-w9", timeout=10.0))
        assert proc.terminated is True
        assert result.cancelled is True
        assert result.state.value == "EXITED"

    def test_transport_allowlist_excludes_credential_shaped_names(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_fake")
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        transport = build_wsl_transport_environment()
        assert "GITHUB_TOKEN" not in transport
        assert "PATH" in transport
