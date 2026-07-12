from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Mapping, Sequence

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from hermes_deployment_sot import (  # noqa: E402
    DeploymentSOTError,
    ReadOnlyRunner,
    ValidationInputs,
    compose_file_paths,
    inspect_image,
    load_fingerprint_baseline,
    load_manifest,
    parse_dotenv,
    run_validation,
    validate_compose_render,
    validate_credential_fingerprints,
    validate_feature_flags,
    validate_git_state,
    validate_override,
    validate_qdrant_identity,
    validate_required_env,
    validate_runtime_config,
)
from validate_hermes_deployment import main as validator_main  # noqa: E402


SHA = "a" * 40
TARGET_IMAGE = "example/hermes:target"
ROLLBACK_IMAGE = "example/hermes:rollback"
QDRANT_ID = "b" * 64
SYNTHETIC_VALUES = {
    "GEMINI_API_KEY": "synthetic-gemini-value",
    "NOUS_API_KEY": "synthetic-nous-value",
    "NOUS_INFERENCE_BASE_URL": "https://example.invalid/v1",
    "TELEGRAM_BOT_TOKEN": "synthetic-telegram-value",
}


def _manifest_data() -> dict:
    return yaml.safe_load((ROOT / "deploy" / "hermes-production.manifest.yml").read_text(encoding="utf-8"))


def _write_yaml(path: Path, data: object, mode: int | None = None) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def _proven_manifest(tmp_path: Path, mutate=None) -> Path:
    data = _manifest_data()
    data["secrets_override"]["producer_status"] = "proven"
    data["secrets_override"]["generator"] = "synthetic-test-producer"
    if mutate is not None:
        mutate(data)
    return _write_yaml(tmp_path / "manifest.yml", data)


def _override_data() -> dict:
    return {
        "services": {
            "hermes-bot": {
                "environment": {
                    "NOUS_API_KEY": "${NOUS_API_KEY}",
                    "NOUS_INFERENCE_BASE_URL": "${NOUS_INFERENCE_BASE_URL}",
                    "TELEGRAM_BOT_TOKEN": "${TELEGRAM_BOT_TOKEN}",
                }
            }
        }
    }


def _rendered() -> dict:
    return {
        "name": "hermes-agent",
        "services": {
            "hermes-bot": {
                "image": TARGET_IMAGE,
                "restart": "unless-stopped",
                "command": ["hermes", "gateway"],
                "networks": {"default": None},
                "environment": {
                    "NOUS_API_KEY": SYNTHETIC_VALUES["NOUS_API_KEY"],
                    "NOUS_INFERENCE_BASE_URL": SYNTHETIC_VALUES["NOUS_INFERENCE_BASE_URL"],
                    "TELEGRAM_BOT_TOKEN": SYNTHETIC_VALUES["TELEGRAM_BOT_TOKEN"],
                },
                "volumes": [
                    {"type": "bind", "source": "/home/hermes/.hermes", "target": "/home/hermes/.hermes"},
                    {"type": "bind", "source": "/home/hermes/healbite.db", "target": "/home/hermes/healbite.db"},
                    {"type": "bind", "source": "/home/hermes/backups", "target": "/home/hermes/backups"},
                    {
                        "type": "bind",
                        "source": "/home/hermes/.cache/huggingface",
                        "target": "/home/hermes/.cache/huggingface",
                    },
                ],
            },
            "qdrant": {
                "image": "qdrant/qdrant:v1.15.4",
                "restart": "unless-stopped",
                "networks": {"default": None},
                "volumes": [
                    {
                        "type": "volume",
                        "source": "hermes-agent_qdrant_data",
                        "target": "/qdrant/storage",
                    }
                ],
            },
        }
    }


class FakeRunner(ReadOnlyRunner):
    def __init__(self, *, dirty: bool = False, sha: str = SHA, rendered: dict | None = None):
        self.dirty = dirty
        self.sha = sha
        self.rendered = rendered or _rendered()
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        del cwd, env
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("git", "rev-parse"):
            return self.sha + "\n"
        if command[:2] == ("git", "status"):
            return " M file\n" if self.dirty else ""
        if command[:3] == ("docker", "compose", "--project-name"):
            assert command[-3:] == ("config", "--format", "json")
            return json.dumps(self.rendered)
        if command[:3] == ("docker", "image", "inspect"):
            reference = command[3]
            if reference not in {TARGET_IMAGE, ROLLBACK_IMAGE}:
                raise DeploymentSOTError("image unavailable")
            return json.dumps(
                [
                    {
                        "Id": "sha256:" + "c" * 64,
                        "Config": {
                            "Entrypoint": ["/init", "/opt/hermes/docker/main-wrapper.sh"],
                            "Labels": {"org.opencontainers.image.revision": SHA},
                        },
                    }
                ]
            )
        if command[:2] == ("docker", "inspect"):
            return json.dumps([{"Id": QDRANT_ID}])
        raise AssertionError(f"unexpected command class: {command[:3]}")


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[ValidationInputs, FakeRunner]:
    source = tmp_path / "source"
    source.mkdir()
    (source / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    manifest_path = _proven_manifest(tmp_path)
    override = _write_yaml(tmp_path / "override.yml", _override_data(), 0o600)
    interpolation_env = tmp_path / "interpolation.env"
    interpolation_env.write_text(
        "\n".join(f"{name}={value}" for name, value in SYNTHETIC_VALUES.items()) + "\n",
        encoding="utf-8",
    )
    service_env = tmp_path / "service.env"
    service_env.write_text("HEALBITE_WEEKLY_MENU_ENABLED=false\n", encoding="utf-8")
    runtime_config = _write_yaml(
        tmp_path / "config.yml",
        {
            "model": {"provider": "deepseek", "default": "deepseek-chat"},
            "auxiliary": {"vision": {"provider": "gemini", "model": "gemini-2.5-flash"}},
        },
    )
    fingerprints = {
        name: hashlib.sha256(SYNTHETIC_VALUES[name].encode("utf-8")).hexdigest()
        for name in ("GEMINI_API_KEY", "NOUS_API_KEY", "TELEGRAM_BOT_TOKEN")
    }
    baseline = _write_yaml(
        tmp_path / "fingerprints.yml",
        {"version": 1, "algorithm": "sha256", "fingerprints": fingerprints},
    )
    inputs = ValidationInputs(
        source_root=source,
        manifest_path=manifest_path,
        expected_sha=SHA,
        interpolation_env_file=interpolation_env,
        service_env_file=service_env,
        runtime_config_file=runtime_config,
        secrets_override_path=override,
        credential_baseline_path=baseline,
        target_image=TARGET_IMAGE,
        rollback_image=ROLLBACK_IMAGE,
        expected_qdrant_id=QDRANT_ID,
    )
    return inputs, FakeRunner()


def test_tracked_manifest_is_valid_and_records_inconclusive_producer() -> None:
    manifest = load_manifest(ROOT / "deploy" / "hermes-production.manifest.yml")
    assert manifest.secrets_override["producer_status"] == "inconclusive"
    assert manifest.secrets_override["generator"] is None


def test_tracked_manifest_contains_no_literal_secret_or_private_identity() -> None:
    text = (ROOT / "deploy" / "hermes-production.manifest.yml").read_text(encoding="utf-8")
    assert re.search(r"(?<!\d)\d{8,12}(?!\d)", text) is None
    assert "PRIVATE KEY" not in text
    assert "Bearer " not in text
    assert "@github" not in text


def test_unknown_manifest_version_fails(tmp_path: Path) -> None:
    path = _proven_manifest(tmp_path, lambda data: data.update(version=2))
    with pytest.raises(DeploymentSOTError, match="version"):
        load_manifest(path)


def test_missing_manifest_field_fails(tmp_path: Path) -> None:
    path = _proven_manifest(tmp_path, lambda data: data["compose"].pop("project"))
    with pytest.raises(DeploymentSOTError, match="fields"):
        load_manifest(path)


def test_duplicate_manifest_yaml_key_fails(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yml"
    path.write_text("version: 1\nversion: 1\n", encoding="utf-8")
    with pytest.raises(DeploymentSOTError, match="duplicate"):
        load_manifest(path)


def test_duplicate_compose_file_contract_fails(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["compose"]["files"][1] = {"kind": "repository", "path": "docker-compose.yml"}

    with pytest.raises(DeploymentSOTError, match="override|order"):
        load_manifest(_proven_manifest(tmp_path, mutate))


def test_wrong_compose_file_order_fails(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["compose"]["files"].reverse()

    with pytest.raises(DeploymentSOTError, match="first compose file"):
        load_manifest(_proven_manifest(tmp_path, mutate))


def test_application_service_cannot_equal_qdrant(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["compose"]["qdrant_service"] = "hermes-bot"

    with pytest.raises(DeploymentSOTError, match="cannot equal"):
        load_manifest(_proven_manifest(tmp_path, mutate))


def test_qdrant_cannot_enter_recreate_plan(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["deployment"]["recreate_services"] = ["hermes-bot", "qdrant"]

    with pytest.raises(DeploymentSOTError, match="recreate"):
        load_manifest(_proven_manifest(tmp_path, mutate))


def test_inconclusive_producer_cannot_claim_generator(tmp_path: Path) -> None:
    def mutate(data: dict) -> None:
        data["secrets_override"]["producer_status"] = "inconclusive"

    with pytest.raises(DeploymentSOTError, match="generator"):
        load_manifest(_proven_manifest(tmp_path, mutate))


def test_override_valid(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    override = _write_yaml(tmp_path / "override.yml", _override_data(), 0o600)
    assert set(validate_override(override, manifest)) == set(
        manifest.secrets_override["required_variables"]
    )


def test_missing_override_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    with pytest.raises(DeploymentSOTError, match="missing"):
        validate_override(tmp_path / "missing.yml", manifest)


def test_override_symlink_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    target = _write_yaml(tmp_path / "target.yml", _override_data(), 0o600)
    link = tmp_path / "link.yml"
    link.symlink_to(target)
    with pytest.raises(DeploymentSOTError, match="symlink"):
        validate_override(link, manifest)


def test_override_wrong_permissions_fail(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    override = _write_yaml(tmp_path / "override.yml", _override_data(), 0o644)
    with pytest.raises(DeploymentSOTError, match="permissions"):
        validate_override(override, manifest)


def test_override_wrong_owner_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    override = _write_yaml(tmp_path / "override.yml", _override_data(), 0o600)
    real_stat = Path.stat

    def fake_stat(path: Path, *args, **kwargs):
        metadata = real_stat(path, *args, **kwargs)
        if path == override:
            return SimpleNamespace(st_mode=metadata.st_mode, st_uid=os.geteuid() + 1)
        return metadata

    monkeypatch.setattr(Path, "stat", fake_stat)
    with pytest.raises(DeploymentSOTError, match="owner"):
        validate_override(override, manifest)


def test_override_malformed_yaml_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    override = tmp_path / "override.yml"
    override.write_text("services: [\n", encoding="utf-8")
    override.chmod(0o600)
    with pytest.raises(DeploymentSOTError, match="YAML"):
        validate_override(override, manifest)


def test_override_missing_key_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    data = _override_data()
    del data["services"]["hermes-bot"]["environment"]["NOUS_API_KEY"]
    override = _write_yaml(tmp_path / "override.yml", data, 0o600)
    with pytest.raises(DeploymentSOTError, match="contract"):
        validate_override(override, manifest)


def test_override_duplicate_key_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    override = tmp_path / "override.yml"
    override.write_text(
        "services:\n  hermes-bot:\n    environment:\n      NOUS_API_KEY: ${NOUS_API_KEY}\n"
        "      NOUS_API_KEY: ${NOUS_API_KEY}\n",
        encoding="utf-8",
    )
    override.chmod(0o600)
    with pytest.raises(DeploymentSOTError, match="duplicate"):
        validate_override(override, manifest)


def test_override_literal_secret_fails_without_exposing_value(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    data = _override_data()
    sensitive_value = "synthetic-sensitive-value-must-not-appear"
    data["services"]["hermes-bot"]["environment"]["NOUS_API_KEY"] = sensitive_value
    override = _write_yaml(tmp_path / "override.yml", data, 0o600)
    with pytest.raises(DeploymentSOTError) as caught:
        validate_override(override, manifest)
    assert sensitive_value not in str(caught.value)


def test_dotenv_duplicate_key_fails(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("GEMINI_API_KEY=one\nGEMINI_API_KEY=two\n", encoding="utf-8")
    with pytest.raises(DeploymentSOTError, match="duplicate"):
        parse_dotenv(path)


def test_required_env_missing_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    with pytest.raises(DeploymentSOTError, match="missing"):
        validate_required_env({}, manifest)


def test_feature_flag_drift_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    with pytest.raises(DeploymentSOTError, match="feature"):
        validate_feature_flags({"HEALBITE_WEEKLY_MENU_ENABLED": "true"}, manifest)


def test_feature_allowlist_drift_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    with pytest.raises(DeploymentSOTError, match="allowlist"):
        validate_feature_flags({"HEALBITE_WEEKLY_MENU_ALLOWLIST": "synthetic"}, manifest)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("model", "provider"), "other"),
        (("model", "default"), "other-model"),
        (("auxiliary", "vision", "provider"), "other"),
        (("auxiliary", "vision", "model"), "other-model"),
    ],
)
def test_provider_or_model_drift_fails(tmp_path: Path, path: tuple[str, ...], value: str) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    config = {
        "model": {"provider": "deepseek", "default": "deepseek-chat"},
        "auxiliary": {"vision": {"provider": "gemini", "model": "gemini-2.5-flash"}},
    }
    target = config
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    config_path = _write_yaml(tmp_path / "config.yml", config)
    with pytest.raises(DeploymentSOTError, match="provider|model"):
        validate_runtime_config(config_path, manifest)


def test_sha_mismatch_fails(tmp_path: Path) -> None:
    with pytest.raises(DeploymentSOTError, match="SHA mismatch"):
        validate_git_state(tmp_path, SHA, FakeRunner(sha="b" * 40))


def test_dirty_worktree_fails(tmp_path: Path) -> None:
    with pytest.raises(DeploymentSOTError, match="dirty"):
        validate_git_state(tmp_path, SHA, FakeRunner(dirty=True))


def test_missing_compose_file_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    override = _write_yaml(tmp_path / "override.yml", _override_data(), 0o600)
    with pytest.raises(DeploymentSOTError, match="missing"):
        compose_file_paths(tmp_path / "source", override, manifest)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["services"]["hermes-bot"].update(image="wrong"), "image"),
        (lambda data: data["services"]["hermes-bot"].update(restart="always"), "restart"),
        (lambda data: data["services"]["hermes-bot"].update(command=["other"]), "command"),
        (lambda data: data["services"]["hermes-bot"].update(networks={"other": None}), "network"),
        (lambda data: data["services"]["hermes-bot"].update(volumes=[]), "mount"),
        (lambda data: data["services"].update(unexpected={}), "service"),
        (lambda data: data.update(name="other"), "project"),
        (lambda data: data["services"]["hermes-bot"].update(profiles=["other"]), "profile"),
    ],
)
def test_compose_render_drift_fails(tmp_path: Path, mutation, message: str) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    rendered = _rendered()
    mutation(rendered)
    with pytest.raises(DeploymentSOTError, match=message):
        validate_compose_render(rendered, TARGET_IMAGE, manifest)


def test_target_image_revision_mismatch_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    with pytest.raises(DeploymentSOTError, match="revision"):
        inspect_image(TARGET_IMAGE, "b" * 40, manifest, FakeRunner())


def test_rollback_image_missing_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    with pytest.raises(DeploymentSOTError, match="unavailable"):
        inspect_image("example/hermes:missing", None, manifest, FakeRunner())


def test_credential_fingerprint_mismatch_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    baseline = _write_yaml(
        tmp_path / "fingerprints.yml",
        {
            "version": 1,
            "algorithm": "sha256",
            "fingerprints": {
                "GEMINI_API_KEY": "0" * 64,
                "NOUS_API_KEY": "0" * 64,
                "TELEGRAM_BOT_TOKEN": "0" * 64,
            },
        },
    )
    fingerprints = load_fingerprint_baseline(baseline, manifest)
    with pytest.raises(DeploymentSOTError, match="fingerprint"):
        validate_credential_fingerprints(SYNTHETIC_VALUES, {}, fingerprints)


def test_qdrant_identity_mismatch_fails(tmp_path: Path) -> None:
    manifest = load_manifest(_proven_manifest(tmp_path))
    with pytest.raises(DeploymentSOTError, match="qdrant"):
        validate_qdrant_identity("different", manifest, FakeRunner())


def test_successful_check_only_is_read_only(prepared) -> None:
    inputs, runner = prepared
    run_validation(inputs, runner)
    flattened = {token for call in runner.calls for token in call}
    assert not flattened.intersection({"build", "up", "down", "restart", "create", "run", "exec", "rm"})
    assert any(call[:3] == ("docker", "compose", "--project-name") for call in runner.calls)


def test_inconclusive_tracked_producer_blocks_validation(prepared) -> None:
    inputs, runner = prepared
    inputs = ValidationInputs(
        **{**inputs.__dict__, "manifest_path": ROOT / "deploy" / "hermes-production.manifest.yml"}
    )
    with pytest.raises(DeploymentSOTError, match="producer"):
        run_validation(inputs, runner)


def test_cli_requires_check_only(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        validator_main([])
    assert "secret" not in capsys.readouterr().out.lower()


def test_read_only_runner_rejects_mutating_command() -> None:
    with pytest.raises(DeploymentSOTError, match="read-only"):
        ReadOnlyRunner.run(("docker", "compose", "up"))


def test_error_and_cli_output_do_not_expose_synthetic_secret(
    prepared,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs, _ = prepared
    sensitive_value = SYNTHETIC_VALUES["NOUS_API_KEY"]
    data = _override_data()
    data["services"]["hermes-bot"]["environment"]["NOUS_API_KEY"] = sensitive_value
    _write_yaml(inputs.secrets_override_path, data, 0o600)
    argv = [
        "--check-only",
        "--manifest",
        str(inputs.manifest_path),
        "--source-root",
        str(inputs.source_root),
        "--expected-sha",
        inputs.expected_sha,
        "--env-file",
        str(inputs.interpolation_env_file),
        "--service-env-file",
        str(inputs.service_env_file),
        "--runtime-config",
        str(inputs.runtime_config_file),
        "--secrets-override",
        str(inputs.secrets_override_path),
        "--credential-baseline",
        str(inputs.credential_baseline_path),
        "--target-image",
        inputs.target_image,
        "--rollback-image",
        inputs.rollback_image,
        "--expected-qdrant-id",
        inputs.expected_qdrant_id,
    ]
    assert validator_main(argv) == 1
    output = capsys.readouterr().out
    assert sensitive_value not in output
    assert "STATUS=FAIL" in output


def test_check_only_does_not_modify_inputs(prepared) -> None:
    inputs, runner = prepared
    paths = [
        inputs.manifest_path,
        inputs.interpolation_env_file,
        inputs.service_env_file,
        inputs.runtime_config_file,
        inputs.secrets_override_path,
        inputs.credential_baseline_path,
    ]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    run_validation(inputs, runner)
    after = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    assert after == before
