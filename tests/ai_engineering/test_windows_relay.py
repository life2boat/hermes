import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from tools.computer_use.windows_relay_backend import WindowsRelayBackend
from tools.computer_use.backend import CaptureResult, UIElement, ActionResult

@pytest.fixture
def mock_wsl_env(monkeypatch, tmp_path):
    monkeypatch.setattr("subprocess.run", MagicMock())

    # Mock subprocess.run to return the tmp_path for wslpath and powershell
    def mock_run(*args, **kwargs):
        mock_result = MagicMock()
        if "powershell.exe" in args[0]:
            mock_result.stdout = "C:\\Users\\Mock\\AppData\\Local\n"
        elif "wslpath" in args[0]:
            mock_result.stdout = f"{tmp_path}\n"
        else:
            mock_result.stdout = ""
        mock_result.returncode = 0
        return mock_result

    monkeypatch.setattr("subprocess.run", mock_run)

    # Create the token file and dirs
    base_dir = tmp_path / "Hermes" / "computer-use"
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "relay_ipc" / "req").mkdir(parents=True, exist_ok=True)
    (base_dir / "relay_ipc" / "resp").mkdir(parents=True, exist_ok=True)

    token_path = base_dir / "relay_token.txt"
    token_path.write_text("mock_token")

    return tmp_path

def test_windows_relay_backend_start(mock_wsl_env):
    backend = WindowsRelayBackend()
    backend.start()
    assert backend.token == "mock_token"
    assert backend.req_dir.exists()
    assert backend.resp_dir.exists()
    assert backend.is_available()

def test_windows_relay_backend_capture(mock_wsl_env, monkeypatch):
    backend = WindowsRelayBackend()
    backend.start()

    def mock_send_request(verb, args=None):
        assert verb == "inspect"
        return {
            "width": 1024,
            "height": 768,
            "screenshot": "base64data",
            "elements": [
                {"index": 1, "role": "button", "label": "OK", "bounds": [10, 10, 50, 20]}
            ]
        }
    monkeypatch.setattr(backend, "_send_request", mock_send_request)

    result = backend.capture()
    assert isinstance(result, CaptureResult)
    assert result.width == 1024
    assert result.height == 768
    assert result.png_b64 == "base64data"
    assert len(result.elements) == 1
    assert result.elements[0].index == 1
    assert result.elements[0].role == "button"
    assert result.elements[0].label == "OK"
    assert result.elements[0].bounds == (10, 10, 50, 20)

def test_windows_relay_backend_click(mock_wsl_env, monkeypatch):
    backend = WindowsRelayBackend()
    backend.start()

    def mock_send_request(verb, args=None):
        assert verb == "click"
        assert args["element_id"] == 1
        return {}

    monkeypatch.setattr(backend, "_send_request", mock_send_request)
    result = backend.click(element=1)
    assert result.ok
    assert result.action == "click"

def test_windows_relay_backend_list_apps(mock_wsl_env, monkeypatch):
    backend = WindowsRelayBackend()
    backend.start()

    def mock_send_request(verb, args=None):
        assert verb == "list_windows"
        return {"windows": [{"app_name": "TestApp", "pid": 1234}]}

    monkeypatch.setattr(backend, "_send_request", mock_send_request)
    apps = backend.list_apps()
    assert len(apps) == 1
    assert apps[0]["app_name"] == "TestApp"
    assert apps[0]["pid"] == 1234

def test_windows_relay_backend_error_handling(mock_wsl_env, monkeypatch):
    backend = WindowsRelayBackend()
    backend.start()

    def mock_send_request(verb, args=None):
        raise RuntimeError("Relay error: mock error")

    monkeypatch.setattr(backend, "_send_request", mock_send_request)
    result = backend.click(element=1)
    assert not result.ok
    assert "mock error" in result.message
