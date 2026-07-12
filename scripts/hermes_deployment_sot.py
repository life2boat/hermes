#!/usr/bin/env python3
"""Fail-closed, secret-safe deployment source-of-truth primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


class DeploymentSOTError(ValueError):
    """A deployment input violated the fail-closed contract."""


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys instead of keeping the last one."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DeploymentSOTError("duplicate YAML key")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_unique_yaml(path: Path) -> Any:
    if path.is_symlink():
        raise DeploymentSOTError("symlink input rejected")
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise DeploymentSOTError("invalid YAML input") from exc


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise DeploymentSOTError(f"{name} must be a mapping")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise DeploymentSOTError(f"{name} must be a list")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentSOTError(f"{name} must be a non-empty string")
    return value


def _exact_keys(mapping: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(mapping) != expected:
        raise DeploymentSOTError(f"{name} fields do not match the contract")


@dataclass(frozen=True)
class DeploymentManifest:
    path: Path
    data: dict[str, Any]

    @property
    def compose(self) -> dict[str, Any]:
        return self.data["compose"]

    @property
    def deployment(self) -> dict[str, Any]:
        return self.data["deployment"]

    @property
    def secrets_override(self) -> dict[str, Any]:
        return self.data["secrets_override"]

    @property
    def contract(self) -> dict[str, Any]:
        return self.data["required_contract"]


def load_manifest(path: Path) -> DeploymentManifest:
    raw = _mapping(load_unique_yaml(path), "manifest")
    _exact_keys(
        raw,
        {"version", "compose", "deployment", "secrets_override", "required_contract"},
        "manifest",
    )
    if raw["version"] != 1:
        raise DeploymentSOTError("unsupported manifest version")

    compose = _mapping(raw["compose"], "compose")
    _exact_keys(
        compose,
        {
            "project",
            "application_service",
            "qdrant_service",
            "files",
            "interpolation_env_input",
            "service_env_input",
            "runtime_config_input",
            "profiles",
            "expected_services",
        },
        "compose",
    )
    project = _string(compose["project"], "compose.project")
    application = _string(compose["application_service"], "application service")
    qdrant = _string(compose["qdrant_service"], "qdrant service")
    if application == qdrant:
        raise DeploymentSOTError("application service cannot equal qdrant service")
    for field in (
        "interpolation_env_input",
        "service_env_input",
        "runtime_config_input",
    ):
        value = _string(compose[field], f"compose.{field}")
        if not ENV_NAME_RE.fullmatch(value):
            raise DeploymentSOTError(f"compose.{field} must be an operator input name")
    if _list(compose["profiles"], "compose.profiles"):
        raise DeploymentSOTError("production compose profiles must be empty")
    services = _list(compose["expected_services"], "expected services")
    if services != [application, qdrant] or len(set(services)) != len(services):
        raise DeploymentSOTError("unexpected service names or order")

    files = _list(compose["files"], "compose.files")
    if len(files) != 2:
        raise DeploymentSOTError("compose requires repository file then generated override")
    first = _mapping(files[0], "first compose file")
    second = _mapping(files[1], "second compose file")
    if first != {"kind": "repository", "path": "docker-compose.yml"}:
        raise DeploymentSOTError("invalid first compose file or order")
    if second != {
        "kind": "generated_override",
        "operator_input": "HERMES_SECRETS_OVERRIDE_PATH",
    }:
        raise DeploymentSOTError("invalid generated override or order")
    if len({first.get("path"), second.get("operator_input")}) != 2:
        raise DeploymentSOTError("duplicate compose file contract")
    repository_path = Path(first["path"])
    if repository_path.is_absolute() or ".." in repository_path.parts:
        raise DeploymentSOTError("repository compose path must be relative")

    deployment = _mapping(raw["deployment"], "deployment")
    _exact_keys(
        deployment,
        {
            "recreate_policy",
            "recreate_services",
            "image_variable",
            "revision_variable",
            "rollback_image_input",
            "revision_label",
        },
        "deployment",
    )
    if deployment["recreate_policy"] != "application-only":
        raise DeploymentSOTError("recreate policy must be application-only")
    if deployment["recreate_services"] != [application]:
        raise DeploymentSOTError("recreate plan must contain only the application service")
    if qdrant in deployment["recreate_services"]:
        raise DeploymentSOTError("qdrant cannot be in recreate plan")
    for field in ("image_variable", "revision_variable", "rollback_image_input"):
        if not ENV_NAME_RE.fullmatch(_string(deployment[field], field)):
            raise DeploymentSOTError(f"{field} must be an environment input name")
    _string(deployment["revision_label"], "revision label")

    override = _mapping(raw["secrets_override"], "secrets_override")
    _exact_keys(
        override,
        {
            "runtime_path_input",
            "producer_status",
            "generator",
            "service",
            "required_mode",
            "required_owner",
            "symlink_allowed",
            "required_variables",
            "interpolation_template",
            "credential_fingerprint_variables",
        },
        "secrets_override",
    )
    if override["runtime_path_input"] != "HERMES_SECRETS_OVERRIDE_PATH":
        raise DeploymentSOTError("override path input mismatch")
    if override["producer_status"] not in {"proven", "inconclusive"}:
        raise DeploymentSOTError("invalid override producer status")
    if override["producer_status"] == "inconclusive" and override["generator"] is not None:
        raise DeploymentSOTError("inconclusive producer cannot name a generator")
    if override["producer_status"] == "proven":
        _string(override["generator"], "secrets override generator")
    if override["service"] != application:
        raise DeploymentSOTError("override service mismatch")
    if (
        override["required_mode"] != "0600"
        or override["required_owner"] != "current-user"
        or override["symlink_allowed"] is not False
    ):
        raise DeploymentSOTError("override filesystem contract is unsafe")
    required_variables = _list(override["required_variables"], "required variables")
    if not required_variables or len(set(required_variables)) != len(required_variables):
        raise DeploymentSOTError("required variables must be unique")
    if not all(isinstance(name, str) and ENV_NAME_RE.fullmatch(name) for name in required_variables):
        raise DeploymentSOTError("invalid required variable name")
    template = _mapping(override["interpolation_template"], "interpolation template")
    if set(template) != set(required_variables):
        raise DeploymentSOTError("interpolation template variables mismatch")
    for name in required_variables:
        if template[name] != "${" + name + "}":
            raise DeploymentSOTError("override must contain interpolation only")
    fingerprint_variables = _list(
        override["credential_fingerprint_variables"],
        "credential fingerprint variables",
    )
    if not fingerprint_variables or len(set(fingerprint_variables)) != len(fingerprint_variables):
        raise DeploymentSOTError("credential fingerprint variables must be unique")
    if not all(isinstance(name, str) and ENV_NAME_RE.fullmatch(name) for name in fingerprint_variables):
        raise DeploymentSOTError("invalid credential fingerprint variable")

    contract = _mapping(raw["required_contract"], "required_contract")
    _exact_keys(
        contract,
        {
            "restart_policy",
            "command",
            "image_entrypoint",
            "networks",
            "mounts",
            "database_mount_target",
            "feature_flags",
            "providers",
        },
        "required_contract",
    )
    _string(contract["restart_policy"], "restart policy")
    if not all(isinstance(item, str) and item for item in _list(contract["command"], "command")):
        raise DeploymentSOTError("invalid command")
    if not all(
        isinstance(item, str) and item
        for item in _list(contract["image_entrypoint"], "image entrypoint")
    ):
        raise DeploymentSOTError("invalid image entrypoint")
    networks = _list(contract["networks"], "networks")
    if not networks or len(set(networks)) != len(networks):
        raise DeploymentSOTError("networks must be unique")
    mounts = _list(contract["mounts"], "mounts")
    mount_keys: set[tuple[str, str]] = set()
    for item in mounts:
        mount = _mapping(item, "mount")
        _exact_keys(mount, {"service", "type", "source", "target"}, "mount")
        if mount["service"] not in services or mount["type"] not in {"bind", "volume"}:
            raise DeploymentSOTError("invalid mount service or type")
        key = (_string(mount["service"], "mount service"), _string(mount["target"], "mount target"))
        if key in mount_keys:
            raise DeploymentSOTError("duplicate mount target")
        mount_keys.add(key)
        _string(mount["source"], "mount source")
    database_target = _string(contract["database_mount_target"], "database mount target")
    if (application, database_target) not in mount_keys:
        raise DeploymentSOTError("database mount target missing")
    flags = _mapping(contract["feature_flags"], "feature flags")
    if not flags or any(
        not ENV_NAME_RE.fullmatch(name)
        or expectation not in {"absent_or_false", "absent_or_empty"}
        for name, expectation in flags.items()
    ):
        raise DeploymentSOTError("invalid feature flag contract")
    providers = _mapping(contract["providers"], "providers")
    if set(providers) != {"text", "vision"}:
        raise DeploymentSOTError("text and vision providers are required")
    for name, provider in providers.items():
        provider = _mapping(provider, f"{name} provider")
        _exact_keys(provider, {"provider_path", "provider", "model_path", "model"}, name)
        for field in provider.values():
            _string(field, f"{name} provider field")

    return DeploymentManifest(path=path, data=raw)


def parse_dotenv(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise DeploymentSOTError("symlink env file rejected")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise DeploymentSOTError("env file unavailable") from exc
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise DeploymentSOTError("malformed env line")
        name, value = line.split("=", 1)
        name = name.strip()
        if not ENV_NAME_RE.fullmatch(name):
            raise DeploymentSOTError("invalid env variable name")
        if name in values:
            raise DeploymentSOTError("duplicate env variable")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


def validate_override(path: Path, manifest: DeploymentManifest) -> dict[str, str]:
    if path.is_symlink():
        raise DeploymentSOTError("override symlink rejected")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise DeploymentSOTError("secrets override missing") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise DeploymentSOTError("secrets override must be a regular file")
    if stat.S_IMODE(metadata.st_mode) != int(manifest.secrets_override["required_mode"], 8):
        raise DeploymentSOTError("secrets override permissions mismatch")
    get_effective_uid = getattr(os, "geteuid", None)
    if not callable(get_effective_uid):
        raise DeploymentSOTError("secrets override owner validation unavailable")
    if metadata.st_uid != get_effective_uid():
        raise DeploymentSOTError("secrets override owner mismatch")
    parsed = _mapping(load_unique_yaml(path), "secrets override")
    if set(parsed) != {"services"}:
        raise DeploymentSOTError("unexpected secrets override root")
    services = _mapping(parsed["services"], "override services")
    service_name = manifest.secrets_override["service"]
    if set(services) != {service_name}:
        raise DeploymentSOTError("unexpected override service")
    service = _mapping(services[service_name], "override service")
    if set(service) != {"environment"}:
        raise DeploymentSOTError("override may only define environment")
    environment = _mapping(service["environment"], "override environment")
    expected = manifest.secrets_override["interpolation_template"]
    if environment != expected:
        raise DeploymentSOTError("override interpolation contract mismatch")
    return environment


def validate_required_env(
    interpolation_env: Mapping[str, str],
    manifest: DeploymentManifest,
) -> None:
    for name in manifest.secrets_override["required_variables"]:
        if not interpolation_env.get(name):
            raise DeploymentSOTError("required interpolation variable missing")


def validate_feature_flags(service_env: Mapping[str, str], manifest: DeploymentManifest) -> None:
    false_values = {"", "0", "false", "no", "off"}
    for name, expectation in manifest.contract["feature_flags"].items():
        value = service_env.get(name)
        if expectation == "absent_or_false":
            if value is not None and value.strip().lower() not in false_values:
                raise DeploymentSOTError("feature flag drift")
        elif value is not None and value.strip():
            raise DeploymentSOTError("feature allowlist drift")


def _dotted_value(data: Mapping[str, Any], dotted_path: str) -> Any:
    current: Any = data
    for part in dotted_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise DeploymentSOTError("runtime config path missing")
        current = current[part]
    return current


def validate_runtime_config(path: Path, manifest: DeploymentManifest) -> None:
    config = _mapping(load_unique_yaml(path), "runtime config")
    for provider in manifest.contract["providers"].values():
        if _dotted_value(config, provider["provider_path"]) != provider["provider"]:
            raise DeploymentSOTError("provider drift")
        if _dotted_value(config, provider["model_path"]) != provider["model"]:
            raise DeploymentSOTError("model drift")


def load_fingerprint_baseline(path: Path, manifest: DeploymentManifest) -> dict[str, str]:
    parsed = _mapping(load_unique_yaml(path), "credential baseline")
    _exact_keys(parsed, {"version", "algorithm", "fingerprints"}, "credential baseline")
    if parsed["version"] != 1 or parsed["algorithm"] != "sha256":
        raise DeploymentSOTError("credential baseline contract mismatch")
    fingerprints = _mapping(parsed["fingerprints"], "credential fingerprints")
    expected = set(manifest.secrets_override["credential_fingerprint_variables"])
    if set(fingerprints) != expected:
        raise DeploymentSOTError("credential fingerprint names mismatch")
    if not all(isinstance(value, str) and FINGERPRINT_RE.fullmatch(value) for value in fingerprints.values()):
        raise DeploymentSOTError("invalid credential fingerprint")
    return fingerprints


def validate_credential_fingerprints(
    interpolation_env: Mapping[str, str],
    service_env: Mapping[str, str],
    fingerprints: Mapping[str, str],
) -> None:
    combined = dict(service_env)
    combined.update(interpolation_env)
    for name, expected in fingerprints.items():
        value = combined.get(name)
        if not value:
            raise DeploymentSOTError("credential missing")
        actual = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if actual != expected:
            raise DeploymentSOTError("credential fingerprint mismatch")


class ReadOnlyRunner:
    """Runs only the exact read-only commands used by the validator."""

    @staticmethod
    def run(
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> str:
        command = tuple(args)
        allowed = (
            command[:2] in {("git", "rev-parse"), ("git", "status")}
            or command[:3] == ("docker", "compose", "--project-name")
            or command[:3] == ("docker", "image", "inspect")
            or command[:2] == ("docker", "inspect")
        )
        if not allowed or any(
            token in command
            for token in ("build", "up", "down", "restart", "create", "run", "exec", "rm")
        ):
            raise DeploymentSOTError("non-read-only command rejected")
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=dict(env) if env is not None else None,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DeploymentSOTError("read-only command failed") from exc
        return completed.stdout


def validate_git_state(source_root: Path, expected_sha: str, runner: ReadOnlyRunner) -> None:
    if not SHA_RE.fullmatch(expected_sha):
        raise DeploymentSOTError("invalid expected SHA")
    actual = runner.run(("git", "rev-parse", "HEAD"), cwd=source_root).strip()
    if actual != expected_sha:
        raise DeploymentSOTError("source SHA mismatch")
    status_output = runner.run(("git", "status", "--porcelain=v1"), cwd=source_root)
    if status_output.strip():
        raise DeploymentSOTError("source worktree is dirty")


def compose_file_paths(
    source_root: Path,
    override_path: Path,
    manifest: DeploymentManifest,
) -> list[Path]:
    repository_path = source_root / manifest.compose["files"][0]["path"]
    if not repository_path.is_file():
        raise DeploymentSOTError("repository compose file missing")
    if override_path == repository_path:
        raise DeploymentSOTError("duplicate compose file")
    return [repository_path, override_path]


def render_compose(
    source_root: Path,
    env_file: Path,
    compose_files: Sequence[Path],
    expected_sha: str,
    target_image: str,
    manifest: DeploymentManifest,
    runner: ReadOnlyRunner,
) -> dict[str, Any]:
    if not target_image or target_image.startswith("-"):
        raise DeploymentSOTError("invalid target image reference")
    args = [
        "docker",
        "compose",
        "--project-name",
        manifest.compose["project"],
        "--project-directory",
        str(source_root),
        "--env-file",
        str(env_file),
    ]
    for compose_file in compose_files:
        args.extend(("-f", str(compose_file)))
    args.extend(("config", "--format", "json"))
    command_env = os.environ.copy()
    command_env[manifest.deployment["revision_variable"]] = expected_sha
    command_env[manifest.deployment["image_variable"]] = target_image
    output = runner.run(tuple(args), cwd=source_root, env=command_env)
    try:
        parsed = json.loads(output)
    except (TypeError, json.JSONDecodeError) as exc:
        raise DeploymentSOTError("compose render is invalid") from exc
    return _mapping(parsed, "compose render")


def _mount_signature(mount: Mapping[str, Any], project: str) -> tuple[str, str, str]:
    mount_type = str(mount.get("type", ""))
    source = str(mount.get("source", ""))
    if mount_type == "volume" and source == f"{project}_qdrant_data":
        source = "qdrant_data"
    return mount_type, source, str(mount.get("target", ""))


def validate_compose_render(
    rendered: Mapping[str, Any],
    target_image: str,
    manifest: DeploymentManifest,
) -> None:
    if rendered.get("name") != manifest.compose["project"]:
        raise DeploymentSOTError("compose project name drift")
    services = _mapping(rendered.get("services"), "rendered services")
    if list(services) != manifest.compose["expected_services"] and set(services) != set(
        manifest.compose["expected_services"]
    ):
        raise DeploymentSOTError("unexpected rendered services")
    application_name = manifest.compose["application_service"]
    application = _mapping(services.get(application_name), "rendered application")
    if application.get("image") != target_image:
        raise DeploymentSOTError("target image mismatch")
    if application.get("restart") != manifest.contract["restart_policy"]:
        raise DeploymentSOTError("restart policy drift")
    if application.get("command") != manifest.contract["command"]:
        raise DeploymentSOTError("command drift")
    rendered_networks = set(_mapping(application.get("networks"), "application networks"))
    if rendered_networks != set(manifest.contract["networks"]):
        raise DeploymentSOTError("network drift")
    for service_name in manifest.compose["expected_services"]:
        service = _mapping(services.get(service_name), "rendered service")
        if service.get("profiles"):
            raise DeploymentSOTError("unexpected compose profile")
        if service.get("restart") != manifest.contract["restart_policy"]:
            raise DeploymentSOTError("restart policy drift")
        expected_mounts = {
            (mount["type"], mount["source"], mount["target"])
            for mount in manifest.contract["mounts"]
            if mount["service"] == service_name
        }
        actual_mounts = {
            _mount_signature(_mapping(mount, "rendered mount"), manifest.compose["project"])
            for mount in service.get("volumes", [])
        }
        if actual_mounts != expected_mounts:
            raise DeploymentSOTError("mount or database path drift")
    application_mount_targets = {
        str(mount.get("target", "")) for mount in application.get("volumes", [])
    }
    if manifest.contract["database_mount_target"] not in application_mount_targets:
        raise DeploymentSOTError("database mount path drift")
    environment = _mapping(application.get("environment"), "rendered application environment")
    for name in manifest.secrets_override["required_variables"]:
        if not environment.get(name):
            raise DeploymentSOTError("rendered credential missing")


def inspect_image(
    reference: str,
    expected_sha: str | None,
    manifest: DeploymentManifest,
    runner: ReadOnlyRunner,
) -> dict[str, Any]:
    if not reference or reference.startswith("-"):
        raise DeploymentSOTError("invalid image reference")
    output = runner.run(("docker", "image", "inspect", reference))
    try:
        images = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentSOTError("image inspect response invalid") from exc
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
        raise DeploymentSOTError("image reference missing or ambiguous")
    image = images[0]
    if expected_sha is not None:
        config = _mapping(image.get("Config"), "image config")
        labels = _mapping(config.get("Labels") or {}, "image labels")
        if labels.get(manifest.deployment["revision_label"]) != expected_sha:
            raise DeploymentSOTError("target image revision mismatch")
        if config.get("Entrypoint") != manifest.contract["image_entrypoint"]:
            raise DeploymentSOTError("image entrypoint drift")
    return image


def validate_qdrant_identity(
    expected_qdrant_id: str,
    manifest: DeploymentManifest,
    runner: ReadOnlyRunner,
) -> None:
    if not expected_qdrant_id or expected_qdrant_id.startswith("-"):
        raise DeploymentSOTError("invalid expected qdrant identity")
    output = runner.run(("docker", "inspect", manifest.compose["qdrant_service"]))
    try:
        containers = json.loads(output)
    except json.JSONDecodeError as exc:
        raise DeploymentSOTError("qdrant inspect response invalid") from exc
    if not isinstance(containers, list) or len(containers) != 1:
        raise DeploymentSOTError("qdrant identity unavailable")
    if containers[0].get("Id") != expected_qdrant_id:
        raise DeploymentSOTError("qdrant identity drift")


@dataclass(frozen=True)
class ValidationInputs:
    source_root: Path
    manifest_path: Path
    expected_sha: str
    interpolation_env_file: Path
    service_env_file: Path
    runtime_config_file: Path
    secrets_override_path: Path
    credential_baseline_path: Path
    target_image: str
    rollback_image: str
    expected_qdrant_id: str


def run_validation(inputs: ValidationInputs, runner: ReadOnlyRunner | None = None) -> None:
    runner = runner or ReadOnlyRunner()
    manifest = load_manifest(inputs.manifest_path)
    validate_git_state(inputs.source_root, inputs.expected_sha, runner)
    if manifest.secrets_override["producer_status"] != "proven":
        raise DeploymentSOTError("secrets override producer is inconclusive")
    compose_files = compose_file_paths(inputs.source_root, inputs.secrets_override_path, manifest)
    interpolation_env = parse_dotenv(inputs.interpolation_env_file)
    service_env = parse_dotenv(inputs.service_env_file)
    validate_required_env(interpolation_env, manifest)
    validate_feature_flags(service_env, manifest)
    validate_override(inputs.secrets_override_path, manifest)
    validate_runtime_config(inputs.runtime_config_file, manifest)
    fingerprints = load_fingerprint_baseline(inputs.credential_baseline_path, manifest)
    validate_credential_fingerprints(interpolation_env, service_env, fingerprints)
    rendered = render_compose(
        inputs.source_root,
        inputs.interpolation_env_file,
        compose_files,
        inputs.expected_sha,
        inputs.target_image,
        manifest,
        runner,
    )
    validate_compose_render(rendered, inputs.target_image, manifest)
    inspect_image(inputs.target_image, inputs.expected_sha, manifest, runner)
    inspect_image(inputs.rollback_image, None, manifest, runner)
    validate_qdrant_identity(inputs.expected_qdrant_id, manifest, runner)


def safe_failure_detail(exc: BaseException) -> str:
    if isinstance(exc, DeploymentSOTError):
        return str(exc)
    return "unexpected validation failure"
