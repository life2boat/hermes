import pytest
import os
import signal
from pathlib import Path
from ai_engineering.linear_dispatcher.worker import DispatcherWorker
from ai_engineering.linear_dispatcher.contracts import DispatcherConfig

def test_worker_fails_closed_missing_deps(tmp_path):
    worker = DispatcherWorker()
    config = DispatcherConfig(enabled=True, mode="shadow")
    # without env vars set it should fail
    os.environ.pop("GITHUB_TOKEN", None)
    os.environ.pop("LINEAR_API_KEY", None)
    assert not worker.validate_dependencies(config, tmp_path)

def test_worker_mode_live_fails_closed(monkeypatch):
    monkeypatch.setenv("HEALBITE_LINEAR_DISPATCHER_ENABLED", "true")
    monkeypatch.setenv("HEALBITE_LINEAR_DISPATCHER_MODE", "live")
    worker = DispatcherWorker()
    with pytest.raises(SystemExit) as e:
        worker.run()
    assert e.value.code == 1

def test_worker_disabled_exits_cleanly(monkeypatch):
    monkeypatch.setenv("HEALBITE_LINEAR_DISPATCHER_ENABLED", "false")
    worker = DispatcherWorker()
    with pytest.raises(SystemExit) as e:
        worker.run()
    assert e.value.code == 0
