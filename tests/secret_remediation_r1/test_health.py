import subprocess
import pytest
from ops.secret_remediation_r1.health import check_health, HealthCheckError
from ops.secret_remediation_r1.poller_checker import PollerCheckerError


def test_check_health_success(monkeypatch):
    import ops.secret_remediation_r1.health

    captured = {}
    monkeypatch.setattr(
        ops.secret_remediation_r1.health,
        "check_exactly_one_poller",
        lambda docker=None: 123,
    )

    class MockProcess:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return MockProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    check_health()
    assert captured["argv"] == [
        "docker",
        "exec",
        "hermes-bot",
        "hermes",
        "gateway",
        "status",
    ]
    assert captured["kwargs"]["timeout"] == 30


def test_check_health_poller_fails(monkeypatch):
    import ops.secret_remediation_r1.health

    def mock_poller(docker=None):
        raise PollerCheckerError("Poller check failed")

    monkeypatch.setattr(
        ops.secret_remediation_r1.health, "check_exactly_one_poller", mock_poller
    )

    with pytest.raises(HealthCheckError, match="Poller check failed"):
        check_health()


def test_check_health_gateway_status_fails(monkeypatch):
    import ops.secret_remediation_r1.health

    monkeypatch.setattr(
        ops.secret_remediation_r1.health,
        "check_exactly_one_poller",
        lambda docker=None: 123,
    )

    class MockProcess:
        returncode = 1
        stderr = "Error connecting"

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: MockProcess())

    with pytest.raises(HealthCheckError, match="hermes gateway status exited 1"):
        check_health()


def test_check_health_gateway_status_not_found(monkeypatch):
    import ops.secret_remediation_r1.health

    monkeypatch.setattr(
        ops.secret_remediation_r1.health,
        "check_exactly_one_poller",
        lambda docker=None: 123,
    )

    def mock_run(*args, **kwargs):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(subprocess, "run", mock_run)

    with pytest.raises(HealthCheckError, match="docker command not found"):
        check_health()


def test_check_health_gateway_status_timeout(monkeypatch):
    import ops.secret_remediation_r1.health

    monkeypatch.setattr(
        ops.secret_remediation_r1.health,
        "check_exactly_one_poller",
        lambda docker=None: 123,
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(HealthCheckError, match="timed out"):
        check_health()


def test_nonzero_health_error_does_not_expose_stderr(monkeypatch):
    import ops.secret_remediation_r1.health

    monkeypatch.setattr(
        ops.secret_remediation_r1.health,
        "check_exactly_one_poller",
        lambda docker=None: 123,
    )
    result = type("Result", (), {"returncode": 1, "stderr": "synthetic-private"})()
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(HealthCheckError) as caught:
        check_health()
    assert "synthetic-private" not in str(caught.value)
