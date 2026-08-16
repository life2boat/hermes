import subprocess
import pytest
from ops.secret_remediation_r1.health import check_health, HealthCheckError
from ops.secret_remediation_r1.poller_checker import PollerCheckerError

def test_check_health_success(monkeypatch):
    import ops.secret_remediation_r1.health
    monkeypatch.setattr(ops.secret_remediation_r1.health, "check_exactly_one_poller", lambda docker=None: 123)
    
    class MockProcess:
        returncode = 0
        stderr = ""
        
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: MockProcess())
    check_health()

def test_check_health_poller_fails(monkeypatch):
    import ops.secret_remediation_r1.health
    def mock_poller(docker=None):
        raise PollerCheckerError("Poller check failed")
    monkeypatch.setattr(ops.secret_remediation_r1.health, "check_exactly_one_poller", mock_poller)
    
    with pytest.raises(HealthCheckError, match="Poller check failed"):
        check_health()

def test_check_health_gateway_status_fails(monkeypatch):
    import ops.secret_remediation_r1.health
    monkeypatch.setattr(ops.secret_remediation_r1.health, "check_exactly_one_poller", lambda docker=None: 123)
    
    class MockProcess:
        returncode = 1
        stderr = "Error connecting"
        
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: MockProcess())
    
    with pytest.raises(HealthCheckError, match="hermes gateway status exited 1"):
        check_health()

def test_check_health_gateway_status_not_found(monkeypatch):
    import ops.secret_remediation_r1.health
    monkeypatch.setattr(ops.secret_remediation_r1.health, "check_exactly_one_poller", lambda docker=None: 123)
    
    def mock_run(*args, **kwargs):
        raise FileNotFoundError("hermes not found")
        
    monkeypatch.setattr(subprocess, "run", mock_run)
    
    with pytest.raises(HealthCheckError, match="hermes command not found"):
        check_health()
