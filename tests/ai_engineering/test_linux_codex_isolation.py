"""Comprehensive 15-test isolation matrix for Linux/WSL Codex Executor."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from typing import Any
import pytest

from ai_engineering.linear_dispatcher.contracts import (
    BlockReasonCode,
    DispatcherConfig,
    LinearTask,
    TaskState,
)
from ai_engineering.linear_dispatcher.dispatcher import AutonomousDispatcher
from ai_engineering.linear_dispatcher.github_service import (
    CIStatusResult,
    IGitHubService,
    PRCreationResult,
)
from ai_engineering.linear_dispatcher.lease_manager import LeaseManager
from ai_engineering.linear_dispatcher.task_executor import (
    CodexTaskExecutor,
    ExecutionResult,
    LinuxCodexTaskExecutor,
)
from ai_engineering.linear_dispatcher.worktree_service import WorktreeService


def make_linear_task(task_id: str = "HER-101", title: str = "Test Task") -> LinearTask:
    return LinearTask(
        id=task_id,
        uuid=f"uuid-{task_id}",
        title=title,
        description="Test description for isolation",
        state="Todo",
        priority=1,
        assignee="agent",
        labels=("agent:auto", "agent:shadow"),
        created_at="2026-10-05T00:00:00Z",
        updated_at="2026-10-05T00:00:00Z",
        url=f"https://linear.app/hermes/issue/{task_id}",
    )


class MockLinearClient:
    def __init__(self, tasks: list[LinearTask] | None = None) -> None:
        self.tasks = {t.id: t for t in (tasks or [])}
        self.comments: dict[str, list[str]] = {}

    def get_issue(self, issue_id: str) -> LinearTask | None:
        return self.tasks.get(issue_id)

    def list_issues(self, team: str = "Hermes", limit: int = 50) -> list[LinearTask]:
        return list(self.tasks.values())

    def get_issue_comments(self, issue_id: str) -> list[str]:
        return self.comments.get(issue_id, [])

    def add_comment(self, issue_id: str, body: str) -> bool:
        self.comments.setdefault(issue_id, []).append(body)
        return True

    def update_issue(self, issue_id: str, fields: dict[str, Any]) -> bool:
        t = self.tasks.get(issue_id)
        if not t:
            return False
        from dataclasses import replace
        new_state = fields.get("state", t.state)
        new_desc = fields.get("description", t.description)
        self.tasks[issue_id] = replace(t, state=new_state, description=new_desc)
        return True


class MockGitHubService(IGitHubService):
    def __init__(self, ci_status: str = "PASS") -> None:
        self.ci_status = ci_status
        self.created_prs: list[dict[str, Any]] = []

    def create_draft_pr(
        self,
        task: LinearTask,
        branch_name: str,
        base_sha: str,
        head_sha: str,
        cwd: Path | str,
    ) -> tuple[bool, PRCreationResult | None, str | None]:
        pr = PRCreationResult(
            pr_number=999,
            pr_url="https://github.com/life2boat/hermes/pull/999",
            base_sha=base_sha,
            head_sha=head_sha,
            is_draft=True,
        )
        self.created_prs.append({"task_id": task.id, "branch": branch_name, "pr": pr})
        return True, pr, None

    def get_ci_status(
        self,
        repo: str,
        expected_head_sha: str,
        pr_number: int,
    ) -> tuple[bool, CIStatusResult | None, BlockReasonCode | None]:
        return (
            True,
            CIStatusResult(
                overall_status=self.ci_status,
                head_sha=expected_head_sha,
                runs=[{"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
                details={"test": "SUCCESS"},
            ),
            None,
        )


def init_mock_git_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    readme = path / "README.md"
    readme.write_text("# Mock Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(path), check=True)
    res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(path), capture_output=True, text=True, check=True)
    return res.stdout.strip()


# ==============================================================================
# 15-Test Matrix
# ==============================================================================


# Test 1: On Windows host, CodexTaskExecutor fails closed
def test_windows_codex_executor_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    wt = tmp_path / "worktree"
    wt.mkdir()
    executor = CodexTaskExecutor(allow_windows_unsandboxed=False)
    res = executor.execute(make_linear_task(), wt, "base_sha")
    assert res.status == "FAILED"
    assert res.error_reason == "WINDOWS_CODEX_UNSUPPORTED"


# Test 2: On Windows host, CodexTaskExecutor.health() returns False
def test_windows_codex_executor_health_check_fails(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    executor = CodexTaskExecutor(codex_bin="codex", allow_windows_unsandboxed=False)
    assert executor.health() is False


# Test 3: LinuxCodexTaskExecutor health check success
def test_linux_codex_executor_health_check_success():
    def mock_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="/usr/bin/bwrap\n")

    executor = LinuxCodexTaskExecutor(
        distro="Ubuntu",
        bwrap_bin="bwrap",
        codex_bin="codex",
        credentials_path="/var/lib/hermes/codex-credentials/auth.json",
        is_windows=True,
        runner=mock_runner,
    )
    assert executor.health() is True


# Test 4: LinuxCodexTaskExecutor health check fails when bwrap missing
def test_linux_codex_executor_health_check_missing_bwrap():
    def mock_runner(cmd, **kwargs):
        if "bwrap" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="/bin/codex\n")

    executor = LinuxCodexTaskExecutor(is_windows=True, runner=mock_runner)
    assert executor.health() is False


# Test 5: LinuxCodexTaskExecutor health check fails when codex missing
def test_linux_codex_executor_health_check_missing_codex():
    def mock_runner(cmd, **kwargs):
        if "codex" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="/bin/bwrap\n")

    executor = LinuxCodexTaskExecutor(is_windows=True, runner=mock_runner)
    assert executor.health() is False


# Test 6: LinuxCodexTaskExecutor health check fails when credentials missing
def test_linux_codex_executor_health_check_missing_credentials():
    def mock_runner(cmd, **kwargs):
        if "test" in cmd and "-f" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="/bin/bwrap\n")

    executor = LinuxCodexTaskExecutor(is_windows=True, runner=mock_runner)
    assert executor.health() is False


# Test 7: Verify bwrap command construction arguments
def test_bwrap_command_construction():
    executor = LinuxCodexTaskExecutor(
        bwrap_bin="/usr/bin/bwrap",
        codex_bin="/usr/bin/codex",
        credentials_path="/var/lib/hermes/codex-credentials/auth.json",
        model="gpt-5",
        is_windows=True,
    )
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    assert "/usr/bin/bwrap" in cmd
    assert "--ro-bind" in cmd
    assert "/usr" in cmd
    assert "/bin" in cmd
    assert "/etc/ssl" in cmd
    assert "--tmpfs" in cmd
    assert "/run" in cmd
    assert "/tmp" in cmd
    assert "--dir" in cmd
    assert "/tmp/codex-home" in cmd
    assert "/var/lib/hermes/codex-credentials/auth.json" not in cmd
    assert "--bind" in cmd
    assert "/var/tmp/wt" in cmd
    assert "--clearenv" in cmd
    assert "CODEX_HOME" in cmd
    assert "/usr/bin/codex" in cmd
    assert "exec" in cmd
    assert "-C" in cmd
    assert "--approve-for-me" in cmd
    assert "--ephemeral" in cmd
    assert "-m" in cmd
    assert "gpt-5" in cmd
    assert cmd[-1] == "-"
    assert not any(cmd[i] == "--ro-bind" and cmd[i + 1] == "/" and cmd[i + 2] == "/" for i in range(len(cmd) - 2))


# Test 8: WSL command wrapping on Windows
def test_wsl_command_wrapping_on_windows(tmp_path):
    captured_cmd = []

    def mock_runner(cmd, **kwargs):
        captured_cmd.append(list(cmd))
        if "which" in cmd or "test" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0)
        if "wslpath" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="/mnt/d/wt\n")
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=" M file.py\n")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="success")

    wt = tmp_path / "worktree"
    wt.mkdir()
    executor = LinuxCodexTaskExecutor(distro="Ubuntu", is_windows=True, runner=mock_runner)
    res = executor.execute(make_linear_task(), wt, "base_sha")

    assert res.status == "SUCCESS"
    # Find execution command
    exec_cmds = [c for c in captured_cmd if "bwrap" in c]
    assert len(exec_cmds) > 0
    assert exec_cmds[0][0] == "wsl.exe"
    assert exec_cmds[0][1] == "-d"
    assert exec_cmds[0][2] == "Ubuntu"
    assert exec_cmds[0][3] == "--"


# Test 9: Native Linux command execution without wsl.exe
def test_native_linux_command_execution(tmp_path, monkeypatch):
    captured_cmd = []

    def mock_runner(cmd, **kwargs):
        captured_cmd.append(list(cmd))
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=" M file.py\n")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="success")

    wt = tmp_path / "worktree"
    wt.mkdir()
    creds = tmp_path / "auth.json"
    creds.write_text("{}", encoding="utf-8")

    executor = LinuxCodexTaskExecutor(
        credentials_path=creds,
        is_windows=False,
        runner=mock_runner,
    )
    monkeypatch.setattr(executor, "health", lambda: True)
    monkeypatch.setattr(executor, "_setup_ephemeral_credentials", lambda home: None)
    monkeypatch.setattr(executor, "_start_credential_unlinking_watcher", lambda home: None)
    monkeypatch.setattr(executor, "_verify_credential_unlinked", lambda home: True)
    monkeypatch.setattr(executor, "_cleanup_ephemeral_credentials", lambda home: None)
    res = executor.execute(make_linear_task(), wt, "base_sha")

    assert res.status == "SUCCESS"
    exec_cmds = [c for c in captured_cmd if "bwrap" in c]
    assert len(exec_cmds) > 0
    assert exec_cmds[0][0] == "bwrap"
    assert "wsl.exe" not in exec_cmds[0]


# Test 10: Worktree must exist
def test_worktree_must_exist(tmp_path):
    executor = LinuxCodexTaskExecutor()
    non_existent = tmp_path / "missing_dir_xyz"
    res = executor.execute(make_linear_task(), non_existent, "base_sha")
    assert res.status == "FAILED"
    assert res.error_reason == "INVALID_WORKTREE_PATH"


# Test 11: Timeout handling
def test_timeout_handling(tmp_path, monkeypatch):
    def timeout_runner(cmd, **kwargs):
        if "which" in cmd or "test" in cmd or "bash" in cmd or "rm" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0)
        if "wslpath" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="/tmp/wt\n")
        raise subprocess.TimeoutExpired(cmd, timeout=5)

    wt = tmp_path / "worktree"
    wt.mkdir()
    executor = LinuxCodexTaskExecutor(is_windows=True, runner=timeout_runner)
    res = executor.execute(make_linear_task(), wt, "base_sha")
    assert res.status == "FAILED"
    assert res.error_reason == "CODEX_EXEC_TIMEOUT"
    assert res.evidence.get("timed_out") is True


# Test 12: Non-zero exit code handling
def test_nonzero_exit_code_handling(tmp_path):
    def fail_runner(cmd, **kwargs):
        if "which" in cmd or "test" in cmd or "bash" in cmd or "rm" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0)
        if "wslpath" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="/tmp/wt\n")
        return subprocess.CompletedProcess(cmd, returncode=42, stdout="", stderr="syntax error")

    wt = tmp_path / "worktree"
    wt.mkdir()
    executor = LinuxCodexTaskExecutor(is_windows=True, runner=fail_runner)
    res = executor.execute(make_linear_task(), wt, "base_sha")
    assert res.status == "FAILED"
    assert res.error_reason == "CODEX_EXEC_FAILED_EXIT_42"
    assert res.evidence.get("exit_code") == 42


# Test 13: No changes detected blocks
def test_no_changes_detected_blocks(tmp_path):
    def empty_runner(cmd, **kwargs):
        if "which" in cmd or "test" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0)
        if "wslpath" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="/tmp/wt\n")
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="\n")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="success")

    wt = tmp_path / "worktree"
    wt.mkdir()
    executor = LinuxCodexTaskExecutor(is_windows=True, runner=empty_runner)
    res = executor.execute(make_linear_task(), wt, "base_sha")
    assert res.status == "BLOCKED"
    assert res.error_reason == "NO_CHANGES_PRODUCED"


# Test 14: Changes detected success
def test_changes_detected_success(tmp_path):
    def success_runner(cmd, **kwargs):
        if "which" in cmd or "test" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0)
        if "wslpath" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="/tmp/wt\n")
        if "status" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=" M app.py\n?? new.py\n")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="success")

    wt = tmp_path / "worktree"
    wt.mkdir()
    executor = LinuxCodexTaskExecutor(is_windows=True, runner=success_runner)
    res = executor.execute(make_linear_task(), wt, "base_sha")
    assert res.status == "SUCCESS"
    assert res.changed_files == ("app.py", "new.py")
    assert res.evidence.get("changed_count") == 2


# Test 15: AutonomousDispatcher integration with LinuxCodexTaskExecutor
def test_dispatcher_integration_with_linux_codex_executor(tmp_path):
    canonical_root = tmp_path / "canonical"
    canonical_sha = init_mock_git_repo(canonical_root)
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    config = DispatcherConfig(worker_id="test-worker")
    task = make_linear_task("HER-900")
    linear = MockLinearClient([task])
    gh = MockGitHubService(ci_status="PASS")
    leases = LeaseManager(tmp_path / "leases.json")
    wt_service = WorktreeService(canonical_root=canonical_root, base_sha=canonical_sha)

    def dispatch_runner(cmd, **kwargs):
        if "which" in cmd or "test" in cmd or "bash" in cmd or "rm" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0)
        if "wslpath" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=f"/tmp/{Path(cmd[-1]).name}\n")
        if "bwrap" in cmd:
            for wt_dir in wt_base.glob("*"):
                if wt_dir.is_dir():
                    (wt_dir / "feature.py").write_text("# feature\n", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="bwrap executed")
        return subprocess.run(cmd, **kwargs)

    executor = LinuxCodexTaskExecutor(is_windows=True, runner=dispatch_runner)
    dispatcher = AutonomousDispatcher(
        config=config,
        linear_client=linear,
        lease_manager=leases,
        worktree_service=wt_service,
        github_service=gh,
        worktree_base_dir=wt_base,
        canonical_main_sha=canonical_sha,
        task_executor=executor,
    )

    dispatcher._resolve_remote_main_sha = lambda *a, **kw: canonical_sha

    result = dispatcher.dispatch_one_task([task])
    assert result is not None
    assert result.final_state == TaskState.DONE
    assert len(gh.created_prs) == 1
    assert len(linear.comments[task.id]) == 1
    assert len(list(wt_base.glob("*"))) == 0


# ==============================================================================
# Phase 8: Credential & Secret Read Isolation Regression Tests
# ==============================================================================


def test_executor_does_not_ro_bind_entire_host_root():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # Entire host root must never be ro-bound
    for i in range(len(cmd) - 2):
        assert not (cmd[i] == "--ro-bind" and cmd[i + 1] == "/" and cmd[i + 2] == "/")


def test_executor_host_home_not_visible():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # /home must not be mounted
    for i in range(len(cmd) - 1):
        if cmd[i] in ("--bind", "--ro-bind"):
            assert cmd[i + 1] != "/home"


def test_executor_root_home_not_visible():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # /root must not be mounted
    for i in range(len(cmd) - 1):
        if cmd[i] in ("--bind", "--ro-bind"):
            assert cmd[i + 1] != "/root"


def test_executor_persistent_auth_file_not_shell_readable():
    cred_path = "/var/lib/hermes/codex-credentials/auth.json"
    executor = LinuxCodexTaskExecutor(credentials_path=cred_path, is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # Persistent credentials must NEVER be directly mounted in the command
    assert cred_path not in cmd


def test_executor_fake_secret_file_not_readable():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # /var must not be mounted
    for i in range(len(cmd) - 1):
        if cmd[i] in ("--bind", "--ro-bind"):
            assert cmd[i + 1] != "/var"


def test_executor_fake_secret_env_not_inherited():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # Environment must be cleared to prevent secret inheritance
    assert "--clearenv" in cmd


def test_executor_docker_socket_hidden():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # /run must be an isolated tmpfs, not host bind
    assert "--tmpfs" in cmd
    run_idx = cmd.index("/run")
    assert cmd[run_idx - 1] == "--tmpfs"


def test_executor_windows_drives_hidden():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # /mnt or /mnt/c must not be mounted
    for i in range(len(cmd) - 1):
        if cmd[i] in ("--bind", "--ro-bind"):
            assert not cmd[i + 1].startswith(("/mnt/c", "/mnt/d"))


def test_executor_unrelated_repo_not_readable():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    wt = "/var/tmp/authorized-wt"
    cmd = executor.build_bwrap_command(wt)
    # Only the authorized worktree is bound read-write
    bind_targets = [cmd[i + 2] for i in range(len(cmd) - 2) if cmd[i] == "--bind"]
    assert wt in bind_targets
    for target in bind_targets:
        assert target == wt or target == "/tmp/codex-home"


def test_executor_worktree_read_write_allowed():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    wt = "/var/tmp/my-task-worktree"
    cmd = executor.build_bwrap_command(wt)
    assert "--bind" in cmd
    wt_idx = cmd.index(wt)
    assert cmd[wt_idx - 1] == "--bind"
    assert cmd[wt_idx + 1] == wt


def test_executor_outside_write_blocked():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    # System directories must be mounted read-only (--ro-bind)
    assert cmd[cmd.index("/usr") - 1] == "--ro-bind"
    assert cmd[cmd.index("/etc/ssl") - 1] == "--ro-bind"


def test_windows_executor_remains_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    wt = tmp_path / "worktree"
    wt.mkdir()
    executor = CodexTaskExecutor(allow_windows_unsandboxed=False)
    res = executor.execute(make_linear_task(), wt, "base_sha")
    assert res.status == "FAILED"
    assert res.error_reason == "WINDOWS_CODEX_UNSUPPORTED"


def test_worker_not_ready_without_secret_isolation_health(tmp_path, monkeypatch):
    from ai_engineering.linear_dispatcher.worker import DispatcherWorker

    monkeypatch.setenv("GITHUB_TOKEN", "fake_gh")
    monkeypatch.setenv("LINEAR_API_KEY", "fake_linear")

    class BrokenIsolationExecutor(LinuxCodexTaskExecutor):
        def validate_isolation(self):
            return False, "ROOT_FILESYSTEM_EXPOSED"

    worker = DispatcherWorker(
        task_executor=BrokenIsolationExecutor(),
    )
    config = DispatcherConfig(worker_id="test-worker")
    ready = worker.validate_dependencies(
        config=config,
        wt_base=tmp_path / "wt_base",
        lease_path=tmp_path / "leases.json",
    )
    assert ready is False


def test_executor_validate_isolation_success():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    ok, err = executor.validate_isolation()
    assert ok is True
    assert err is None


def test_executor_validate_isolation_detects_root_mount():
    class BadExecutor(LinuxCodexTaskExecutor):
        def build_bwrap_command(self, worktree_linux_path, ephemeral_home_path="/tmp/codex-home"):
            cmd = super().build_bwrap_command(worktree_linux_path, ephemeral_home_path)
            return ["--ro-bind", "/", "/"] + cmd

    executor = BadExecutor(is_windows=True)
    ok, err = executor.validate_isolation()
    assert ok is False
    assert err == "ROOT_FILESYSTEM_EXPOSED"


def test_executor_validate_isolation_detects_missing_clearenv():
    class BadExecutor(LinuxCodexTaskExecutor):
        def build_bwrap_command(self, worktree_linux_path, ephemeral_home_path="/tmp/codex-home"):
            cmd = super().build_bwrap_command(worktree_linux_path, ephemeral_home_path)
            return [c for c in cmd if c != "--clearenv"]

    executor = BadExecutor(is_windows=True)
    ok, err = executor.validate_isolation()
    assert ok is False
    assert err == "CLEARENV_MISSING"


def test_executor_validate_isolation_detects_persistent_cred_mount():
    cred_file = "/var/lib/hermes/codex-credentials/auth.json"

    class BadExecutor(LinuxCodexTaskExecutor):
        def build_bwrap_command(self, worktree_linux_path, ephemeral_home_path="/tmp/codex-home"):
            cmd = super().build_bwrap_command(worktree_linux_path, ephemeral_home_path)
            return cmd + ["--ro-bind", cred_file, "/tmp/codex-home/auth.json"]

    executor = BadExecutor(credentials_path=cred_file, is_windows=True)
    ok, err = executor.validate_isolation()
    assert ok is False
    assert err == "PERSISTENT_CREDENTIALS_DIRECTLY_MOUNTED"


def test_executor_unshares_pid_namespace():
    executor = LinuxCodexTaskExecutor(is_windows=True)
    cmd = executor.build_bwrap_command("/var/tmp/wt")
    assert "--unshare-pid" in cmd


def test_executor_validate_isolation_detects_missing_unshare_pid():
    class BadExecutor(LinuxCodexTaskExecutor):
        def build_bwrap_command(self, worktree_linux_path, ephemeral_home_path="/tmp/codex-home"):
            cmd = super().build_bwrap_command(worktree_linux_path, ephemeral_home_path)
            return [c for c in cmd if c != "--unshare-pid"]

    executor = BadExecutor(is_windows=True)
    ok, err = executor.validate_isolation()
    assert ok is False
    assert err == "PID_NAMESPACE_NOT_UNSHARED"
