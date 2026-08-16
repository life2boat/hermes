import pytest
import json
import subprocess
from ops.secret_remediation_r1.preflight import run_compose_preflight, PreflightError


def make_mock_run(env_list):
    def mock_run(cmd, *args, **kwargs):
        class DummyResult:
            def __init__(self, stdout=""):
                self.stdout = stdout

        if cmd == ["docker", "compose", "create", "test"]:
            return DummyResult()
        elif cmd == ["docker", "compose", "ps", "-q", "test"]:
            return DummyResult("mock_id\n")
        elif cmd == ["docker", "inspect", "mock_id"]:
            data = [{"Config": {"Env": env_list}}]
            return DummyResult(json.dumps(data))
        elif cmd == ["docker", "compose", "rm", "-f", "test"]:
            return DummyResult()
        raise RuntimeError(f"Unexpected command: {cmd}")

    return mock_run


def test_preflight_success(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    monkeypatch.setattr(subprocess, "run", make_mock_run(env_list))
    run_compose_preflight()


def test_preflight_fail_embedded_equals(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a",  # Broken semantics
        "DOLLAR=value$with$dollars",
        "BACKSLASH=value\\with\\slashes",
        'QUOTED_OR_SPACE_CASE="quoted string"',
    ]
    monkeypatch.setattr(subprocess, "run", make_mock_run(env_list))
    with pytest.raises(PreflightError, match="EMBEDDED_EQUALS semantics mismatch"):
        run_compose_preflight()


def test_preflight_fail_dollar(monkeypatch):
    env_list = [
        "EMBEDDED_EQUALS=a=b=c",
        "DOLLAR=valuewithdollars",  # Broken semantics (expanded)
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
        "QUOTED_OR_SPACE_CASE=quoted string",  # Broken semantics (quotes stripped)
    ]
    monkeypatch.setattr(subprocess, "run", make_mock_run(env_list))
    with pytest.raises(PreflightError, match="QUOTED_OR_SPACE_CASE semantics mismatch"):
        run_compose_preflight()


def test_preflight_command_fail_create(monkeypatch):
    def mock_run(cmd, *args, **kwargs):
        if cmd == ["docker", "compose", "create", "test"]:
            raise subprocess.CalledProcessError(
                1, "docker compose create test", stderr="create error"
            )
        raise RuntimeError("Unexpected command")

    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="create error"):
        run_compose_preflight()
