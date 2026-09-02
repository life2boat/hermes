"""PR-13 runtime policy tests: activation modes, command policy, environment."""

from __future__ import annotations

import pytest

from ai_engineering.runtime.runtime_contracts import AgentRuntimeError, RuntimeMode
from ai_engineering.runtime.runtime_policy import (
    RuntimePolicy,
    build_child_environment,
    name_is_secret_like,
    validate_runtime_command,
)


class TestRuntimePolicy:
    def test_default_mode_is_disabled(self):
        assert RuntimePolicy().mode == RuntimeMode.DISABLED

    def test_disabled_policy_has_no_execution_mode(self):
        with pytest.raises(AgentRuntimeError):
            RuntimePolicy().requires_mode()

    def test_shadow_local_requires_local(self):
        assert RuntimePolicy(mode=RuntimeMode.SHADOW_LOCAL).requires_mode() == "LOCAL"

    def test_shadow_wsl_requires_wsl(self):
        assert RuntimePolicy(mode=RuntimeMode.SHADOW_WSL).requires_mode() == "WSL"

    def test_unknown_mode_rejected(self):
        with pytest.raises(AgentRuntimeError):
            RuntimePolicy(mode="PRODUCTION")  # type: ignore[arg-type]

    def test_invalid_budget_rejected(self):
        with pytest.raises(AgentRuntimeError):
            RuntimePolicy(mode=RuntimeMode.SHADOW_LOCAL, max_concurrent_processes=0)


class TestCommandPolicy:
    def test_python_allowed(self):
        validate_runtime_command(("python", "-c", "print(1)"))

    def test_git_status_allowed(self):
        validate_runtime_command(("git", "status", "--porcelain"))

    def test_git_push_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("git", "push", "origin", "main"))

    def test_git_reset_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("git", "reset", "--hard", "HEAD~1"))

    def test_gh_merge_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("gh", "pr", "merge", "1"))

    def test_docker_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("docker", "compose", "up"))

    def test_kubectl_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("kubectl", "delete", "pod", "x"))

    def test_ssh_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("ssh", "user@host"))

    def test_scp_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("scp", "a", "b"))

    def test_rsync_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("rsync", "-a", "a", "b"))

    def test_curl_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("curl", "https://example.com"))

    def test_wget_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("wget", "https://example.com"))

    def test_shell_bash_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("bash", "-c", "echo hi"))

    def test_shell_cmd_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("cmd", "/c", "echo hi"))

    def test_powershell_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("powershell", "-Command", "ls"))

    def test_sqlite3_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("sqlite3", "prod.db"))

    def test_systemctl_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("systemctl", "restart", "x"))

    def test_exe_suffix_normalized(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("ssh.exe", "host"))

    def test_pathed_forbidden_binary_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("C:\\Windows\\System32\\curl.exe", "https://x"))

    def test_empty_argv_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(())

    def test_wsl_invocation_rejected(self):
        with pytest.raises(AgentRuntimeError):
            validate_runtime_command(("wsl", "-d", "Ubuntu", "--", "ls"))


class TestEnvironmentPolicy:
    PARENT = {
        "PATH": "/usr/bin",
        "GITHUB_TOKEN": "ghp_sentinel",
        "TELEGRAM_BOT_TOKEN": "tg_sentinel",
        "OPENAI_API_KEY": "sk_sentinel",
        "ANTHROPIC_API_KEY": "ak_sentinel",
        "AWS_SECRET_ACCESS_KEY": "aws_sentinel",
        "HERMES_OBSERVABILITY_SECRET_SENTINEL_DO_NOT_EXPOSE": "sentinel",
        "DATABASE_URL": "postgres://...",
        "QDRANT_URL": "http://...",
        "SSH_AUTH_SOCK": "/tmp/sock",
    }

    def test_deny_by_default(self):
        child = build_child_environment(self.PARENT)
        assert set(child) == {"PATH"}

    def test_allowlisted_names_copied(self):
        child = build_child_environment(self.PARENT)
        assert child["PATH"] == "/usr/bin"

    def test_no_token_keys(self):
        child = build_child_environment(self.PARENT)
        assert "GITHUB_TOKEN" not in child
        assert "TELEGRAM_BOT_TOKEN" not in child

    def test_no_api_keys(self):
        child = build_child_environment(self.PARENT)
        assert "OPENAI_API_KEY" not in child
        assert "ANTHROPIC_API_KEY" not in child

    def test_no_secret_keys(self):
        child = build_child_environment(self.PARENT)
        assert "AWS_SECRET_ACCESS_KEY" not in child
        assert "HERMES_OBSERVABILITY_SECRET_SENTINEL_DO_NOT_EXPOSE" not in child

    def test_no_database_or_qdrant_urls(self):
        child = build_child_environment(self.PARENT)
        assert "DATABASE_URL" not in child
        assert "QDRANT_URL" not in child

    def test_extra_benign_values_applied(self):
        child = build_child_environment(self.PARENT, extra={"HERMES_TASK_ID": "task-1"})
        assert child["HERMES_TASK_ID"] == "task-1"

    def test_extra_secret_name_rejected(self):
        with pytest.raises(AgentRuntimeError):
            build_child_environment(self.PARENT, extra={"MY_API_KEY": "sk-x"})

    def test_extra_token_name_rejected(self):
        with pytest.raises(AgentRuntimeError):
            build_child_environment(self.PARENT, extra={"SESSION_TOKEN": "t"})

    def test_credential_shaped_allowlist_name_ignored(self):
        parent = {"PATH": "/usr/bin", "MY_SECRET": "s"}
        child = build_child_environment(parent)
        assert "MY_SECRET" not in child

    def test_name_is_secret_like(self):
        assert name_is_secret_like("API_KEY")
        assert name_is_secret_like("auth_token")
        assert name_is_secret_like("PRIVATE_KEY_PATH")
        assert not name_is_secret_like("PATH")
        assert not name_is_secret_like("HERMES_TASK_ID")
