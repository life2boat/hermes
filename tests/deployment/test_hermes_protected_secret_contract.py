from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import hermes_production_deploy as deploy  # noqa: E402


PROTECTED_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "NOUS_API_KEY",
    "OPENAI_API_KEY",
    "QWEN_API_KEY",
)
OPTIONAL_NAMES = frozenset(PROTECTED_NAMES[1:])
SYNTHETIC_VALUES = {
    name: f"synthetic-protected-value-{index}"
    for index, name in enumerate(PROTECTED_NAMES, start=1)
}
IMAGE_A = "sha256:" + "a" * 64
IMAGE_B = "sha256:" + "b" * 64
REVISION = "c" * 40


@pytest.fixture
def protected_contract(tmp_path: Path) -> tuple[deploy.DeploymentContract, Path]:
    source = tmp_path / "approved-source.env"
    source.write_text("", encoding="utf-8")
    source.chmod(0o600)
    runtime = tmp_path / "run" / "hermes"
    runtime.parent.mkdir(mode=0o700)
    contract = replace(
        deploy.load_contract(),
        runtime_directory=runtime,
        secret_override=runtime / "hermes-secrets-override.yml",
        approved_secret_source=source,
        approved_source_owner_uids=frozenset({deploy._effective_uid()}),
    )
    return contract, source


def _write_source(
    source: Path,
    names: tuple[str, ...] = PROTECTED_NAMES,
    *,
    values: dict[str, str] | None = None,
) -> None:
    selected = SYNTHETIC_VALUES if values is None else values
    source.write_text(
        "".join(f"{name}={selected[name]}\n" for name in names),
        encoding="utf-8",
    )
    source.chmod(0o600)


def _install_full_live(contract: deploy.DeploymentContract) -> bytes:
    deploy._write_secret_override(contract, dict(SYNTHETIC_VALUES))
    return contract.secret_override.read_bytes()


def _override_environment(path: Path) -> dict[str, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return document["services"]["hermes-bot"]["environment"]


def test_manifest_declares_complete_six_key_policy() -> None:
    contract = deploy.load_contract()
    assert contract.protected_secret_names == PROTECTED_NAMES
    assert contract.required_secret_names == ("TELEGRAM_BOT_TOKEN",)
    assert all(
        spec.source_class == "approved-production-secret-source"
        and spec.destination_name == spec.name
        and not spec.allow_empty
        and spec.removal_requires_authorization
        for spec in contract.protected_secrets
    )


def test_complete_six_key_source_is_accepted_without_value_normalization(
    protected_contract,
) -> None:
    contract, source = protected_contract
    exact_values = dict(SYNTHETIC_VALUES)
    exact_values["TELEGRAM_BOT_TOKEN"] = '  synthetic token = "#kept"  '
    _write_source(source, values=exact_values)
    parsed = deploy.read_required_secrets(contract, source)
    assert parsed == exact_values


def test_minimal_required_source_is_accepted_for_clean_install(
    protected_contract,
) -> None:
    contract, source = protected_contract
    _write_source(source, ("TELEGRAM_BOT_TOKEN",))
    deploy.prepare_secret_override(contract, source)
    assert _override_environment(contract.secret_override) == {
        "TELEGRAM_BOT_TOKEN": SYNTHETIC_VALUES["TELEGRAM_BOT_TOKEN"]
    }


def test_minimal_source_is_blocked_against_six_key_live_override(
    protected_contract,
) -> None:
    contract, source = protected_contract
    live_before = _install_full_live(contract)
    _write_source(source, ("TELEGRAM_BOT_TOKEN",))
    with pytest.raises(
        deploy.DeploymentContractError,
        match="protected-credential-removal",
    ):
        deploy.prepare_secret_override(contract, source)
    assert contract.secret_override.read_bytes() == live_before


def test_missing_required_key_is_rejected(protected_contract) -> None:
    contract, source = protected_contract
    _write_source(source, tuple(PROTECTED_NAMES[1:]))
    with pytest.raises(
        deploy.DeploymentContractError,
        match="required-secret-missing",
    ):
        deploy.read_required_secrets(contract, source)


def test_optional_keys_may_be_absent_without_live_removal(
    protected_contract,
) -> None:
    contract, source = protected_contract
    _write_source(source, ("TELEGRAM_BOT_TOKEN",))
    assert deploy.read_required_secrets(contract, source) == {
        "TELEGRAM_BOT_TOKEN": SYNTHETIC_VALUES["TELEGRAM_BOT_TOKEN"]
    }


@pytest.mark.parametrize(
    ("text", "error"),
    (
        (
            "TELEGRAM_BOT_TOKEN=one\nTELEGRAM_BOT_TOKEN=two\n",
            "secret-source-variable",
        ),
        (
            "TELEGRAM_BOT_TOKEN=one\nUNDECLARED_KEY=two\n",
            "secret-source-variable-set",
        ),
        ("TELEGRAM_BOT_TOKEN\n", "secret-source-syntax"),
        (
            "TELEGRAM_BOT_TOKEN=one\nGEMINI_API_KEY=\n",
            "secret-empty-value",
        ),
    ),
)
def test_invalid_approved_sources_fail_closed(
    protected_contract,
    text: str,
    error: str,
) -> None:
    contract, source = protected_contract
    source.write_text(text, encoding="utf-8")
    source.chmod(0o600)
    with pytest.raises(deploy.DeploymentContractError, match=error):
        deploy.read_required_secrets(contract, source)


def test_staged_generator_emits_exact_six_key_set(
    protected_contract,
) -> None:
    contract, source = protected_contract
    _write_source(source)
    staged = deploy._stage_secret_override(
        contract,
        deploy.read_required_secrets(contract, source),
    )
    try:
        assert set(_override_environment(staged)) == set(PROTECTED_NAMES)
        assert deploy._read_secret_override(
            contract,
            staged,
            code="staged-override",
        ) == SYNTHETIC_VALUES
    finally:
        staged.unlink()


def test_generator_rejects_undeclared_key_name(
    protected_contract,
) -> None:
    contract, _source = protected_contract
    with pytest.raises(
        deploy.DeploymentContractError,
        match="override-input-variable-set",
    ):
        deploy._override_document(
            contract,
            {
                **SYNTHETIC_VALUES,
                "UNDECLARED_KEY": "synthetic-value",
            },
        )


def test_fingerprint_drift_is_rejected() -> None:
    contract = deploy.load_contract()
    changed = dict(SYNTHETIC_VALUES)
    changed["GEMINI_API_KEY"] = "different-synthetic-value"
    with pytest.raises(
        deploy.DeploymentContractError,
        match="credential-fingerprint-drift",
    ):
        deploy._validate_secret_transition(
            contract,
            live=dict(SYNTHETIC_VALUES),
            staged=changed,
        )


def test_removal_requires_exact_authorization_and_rollback_readiness() -> None:
    contract = deploy.load_contract()
    minimal = {
        "TELEGRAM_BOT_TOKEN": SYNTHETIC_VALUES["TELEGRAM_BOT_TOKEN"]
    }
    with pytest.raises(
        deploy.DeploymentContractError,
        match="protected-credential-removal",
    ):
        deploy._validate_secret_transition(
            contract,
            live=dict(SYNTHETIC_VALUES),
            staged=minimal,
        )
    deploy._validate_secret_transition(
        contract,
        live=dict(SYNTHETIC_VALUES),
        staged=minimal,
        removal_authorization=deploy.ProtectedSecretRemovalAuthorization(
            exact_names=OPTIONAL_NAMES,
            rollback_ready=True,
        ),
    )
    with pytest.raises(
        deploy.DeploymentContractError,
        match="protected-credential-removal",
    ):
        deploy._validate_secret_transition(
            contract,
            live=dict(SYNTHETIC_VALUES),
            staged=minimal,
            removal_authorization=deploy.ProtectedSecretRemovalAuthorization(
                exact_names=OPTIONAL_NAMES,
                rollback_ready=False,
            ),
        )


def test_unsafe_staged_permissions_are_rejected(protected_contract) -> None:
    contract, _source = protected_contract
    staged = deploy._stage_secret_override(contract, dict(SYNTHETIC_VALUES))
    staged.chmod(0o640)
    try:
        with pytest.raises(
            deploy.DeploymentContractError,
            match="staged-override-mode",
        ):
            deploy._read_secret_override(
                contract,
                staged,
                code="staged-override",
            )
    finally:
        staged.chmod(0o600)
        staged.unlink()


def test_atomic_publish_success_and_transaction_restore(
    protected_contract,
) -> None:
    contract, _source = protected_contract
    live_before = _install_full_live(contract)
    transaction = deploy._begin_secret_override_transaction(
        contract,
        dict(SYNTHETIC_VALUES),
    )
    assert transaction.rollback_path is not None
    assert transaction.rollback_path.is_file()
    deploy._finish_secret_override_transaction(
        contract,
        transaction,
        preserve_published=False,
    )
    assert contract.secret_override.read_bytes() == live_before
    assert list(contract.runtime_directory.glob(".hermes-secrets-*")) == []


def test_atomic_publish_interruption_preserves_live_override(
    protected_contract,
    monkeypatch,
) -> None:
    contract, source = protected_contract
    live_before = _install_full_live(contract)
    _write_source(source)
    original_replace = os.replace

    def fail_candidate_replace(source_path, target_path):
        if Path(target_path) == contract.secret_override:
            raise OSError
        return original_replace(source_path, target_path)

    monkeypatch.setattr(deploy.os, "replace", fail_candidate_replace)
    with pytest.raises(
        deploy.DeploymentContractError,
        match="override-atomic-write",
    ):
        deploy.prepare_secret_override(contract, source)
    assert contract.secret_override.read_bytes() == live_before
    assert list(contract.runtime_directory.glob(".hermes-secrets-*")) == []


def test_deploy_aborts_before_compose_on_five_key_removal(
    protected_contract,
    monkeypatch,
) -> None:
    contract, source = protected_contract
    _install_full_live(contract)
    _write_source(source, ("TELEGRAM_BOT_TOKEN",))
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    monkeypatch.setattr(
        deploy,
        "inspect_local_image",
        lambda *_args, **_kwargs: deploy.InspectedImage(
            image_id=IMAGE_A,
            revision=REVISION,
        ),
    )
    monkeypatch.setattr(
        deploy,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("Compose must not run"),
    )
    with pytest.raises(
        deploy.DeploymentContractError,
        match="protected-credential-removal",
    ):
        deploy.execute_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            confirmation=deploy.DEPLOY_CONFIRMATION,
            rollback=False,
        )


def test_rollback_preflight_uses_same_removal_guard(
    protected_contract,
    monkeypatch,
) -> None:
    contract, source = protected_contract
    _install_full_live(contract)
    _write_source(source, ("TELEGRAM_BOT_TOKEN",))
    monkeypatch.setattr(
        deploy,
        "current_source_head_revision",
        lambda _contract: REVISION,
    )
    monkeypatch.setattr(deploy, "validate_repository", lambda *_args: None)
    monkeypatch.setattr(
        deploy,
        "validate_rollback_revision",
        lambda *_args, **_kwargs: None,
    )

    def inspect(_contract, image, **_kwargs):
        return deploy.InspectedImage(
            image_id=IMAGE_A if image == IMAGE_A else IMAGE_B,
            revision=REVISION,
        )

    monkeypatch.setattr(deploy, "inspect_local_image", inspect)
    with pytest.raises(
        deploy.DeploymentContractError,
        match="protected-credential-removal",
    ):
        deploy.plan_operation(
            contract,
            source=source,
            image=IMAGE_A,
            revision=REVISION,
            rollback_from=IMAGE_B,
        )


def test_safe_failure_output_never_contains_raw_values(
    protected_contract,
    monkeypatch,
    capsys,
) -> None:
    contract, source = protected_contract
    _write_source(source)
    with source.open("a", encoding="utf-8") as handle:
        handle.write("GEMINI_API_KEY=duplicate-synthetic-value\n")
    monkeypatch.setattr(deploy, "load_contract", lambda: contract)
    assert deploy.main(["check-secret-source"]) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert all(value not in combined for value in SYNTHETIC_VALUES.values())
    assert "duplicate-synthetic-value" not in combined
