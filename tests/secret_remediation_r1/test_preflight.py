import pytest
from ops.secret_remediation_r1.preflight import run_compose_preflight, PreflightError

def test_preflight_success(monkeypatch):
    import subprocess
    
    class DummyResult:
        stdout = "services:\n  test:\n    environment:\n      RAW_VAR: val1\n      QUOTED_VAR: val2\n      my_env_file.env\n"
        
    def mock_run(*args, **kwargs):
        return DummyResult()
        
    monkeypatch.setattr(subprocess, "run", mock_run)
    run_compose_preflight()

def test_preflight_fail_quoted(monkeypatch):
    import subprocess
    
    class DummyResult:
        stdout = "services:\n  test:\n    environment:\n      RAW_VAR: val1\n      QUOTED_VAR: '\"val2\"'\n      my_env_file.env\n"
        
    def mock_run(*args, **kwargs):
        return DummyResult()
        
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="Quotes were not stripped"):
        run_compose_preflight()

def test_preflight_fail_raw(monkeypatch):
    import subprocess
    
    class DummyResult:
        stdout = "services:\n  test:\n    environment:\n      QUOTED_VAR: val2\n      my_env_file.env\n"
        
    def mock_run(*args, **kwargs):
        return DummyResult()
        
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="RAW_VAR not resolved"):
        run_compose_preflight()

def test_preflight_command_fail(monkeypatch):
    import subprocess
    
    def mock_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "docker compose config", stderr="config error")
        
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="config error"):
        run_compose_preflight()

def test_preflight_fail_env_file(monkeypatch):
    import subprocess
    
    class DummyResult:
        stdout = "services:\n  test:\n    environment:\n      RAW_VAR: val1\n      QUOTED_VAR: val2\n"
        
    def mock_run(*args, **kwargs):
        return DummyResult()
        
    monkeypatch.setattr(subprocess, "run", mock_run)
    with pytest.raises(PreflightError, match="env_file capability not supported or file dropped"):
        run_compose_preflight()
