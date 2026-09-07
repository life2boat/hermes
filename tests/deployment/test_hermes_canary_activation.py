import json
import os
import tempfile
import sys
from pathlib import Path
from unittest import mock
import pytest
from dataclasses import replace

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_deploy
import hermes_post_deploy_attestation as attestation

from test_hermes_production_deploy import protected_contract

@pytest.fixture
def mock_canary_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_text('HEALBITE_INVENTORY_PHOTO_ENABLED=true\nHEALBITE_INVENTORY_PHOTO_ALLOWLIST="123,456"\n')
    return env_file

def _make_dummy_baseline():
    return attestation.RuntimeBaseline(
        captured_at="now",
        log_cursor="now",
        hermes=attestation.ContainerSnapshot(
            container_id="abc",
            image_id="abc",
            revision="abc",
            created_at="now",
            started_at="now",
            state="running",
            restart_count=0,
            mounts=(),
            feature_gates=(),
            allowlists=(),
            secret_fingerprints=(),
            runtime_configuration_fingerprint="abc"
        ),
        qdrant=None,
        database=None,
        telegram_health="pass",
        gateway_health="pass",
        provider_request_count=0
    )


def test_canary_activation_success(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mock_canary_env: Path):
    contract, source = protected_contract
    contract = replace(contract, canary_source_file=str(mock_canary_env), canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), runtime_directory=tmp_path / "runtime")
    (tmp_path / "runtime").mkdir(exist_ok=True)
    
    # Mock preflight and operations
    monkeypatch.setattr(hermes_production_deploy, "_preflight", mock.Mock(return_value=mock.Mock()))
    monkeypatch.setattr(hermes_production_deploy, "_validate_operation_identity", mock.Mock(return_value=(mock.Mock(image_id="sha256:abc"), "main")))
    monkeypatch.setattr(hermes_production_deploy, "_validate_runtime_directory", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "read_required_secrets", mock.Mock(return_value={"TELEGRAM_BOT_TOKEN": "token"}))
    monkeypatch.setattr(hermes_production_deploy, "_temporary_render_contract", mock.Mock(return_value=contract))
    monkeypatch.setattr(hermes_production_deploy, "_write_secret_override", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "validate_compose_render", mock.Mock(return_value=[]))
    monkeypatch.setattr(hermes_production_deploy, "cleanup_secret_override", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_validate_live_future_mounts", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_validate_capacity", mock.Mock())
    
    mock_baseline = _make_dummy_baseline()
    monkeypatch.setattr(hermes_production_deploy, "_capture_pre_mutation_baseline", mock.Mock(return_value=mock_baseline))
    monkeypatch.setattr(hermes_production_deploy, "_begin_secret_override_transaction", mock.Mock(return_value=mock.Mock()))
    monkeypatch.setattr(hermes_production_deploy, "_compose_recreate_hermes", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_post_deploy_attestation", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_finish_secret_override_transaction", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_write_operation_evidence", mock.Mock())
    
    hermes_production_deploy.execute_canary_activation(
        contract,
        source=mock_canary_env,
        image="ghcr.io/life2boat/hermes:main",
        revision="9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f",
        confirmation=hermes_production_deploy.DEPLOY_CONFIRMATION,
    )
    
    hermes_production_deploy._compose_recreate_hermes.assert_called_once_with(
        contract,
        image_id="sha256:abc",
        revision="9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f",
        canary_override=True,
    )
    
def test_canary_activation_rejects_unauthorized_feature(protected_contract, monkeypatch: pytest.MonkeyPatch, mock_canary_env: Path):
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_HOUSEHOLDS",))
    
    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, mock_canary_env)
    assert exc.value.code == "canary-feature-unauthorized"
    
def test_canary_activation_rejects_large_allowlist(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_text('HEALBITE_INVENTORY_PHOTO_ENABLED=true\nHEALBITE_INVENTORY_PHOTO_ALLOWLIST="1,2,3,4,5,6"\n')
    
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",))
    
    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-allowlist-too-large"

def test_canary_activation_rejects_missing_file(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch):
    env_file = tmp_path / "hermes-canary.env"
    
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",))
    
    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-authority-missing"

def test_canary_activation_writes_override(tmp_path: Path, protected_contract):
    contract, _ = protected_contract
    contract = replace(contract, runtime_directory=tmp_path / "runtime", target_service="hermes-bot")
    
    hermes_production_deploy._validate_runtime_directory(contract, create=True)
    
    gates = {
        "HEALBITE_INVENTORY_PHOTO_ENABLED": "true",
        "HEALBITE_INVENTORY_PHOTO_ALLOWLIST": "123"
    }
    
    hermes_production_deploy._write_canary_override(contract, gates)
    
    override_path = contract.runtime_directory / "hermes-canary-override.yml"
    assert override_path.exists()
    
    with open(override_path) as f:
        doc = json.load(f)
        
    assert doc["services"]["hermes-bot"]["environment"] == gates

def test_ordinary_deploy_removes_canary_override(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch):
    contract, source = protected_contract
    contract = replace(contract, runtime_directory=tmp_path / "runtime")
    
    hermes_production_deploy._validate_runtime_directory(contract, create=True)
    
    override_path = contract.runtime_directory / "hermes-canary-override.yml"
    override_path.write_text("{}")
    
    assert override_path.exists()
    
    monkeypatch.setattr(hermes_production_deploy, "_preflight", mock.Mock(return_value=mock.Mock()))
    monkeypatch.setattr(hermes_production_deploy, "_validate_operation_identity", mock.Mock(return_value=(mock.Mock(image_id="sha256:abc"), "main")))
    monkeypatch.setattr(hermes_production_deploy, "read_required_secrets", mock.Mock(return_value={"TELEGRAM_BOT_TOKEN": "token"}))
    monkeypatch.setattr(hermes_production_deploy, "_temporary_render_contract", mock.Mock(return_value=contract))
    monkeypatch.setattr(hermes_production_deploy, "_write_secret_override", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "validate_compose_render", mock.Mock(return_value=[]))
    monkeypatch.setattr(hermes_production_deploy, "cleanup_secret_override", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_validate_live_future_mounts", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_validate_capacity", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_capture_pre_mutation_baseline", mock.Mock(return_value=_make_dummy_baseline()))
    monkeypatch.setattr(hermes_production_deploy, "_begin_secret_override_transaction", mock.Mock(return_value=mock.Mock()))
    monkeypatch.setattr(hermes_production_deploy, "_compose_recreate_hermes", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_post_deploy_attestation", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_finish_secret_override_transaction", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_validate_automatic_rollback_readiness", mock.Mock(return_value=None))
    monkeypatch.setattr(hermes_production_deploy, "_write_operation_evidence", mock.Mock())
    
    hermes_production_deploy.execute_operation(
        contract,
        source=source,
        image="ghcr.io/life2boat/hermes@sha256:abc",
        revision="9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f",
        confirmation=hermes_production_deploy.DEPLOY_CONFIRMATION,
        rollback=False,
    )
    
    assert not override_path.exists()
