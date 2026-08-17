import pytest
import json
import subprocess
import os
from ops.secret_remediation_r1.preflight import run_compose_preflight, PreflightError


def make_mock_run(env_list, create_fail=False, ps_id="mock_id\n", ps_after_cleanup=""):
    def mock_run(cmd, *args, **kwargs):
        class DummyResult:
            def __init__(self, stdout="", stderr=""):
                self.stdout = stdout
                self.stderr = stderr

        # Handle dynamic project name checking
        if len(cmd) >= 4 and cmd[0:2] == ["docker", "compose"] and cmd[2] == "-p":
            project_name = cmd[3]
            cmd_type = cmd[4]
            if cmd_type == "create" and cmd[5:] == ["test"]:
                if create_fail:
                    raise subprocess.CalledProcessError(1, cmd, stderr="create error")
                return DummyResult()
            elif cmd_type == "ps" and cmd[5:] == ["--all", "--quiet", "test"]:
                return DummyResult(ps_id)
            elif cmd_type == "down" and cmd[5:] == ["--remove-orphans"]:
                if hasattr(mock_run, "down_fail") and mock_run.down_fail:
                    raise subprocess.CalledProcessError(1, cmd, stderr="down error")
                return DummyResult()
            elif cmd_type == "ps" and cmd[5:] == ["--all", "--quiet"]:
                return DummyResult(ps_after_cleanup)

        if cmd == ["docker", "inspect", ps_id.strip()]:
            if hasattr(mock_run, "inspect_fail") and mock_run.inspect_fail:
                raise subprocess.CalledProcessError(1, cmd, stderr="inspect error")
            data = [{"Config": {"Env": env_list}}]
            return DummyResult(json.dumps(data))

        raise RuntimeError(f"Unexpected command: {cmd}")

    return mock_run


def test_preflight_uses_all_for_created_container(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    monkeypatch.setattr(subprocess, "run", make_mock_run(env_list))
    run_compose_preflight()


def test_preflight_empty_created_container_lookup_fails(monkeypatch):
    env_list = []
    # If ps_id is empty, it should raise error
    monkeypatch.setattr(subprocess, "run", make_mock_run(env_list, ps_id=""))
    with pytest.raises(
        PreflightError, match="Could not get container ID from compose ps"
    ):
        run_compose_preflight()


def test_preflight_cleanup_on_success(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    mock_run = make_mock_run(env_list)
    monkeypatch.setattr(subprocess, "run", mock_run)
    run_compose_preflight()


def test_preflight_cleanup_on_ps_failure(monkeypatch):
    env_list = []
    mock_run = make_mock_run(env_list, ps_id="")
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(
        PreflightError, match="Could not get container ID from compose ps"
    ):
        run_compose_preflight()


def test_preflight_cleanup_on_inspect_failure(monkeypatch):
    env_list = []
    mock_run = make_mock_run(env_list)
    mock_run.inspect_fail = True
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(subprocess.CalledProcessError):
        run_compose_preflight()


def test_preflight_cleanup_on_semantic_failure(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a",  # Broken semantics
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    mock_run = make_mock_run(env_list)
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="EMBEDDED_EQUALS semantics mismatch"):
        run_compose_preflight()


def test_preflight_fail_dollar(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=valuewithdollars",  # Broken semantics
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    monkeypatch.setattr(subprocess, "run", make_mock_run(env_list))
    with pytest.raises(PreflightError, match="DOLLAR semantics mismatch"):
        run_compose_preflight()


def test_preflight_fail_quoted(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        "QUOTED_OR_SPACE_CASE=quoted string",  # Broken semantics
    ]
    monkeypatch.setattr(subprocess, "run", make_mock_run(env_list))
    with pytest.raises(PreflightError, match="QUOTED_OR_SPACE_CASE semantics mismatch"):
        run_compose_preflight()


def test_preflight_cleanup_failure_propagates(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    mock_run = make_mock_run(env_list)
    mock_run.down_fail = True
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="Preflight cleanup failed: down error"):
        run_compose_preflight()


def test_preflight_no_residual_container_after_cleanup(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    # Set ps_after_cleanup so that it fails the residual container check
    mock_run = make_mock_run(env_list, ps_after_cleanup="orphan_id\n")
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(
        PreflightError, match="Preflight cleanup failed: residual containers found"
    ):
        run_compose_preflight()


def test_preflight_primary_and_cleanup_failure_propagates(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a",  # Broken semantics -> Primary Error
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    mock_run = make_mock_run(env_list)
    mock_run.down_fail = True  # Cleanup Error
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(
        PreflightError,
        match="EMBEDDED_EQUALS semantics mismatch AND Preflight cleanup failed: down error",
    ):
        run_compose_preflight()


def test_preflight_command_fail_create(monkeypatch):
    mock_run = make_mock_run([], create_fail=True)
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="create error"):
        run_compose_preflight()
