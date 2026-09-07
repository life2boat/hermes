from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_post_deploy_attestation as attestation
import hermes_production_deploy as deploy
from test_hermes_production_deploy import protected_contract

REVISION = "9e7b6a67890db6b6acbe94f2bb7fba35f1426a7f"
IMAGE_ID = "sha256:" + "a" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "b" * 64
FEATURE = "HEALBITE_INVENTORY_PHOTO"
ENABLED = f"{FEATURE}_ENABLED"
ALLOWLIST = f"{FEATURE}_ALLOWLIST"
VALID_AUTHORITY = f"{ENABLED}=true\n{ALLOWLIST}=101,202\n".encode()
PREVIOUS_CANARY = b'{"previous":"exact-bytes"}\n'


def _baseline() -> attestation.RuntimeBaseline:
    return attestation.RuntimeBaseline(
        captured_at="synthetic",
        log_cursor="synthetic",
        hermes=attestation.ContainerSnapshot(
            container_id="synthetic-container",
            image_id=PREVIOUS_IMAGE_ID,
            revision=REVISION,
            created_at="synthetic",
            started_at="synthetic",
            state="running",
            restart_count=0,
            mounts=(),
            feature_gates=(),
            allowlists=(),
            secret_fingerprints=(),
            runtime_configuration_fingerprint="synthetic",
        ),
        qdrant=None,
        database=None,
        telegram_health="pass",
        gateway_health="pass",
        provider_request_count=0,
    )


def _authority(data: bytes) -> dict[str, str]:
    return deploy._parse_canary_authority(
        data,
        authorized_features=(FEATURE,),
    )


def _prepare_contract(contract: deploy.DeploymentContract, tmp_path: Path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    return replace(
        contract,
        runtime_directory=runtime,
        secret_override=runtime / "hermes-secrets-override.yml",
        lease_path=runtime / "hermes-deployment-operation.json",
        canary_authorized_features=(FEATURE,),
    )


def _install_operation_harness(
    monkeypatch: pytest.MonkeyPatch,
    contract: deploy.DeploymentContract,
    *,
    fail_at: str | None = None,
) -> tuple[list[tuple[str, object]], dict[str, str]]:
    events: list[tuple[str, object]] = []
    secrets = {"TELEGRAM_BOT_TOKEN": "placeholder"}
    lease = SimpleNamespace(holder_fingerprint="synthetic-lease")
    secret_transaction = SimpleNamespace()

    def preflight(function, *args, **kwargs):
        del args, kwargs
        if function is deploy.preflight.acquire_deployment_lease:
            events.append(("lease-acquire", None))
            return lease
        if function is deploy.preflight.release_deployment_lease:
            events.append(("lease-release", None))
            if fail_at == "lease-release":
                raise deploy.DeploymentContractError("lease-release-failed")
            return None
        return None

    monkeypatch.setattr(deploy, "_preflight", preflight)
    monkeypatch.setattr(
        deploy,
        "_validate_operation_identity",
        lambda *_args, **_kwargs: (SimpleNamespace(image_id=IMAGE_ID), REVISION),
    )
    monkeypatch.setattr(deploy, "_validate_runtime_directory", lambda *_a, **_k: None)
    original_temporary_contract = deploy._temporary_render_contract

    def temporary_contract(*args, **kwargs):
        temporary = original_temporary_contract(*args, **kwargs)
        temporary.runtime_directory.mkdir(mode=0o700)
        return temporary

    monkeypatch.setattr(deploy, "_temporary_render_contract", temporary_contract)

    def read_authority(*_args, **_kwargs):
        events.append(("authority-read", None))
        if fail_at == "authority-read":
            raise deploy.DeploymentContractError("canary-authority-missing")
        return {ENABLED: "true", ALLOWLIST: "101,202"}

    monkeypatch.setattr(deploy, "_read_canary_authority", read_authority)

    def read_secrets(*_args, **_kwargs):
        events.append(("secret-read", None))
        if fail_at == "secret-read":
            raise deploy.DeploymentContractError("secret-source-missing")
        return dict(secrets)

    monkeypatch.setattr(deploy, "read_required_secrets", read_secrets)
    monkeypatch.setattr(deploy, "_write_secret_override", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy, "cleanup_secret_override", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy, "validate_compose_render", lambda *_a, **_k: ())
    monkeypatch.setattr(deploy, "_validate_live_future_mounts", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy, "_validate_capacity", lambda *_a, **_k: None)
    monkeypatch.setattr(deploy, "_capture_pre_mutation_baseline", lambda *_a: _baseline())
    monkeypatch.setattr(
        deploy,
        "_validate_automatic_rollback_readiness",
        lambda *_a, **_k: None,
    )

    def begin_secret(*_args, **_kwargs):
        events.append(("secret-begin", None))
        return secret_transaction

    def finish_secret(*_args, preserve_published, **_kwargs):
        events.append(("secret-finish", preserve_published))

    monkeypatch.setattr(deploy, "_begin_secret_override_transaction", begin_secret)
    monkeypatch.setattr(deploy, "_finish_secret_override_transaction", finish_secret)

    def recreate(*_args, image_id, revision, canary_override=False, **_kwargs):
        del revision
        events.append(("recreate", (image_id, canary_override)))
        if fail_at == "recreate":
            raise deploy.DeploymentContractError("compose-up")

    monkeypatch.setattr(deploy, "_compose_recreate_hermes", recreate)

    def post(*_args, **_kwargs):
        events.append(("post-attestation", None))
        if fail_at == "post-attestation":
            raise deploy.DeploymentContractError("post-attestation")
        return SimpleNamespace(status="PASS")

    monkeypatch.setattr(deploy, "_post_deploy_attestation", post)
    monkeypatch.setattr(
        deploy,
        "_write_operation_evidence",
        lambda *_a, operation_status, **_k: events.append(
            ("evidence", operation_status)
        ),
    )

    def automatic_rollback(*_args, canary_override=False, **_kwargs):
        events.append(("rollback", canary_override))
        if fail_at == "rollback":
            raise deploy.DeploymentContractError("rollback-failed")
        return SimpleNamespace(status="PASS")

    monkeypatch.setattr(deploy, "_automatic_rollback", automatic_rollback)
    return events, secrets


def _activate(contract: deploy.DeploymentContract) -> None:
    deploy.execute_canary_activation(
        contract,
        source=deploy.CANARY_AUTHORITY_PATH,
        image=IMAGE_ID,
        revision=REVISION,
        confirmation=deploy.CANARY_ACTIVATION_CONFIRMATION,
    )


def _deactivate(contract: deploy.DeploymentContract) -> None:
    deploy.execute_canary_deactivation(
        contract,
        image=IMAGE_ID,
        revision=REVISION,
        confirmation=deploy.CANARY_DEACTIVATION_CONFIRMATION,
    )


def test_cli_parser_activate_canary() -> None:
    args = deploy.build_parser().parse_args(
        [
            "activate-canary",
            "--image",
            IMAGE_ID,
            "--revision",
            REVISION,
            "--confirm",
            deploy.CANARY_ACTIVATION_CONFIRMATION,
        ]
    )
    assert (args.command, args.image, args.revision, args.confirm) == (
        "activate-canary",
        IMAGE_ID,
        REVISION,
        deploy.CANARY_ACTIVATION_CONFIRMATION,
    )


def test_cli_parser_deactivate_canary() -> None:
    args = deploy.build_parser().parse_args(
        [
            "deactivate-canary",
            "--image",
            IMAGE_ID,
            "--revision",
            REVISION,
            "--confirm",
            deploy.CANARY_DEACTIVATION_CONFIRMATION,
        ]
    )
    assert (args.command, args.image, args.revision, args.confirm) == (
        "deactivate-canary",
        IMAGE_ID,
        REVISION,
        deploy.CANARY_DEACTIVATION_CONFIRMATION,
    )


@pytest.mark.parametrize("operation", ["activate", "deactivate"])
def test_canary_commands_require_exact_confirmation(
    operation: str,
    protected_contract,
) -> None:
    contract, _ = protected_contract
    function = (
        deploy.execute_canary_activation
        if operation == "activate"
        else deploy.execute_canary_deactivation
    )
    arguments = {"image": IMAGE_ID, "revision": REVISION, "confirmation": "wrong"}
    if operation == "activate":
        arguments["source"] = deploy.CANARY_AUTHORITY_PATH
    with pytest.raises(deploy.DeploymentContractError) as error:
        function(contract, **arguments)
    assert error.value.code == "explicit-confirmation-required"


@pytest.mark.parametrize("operation", ["activate", "deactivate"])
def test_main_routes_canary_command(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    protected_contract,
) -> None:
    contract, _ = protected_contract
    execute = mock.Mock()
    command = f"{operation}-canary"
    confirmation = (
        deploy.CANARY_ACTIVATION_CONFIRMATION
        if operation == "activate"
        else deploy.CANARY_DEACTIVATION_CONFIRMATION
    )
    monkeypatch.setattr(deploy, "load_contract", lambda: contract)
    entrypoint = (
        "execute_canary_activation"
        if operation == "activate"
        else "execute_canary_deactivation"
    )
    monkeypatch.setattr(deploy, entrypoint, execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hermes_production_deploy.py",
            command,
            "--image",
            IMAGE_ID,
            "--revision",
            REVISION,
            "--confirm",
            confirmation,
        ],
    )
    deploy.main()
    execute.assert_called_once()


def test_authority_parser_accepts_exact_activation_contract() -> None:
    assert _authority(VALID_AUTHORITY) == {
        ENABLED: "true",
        ALLOWLIST: "101,202",
    }


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xff", "canary-authority-invalid-utf8"),
        (f"{ENABLED} true\n".encode(), "canary-authority-malformed-line"),
        (f"{ENABLED}=true\n{ENABLED}=true\n".encode(), "canary-authority-duplicate-key"),
        (b"lowercase=true\n", "canary-authority-malformed-key"),
        (b"HEALBITE_INVENTORY_PHOTO_OTHER=x\n", "canary-authority-unknown-variable"),
        (b"HEALBITE_HOUSEHOLDS_ENABLED=true\n", "canary-feature-unauthorized"),
        (f"{ENABLED}=yes\n{ALLOWLIST}=101\n".encode(), "canary-authority-invalid-boolean"),
        (f"{ALLOWLIST}=101\n".encode(), "canary-authority-missing-enabled"),
        (f"{ENABLED}=true\n".encode(), "canary-authority-missing-allowlist"),
        (f"{ENABLED}=false\n{ALLOWLIST}=101\n".encode(), "canary-authority-not-enabled"),
        (f"{ENABLED}=true\n{ALLOWLIST}=\n".encode(), "canary-allowlist-empty"),
        (f"{ENABLED}=true\n{ALLOWLIST}=1,2,3,4,5,6\n".encode(), "canary-allowlist-too-large"),
        (f"{ENABLED}=true\n{ALLOWLIST}=101,101\n".encode(), "canary-allowlist-duplicate"),
        (f"{ENABLED}=true\n{ALLOWLIST}=not-an-id\n".encode(), "canary-allowlist-invalid-member"),
        (
            f"{ENABLED}=true\n{ALLOWLIST}={'9' * 5000}\n".encode(),
            "canary-allowlist-invalid-member",
        ),
    ],
)
def test_authority_parser_fails_closed(data: bytes, code: str) -> None:
    with pytest.raises(deploy.DeploymentContractError) as error:
        _authority(data)
    assert error.value.code == code


def test_read_authority_requires_exact_canonical_path(protected_contract) -> None:
    contract, _ = protected_contract
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority(contract, Path("/tmp/hermes-canary.env"))
    assert error.value.code == "canary-authority-invalid-path"


@pytest.fixture
def safe_posix_authority():
    if os.name != "posix":
        pytest.skip("descriptor-chain contract requires POSIX")
    with tempfile.TemporaryDirectory(
        prefix="hermes-canary-authority-",
        dir=Path.home(),
    ) as raw_directory:
        directory = Path(raw_directory)
        directory.chmod(0o700)
        path = directory / "hermes-canary.env"
        path.write_bytes(VALID_AUTHORITY)
        path.chmod(0o600)
        yield directory, path


def test_fd_authority_reader_accepts_safe_regular_file(safe_posix_authority) -> None:
    _, path = safe_posix_authority
    assert deploy._read_canary_authority_bytes(
        path,
        allowed_owner_uids=frozenset({os.geteuid()}),
    ) == VALID_AUTHORITY


def test_fd_authority_reader_normalizes_missing_file(safe_posix_authority) -> None:
    directory, _ = safe_posix_authority
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority_bytes(
            directory / "missing.env",
            allowed_owner_uids=frozenset({os.geteuid()}),
        )
    assert error.value.code == "canary-authority-missing"


def test_fd_authority_reader_rejects_final_symlink(safe_posix_authority) -> None:
    directory, path = safe_posix_authority
    link = directory / "linked.env"
    link.symlink_to(path)
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority_bytes(
            link,
            allowed_owner_uids=frozenset({os.geteuid()}),
        )
    assert error.value.code == "canary-authority-invalid-path"


def test_fd_authority_reader_rejects_parent_symlink(safe_posix_authority) -> None:
    directory, path = safe_posix_authority
    link = directory.parent / f"{directory.name}-link"
    link.symlink_to(directory, target_is_directory=True)
    try:
        with pytest.raises(deploy.DeploymentContractError) as error:
            deploy._read_canary_authority_bytes(
                link / path.name,
                allowed_owner_uids=frozenset({os.geteuid()}),
            )
        assert error.value.code == "canary-authority-invalid-path"
    finally:
        link.unlink()


def test_fd_authority_reader_rejects_insecure_mode(safe_posix_authority) -> None:
    _, path = safe_posix_authority
    path.chmod(0o640)
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority_bytes(
            path,
            allowed_owner_uids=frozenset({os.geteuid()}),
        )
    assert error.value.code == "canary-authority-insecure-mode"


def test_fd_authority_reader_rejects_wrong_owner_contract(safe_posix_authority) -> None:
    _, path = safe_posix_authority
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority_bytes(
            path,
            allowed_owner_uids=frozenset({os.geteuid() + 1}),
        )
    assert error.value.code == "canary-authority-invalid-owner"


def test_fd_authority_reader_rejects_oversized_source(safe_posix_authority) -> None:
    _, path = safe_posix_authority
    path.write_bytes(b"x" * (deploy.MAX_CANARY_AUTHORITY_BYTES + 1))
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority_bytes(
            path,
            allowed_owner_uids=frozenset({os.geteuid()}),
        )
    assert error.value.code == "canary-authority-too-large"


def test_fd_authority_reader_normalizes_permission_error(
    safe_posix_authority,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, path = safe_posix_authority
    original_open = os.open

    def denied_open(target, flags, mode=0o777, *, dir_fd=None):
        if target == path.name and dir_fd is not None:
            raise PermissionError("synthetic")
        return original_open(target, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", denied_open)
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority_bytes(
            path,
            allowed_owner_uids=frozenset({os.geteuid()}),
        )
    assert error.value.code == "canary-authority-permission"


def test_fd_authority_reader_detects_removal_during_read(
    safe_posix_authority,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, path = safe_posix_authority
    original_read = os.read
    removed = False

    def removing_read(fd: int, size: int) -> bytes:
        nonlocal removed
        if not removed:
            path.unlink()
            removed = True
        return original_read(fd, size)

    monkeypatch.setattr(os, "read", removing_read)
    with pytest.raises(deploy.DeploymentContractError) as error:
        deploy._read_canary_authority_bytes(
            path,
            allowed_owner_uids=frozenset({os.geteuid()}),
        )
    assert error.value.code == "canary-authority-race"


def test_activation_success_releases_lease_and_writes_only_canary_gates(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    events, secrets = _install_operation_harness(monkeypatch, contract)
    original_feature_gates = dict(contract.feature_gates)
    _activate(contract)
    assert [event for event, _ in events].count("lease-acquire") == 1
    assert [event for event, _ in events].count("lease-release") == 1
    assert events.index(("lease-acquire", None)) < events.index(("lease-release", None))
    assert dict(contract.feature_gates) == original_feature_gates
    snapshot = deploy._capture_canary_override(contract)
    assert snapshot.present
    assert json.loads(snapshot.data) == {
        "services": {
            contract.target_service: {
                "environment": {ENABLED: "true", ALLOWLIST: "101,202"}
            }
        }
    }
    assert secrets == {"TELEGRAM_BOT_TOKEN": "placeholder"}


def test_activation_pre_mutation_failure_releases_lease(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    events, _ = _install_operation_harness(
        monkeypatch,
        contract,
        fail_at="authority-read",
    )
    with pytest.raises(deploy.DeploymentContractError) as error:
        _activate(contract)
    assert error.value.code == "canary-authority-missing"
    assert [event for event, _ in events].count("lease-release") == 1
    assert not deploy._capture_canary_override(contract).present


def test_activation_post_mutation_failure_restores_exact_state(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    events, _ = _install_operation_harness(
        monkeypatch,
        contract,
        fail_at="post-attestation",
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        _activate(contract)
    assert error.value.status == "ROLLED_BACK"
    assert deploy._capture_canary_override(contract).data == PREVIOUS_CANARY
    assert ("rollback", True) in events
    assert [event for event, _ in events].count("lease-release") == 1


def test_activation_post_mutation_failure_restores_absent_state(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    events, _ = _install_operation_harness(
        monkeypatch,
        contract,
        fail_at="post-attestation",
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        _activate(contract)
    assert error.value.status == "ROLLED_BACK"
    assert not deploy._capture_canary_override(contract).present
    assert ("rollback", False) in events
    assert [event for event, _ in events].count("lease-release") == 1


def test_activation_rollback_failure_is_fail_closed_and_releases_lease(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    events, _ = _install_operation_harness(monkeypatch, contract, fail_at="rollback")
    monkeypatch.setattr(
        deploy,
        "_post_deploy_attestation",
        lambda *_a, **_k: (_ for _ in ()).throw(
            deploy.DeploymentContractError("post-attestation")
        ),
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        _activate(contract)
    assert error.value.status == "FAIL"
    assert error.value.rollback_error_code == "ROLLBACK_FAILED"
    assert [event for event, _ in events].count("lease-release") == 1


def test_activation_lease_release_failure_is_surfaced(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    _install_operation_harness(monkeypatch, contract, fail_at="lease-release")
    with pytest.raises(deploy.DeploymentContractError) as error:
        _activate(contract)
    assert error.value.code == "lease-release-failed"


def test_deactivation_success_releases_lease_and_removes_override(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    events, _ = _install_operation_harness(monkeypatch, contract)
    _deactivate(contract)
    assert not deploy._capture_canary_override(contract).present
    assert [event for event, _ in events].count("lease-acquire") == 1
    assert [event for event, _ in events].count("lease-release") == 1
    assert ("recreate", (IMAGE_ID, False)) in events


def test_deactivation_pre_mutation_failure_releases_lease(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    events, _ = _install_operation_harness(monkeypatch, contract, fail_at="secret-read")
    with pytest.raises(deploy.DeploymentContractError) as error:
        _deactivate(contract)
    assert error.value.code == "secret-source-missing"
    assert deploy._capture_canary_override(contract).data == PREVIOUS_CANARY
    assert [event for event, _ in events].count("lease-release") == 1


def test_deactivation_recreate_failure_restores_exact_previous_state(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    events, _ = _install_operation_harness(monkeypatch, contract, fail_at="recreate")
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        _deactivate(contract)
    assert error.value.status == "ROLLED_BACK"
    assert deploy._capture_canary_override(contract).data == PREVIOUS_CANARY
    assert ("rollback", True) in events
    assert [event for event, _ in events].count("lease-release") == 1


def test_deactivation_post_attestation_failure_restores_exact_previous_state(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    events, _ = _install_operation_harness(
        monkeypatch,
        contract,
        fail_at="post-attestation",
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        _deactivate(contract)
    assert error.value.status == "ROLLED_BACK"
    assert deploy._capture_canary_override(contract).data == PREVIOUS_CANARY
    assert ("rollback", True) in events


def test_deactivation_rollback_failure_is_fail_closed_and_releases_lease(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    events, _ = _install_operation_harness(monkeypatch, contract, fail_at="rollback")
    monkeypatch.setattr(
        deploy,
        "_compose_recreate_hermes",
        lambda *_a, **_k: (_ for _ in ()).throw(
            deploy.DeploymentContractError("compose-up")
        ),
    )
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        _deactivate(contract)
    assert error.value.status == "FAIL"
    assert error.value.rollback_error_code == "ROLLBACK_FAILED"
    assert [event for event, _ in events].count("lease-release") == 1


def test_deactivation_lease_release_failure_is_surfaced(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    _install_operation_harness(monkeypatch, contract, fail_at="lease-release")
    with pytest.raises(deploy.DeploymentContractError) as error:
        _deactivate(contract)
    assert error.value.code == "lease-release-failed"


def test_canary_operations_leave_sqlite_and_qdrant_sentinels_unchanged(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, _ = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    sqlite = tmp_path / "sqlite-sentinel"
    qdrant = tmp_path / "qdrant-sentinel"
    sqlite.write_bytes(b"sqlite-before")
    qdrant.write_bytes(b"qdrant-before")
    _install_operation_harness(monkeypatch, contract)
    _activate(contract)
    assert sqlite.read_bytes() == b"sqlite-before"
    assert qdrant.read_bytes() == b"qdrant-before"


def _install_ordinary_deploy_harness(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compose_fails: bool,
) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    lease = SimpleNamespace(holder_fingerprint="ordinary")
    monkeypatch.setattr(
        deploy,
        "_preflight",
        lambda function, *_a, **_k: (
            lease
            if function is deploy.preflight.acquire_deployment_lease
            else events.append(("lease-release", None))
            if function is deploy.preflight.release_deployment_lease
            else None
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_validate_operation_identity",
        lambda *_a, **_k: (SimpleNamespace(image_id=IMAGE_ID), REVISION),
    )
    monkeypatch.setattr(deploy, "_validate_runtime_directory", lambda *_a, **_k: None)
    monkeypatch.setattr(
        deploy,
        "_ordinary_deploy_pre_mutation_barrier",
        lambda *_a, **_k: (
            SimpleNamespace(image_id=IMAGE_ID),
            {"TELEGRAM_BOT_TOKEN": "placeholder"},
            REVISION,
        ),
    )
    monkeypatch.setattr(deploy, "_capture_pre_mutation_baseline", lambda *_a: _baseline())
    monkeypatch.setattr(
        deploy,
        "_validate_automatic_rollback_readiness",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        deploy,
        "_begin_secret_override_transaction",
        lambda *_a, **_k: SimpleNamespace(),
    )
    monkeypatch.setattr(
        deploy,
        "_finish_secret_override_transaction",
        lambda *_a, **_k: None,
    )

    def recreate(*_a, **_k):
        if compose_fails:
            raise deploy.DeploymentContractError("compose-up")

    monkeypatch.setattr(deploy, "_compose_recreate_hermes", recreate)
    monkeypatch.setattr(deploy, "_post_deploy_attestation", lambda *_a, **_k: object())
    monkeypatch.setattr(
        deploy,
        "_automatic_rollback",
        lambda *_a, canary_override=False, **_k: events.append(
            ("rollback", canary_override)
        ),
    )
    monkeypatch.setattr(deploy, "_write_operation_evidence", lambda *_a, **_k: None)
    return events


def test_ordinary_deploy_cannot_inherit_stale_canary(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, source = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    _install_ordinary_deploy_harness(monkeypatch, compose_fails=False)
    deploy.execute_operation(
        contract,
        source=source,
        image=IMAGE_ID,
        revision=REVISION,
        confirmation=deploy.DEPLOY_CONFIRMATION,
        rollback=False,
    )
    assert not deploy._capture_canary_override(contract).present


def test_ordinary_deploy_failure_restores_exact_canary_state(
    tmp_path: Path,
    protected_contract,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract, source = protected_contract
    contract = _prepare_contract(contract, tmp_path)
    deploy._atomic_write_canary_override_bytes(contract, PREVIOUS_CANARY)
    events = _install_ordinary_deploy_harness(monkeypatch, compose_fails=True)
    with pytest.raises(deploy.PostMutationDeploymentError) as error:
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_ID,
            revision=REVISION,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )
    assert error.value.status == "ROLLED_BACK"
    assert deploy._capture_canary_override(contract).data == PREVIOUS_CANARY
    assert ("rollback", True) in events


def test_changed_sources_contain_no_inline_operator_identity() -> None:
    source = (REPO_ROOT / "scripts" / "hermes_production_deploy.py").read_text(
        encoding="utf-8"
    )
    assert "operator_user_id" not in source
    assert "operator_telegram_id" not in source
