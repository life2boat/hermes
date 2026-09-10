import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
import subprocess

from ai_engineering.supervisor.staging.deploy import StagingDeployer

@pytest.fixture
def deployer():
    return StagingDeployer("/fake/workspace")

def test_preflight_success(deployer):
    with patch("os.path.exists", return_value=True):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is True
        assert receipt.target_id == "staging-123"

def test_preflight_fail_same_id(deployer):
    receipt = deployer.preflight("same-id", "same-id")
    assert receipt.success is False
    assert "must not equal" in receipt.error_message

def test_preflight_fail_no_compose(deployer):
    with patch("os.path.exists", return_value=False):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "not found" in receipt.error_message

def test_deploy_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        receipt = deployer.deploy()
        assert receipt.success is True
        mock_run.assert_called_once()

def test_deploy_failure(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="error")
        receipt = deployer.deploy()
        assert receipt.success is False
        assert "error" in receipt.error_message

def test_health_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="hermes-bot-staging\nqdrant-staging\n")
        receipt = deployer.health()
        assert receipt.success is True
        assert receipt.is_healthy is True

def test_health_failure(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="hermes-bot-staging\n")
        receipt = deployer.health()
        assert receipt.success is False
        assert receipt.is_healthy is False

def test_canary_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="Hermes 1.0.0")
        receipt = deployer.canary()
        assert receipt.success is True
        assert receipt.canary_result == "Hermes 1.0.0"

def test_rollback_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        receipt = deployer.rollback()
        assert receipt.success is True
