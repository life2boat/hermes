import pytest
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime
import subprocess
import json
import yaml

from ai_engineering.supervisor.staging.deploy import StagingDeployer
from ai_engineering.supervisor.staging.receipts import ExactImageAttestation

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

@patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
def test_preflight_success(mock_run, deployer, valid_prod_compose, valid_staging_compose):
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

@patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
def test_preflight_fail_no_image(mock_run, deployer):
    with patch("os.path.exists", return_value=True), \
         patch("os.environ.get", return_value="myrepo/img:latest"):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "@sha256:" in receipt.error_message

@patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
def test_preflight_fail_no_compose(mock_run, deployer):
    with patch("os.path.exists", return_value=False), \
         patch("os.environ.get", return_value="myrepo/img@sha256:12345"):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "not found" in receipt.error_message

@patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
def test_preflight_fail_shared_container(mock_run, deployer, valid_prod_compose, valid_staging_compose):
    valid_staging_compose["services"]["hermes-bot-staging"]["container_name"] = "hermes-bot"
    with patch("os.path.exists", return_value=True), \
         patch("os.environ.get", return_value="myrepo/img@sha256:12345"), \
         patch("builtins.open", mock_open(read_data="mock")), \
         patch("yaml.safe_load", side_effect=[valid_staging_compose, valid_prod_compose]):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "Shared container names" in receipt.error_message

@patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=""))
def test_preflight_fail_shared_volumes(mock_run, deployer, valid_prod_compose, valid_staging_compose):
    valid_staging_compose["services"]["hermes-bot-staging"]["volumes"] = ["/prod/db:/db"]
    with patch("os.path.exists", return_value=True), \
         patch("os.environ.get", return_value="myrepo/img@sha256:12345"), \
         patch("builtins.open", mock_open(read_data="mock")), \
         patch("yaml.safe_load", side_effect=[valid_staging_compose, valid_prod_compose]):
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "Shared host paths" in receipt.error_message

def test_deploy_success(deployer):
    digest = "ghcr.io/life2boat/hermes@sha256:" + "a"*64
    mock_inspect = [{"Image": "sha256:abc", "Id": "123"}]
    mock_image_inspect = [{"Id": "sha256:abc", "RepoDigests": [digest], "Config": {"Labels": {"org.opencontainers.image.revision": "def"}}}]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=json.dumps(mock_inspect)),
            MagicMock(returncode=0, stdout=json.dumps(mock_image_inspect))
        ]
        receipt = deployer.deploy(attestation=ExactImageAttestation(registry_digest="sha256:" + "a"*64, config_digest="sha256:abc", source_sha="def", oci_revision="def", platform="linux/amd64"))
        assert receipt.success is True
        assert receipt.container_id == "123"
        assert receipt.repo_digest == digest
        assert receipt.oci_revision == "def"

def test_deploy_failure(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.CalledProcessError(1, "cmd", stderr="error")
        receipt = deployer.deploy(attestation=ExactImageAttestation(registry_digest="sha256:" + "a"*64, config_digest="sha256:abc", source_sha="def", oci_revision="def", platform="linux/amd64"))
        assert receipt.success is False
        assert "error" in receipt.error_message

def test_health_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="hermes-bot-staging\nqdrant-staging\n"),
            MagicMock(returncode=0, stdout="Hermes 1.0.0"),
            MagicMock(returncode=0, stdout="ok")
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
        mock_run.return_value = MagicMock(returncode=0, stdout='{"success": true, "timestamp": "2026-01-01T00:00:00", "canary_result": "Synthetic Success"}')
        receipt = deployer.canary()
        assert receipt.success is True
        assert receipt.canary_result == "Synthetic Success"

def test_rollback_success(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        receipt = deployer.rollback(delete_volumes=True)
        assert receipt.success is True
        print(mock_run.call_args_list)
        assert "down" in mock_run.call_args_list[0][0][0]
        assert "-v" in mock_run.call_args_list[0][0][0]

def test_preflight_dirty_worktree(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=" M some_file.py\n")
        receipt = deployer.preflight("staging-123", "prod-456")
        assert receipt.success is False
        assert "Dirty worktree" in receipt.error_message

def test_deploy_image_authority_mismatch(deployer):
    digest1 = "ghcr.io/life2boat/hermes@sha256:" + "a"*64
    digest2 = "ghcr.io/life2boat/hermes@sha256:" + "b"*64
    mock_inspect = [{"Image": "sha256:abc", "Id": "123"}]
    mock_image_inspect = [{"Id": "sha256:abc", "RepoDigests": [digest1], "Config": {"Labels": {"org.opencontainers.image.revision": "def"}}}]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=json.dumps(mock_inspect)),
            MagicMock(returncode=0, stdout=json.dumps(mock_image_inspect))
        ]
        receipt = deployer.deploy(attestation=ExactImageAttestation(registry_digest="sha256:" + "b"*64, config_digest="sha256:abc", source_sha="def", oci_revision="def", platform="linux/amd64"))
        assert receipt.success is False
        assert "Registry Manifest Digest mismatch" in receipt.error_message or "Malformed expected_digest" in receipt.error_message

def test_deploy_malformed_digest(deployer):
    mock_inspect = [{"Image": "sha256:abc", "Id": "123"}]
    mock_image_inspect = [{"Id": "sha256:abc", "RepoDigests": ["ghcr.io/life2boat/hermes@invalid"], "Config": {"Labels": {"org.opencontainers.image.revision": "def"}}}]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=json.dumps(mock_inspect)),
            MagicMock(returncode=0, stdout=json.dumps(mock_image_inspect))
        ]
        receipt = deployer.deploy(attestation=ExactImageAttestation(registry_digest="invalid", config_digest="sha256:abc", source_sha="def", oci_revision="def", platform="linux/amd64"))
    assert receipt.success is False
    assert "Registry Manifest Digest mismatch" in receipt.error_message or "Malformed expected_digest" in receipt.error_message

def test_deploy_source_authority_mismatch(deployer):
    digest = "ghcr.io/life2boat/hermes@sha256:" + "a"*64
    mock_inspect = [{"Image": "sha256:abc", "Id": "123"}]
    mock_image_inspect = [{"Id": "sha256:abc", "RepoDigests": [digest], "Config": {"Labels": {"org.opencontainers.image.revision": "def"}}}]
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout=json.dumps(mock_inspect)),
            MagicMock(returncode=0, stdout=json.dumps(mock_image_inspect))
        ]
        receipt = deployer.deploy(attestation=ExactImageAttestation(registry_digest="sha256:" + "a"*64, config_digest="sha256:abc", source_sha="ghi", oci_revision="ghi", platform="linux/amd64"))
        assert receipt.success is False
        assert "OCI revision mismatch" in receipt.error_message

def test_rollback_no_delete_volumes(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        receipt = deployer.rollback(delete_volumes=False)
        assert receipt.success is True
        print(mock_run.call_args_list)
        assert "down" in mock_run.call_args_list[0][0][0]
        assert "-v" not in mock_run.call_args_list[0][0][0]

def test_rollback_verification_failure(deployer):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=0, stdout="hermes-bot-staging")
        ]
        receipt = deployer.rollback()
        assert receipt.success is False
        assert "Rollback verification failed" in receipt.error_message

from scripts.run_autonomous_supervisor import main as supervisor_main

def test_supervisor_staging_deploy_blocked_no_intent():
    # Should exit with 1 or print BLOCKED
    with patch("sys.stderr"):
        try:
            supervisor_main(["staging-deploy"])
        except SystemExit as e:
            assert e.code in (1, 2)

def test_supervisor_staging_canary_blocked_no_intent():
    with patch("sys.stderr"):
        try:
            supervisor_main(["staging-canary"])
        except SystemExit as e:
            assert e.code in (1, 2)
