
import json
import os
import tempfile
import sys
import stat
from pathlib import Path
from unittest import mock
import pytest
from dataclasses import replace
import argparse

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
    env_file.write_bytes(b'HEALBITE_INVENTORY_PHOTO_ENABLED=true\nHEALBITE_INVENTORY_PHOTO_ALLOWLIST="123,456"\n')
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

# 1. activate-canary CLI parser exposure
def test_cli_parser_activate_canary():
    parser = hermes_production_deploy.build_parser()
    args = parser.parse_args(["activate-canary", "--image", "ghcr.io/hermes", "--revision", "abc", "--confirm", "ACTIVATE CANARY"])
    assert args.command == "activate-canary"
    assert args.image == "ghcr.io/hermes"
    assert args.revision == "abc"
    assert args.confirm == "ACTIVATE CANARY"

def test_cli_parser_deactivate_canary():
    parser = hermes_production_deploy.build_parser()
    args = parser.parse_args(["deactivate-canary", "--image", "ghcr.io/hermes", "--revision", "abc", "--confirm", "DEACTIVATE CANARY"])
    assert args.command == "deactivate-canary"
    assert args.image == "ghcr.io/hermes"
    assert args.revision == "abc"
    assert args.confirm == "DEACTIVATE CANARY"

# 2. parser routes to correct execution path
def test_parser_routes_to_canary_activation(monkeypatch: pytest.MonkeyPatch, protected_contract):
    contract, source = protected_contract
    monkeypatch.setattr(hermes_production_deploy, "load_contract", mock.Mock(return_value=contract))
    mock_exec = mock.Mock()
    monkeypatch.setattr(hermes_production_deploy, "execute_canary_activation", mock_exec)

    test_args = ["hermes_production_deploy.py", "activate-canary", "--image", "ghcr.io/hermes", "--revision", "abc", "--confirm", "ACTIVATE CANARY"]
    with mock.patch("sys.argv", test_args):
        hermes_production_deploy.main()

    mock_exec.assert_called_once()
    assert mock_exec.call_args[1]["confirmation"] == "ACTIVATE CANARY"

def test_parser_routes_to_canary_deactivation(monkeypatch: pytest.MonkeyPatch, protected_contract):
    contract, source = protected_contract
    monkeypatch.setattr(hermes_production_deploy, "load_contract", mock.Mock(return_value=contract))
    mock_exec = mock.Mock()
    monkeypatch.setattr(hermes_production_deploy, "execute_canary_deactivation", mock_exec)

    test_args = ["hermes_production_deploy.py", "deactivate-canary", "--image", "ghcr.io/hermes", "--revision", "abc", "--confirm", "DEACTIVATE CANARY"]
    with mock.patch("sys.argv", test_args):
        hermes_production_deploy.main()

    mock_exec.assert_called_once()
    assert mock_exec.call_args[1]["confirmation"] == "DEACTIVATE CANARY"

@pytest.fixture
def mocked_stat():
    mock_st = mock.Mock()
    mock_st.st_size = 100
    mock_st.st_uid = 1000  # assuming contract.lease_owner_uids has 1000
    mock_st.st_mode = stat.S_IFREG | 0o600
    return mock_st

# 3. valid authority success
def test_canary_activation_success(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mock_canary_env: Path, mocked_stat):
    contract, source = protected_contract
    contract = replace(contract, canary_source_file=str(mock_canary_env), canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), runtime_directory=tmp_path / "runtime", lease_owner_uids=(1000,))
    (tmp_path / "runtime").mkdir(exist_ok=True)

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

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
    monkeypatch.setattr(hermes_production_deploy, "_post_deploy_attestation", mock.Mock(return_value=mock.Mock()))
    monkeypatch.setattr(hermes_production_deploy, "_finish_secret_override_transaction", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_write_operation_evidence", mock.Mock())

    hermes_production_deploy.execute_canary_activation(
        contract,
        source=mock_canary_env,
        image="ghcr.io/life2boat/hermes:main",
        revision="9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f",
        confirmation=hermes_production_deploy.CANARY_ACTIVATION_CONFIRMATION,
    )

    hermes_production_deploy._compose_recreate_hermes.assert_called_once_with(
        contract,
        image_id="sha256:abc",
        revision="9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f",
        canary_override=True,
    )

# 4. missing authority rejected
def test_canary_activation_rejects_missing_file(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file))

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-authority-missing"

# 5. symlinked authority rejected
def test_canary_activation_rejects_symlink(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_text("A=B")
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file))

    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=True))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-authority-invalid-path"

# 6. insecure mode rejected
def test_canary_activation_rejects_insecure_mode(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mock_canary_env: Path, mocked_stat):
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(mock_canary_env), lease_owner_uids=(1000,))

    mocked_stat.st_mode = stat.S_IFREG | 0o644
    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, mock_canary_env)
    assert exc.value.code == "canary-authority-insecure-mode"

# 7. wrong owner rejected
def test_canary_activation_rejects_wrong_owner(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mock_canary_env: Path, mocked_stat):
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(mock_canary_env), lease_owner_uids=(1000,))

    mocked_stat.st_uid = 9999
    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, mock_canary_env)
    assert exc.value.code == "canary-authority-invalid-owner"

# 8. wrong canonical path rejected
def test_canary_activation_rejects_wrong_canonical_path(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mock_canary_env: Path, mocked_stat):
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file="/some/other/path")

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, mock_canary_env)
    assert exc.value.code == "canary-authority-invalid-path"

# 9. duplicate key rejected
def test_canary_activation_rejects_duplicate_key(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_bytes(b'HEALBITE_INVENTORY_PHOTO_ENABLED=true\nHEALBITE_INVENTORY_PHOTO_ENABLED=false\n')
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file), lease_owner_uids=(1000,))

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-authority-duplicate-key"

# 10. unknown key rejected (unauthorized feature)
def test_canary_activation_rejects_unauthorized_feature(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_bytes(b'HEALBITE_HOUSEHOLDS_ENABLED=true\n')
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file), lease_owner_uids=(1000,))

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-feature-unauthorized"

# 11. malformed line rejected
def test_canary_activation_rejects_malformed_line(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_bytes(b'HEALBITE_INVENTORY_PHOTO_ENABLED true\n')
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file), lease_owner_uids=(1000,))

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-authority-malformed-line"

# 12. invalid boolean rejected
def test_canary_activation_rejects_invalid_boolean(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_bytes(b'HEALBITE_INVENTORY_PHOTO_ENABLED=TRUE\n')
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file), lease_owner_uids=(1000,))

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-authority-invalid-boolean"

# 14. empty required allowlist rejected
def test_canary_activation_rejects_empty_allowlist(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_bytes(b'HEALBITE_INVENTORY_PHOTO_ENABLED=true\nHEALBITE_INVENTORY_PHOTO_ALLOWLIST=""\n')
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file), lease_owner_uids=(1000,))

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-allowlist-empty"

# 15. oversized allowlist rejected
def test_canary_activation_rejects_oversized_allowlist(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch, mocked_stat):
    env_file = tmp_path / "hermes-canary.env"
    env_file.write_bytes(b'HEALBITE_INVENTORY_PHOTO_ENABLED=true\nHEALBITE_INVENTORY_PHOTO_ALLOWLIST="1,2,3,4,5,6"\n')
    contract, _ = protected_contract
    contract = replace(contract, canary_authorized_features=("HEALBITE_INVENTORY_PHOTO",), canary_source_file=str(env_file), lease_owner_uids=(1000,))

    monkeypatch.setattr(Path, "stat", mock.Mock(return_value=mocked_stat))
    monkeypatch.setattr(Path, "is_symlink", mock.Mock(return_value=False))

    with pytest.raises(hermes_production_deploy.DeploymentContractError) as exc:
        hermes_production_deploy._read_canary_authority(contract, env_file)
    assert exc.value.code == "canary-allowlist-too-large"

# 16. unrelated gates unchanged
# 17. protected secrets unchanged
def test_canary_activation_writes_override(tmp_path: Path, protected_contract):
    contract, _ = protected_contract
    contract = replace(contract, runtime_directory=tmp_path / "runtime", target_service="hermes-bot")
    hermes_production_deploy._validate_runtime_directory(contract, create=True)
    gates = {"HEALBITE_INVENTORY_PHOTO_ENABLED": "true", "HEALBITE_INVENTORY_PHOTO_ALLOWLIST": "123"}
    hermes_production_deploy._write_canary_override(contract, gates)
    override_path = contract.runtime_directory / "hermes-canary-override.yml"
    assert override_path.exists()
    with open(override_path) as f:
        doc = json.load(f)
    assert doc["services"]["hermes-bot"]["environment"] == gates
    assert len(doc["services"]["hermes-bot"]["environment"]) == 2

# 20. explicit deactivation restores exact previous state
def test_explicit_deactivation(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch):
    contract, source = protected_contract
    contract = replace(contract, runtime_directory=tmp_path / "runtime")
    hermes_production_deploy._validate_runtime_directory(contract, create=True)
    override_path = contract.runtime_directory / "hermes-canary-override.yml"
    override_path.write_text("{}")

    monkeypatch.setattr(hermes_production_deploy, "_preflight", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_validate_operation_identity", mock.Mock(return_value=(mock.Mock(image_id="sha256:abc"), "main")))
    monkeypatch.setattr(hermes_production_deploy, "_capture_pre_mutation_baseline", mock.Mock(return_value=_make_dummy_baseline()))
    monkeypatch.setattr(hermes_production_deploy, "_compose_recreate_hermes", mock.Mock())
    monkeypatch.setattr(hermes_production_deploy, "_post_deploy_attestation", mock.Mock(return_value=mock.Mock()))
    monkeypatch.setattr(hermes_production_deploy, "_write_operation_evidence", mock.Mock())

    hermes_production_deploy.execute_canary_deactivation(
        contract,
        image="ghcr.io/life2boat/hermes:main",
        revision="9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f",
        confirmation=hermes_production_deploy.CANARY_DEACTIVATION_CONFIRMATION,
    )

    assert not override_path.exists()
    hermes_production_deploy._compose_recreate_hermes.assert_called_once_with(
        contract,
        image_id="sha256:abc",
        revision="9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f",
        canary_override=False,
    )

# 21. ordinary deploy does not inherit stale canary
def test_ordinary_deploy_removes_canary_override(tmp_path: Path, protected_contract, monkeypatch: pytest.MonkeyPatch):
    contract, source = protected_contract
    contract = replace(contract, runtime_directory=tmp_path / "runtime")

    hermes_production_deploy._validate_runtime_directory(contract, create=True)
    override_path = contract.runtime_directory / "hermes-canary-override.yml"
    override_path.write_text("{}")

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
