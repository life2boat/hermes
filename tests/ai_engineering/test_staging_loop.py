import pytest
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime
import subprocess
import json
import yaml

from ai_engineering.supervisor.staging.deploy import StagingDeployer

@pytest.fixture
def deployer():
    return StagingDeployer("/fake/workspace")

@pytest.fixture
def valid_prod_compose():
    return {
        "services": {
            "hermes-bot": {
                "container_name": "hermes-bot",
                "volumes": ["/prod/db:/db"],
                "env_file": [".env"]
            }
        }
    }

@pytest.fixture
def valid_staging_compose():
    return {
        "services": {
            "hermes-bot-staging": {
                "container_name": "hermes-bot-staging",
                "volumes": ["/staging/db:/db"],
                "env_file": [".env.staging"]
            }
        }
    }

def test_preflight_success(deployer, valid_prod_compose, valid_staging_compose):
    with patch("os.path.exists", return_value=True), \
         patch("os.environ.get", return_value="myrepo/img@sha256:12345"), \
         patch("builtins.open", mock_open(read_data="mock")), \
         patch("yaml.safe_load", side_effect=[valid_staging_compose, valid_prod_compose]):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is True
        assert receipt.target_id == "staging-123"

def test_preflight_fail_same_id(deployer):
    receipt = deployer.preflight("same-id", "same-id")
    assert receipt.success is False
    assert "must not equal" in receipt.error_message

def test_preflight_fail_no_image(deployer):
    with patch("os.path.exists", return_value=True), \
         patch("os.environ.get", return_value="myrepo/img:latest"):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "@sha256:" in receipt.error_message

def test_preflight_fail_no_compose(deployer):
    with patch("os.path.exists", return_value=False), \
         patch("os.environ.get", return_value="myrepo/img@sha256:12345"):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "not found" in receipt.error_message

def test_preflight_fail_shared_container(deployer, valid_prod_compose, valid_staging_compose):
    valid_staging_compose["services"]["hermes-bot-staging"]["container_name"] = "hermes-bot"
    with patch("os.path.exists", return_value=True), \
         patch("os.environ.get", return_value="myrepo/img@sha256:12345"), \
         patch("builtins.open", mock_open(read_data="mock")), \
         patch("yaml.safe_load", side_effect=[valid_staging_compose, valid_prod_compose]):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "Shared container names" in receipt.error_message

def test_preflight_fail_shared_volumes(deployer, valid_prod_compose, valid_staging_compose):
    valid_staging_compose["services"]["hermes-bot-staging"]["volumes"] = ["/prod/db:/db"]
    with patch("os.path.exists", return_value=True), \
         patch("os.environ.get", return_value="myrepo/img@sha256:12345"), \
         patch("builtins.open", mock_open(read_data="mock")), \
         patch("yaml.safe_load", side_effect=[valid_staging_compose, valid_prod_compose]):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "Shared host paths" in receipt.error_message

def test_deploy_success(deployer):
    mock_inspect = [{"Image": "sha256:abc", "Config": {"Labels": {"org.opencontainers.image.revision": "def"}}, "Id": "123"}]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=json.dumps(mock_inspect))
        ]
        receipt = deployer.deploy()
        assert receipt.success is True
        assert receipt.container_id == "123"
        assert receipt.repo_digest == "sha256:abc"
        assert receipt.oci_revision == "def"

def test_deploy_failure(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="error")
        receipt = deployer.deploy()
        assert receipt.success is False
        assert "error" in receipt.error_message

def test_health_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="hermes-bot-staging\nqdrant-staging\n"),
            MagicMock(returncode=0, stdout="Hermes 1.0.0")
        ]
        receipt = deployer.health()
        assert receipt.success is True
        assert receipt.is_healthy is True

def test_health_failure_ps(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="hermes-bot-staging\n")
        receipt = deployer.health()
        assert receipt.success is False
        assert receipt.is_healthy is False

def test_health_failure_exec(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="hermes-bot-staging\nqdrant-staging\n"),
            subprocess.CalledProcessError(1, "cmd", stderr="error")
        ]
        receipt = deployer.health()
        assert receipt.success is False
        assert receipt.is_healthy is False

def test_canary_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="Synthetic Success")
        receipt = deployer.canary()
        assert receipt.success is True
        assert receipt.canary_result == "Synthetic Success"

def test_rollback_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        receipt = deployer.rollback()
        assert receipt.success is True
        assert "down" in mock_run.call_args[0][0]
        assert "-v" in mock_run.call_args[0][0]
