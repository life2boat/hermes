#!/usr/bin/env python3
"""Canonical, fail-closed Hermes production deployment entrypoint."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence


try:
    from scripts import hermes_deploy_preflight as preflight
except ModuleNotFoundError:  # Direct execution adds scripts/ rather than its parent.
    import hermes_deploy_preflight as preflight

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPOSITORY_ROOT / "deploy" / "hermes-production.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_DIGEST_RE = re.compile(r"^[a-zA-Z0-9._/-]+@sha256:[0-9a-f]{64}$")
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
MAX_SECRET_SOURCE_BYTES = 1024 * 1024
DEPLOY_CONFIRMATION = "DEPLOY_HERMES_BOT"
ROLLBACK_CONFIRMATION = "ROLLBACK_HERMES_BOT"
LEGACY_REFERENCES = (
    "/tmp/hermes-" "secrets-override.yml",
    "healbite-s71v2-" "r6-deploy",
)


class DeploymentContractError(RuntimeError):
    """A fail-closed deployment contract check failed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProtectedSecretSpec:
    name: str
    required: bool
    source_class: str
    destination_name: str
    allow_empty: bool
    removal_requires_authorization: bool


@dataclass(frozen=True)
class DeploymentContract:
    version: int
    root: Path
    manifest_path: Path
    base_compose: Path
    canonical_repository: str
    canonical_repository_slug: str
    canonical_remote: str
    canonical_remote_urls: tuple[str, ...]
    canonical_main_branch: str
    required_ci_workflows: tuple[str, ...]
    database_source: Path
    database_target: Path
    production_override: Path
    runtime_directory: Path
    secret_override: Path
    approved_secret_source: Path
    approved_source_owner_uids: frozenset[int]
    protected_secrets: tuple[ProtectedSecretSpec, ...]
    project_name: str
    database_mount_type: str
    database_read_only: bool
    legacy_database_sources: tuple[Path, ...]
    lease_path: Path
    lease_owner_uids: frozenset[int]
    lease_timeout_seconds: int
    capacity_filesystem: Path
    minimum_free_basis_points: int
    estimated_peak_incremental_build_bytes: int
    build_peak_multiplier: int
    staging_safety_margin_bytes: int
    capacity_formula_source: str
    target_service: str
    image_revision_label: str
    allowed_revision_ref: str
    feature_gates: dict[str, str]

    @property
    def protected_secret_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.protected_secrets)

    @property
    def required_secret_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.protected_secrets if spec.required)


@dataclass(frozen=True)
class ProtectedSecretRemovalAuthorization:
    exact_names: frozenset[str]
    rollback_ready: bool


@dataclass(frozen=True)
class SecretOverrideTransaction:
    staged_path: Path
    rollback_path: Path | None
    live_was_present: bool
    previous_fingerprints: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class InspectedImage:
    image_id: str
    revision: str


def _preflight(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except preflight.DeployPreflightError as exc:
        _fail(exc.code)


def _fail(code: str) -> None:
    raise DeploymentContractError(code)


def _effective_uid() -> int:
    get_euid = getattr(os, "geteuid", None)
    if not callable(get_euid):
        _fail("posix-runtime-required")
    return get_euid()


def _decode_json_document(data: bytes, *, code: str) -> dict[str, object]:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _fail(f"{code}-invalid")
    if not isinstance(raw, dict):
        _fail(f"{code}-invalid")
    return raw


def _read_json_file(path: Path, *, code: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            _fail(f"{code}-not-regular")
        data = path.read_bytes()
    except DeploymentContractError:
        raise
    except OSError:
        _fail(f"{code}-invalid")
    return _decode_json_document(data, code=code)


def _mapping(value: object, *, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(code)
    return value


def _string(value: object, *, code: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(code)
    return value


def _mode(value: object, *, expected: str, code: str) -> None:
    if value != expected:
        _fail(code)


def load_contract(
    root: Path = REPOSITORY_ROOT,
    *,
    manifest_bytes: bytes | None = None,
) -> DeploymentContract:
    root = root.absolute()
    manifest_path = root / "deploy" / "hermes-production.json"
    raw = (
        _read_json_file(manifest_path, code="manifest")
        if manifest_bytes is None
        else _decode_json_document(manifest_bytes, code="manifest")
    )
    if set(raw) != {
        "version", "provenance", "compose", "runtime", "database_mount",
        "capacity", "secrets", "deployment", "rollback", "feature_gates",
    }:
        _fail("manifest-fields")
    if raw["version"] != 2:
        _fail("manifest-version")

    provenance = _mapping(raw["provenance"], code="manifest-provenance")
    canonical_repository = "https://github.com/life2boat/hermes.git"
    canonical_remote = "github"
    canonical_main_ref = "refs/remotes/github/main"
    canonical_main_branch = "refs/heads/main"
    expected_remote_urls = (
        "git@github-healbite:life2boat/hermes.git",
        "git@github.com:life2boat/hermes.git",
        canonical_repository,
    )
    expected_ci = ("Tests", "Lint (ruff + ty)", "Typecheck", "Nix")
    if (
        provenance.get("canonical_repository") != canonical_repository
        or provenance.get("canonical_remote") != canonical_remote
        or tuple(provenance.get("canonical_remote_urls", ())) != expected_remote_urls
        or provenance.get("canonical_main_ref") != canonical_main_ref
        or provenance.get("canonical_main_branch") != canonical_main_branch
        or tuple(provenance.get("required_ci_workflows", ())) != expected_ci
    ):
        _fail("provenance-policy")

    compose = _mapping(raw["compose"], code="manifest-compose")
    if compose.get("project_name") != "hermes-agent" or compose.get("target_service") != "hermes-bot":
        _fail("compose-identity")
    if compose.get("project_directory") != "repository-root":
        _fail("compose-project-directory")
    base_relative = Path(_string(compose.get("base_file"), code="compose-base"))
    production_relative = Path(_string(compose.get("production_override"), code="compose-production-override"))
    for relative in (base_relative, production_relative):
        if relative.is_absolute() or ".." in relative.parts:
            _fail("compose-path")

    runtime = _mapping(raw["runtime"], code="manifest-runtime")
    runtime_directory = Path(_string(runtime.get("directory"), code="runtime-directory"))
    secret_override = Path(_string(runtime.get("secret_override"), code="secret-override"))
    lease_path = Path(_string(runtime.get("deployment_lease"), code="deployment-lease"))
    if (
        runtime_directory != Path("/run/hermes")
        or secret_override != runtime_directory / "hermes-secrets-override.yml"
        or lease_path != runtime_directory / "hermes-deployment-operation.json"
    ):
        _fail("runtime-path")
    _mode(runtime.get("directory_mode"), expected="0700", code="runtime-directory-mode")
    _mode(runtime.get("secret_override_mode"), expected="0600", code="secret-override-mode")
    _mode(runtime.get("deployment_lease_mode"), expected="0600", code="deployment-lease-mode")
    lease_timeout_seconds = runtime.get("deployment_lease_timeout_seconds")
    if lease_timeout_seconds != 900:
        _fail("deployment-lease-timeout")
    if runtime.get("owner") != "deployment-operator":
        _fail("runtime-owner")

    database = _mapping(raw["database_mount"], code="manifest-database-mount")
    database_source = Path(_string(database.get("source"), code="database-source"))
    database_target = Path(_string(database.get("target"), code="database-target"))
    legacy_database_sources = tuple(
        Path(item)
        for item in database.get("legacy_sources", ())
        if isinstance(item, str)
    )
    if (
        database_source != Path("/var/lib/hermes/production-db/healbite.db")
        or database_target != Path("/home/hermes/healbite.db")
        or database.get("type") != "bind"
        or database.get("read_write") is not True
        or legacy_database_sources != (Path("/home/hermes/healbite.db"),)
    ):
        _fail("database-mount-policy")

    capacity = _mapping(raw["capacity"], code="manifest-capacity")
    capacity_filesystem = Path(_string(capacity.get("filesystem"), code="capacity-filesystem"))
    minimum_free_basis_points = capacity.get("minimum_free_basis_points")
    estimated_peak_incremental_build_bytes = capacity.get(
        "estimated_peak_incremental_build_bytes"
    )
    build_peak_multiplier = capacity.get("build_peak_multiplier")
    staging_safety_margin_bytes = capacity.get("staging_safety_margin_bytes")
    capacity_source = capacity.get("formula_source")
    capacity_policy_class = capacity.get("policy_class")
    capacity_policy_reference = capacity.get("policy_reference")
    if (
        capacity_filesystem != Path("/")
        or minimum_free_basis_points != 1000
        or estimated_peak_incremental_build_bytes != 2069000000
        or build_peak_multiplier != 2
        or staging_safety_margin_bytes != 5368709120
        or capacity_source
        != "repository-manifest:max(filesystem-percentage,operation-peak-plus-margin)"
        or capacity_policy_class != "new-explicit-p0"
        or capacity_policy_reference
        != "docs/runbooks/hermes-production-deployment.md#capacity-policy"
    ):
        _fail("capacity-policy")

    secrets = _mapping(raw["secrets"], code="manifest-secrets")
    if secrets.get("source_type") != "explicit-protected-dotenv":
        _fail("secret-source-type")
    approved_source = Path(_string(secrets.get("approved_source_path"), code="secret-source-path"))
    if not approved_source.is_absolute() or approved_source.is_relative_to(root):
        _fail("secret-source-path")
    if approved_source != Path("/etc/hermes/hermes-production.env"):
        _fail("secret-source-path")
    _mode(secrets.get("source_mode"), expected="0600", code="secret-source-mode")
    if secrets.get("ambient_environment_allowed") is not False:
        _fail("ambient-secret-environment")
    owner_uids = secrets.get("approved_owner_uids")
    if not isinstance(owner_uids, list) or not owner_uids or not all(isinstance(uid, int) and uid >= 0 for uid in owner_uids):
        _fail("secret-source-owners")
    if owner_uids != [0]:
        _fail("secret-source-owners")
    protected_raw = secrets.get("protected_variables")
    if not isinstance(protected_raw, list) or len(protected_raw) != 6:
        _fail("protected-secret-manifest")
    protected: list[ProtectedSecretSpec] = []
    expected_secret_names = {
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "NOUS_API_KEY",
        "OPENAI_API_KEY",
        "QWEN_API_KEY",
        "TELEGRAM_BOT_TOKEN",
    }
    for item in protected_raw:
        entry = _mapping(item, code="protected-secret-manifest")
        if set(entry) != {
            "name",
            "required",
            "source_class",
            "destination_variable",
            "allow_empty",
            "removal_requires_authorization",
        }:
            _fail("protected-secret-manifest")
        name = _string(entry.get("name"), code="protected-secret-name")
        destination = _string(
            entry.get("destination_variable"),
            code="protected-secret-destination",
        )
        if (
            not ENV_NAME_RE.fullmatch(name)
            or destination != name
            or entry.get("source_class")
            != "approved-production-secret-source"
            or not isinstance(entry.get("required"), bool)
            or entry.get("allow_empty") is not False
            or entry.get("removal_requires_authorization") is not True
        ):
            _fail("protected-secret-policy")
        protected.append(
            ProtectedSecretSpec(
                name=name,
                required=entry["required"],
                source_class=entry["source_class"],
                destination_name=destination,
                allow_empty=entry["allow_empty"],
                removal_requires_authorization=entry[
                    "removal_requires_authorization"
                ],
            )
        )
    protected_names = [spec.name for spec in protected]
    if (
        set(protected_names) != expected_secret_names
        or len(set(protected_names)) != len(protected_names)
        or [spec.name for spec in protected if spec.required]
        != ["TELEGRAM_BOT_TOKEN"]
    ):
        _fail("protected-secret-policy")

    deployment = _mapping(raw["deployment"], code="manifest-deployment")
    if (
        deployment.get("image_reference_policy") != "digest-only"
        or deployment.get("revision_required") is not True
        or deployment.get("revision_label") != "org.opencontainers.image.revision"
        or deployment.get("allowed_revision_ref") != canonical_main_ref
        or deployment.get("recreate_services") != ["hermes-bot"]
        or deployment.get("cleanup_after_operation") is not True
    ):
        _fail("deployment-policy")
    rollback = _mapping(raw["rollback"], code="manifest-rollback")
    if (
        rollback.get("image_reference_policy") != "digest-only"
        or rollback.get("same_compose_chain") is not True
        or rollback.get("schema_downgrade") is not False
        or rollback.get("database_restore") is not False
    ):
        _fail("rollback-policy")
    feature_gates = _mapping(raw["feature_gates"], code="manifest-feature-gates")
    expected_manifest_gates: dict[str, object] = {
        "HEALBITE_HOUSEHOLDS_ENABLED": False,
        "HEALBITE_HOUSEHOLDS_ALLOWLIST": "",
        "HEALBITE_SHOPPING_LIST_ENABLED": False,
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
    }
    if feature_gates != expected_manifest_gates:
        _fail("feature-gate-policy")
    normalized_feature_gates = {
        "HEALBITE_HOUSEHOLDS_ENABLED": "false",
        "HEALBITE_HOUSEHOLDS_ALLOWLIST": "",
        "HEALBITE_SHOPPING_LIST_ENABLED": "false",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
    }

    return DeploymentContract(
        version=2,
        root=root,
        manifest_path=manifest_path,
        base_compose=root / base_relative,
        canonical_repository=canonical_repository,
        canonical_repository_slug="life2boat/hermes",
        canonical_remote=canonical_remote,
        canonical_remote_urls=expected_remote_urls,
        canonical_main_branch=canonical_main_branch,
        required_ci_workflows=expected_ci,
        database_source=database_source,
        database_target=database_target,
        production_override=root / production_relative,
        runtime_directory=runtime_directory,
        secret_override=secret_override,
        approved_secret_source=approved_source,
        approved_source_owner_uids=frozenset(owner_uids),
        protected_secrets=tuple(protected),
        project_name="hermes-agent",
        database_mount_type="bind",
        database_read_only=False,
        legacy_database_sources=legacy_database_sources,
        lease_path=lease_path,
        lease_owner_uids=frozenset({0}),
        lease_timeout_seconds=lease_timeout_seconds,
        capacity_filesystem=capacity_filesystem,
        minimum_free_basis_points=minimum_free_basis_points,
        estimated_peak_incremental_build_bytes=estimated_peak_incremental_build_bytes,
        build_peak_multiplier=build_peak_multiplier,
        staging_safety_margin_bytes=staging_safety_margin_bytes,
        capacity_formula_source=capacity_source,
        target_service="hermes-bot",
        image_revision_label="org.opencontainers.image.revision",
        allowed_revision_ref=canonical_main_ref,
        feature_gates=normalized_feature_gates,
    )


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _fail("command-unavailable")


def _git_output(contract: DeploymentContract, *args: str) -> str:
    result = _run(("git", "-C", str(contract.root), *args), timeout=20)
    if result.returncode != 0:
        _fail("git-check")
    return result.stdout.strip()


def validate_repository(contract: DeploymentContract, expected_sha: str) -> None:
    if not SHA_RE.fullmatch(expected_sha):
        _fail("expected-sha")
    if Path(_git_output(contract, "rev-parse", "--show-toplevel")).resolve() != contract.root.resolve():
        _fail("repository-root-mismatch")
    if contract.allowed_revision_ref != "refs/remotes/github/main":
        _fail("canonical-main-ref-mismatch")
    if _git_output(contract, "rev-parse", "HEAD") != expected_sha:
        _fail("head-mismatch")
    if _git_output(contract, "status", "--porcelain=v1"):
        _fail("dirty-worktree")
    canonical_main = _git_output(contract, "rev-parse", "--verify", f"{contract.allowed_revision_ref}^{{commit}}")
    if canonical_main != expected_sha:
        _fail("canonical-main-sha-mismatch")
    _preflight(
        preflight.validate_canonical_provenance,
        root=contract.root,
        expected_sha=expected_sha,
        canonical_repository_slug=contract.canonical_repository_slug,
        canonical_remote=contract.canonical_remote,
        allowed_remote_urls=contract.canonical_remote_urls,
        canonical_main_ref=contract.allowed_revision_ref,
        canonical_main_branch=contract.canonical_main_branch,
        required_ci_workflows=contract.required_ci_workflows,
        git_output=lambda *args: _git_output(contract, *args),
        run=_run,
    )
    for path in (contract.manifest_path, contract.base_compose, contract.production_override):
        try:
            metadata = path.lstat()
        except OSError:
            _fail("canonical-file-missing")
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            _fail("canonical-file-not-regular")

    for path in (contract.manifest_path, contract.production_override, Path(__file__).resolve()):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            _fail("canonical-file-read")
        if any(reference in text for reference in LEGACY_REFERENCES):
            _fail("legacy-reference")

    production = _read_json_file(contract.production_override, code="production-override")
    try:
        service = production["services"][contract.target_service]
        environment = service["environment"]
    except (KeyError, TypeError):
        _fail("production-override-service")
    if environment != contract.feature_gates:
        _fail("production-feature-gates")


def current_source_head_revision(contract: DeploymentContract) -> str:
    head = _git_output(contract, "rev-parse", "HEAD")
    if not SHA_RE.fullmatch(head):
        _fail("source-head")
    return head


def validate_rollback_revision(
    contract: DeploymentContract,
    *,
    source_head_revision: str,
    rollback_revision: str,
) -> None:
    validate_revision(rollback_revision)
    validate_revision(source_head_revision)
    commit = _run(
        ("git", "-C", str(contract.root), "cat-file", "-e", f"{rollback_revision}^{{commit}}"),
        timeout=20,
    )
    if commit.returncode != 0:
        _fail("rollback-revision-not-commit")
    ancestry = _run(
        (
            "git",
            "-C",
            str(contract.root),
            "merge-base",
            "--is-ancestor",
            rollback_revision,
            source_head_revision,
        ),
        timeout=20,
    )
    if ancestry.returncode != 0:
        _fail("rollback-revision-not-ancestor")


def _assert_no_symlink_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("path-metadata")
        if stat.S_ISLNK(metadata.st_mode):
            _fail("symlink-path")


def _validate_regular_file(path: Path, *, mode: int, allowed_uids: frozenset[int], code: str) -> os.stat_result:
    _assert_no_symlink_components(path)
    try:
        metadata = path.lstat()
    except OSError:
        _fail(f"{code}-missing")
    if not stat.S_ISREG(metadata.st_mode):
        _fail(f"{code}-not-regular")
    if stat.S_IMODE(metadata.st_mode) != mode:
        _fail(f"{code}-mode")
    if metadata.st_uid not in allowed_uids:
        _fail(f"{code}-owner")
    return metadata


def _read_protected_bytes(
    path: Path,
    *,
    expected: os.stat_result,
    code: str,
) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        _fail(f"{code}-open")
    try:
        opened = os.fstat(fd)
        if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino or not stat.S_ISREG(opened.st_mode):
            _fail(f"{code}-race")
        data = bytearray()
        while len(data) <= MAX_SECRET_SOURCE_BYTES:
            chunk = os.read(fd, min(65536, MAX_SECRET_SOURCE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_SECRET_SOURCE_BYTES:
            _fail(f"{code}-too-large")
        return bytes(data)
    finally:
        os.close(fd)


def _read_protected_file(path: Path, *, expected: os.stat_result) -> str:
    data = _read_protected_bytes(path, expected=expected, code="secret-source")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("secret-source-encoding")


def _parse_dotenv(text: str) -> dict[str, str]:
    if "\x00" in text or "\r" in text:
        _fail("secret-source-control-character")
    values: dict[str, str] = {}
    for line in text.split("\n"):
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            _fail("secret-source-syntax")
        name, value = line.split("=", 1)
        if not ENV_NAME_RE.fullmatch(name) or name in values:
            _fail("secret-source-variable")
        values[name] = value
    return values


def _secret_specs_by_name(
    contract: DeploymentContract,
) -> dict[str, ProtectedSecretSpec]:
    return {spec.name: spec for spec in contract.protected_secrets}


def read_required_secrets(contract: DeploymentContract, source: Path) -> dict[str, str]:
    source = source.absolute()
    if source != contract.approved_secret_source:
        _fail("unapproved-secret-source")
    metadata = _validate_regular_file(
        source,
        mode=0o600,
        allowed_uids=contract.approved_source_owner_uids,
        code="secret-source",
    )
    values = _parse_dotenv(_read_protected_file(source, expected=metadata))
    specs = _secret_specs_by_name(contract)
    selected: dict[str, str] = {}
    for spec in contract.protected_secrets:
        if spec.name not in values:
            if spec.required:
                _fail("required-secret-missing")
            continue
        value = values[spec.name]
        if not value and spec.required:
            _fail("required-secret-missing")
        if not value and not spec.allow_empty:
            _fail("secret-empty-value")
        selected[spec.name] = value
    if not set(values).issubset(specs):
        _fail("secret-source-variable-set")
    return selected


def _validate_runtime_directory(contract: DeploymentContract, *, create: bool) -> None:
    path = contract.runtime_directory
    _assert_no_symlink_components(path.parent)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if not create:
            _fail("runtime-directory-missing")
        old_umask = os.umask(0o077)
        try:
            os.mkdir(path, 0o700)
        except OSError:
            _fail("runtime-directory-create")
        finally:
            os.umask(old_umask)
        metadata = path.lstat()
    except OSError:
        _fail("runtime-directory-metadata")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        _fail("runtime-directory-type")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        _fail("runtime-directory-mode")
    if metadata.st_uid != _effective_uid():
        _fail("runtime-directory-owner")


def _validate_secret_mapping(
    contract: DeploymentContract,
    secrets: dict[str, str],
    *,
    code: str,
    require_required: bool = True,
) -> dict[str, str]:
    specs = _secret_specs_by_name(contract)
    if not set(secrets).issubset(specs):
        _fail(f"{code}-variable-set")
    selected: dict[str, str] = {}
    for spec in contract.protected_secrets:
        if spec.name not in secrets:
            if spec.required and require_required:
                _fail(f"{code}-required-secret-missing")
            continue
        value = secrets[spec.name]
        if not isinstance(value, str):
            _fail(f"{code}-value-type")
        if not value and not spec.allow_empty:
            _fail(f"{code}-empty-secret")
        selected[spec.name] = value
    return selected


def _read_secret_override(
    contract: DeploymentContract,
    path: Path,
    *,
    code: str,
) -> dict[str, str]:
    metadata = _validate_regular_file(
        path,
        mode=0o600,
        allowed_uids=frozenset({_effective_uid()}),
        code=code,
    )
    document = _decode_json_document(
        _read_protected_bytes(path, expected=metadata, code=code),
        code=code,
    )
    if set(document) != {"services"}:
        _fail(f"{code}-document")
    services = _mapping(document["services"], code=f"{code}-document")
    if set(services) != {contract.target_service}:
        _fail(f"{code}-document")
    service = _mapping(
        services[contract.target_service],
        code=f"{code}-document",
    )
    if set(service) != {"environment"}:
        _fail(f"{code}-document")
    environment = _mapping(
        service["environment"],
        code=f"{code}-document",
    )
    if not all(isinstance(value, str) for value in environment.values()):
        _fail(f"{code}-value-type")
    return _validate_secret_mapping(contract, environment, code=code)


def validate_secret_override(contract: DeploymentContract) -> None:
    if contract.secret_override.parent != contract.runtime_directory:
        _fail("override-path")
    _validate_runtime_directory(contract, create=False)
    _read_secret_override(
        contract,
        contract.secret_override,
        code="secret-override",
    )


def _override_document(
    contract: DeploymentContract,
    secrets: dict[str, str],
) -> bytes:
    selected = _validate_secret_mapping(
        contract,
        secrets,
        code="override-input",
    )
    environment = {
        spec.destination_name: selected[spec.name]
        for spec in contract.protected_secrets
        if spec.name in selected
    }
    document = {
        "services": {
            contract.target_service: {
                "environment": environment,
            }
        }
    }
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _secret_fingerprints(
    secrets: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, hashlib.sha256(value.encode("utf-8")).hexdigest())
        for name, value in sorted(secrets.items())
    )


def _validate_secret_transition(
    contract: DeploymentContract,
    *,
    live: dict[str, str],
    staged: dict[str, str],
    removal_authorization: ProtectedSecretRemovalAuthorization | None = None,
) -> None:
    live = _validate_secret_mapping(
        contract,
        live,
        code="live-override",
        require_required=False,
    )
    staged = _validate_secret_mapping(
        contract,
        staged,
        code="staged-override",
    )
    removed = frozenset(set(live) - set(staged))
    if removed:
        specs = _secret_specs_by_name(contract)
        if (
            removal_authorization is None
            or removal_authorization.exact_names != removed
            or not removal_authorization.rollback_ready
            or any(specs[name].required for name in removed)
            or any(
                not specs[name].removal_requires_authorization
                for name in removed
            )
        ):
            _fail("protected-credential-removal")
    live_fingerprints = dict(_secret_fingerprints(live))
    staged_fingerprints = dict(_secret_fingerprints(staged))
    if any(
        live_fingerprints[name] != staged_fingerprints[name]
        for name in set(live) & set(staged)
    ):
        _fail("credential-fingerprint-drift")


def _fsync_runtime_directory(contract: DeploymentContract) -> None:
    directory_fd = os.open(contract.runtime_directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _unlink_private_path(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        _fail("override-partial-cleanup")


def _write_private_payload(
    contract: DeploymentContract,
    *,
    prefix: str,
    payload: bytes,
) -> Path:
    temp_path: Path | None = None
    fd: int | None = None
    completed = False
    old_umask = os.umask(0o077)
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix=prefix,
            dir=contract.runtime_directory,
        )
        temp_path = Path(raw_path)
        os.fchmod(fd, 0o600)
        written = 0
        while written < len(payload):
            count = os.write(fd, payload[written:])
            if count <= 0:
                _fail("override-write")
            written += count
        os.fsync(fd)
        os.close(fd)
        fd = None
        _validate_regular_file(
            temp_path,
            mode=0o600,
            allowed_uids=frozenset({_effective_uid()}),
            code="temporary-override",
        )
        completed = True
        return temp_path
    except DeploymentContractError:
        raise
    except OSError:
        _fail("override-atomic-write")
    finally:
        os.umask(old_umask)
        if fd is not None:
            os.close(fd)
        if temp_path is not None and not completed:
            _unlink_private_path(temp_path)


def _stage_secret_override(
    contract: DeploymentContract,
    secrets: dict[str, str],
) -> Path:
    _validate_runtime_directory(contract, create=True)
    staged_path = _write_private_payload(
        contract,
        prefix=".hermes-secrets-staged.",
        payload=_override_document(contract, secrets),
    )
    try:
        _read_secret_override(
            contract,
            staged_path,
            code="staged-override",
        )
    except BaseException:
        _unlink_private_path(staged_path)
        raise
    return staged_path


def _live_override_secrets(
    contract: DeploymentContract,
) -> dict[str, str] | None:
    try:
        contract.secret_override.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        _fail("secret-override-metadata")
    validate_secret_override(contract)
    return _read_secret_override(
        contract,
        contract.secret_override,
        code="secret-override",
    )


def _stage_live_override_rollback(
    contract: DeploymentContract,
) -> Path:
    metadata = _validate_regular_file(
        contract.secret_override,
        mode=0o600,
        allowed_uids=frozenset({_effective_uid()}),
        code="secret-override",
    )
    payload = _read_protected_bytes(
        contract.secret_override,
        expected=metadata,
        code="secret-override",
    )
    rollback_path = _write_private_payload(
        contract,
        prefix=".hermes-secrets-rollback.",
        payload=payload,
    )
    _read_secret_override(
        contract,
        rollback_path,
        code="rollback-override",
    )
    return rollback_path


def _restore_secret_override_transaction(
    contract: DeploymentContract,
    transaction: SecretOverrideTransaction,
) -> None:
    try:
        if transaction.live_was_present:
            if transaction.rollback_path is None:
                _fail("secret-rollback-artifact-missing")
            _read_secret_override(
                contract,
                transaction.rollback_path,
                code="rollback-override",
            )
            os.replace(
                transaction.rollback_path,
                contract.secret_override,
            )
            _fsync_runtime_directory(contract)
            restored = _read_secret_override(
                contract,
                contract.secret_override,
                code="secret-override",
            )
            if _secret_fingerprints(restored) != transaction.previous_fingerprints:
                _fail("secret-rollback-fingerprint-mismatch")
        else:
            try:
                contract.secret_override.lstat()
            except FileNotFoundError:
                pass
            else:
                _validate_regular_file(
                    contract.secret_override,
                    mode=0o600,
                    allowed_uids=frozenset({_effective_uid()}),
                    code="secret-override",
                )
                contract.secret_override.unlink()
                _fsync_runtime_directory(contract)
        _unlink_private_path(transaction.staged_path)
        _unlink_private_path(transaction.rollback_path)
    except DeploymentContractError:
        raise
    except OSError:
        _fail("secret-rollback-restore")


def _begin_secret_override_transaction(
    contract: DeploymentContract,
    secrets: dict[str, str],
) -> SecretOverrideTransaction:
    staged_path = _stage_secret_override(contract, secrets)
    rollback_path: Path | None = None
    live = _live_override_secrets(contract)
    staged = _read_secret_override(
        contract,
        staged_path,
        code="staged-override",
    )
    _validate_secret_transition(
        contract,
        live={} if live is None else live,
        staged=staged,
    )
    previous_fingerprints: tuple[tuple[str, str], ...] = ()
    if live is not None:
        previous_fingerprints = _secret_fingerprints(live)
        rollback_path = _stage_live_override_rollback(contract)
    transaction = SecretOverrideTransaction(
        staged_path=staged_path,
        rollback_path=rollback_path,
        live_was_present=live is not None,
        previous_fingerprints=previous_fingerprints,
    )
    replaced = False
    try:
        os.replace(staged_path, contract.secret_override)
        replaced = True
        _fsync_runtime_directory(contract)
        validate_secret_override(contract)
        return transaction
    except BaseException as exc:
        if replaced:
            try:
                _restore_secret_override_transaction(contract, transaction)
            except DeploymentContractError:
                if isinstance(exc, DeploymentContractError):
                    raise
                _fail("secret-rollback-restore")
        else:
            _unlink_private_path(staged_path)
            _unlink_private_path(rollback_path)
        if isinstance(exc, DeploymentContractError):
            raise
        _fail("override-atomic-write")


def _finish_secret_override_transaction(
    contract: DeploymentContract,
    transaction: SecretOverrideTransaction,
    *,
    preserve_published: bool,
) -> None:
    if preserve_published:
        _unlink_private_path(transaction.staged_path)
        _unlink_private_path(transaction.rollback_path)
        return
    _restore_secret_override_transaction(contract, transaction)


def _write_secret_override(
    contract: DeploymentContract,
    secrets: dict[str, str],
) -> None:
    transaction = _begin_secret_override_transaction(contract, secrets)
    _finish_secret_override_transaction(
        contract,
        transaction,
        preserve_published=True,
    )


def prepare_secret_override(contract: DeploymentContract, source: Path) -> None:
    _write_secret_override(contract, read_required_secrets(contract, source))


def cleanup_secret_override(contract: DeploymentContract, requested_path: Path | None = None) -> None:
    target = contract.secret_override if requested_path is None else requested_path.absolute()
    if target != contract.secret_override:
        _fail("cleanup-scope")
    try:
        target.lstat()
    except FileNotFoundError:
        return
    except OSError:
        _fail("cleanup-metadata")
    validate_secret_override(contract)
    try:
        target.unlink()
        directory_fd = os.open(contract.runtime_directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        _fail("cleanup-failed")


def validate_immutable_image(image: str) -> None:
    if not (IMAGE_ID_RE.fullmatch(image) or IMAGE_DIGEST_RE.fullmatch(image)):
        _fail("mutable-image-reference")


def validate_revision(revision: str) -> None:
    if not SHA_RE.fullmatch(revision):
        _fail("revision")


def compose_command(contract: DeploymentContract) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        contract.project_name,
        "--project-directory",
        str(contract.root),
        "-f",
        str(contract.base_compose),
        "-f",
        str(contract.production_override),
        "-f",
        str(contract.secret_override),
    ]


def _compose_environment(image: str, revision: str) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/root"),
        "HERMES_IMAGE": image,
        "HERMES_GIT_SHA": revision,
    }
    for name in ("DOCKER_HOST", "DOCKER_CONFIG", "XDG_RUNTIME_DIR"):
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _validate_database_source_path(path: Path) -> None:
    if not path.is_absolute() or path != Path(os.path.normpath(path)):
        _fail("unsafe-db-source-path")
    _assert_no_symlink_components(path)
    try:
        metadata = path.lstat()
    except OSError:
        _fail("unsafe-db-source-path")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail("unsafe-db-source-path")


def _database_mount_kwargs(contract: DeploymentContract) -> dict[str, object]:
    return {
        "expected_source": str(contract.database_source),
        "expected_target": str(contract.database_target),
        "expected_type": contract.database_mount_type,
        "expected_read_only": contract.database_read_only,
        "legacy_sources": tuple(str(path) for path in contract.legacy_database_sources),
    }


def validate_compose_render(
    contract: DeploymentContract,
    image: str,
    revision: str,
) -> tuple[preflight.MountRecord, ...]:
    validate_immutable_image(image)
    validate_revision(revision)
    validate_secret_override(contract)
    environment = _compose_environment(image, revision)
    base = compose_command(contract)
    quiet = _run((*base, "config", "--quiet"), cwd=contract.root, env=environment, timeout=45)
    if quiet.returncode != 0:
        _fail("compose-render")
    services = _run((*base, "config", "--services"), cwd=contract.root, env=environment, timeout=45)
    if services.returncode != 0:
        _fail("compose-services")
    service_names = {line.strip() for line in services.stdout.splitlines() if line.strip()}
    if contract.target_service not in service_names:
        _fail("target-service-missing")
    rendered = _run(
        (*base, "config", "--format", "json"),
        cwd=contract.root,
        env=environment,
        timeout=45,
    )
    if rendered.returncode != 0:
        _fail("compose-render-document")
    try:
        document = json.loads(rendered.stdout)
    except json.JSONDecodeError:
        _fail("compose-render-document")
    if not isinstance(document, dict):
        _fail("compose-render-document")
    mounts = _preflight(
        preflight.compose_mounts_from_document,
        document,
        service_name=contract.target_service,
    )
    _preflight(
        preflight.validate_database_mounts,
        mounts,
        **_database_mount_kwargs(contract),
        source_path_validator=None,
    )
    return mounts


def inspect_live_database_mounts(contract: DeploymentContract) -> tuple[preflight.MountRecord, ...]:
    result = _run(
        ("docker", "inspect", "--format", "{{json .Mounts}}", contract.target_service),
        timeout=30,
    )
    if result.returncode != 0:
        _fail("live-mount-inspect")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        _fail("live-mount-document")
    return _preflight(preflight.live_mounts_from_document, document)


def inspect_local_image(
    contract: DeploymentContract,
    image: str,
    *,
    expected_revision: str | None = None,
) -> InspectedImage:
    validate_immutable_image(image)
    result = _run(("docker", "image", "inspect", image), timeout=30)
    if result.returncode != 0:
        _fail("local-image-missing")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError:
        _fail("image-inspect-invalid")
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        _fail("image-inspect-ambiguous")
    record = records[0]
    image_id = record.get("Id")
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    revision = labels.get(contract.image_revision_label) if isinstance(labels, dict) else None
    if not isinstance(image_id, str) or not IMAGE_ID_RE.fullmatch(image_id):
        _fail("image-inspect-invalid")
    if not isinstance(revision, str) or not revision:
        _fail("image-revision-label-missing")
    if not SHA_RE.fullmatch(revision):
        _fail("image-revision-label-invalid")
    if expected_revision is not None:
        validate_revision(expected_revision)
        if revision != expected_revision:
            _fail("image-revision-mismatch")
    return InspectedImage(image_id=image_id, revision=revision)


def _print_plan(
    contract: DeploymentContract,
    image: str,
    revision: str,
    *,
    rollback: bool,
    source_head_revision: str,
) -> None:
    action = "ROLLBACK" if rollback else "DEPLOY"
    command = (*compose_command(contract), "up", "-d", "--no-deps", "--force-recreate", contract.target_service)
    print(f"PLAN={action}")
    print(f"IMAGE={image}")
    print(f"REVISION={revision}")
    print(f"SOURCE_HEAD_REVISION={source_head_revision}")
    if rollback:
        print(f"ROLLBACK_TARGET_REVISION={revision}")
        print(f"ROLLBACK_TARGET_IMAGE_ID={image}")
        print(f"ROLLBACK_IMAGE_REVISION_LABEL={revision}")
        print("ROLLBACK_REVISION_ANCESTOR_OF_SOURCE=true")
    else:
        print(f"DEPLOY_TARGET_REVISION={revision}")
    print(f"IMAGE_REVISION_LABEL_KEY={contract.image_revision_label}")
    print("IMAGE_REVISION_MATCH=true")
    print(f"COMPOSE_PROJECT={contract.project_name}")
    print(f"TARGET_SERVICE={contract.target_service}")
    print(f"COMMAND={shlex.join(command)}")
    print("DEPLOYMENT_ACTIONS_PERFORMED=false")


def _temporary_render_contract(contract: DeploymentContract, directory: Path) -> DeploymentContract:
    _assert_no_symlink_components(directory)
    metadata = directory.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != _effective_uid()
    ):
        _fail("temporary-plan-directory")
    runtime_directory = directory / "runtime"
    return replace(
        contract,
        runtime_directory=runtime_directory,
        secret_override=runtime_directory / "hermes-secrets-override.yml",
    )


def _validate_operation_identity(
    contract: DeploymentContract,
    *,
    image: str,
    revision: str,
    current_image: str | None,
    rollback: bool,
) -> tuple[InspectedImage, str]:
    if rollback:
        source_head = current_source_head_revision(contract)
        validate_repository(contract, source_head)
        validate_rollback_revision(
            contract,
            source_head_revision=source_head,
            rollback_revision=revision,
        )
    else:
        source_head = revision
        validate_repository(contract, revision)
    target = inspect_local_image(contract, image, expected_revision=revision)
    if current_image is not None:
        current = inspect_local_image(contract, current_image)
        if current.image_id == target.image_id:
            _fail("rollback-image-not-distinct")
    return target, source_head


def _validate_capacity(
    contract: DeploymentContract,
    *,
    phase: str,
    revision: str,
    image_id: str | None,
    available_bytes: int | None = None,
) -> preflight.CapacityAssessment:
    return _preflight(
        preflight.validate_capacity,
        phase=phase,
        filesystem=contract.capacity_filesystem,
        minimum_free_basis_points=contract.minimum_free_basis_points,
        estimated_peak_incremental_build_bytes=contract.estimated_peak_incremental_build_bytes,
        build_peak_multiplier=contract.build_peak_multiplier,
        staging_safety_margin_bytes=contract.staging_safety_margin_bytes,
        formula_source=contract.capacity_formula_source,
        target_sha=revision,
        target_image_id=image_id,
        available_bytes=available_bytes,
    )


def _assert_lease_available(
    contract: DeploymentContract,
    *,
    revision: str,
    image_id: str,
) -> None:
    _preflight(
        preflight.assert_lease_available,
        path=contract.lease_path,
        allowed_owner_uids=contract.lease_owner_uids,
        canonical_repository=contract.canonical_repository,
        target_sha=revision,
        target_image_id=image_id,
    )


def _validate_live_future_mounts(
    contract: DeploymentContract,
    future_mounts: tuple[preflight.MountRecord, ...],
) -> preflight.DatabaseMountAssessment:
    live_mounts = inspect_live_database_mounts(contract)
    return _preflight(
        preflight.validate_live_future_database_mounts,
        future_mounts,
        live_mounts,
        **_database_mount_kwargs(contract),
        source_path_validator=_validate_database_source_path,
    )


def _ordinary_deploy_pre_mutation_barrier(
    contract: DeploymentContract,
    *,
    source: Path,
    image: str,
    revision: str,
    current_image: str | None = None,
    rollback: bool = False,
    lease: preflight.DeploymentLease | None = None,
) -> tuple[InspectedImage, dict[str, str], str]:
    target, source_head = _validate_operation_identity(
        contract,
        image=image,
        revision=revision,
        current_image=current_image,
        rollback=rollback,
    )
    if lease is None:
        _assert_lease_available(contract, revision=revision, image_id=target.image_id)
    else:
        _preflight(
            preflight.validate_held_lease,
            lease,
            allowed_owner_uids=contract.lease_owner_uids,
            operation_class="rollback" if rollback else "deploy",
            canonical_repository=contract.canonical_repository,
            target_sha=revision,
            target_image_id=target.image_id,
        )
    secrets = read_required_secrets(contract, source)
    live = _live_override_secrets(contract)
    if live is not None:
        _validate_secret_transition(
            contract,
            live=live,
            staged=secrets,
        )
    with tempfile.TemporaryDirectory(prefix="hermes-production-plan-") as raw_directory:
        temporary = _temporary_render_contract(contract, Path(raw_directory))
        _write_secret_override(temporary, secrets)
        primary_error: BaseException | None = None
        try:
            future_mounts = validate_compose_render(temporary, target.image_id, revision)
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                cleanup_secret_override(temporary)
            except DeploymentContractError:
                if primary_error is None:
                    raise
    _validate_live_future_mounts(contract, future_mounts)
    _validate_capacity(contract, phase="deploy", revision=revision, image_id=target.image_id)
    return target, secrets, source_head


def plan_operation(
    contract: DeploymentContract,
    *,
    source: Path,
    image: str,
    revision: str,
    rollback_from: str | None = None,
) -> None:
    rollback = rollback_from is not None
    target, _secrets, source_head = _ordinary_deploy_pre_mutation_barrier(
        contract,
        source=source,
        image=image,
        revision=revision,
        current_image=rollback_from,
        rollback=rollback,
    )
    _print_plan(contract, target.image_id, revision, rollback=rollback, source_head_revision=source_head)


def execute_operation(
    contract: DeploymentContract,
    *,
    source: Path,
    image: str,
    revision: str,
    confirmation: str,
    rollback: bool,
    current_image: str | None = None,
) -> None:
    required_confirmation = ROLLBACK_CONFIRMATION if rollback else DEPLOY_CONFIRMATION
    if confirmation != required_confirmation:
        _fail("explicit-confirmation-required")
    if rollback and current_image is None:
        _fail("current-image-required")
    _preflight(
        preflight.validate_deployment_lease_owner,
        allowed_owner_uids=contract.lease_owner_uids,
    )
    target, _source_head = _validate_operation_identity(
        contract,
        image=image,
        revision=revision,
        current_image=current_image,
        rollback=rollback,
    )
    _validate_runtime_directory(contract, create=True)
    lease = _preflight(
        preflight.acquire_deployment_lease,
        path=contract.lease_path,
        allowed_owner_uids=contract.lease_owner_uids,
        operation_class="rollback" if rollback else "deploy",
        canonical_repository=contract.canonical_repository,
        target_sha=revision,
        target_image_id=target.image_id,
        timeout_seconds=contract.lease_timeout_seconds,
    )
    primary_error: BaseException | None = None
    try:
        target, secrets, _source_head = _ordinary_deploy_pre_mutation_barrier(
            contract,
            source=source,
            image=image,
            revision=revision,
            current_image=current_image if rollback else None,
            rollback=rollback,
            lease=lease,
        )
        expected_image_id = target.image_id
        secret_transaction = _begin_secret_override_transaction(contract, secrets)
        mutation_error: BaseException | None = None
        try:
            environment = _compose_environment(expected_image_id, revision)
            command = (*compose_command(contract), "up", "-d", "--no-deps", "--force-recreate", contract.target_service)
            result = _run(command, cwd=contract.root, env=environment, timeout=300)
            if result.returncode != 0:
                _fail("compose-up")
            health = _run(
                (
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Status}} {{.RestartCount}} {{.Image}}",
                    contract.target_service,
                ),
                timeout=30,
            )
            if health.returncode != 0 or health.stdout.strip() != f"running 0 {expected_image_id}":
                _fail("post-operation-health")
        except BaseException as exc:
            mutation_error = exc
            raise
        finally:
            try:
                _finish_secret_override_transaction(
                    contract,
                    secret_transaction,
                    preserve_published=False,
                )
            except DeploymentContractError:
                if mutation_error is None:
                    raise
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            _preflight(
                preflight.release_deployment_lease,
                lease,
                allowed_owner_uids=contract.lease_owner_uids,
            )
        except DeploymentContractError:
            if primary_error is None:
                raise


def _add_image_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-repository")
    check.add_argument("--expected-sha", required=True)

    source = subparsers.add_parser("check-secret-source")
    source.add_argument("--secret-source", type=Path)

    prepare = subparsers.add_parser("prepare-override")
    prepare.add_argument("--secret-source", type=Path)

    subparsers.add_parser("cleanup")

    render = subparsers.add_parser("check-render")
    _add_image_arguments(render)

    capacity = subparsers.add_parser("check-capacity")
    capacity.add_argument("--phase", choices=("build", "deploy"), required=True)
    capacity.add_argument("--revision", required=True)
    capacity.add_argument("--image")

    recover = subparsers.add_parser("recover-lease")
    recover.add_argument("--expected-fingerprint", required=True)
    recover.add_argument("--confirm", required=True)

    plan = subparsers.add_parser("plan")
    _add_image_arguments(plan)
    plan.add_argument("--secret-source", type=Path)

    rollback_plan = subparsers.add_parser("plan-rollback")
    _add_image_arguments(rollback_plan)
    rollback_plan.add_argument("--current-image", required=True)
    rollback_plan.add_argument("--secret-source", type=Path)

    deploy = subparsers.add_parser("execute-deploy")
    _add_image_arguments(deploy)
    deploy.add_argument("--secret-source", type=Path)
    deploy.add_argument("--confirm", required=True)

    rollback = subparsers.add_parser("execute-rollback")
    _add_image_arguments(rollback)
    rollback.add_argument("--secret-source", type=Path)
    rollback.add_argument("--current-image", required=True)
    rollback.add_argument("--confirm", required=True)
    return parser


def _secret_source_argument(contract: DeploymentContract, requested: Path | None) -> Path:
    return contract.approved_secret_source if requested is None else requested


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        contract = load_contract()
        if args.command == "check-repository":
            validate_repository(contract, args.expected_sha)
            print("CHECK_REPOSITORY=PASS")
        elif args.command == "check-secret-source":
            source = _secret_source_argument(contract, args.secret_source)
            read_required_secrets(contract, source)
            metadata = source.lstat()
            print("CHECK_SECRET_SOURCE=PASS")
            print("SOURCE_PATH_CLASS=approved-production-secret-source")
            print("SOURCE_REGULAR_FILE=true")
            print("SOURCE_SYMLINK=false")
            print("SOURCE_OWNER=root")
            print(f"SOURCE_MODE={stat.S_IMODE(metadata.st_mode):04o}")
            print("REQUIRED_VARIABLES=" + ",".join(contract.required_secret_names))
            print("PROTECTED_VARIABLES=" + ",".join(contract.protected_secret_names))
            print("OPTIONAL_VARIABLES=" + ",".join(name for name in contract.protected_secret_names if name not in contract.required_secret_names))
            print("SOURCE_REQUIRED_VARIABLES_PRESENT=true")
            print("SOURCE_DUPLICATE_ASSIGNMENTS=false")
            print("SOURCE_MALFORMED_ASSIGNMENTS=false")
            print("SOURCE_STRUCTURALLY_VALID=true")
            print("SECRET_VALUES_OUTPUT=false")
        elif args.command == "prepare-override":
            prepare_secret_override(contract, _secret_source_argument(contract, args.secret_source))
            print("SECRET_OVERRIDE_PREPARED=true")
            print("REQUIRED_VARIABLES=" + ",".join(contract.required_secret_names))
            print("PROTECTED_VARIABLES=" + ",".join(contract.protected_secret_names))
            print("OPTIONAL_VARIABLES=" + ",".join(name for name in contract.protected_secret_names if name not in contract.required_secret_names))
        elif args.command == "cleanup":
            cleanup_secret_override(contract)
            print("SECRET_OVERRIDE_PRESENT=false")
        elif args.command == "check-render":
            validate_repository(contract, args.revision)
            inspected = inspect_local_image(contract, args.image, expected_revision=args.revision)
            validate_compose_render(contract, inspected.image_id, args.revision)
            print("CHECK_COMPOSE_RENDER=PASS")
            print("DEPLOYMENT_ACTIONS_PERFORMED=false")
        elif args.command == "check-capacity":
            validate_repository(contract, args.revision)
            image_id = None
            if args.phase == "deploy":
                if args.image is None:
                    _fail("capacity-image-required")
                image_id = inspect_local_image(
                    contract,
                    args.image,
                    expected_revision=args.revision,
                ).image_id
            elif args.image is not None:
                _fail("capacity-build-image-not-allowed")
            assessment = _validate_capacity(
                contract,
                phase=args.phase,
                revision=args.revision,
                image_id=image_id,
            )
            print("CAPACITY_GATE=PASS")
            print(f"CAPACITY_PHASE={assessment.phase}")
            print(f"CAPACITY_REQUIRED_BYTES={assessment.required_bytes}")
            print(f"CAPACITY_AVAILABLE_BYTES={assessment.available_bytes}")
            print(f"CAPACITY_FORMULA_SOURCE={assessment.formula_source}")
            print("DEPLOYMENT_ACTIONS_PERFORMED=false")
        elif args.command == "recover-lease":
            _preflight(
                preflight.recover_expired_lease,
                path=contract.lease_path,
                allowed_owner_uids=contract.lease_owner_uids,
                expected_fingerprint=args.expected_fingerprint,
                confirmation=args.confirm,
            )
            print("DEPLOYMENT_LEASE_RECOVERED=true")
        elif args.command == "plan":
            plan_operation(
                contract,
                source=_secret_source_argument(contract, args.secret_source),
                image=args.image,
                revision=args.revision,
            )
        elif args.command == "plan-rollback":
            plan_operation(
                contract,
                source=_secret_source_argument(contract, args.secret_source),
                image=args.image,
                revision=args.revision,
                rollback_from=args.current_image,
            )
        elif args.command == "execute-deploy":
            execute_operation(
                contract,
                source=_secret_source_argument(contract, args.secret_source),
                image=args.image,
                revision=args.revision,
                confirmation=args.confirm,
                rollback=False,
            )
            print("DEPLOYMENT_ACTIONS_PERFORMED=true")
        elif args.command == "execute-rollback":
            execute_operation(
                contract,
                source=_secret_source_argument(contract, args.secret_source),
                image=args.image,
                revision=args.revision,
                confirmation=args.confirm,
                rollback=True,
                current_image=args.current_image,
            )
            print("ROLLBACK_ACTIONS_PERFORMED=true")
        else:
            _fail("unsupported-command")
    except DeploymentContractError as exc:
        print(f"STATUS=FAIL CODE={exc.code}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
