"""PR-13 WSL runtime activation tests (mock launcher + no-fallback proofs)."""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ai_engineering.candidates.candidate_contracts import CandidateIdentity
from ai_engineering.execution.run_contracts import AgentRunIdentity
from ai_engineering.execution.wsl_host import WslExecutionHost
from ai_engineering.runtime.agent_runtime import ControlledAgentRuntime
from ai_engineering.runtime.runtime_contracts import AgentRuntimeError, RuntimeMode
from ai_engineering.runtime.runtime_policy import RuntimePolicy
from ai_engineering.runtime.spawn_gate import authorize_spawn
from tests.ai_engineering.runtime_fixture_helpers import (
    CANDIDATE_ID,
    NODE_ID,
    RUN_ID,
    TASK_ID,
    WSL_HOST_ID,
    WORKSPACE_ID,
    RuntimeFixture,
    init_canonical_repo,
    make_authority,
    make_intent,
    make_local_fixture,
    make_request,
)


class _FakeWslProcess:
    def __init__(self, stdout=b"ok", stderr=b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.terminated = False

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class TestWslActivation:
    def _build_wsl_fixture(self, tmp_path: Path):
        repo_dir = tmp_path / "canonical_repo"
        base_sha = init_canonical_repo(repo_dir)
        from ai_engineering.execution.run_registry import ActiveRunRegistry
        from ai_engineering.workspaces.workspace_manager import WorkspaceManager

        workspace_manager = WorkspaceManager(repo_dir)
        run_registry = ActiveRunRegistry(workspace_manager=workspace_manager)
        workspace, lease = workspace_manager.create_isolated_workspace(
            workspace_id=WORKSPACE_ID,
            task_id=TASK_ID,
            candidate_id=CANDIDATE_ID,
            repository="life2boat/hermes",
            base_ref="refs/heads/main",
            base_sha=base_sha,
            branch="codex/candidate/task-1/cand-1",
            worktree_path=tmp_path / "workspaces" / WORKSPACE_ID,
            execution_host_id=WSL_HOST_ID,
            execution_mode="WSL",
            owner_run_id=RUN_ID,
            auto_acquire_lease=True,
        )
        run_identity = AgentRunIdentity(
            run_id=RUN_ID,
            task_id=TASK_ID,
            node_id=NODE_ID,
            workspace_id=WORKSPACE_ID,
            candidate_id=CANDIDATE_ID,
            model="test-model",
            agent_capability="CANDIDATE_IMPLEMENTATION",
            execution_host_id=WSL_HOST_ID,
            execution_epoch=1,
            start_time=datetime.now(timezone.utc),
        )
        run_registry.spawn_agent(run_identity)
        candidate = CandidateIdentity(
            candidate_id=CANDIDATE_ID,
            task_id=TASK_ID,
            node_id=NODE_ID,
            base_sha=base_sha,
            workspace_id=WORKSPACE_ID,
            run_id=RUN_ID,
        )
        fake_process = _FakeWslProcess(stdout=b"wsl-ok")

        def launcher(cmd, **kwargs):
            launcher.calls.append((cmd, kwargs))
            return fake_process

        launcher.calls = []
        wsl_host = WslExecutionHost(
            execution_host_id=WSL_HOST_ID,
            distro_name="Ubuntu",
            process_launcher=launcher,
        )
        runtime = ControlledAgentRuntime(
            policy=RuntimePolicy(mode=RuntimeMode.SHADOW_WSL),
            workspace_manager=workspace_manager,
            run_registry=run_registry,
            wsl_host=wsl_host,
            parent_env={"PATH": "C:\\fake-path"},
        )
        intent = make_intent(base_sha)
        return (
            RuntimeFixture(
                runtime=runtime,
                workspace_manager=workspace_manager,
                run_registry=run_registry,
                workspace=workspace,
                lease=lease,
                run_identity=run_identity,
                candidate=candidate,
                intent=intent,
                authority=make_authority(),
                base_sha=base_sha,
                canonical_root=repo_dir,
            ),
            launcher,
        )

    def test_wsl_spawn_via_mock_launcher(self, tmp_path):
        fx, launcher = self._build_wsl_fixture(tmp_path)
        request = make_request(
            fx,
            argv=("python3", "-c", "print('hi')"),
            execution_id="exec-wsl1",
            execution_host_id=WSL_HOST_ID,
        )
        evidence = fx.runtime.execute_agent_process(
            request,
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.exit_proven is True
        assert evidence.exit_code == 0
        assert "wsl-ok" in evidence.stdout
        assert evidence.blockers == ()
        # argv preserved through wsl.exe --exec without shell interpolation
        cmd, kwargs = launcher.calls[0]
        assert cmd[0].endswith("wsl.exe") or cmd[0] == "wsl"
        assert "--exec" in cmd
        assert "python3" in cmd
        assert kwargs.get("shell") is False
        assert kwargs.get("env") is not None
        assert "GITHUB_TOKEN" not in kwargs["env"]

    def test_wsl_failure_never_falls_back_to_local(self, tmp_path):
        fx, _launcher = self._build_wsl_fixture(tmp_path)
        # Request bound to an unregistered LOCAL host id while the policy is
        # SHADOW_WSL: mismatch must block, never silently re-run elsewhere.
        request = make_request(
            fx,
            execution_id="exec-wsl2",
            execution_host_id="host-local-unused",
        )
        with pytest.raises(AgentRuntimeError) as exc:
            fx.runtime.execute_agent_process(
                request,
                intent=fx.intent,
                authority=fx.authority,
                run_identity=fx.run_identity,
                candidate=fx.candidate,
            )
        assert exc.value.code == "EXECUTION_HOST_MISMATCH"

    def test_local_request_under_wsl_policy_blocked(self, tmp_path):
        fx = make_local_fixture(tmp_path / "local")  # SHADOW_LOCAL default
        request = make_request(fx, execution_id="exec-wsl3")
        # A SHADOW_WSL policy against a LOCAL workspace must not authorize.
        authorization = authorize_spawn(
            request,
            policy=RuntimePolicy(mode=RuntimeMode.SHADOW_WSL),
            intent=fx.intent,
            authority=fx.authority,
            workspace=fx.workspace,
            run_record=fx.run_registry.get_run(RUN_ID),
            host=fx.runtime._resolve_host_identity(request),
            candidate=fx.candidate,
            workspace_manager=fx.workspace_manager,
        )
        assert "EXECUTION_MODE_INVALID" in authorization.blockers

    def test_wsl_host_unavailable_blocks(self, tmp_path):
        fx, _launcher = self._build_wsl_fixture(tmp_path)
        from dataclasses import replace

        fx.runtime._wsl_host._identity = replace(
            fx.runtime._wsl_host._identity, available=False
        )
        request = make_request(
            fx,
            argv=("python3", "-c", "print('hi')"),
            execution_id="exec-wsl4",
            execution_host_id=WSL_HOST_ID,
        )
        with pytest.raises(AgentRuntimeError):
            fx.runtime.execute_agent_process(
                request,
                intent=fx.intent,
                authority=fx.authority,
                run_identity=fx.run_identity,
                candidate=fx.candidate,
            )

    def test_real_wsl_smoke(self, tmp_path):
        def _probe_wsl(args, timeout=30):
            try:
                return subprocess.run(args, capture_output=True, timeout=timeout, check=False)
            except (FileNotFoundError, OSError):
                return None

        wsl_check = _probe_wsl(["wsl.exe", "--status"])
        if wsl_check is None or wsl_check.returncode != 0:
            pytest.skip("WSL not available on this host")
        list_out = _probe_wsl(["wsl.exe", "-l", "-q"])
        if list_out is None:
            pytest.skip("WSL not available on this host")
        names = list_out.stdout.decode("utf-16-le", errors="ignore") if list_out.stdout else ""
        distro = None
        for line in names.splitlines():
            line = line.strip().strip("\x00")
            if line and not line.startswith(("Windows", "Copyright")):
                distro = line
                break
        if not distro:
            pytest.skip("No WSL distro installed on this host")
        # Probe actual WSL command health; a broken WSL service (e.g. RPC
        # 0x8007xxxx) is an environmental condition, not a code failure.
        probe = _probe_wsl(
            ["wsl.exe", "-d", distro, "--exec", "python3", "-c", "print('probe-ok')"],
            timeout=60,
        )
        if probe is None:
            pytest.skip("WSL not available on this host")
        probe_out = (probe.stdout or b"").decode("utf-8", errors="ignore")
        if probe.returncode != 0 or "probe-ok" not in probe_out or "\x00" in probe_out:
            pytest.skip("WSL distro present but service unhealthy on this host")

        fx0 = make_local_fixture(tmp_path / "wslreal", mode=RuntimeMode.SHADOW_WSL)
        workspace_manager = fx0.workspace_manager
        run_registry = fx0.run_registry
        workspace, lease = workspace_manager.create_isolated_workspace(
            workspace_id="ws-task-1-wsl",
            task_id=TASK_ID,
            candidate_id=CANDIDATE_ID,
            repository="life2boat/hermes",
            base_ref="refs/heads/main",
            base_sha=fx0.base_sha,
            branch="codex/candidate/task-1/cand-1-wsl",
            worktree_path=tmp_path / "workspaces" / "ws-task-1-wsl",
            execution_host_id=WSL_HOST_ID,
            execution_mode="WSL",
            owner_run_id="run-wsl",
            auto_acquire_lease=True,
        )
        run_identity = AgentRunIdentity(
            run_id="run-wsl",
            task_id=TASK_ID,
            node_id=NODE_ID,
            workspace_id="ws-task-1-wsl",
            candidate_id=CANDIDATE_ID,
            model="test-model",
            agent_capability="CANDIDATE_IMPLEMENTATION",
            execution_host_id=WSL_HOST_ID,
            execution_epoch=1,
            start_time=datetime.now(timezone.utc),
        )
        run_registry.spawn_agent(run_identity)
        candidate = CandidateIdentity(
            candidate_id=CANDIDATE_ID,
            task_id=TASK_ID,
            node_id=NODE_ID,
            base_sha=fx0.base_sha,
            workspace_id="ws-task-1-wsl",
            run_id="run-wsl",
        )
        wsl_host = WslExecutionHost(
            execution_host_id=WSL_HOST_ID,
            distro_name=distro,
        )
        runtime = ControlledAgentRuntime(
            policy=RuntimePolicy(mode=RuntimeMode.SHADOW_WSL),
            workspace_manager=workspace_manager,
            run_registry=run_registry,
            wsl_host=wsl_host,
            parent_env={"PATH": "C:\\fake-path"},
        )
        fx = RuntimeFixture(
            runtime=runtime,
            workspace_manager=workspace_manager,
            run_registry=run_registry,
            workspace=workspace,
            lease=lease,
            run_identity=run_identity,
            candidate=candidate,
            intent=make_intent(fx0.base_sha),
            authority=make_authority(),
            base_sha=fx0.base_sha,
            canonical_root=fx0.canonical_root,
        )
        request = make_request(
            fx,
            argv=("python3", "-c", "print('wsl-real-ok')"),
            execution_id="exec-wsl-real",
            execution_host_id=WSL_HOST_ID,
            workspace_id="ws-task-1-wsl",
            run_id="run-wsl",
        )
        evidence = fx.runtime.execute_agent_process(
            request,
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        # wsl.exe reports service failures (e.g. RPC 0x8007072c) as UTF-16
        # text; this is an environmental condition, not a runtime defect.
        if "\x00" in evidence.stdout or "\x00" in evidence.stderr:
            pytest.skip("WSL service error during smoke (environmental)")
        assert evidence.exit_proven is True
        assert "wsl-real-ok" in evidence.stdout
