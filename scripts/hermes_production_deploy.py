#!/usr/bin/env python3
"""Canonical, fail-closed Hermes production deployment entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


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
class DeploymentContract:
    root: Path
    manifest_path: Path
    base_compose: Path
    production_override: Path
    runtime_directory: Path
    secret_override: Path
    approved_secret_source: Path
    approved_source_owner_uids: frozenset[int]
    required_secret_names: tuple[str, ...]
    project_name: str
    target_service: str
    feature_gates: dict[str, str]


def _fail(code: str) -> None:
    raise DeploymentContractError(code)


def _read_json_file(path: Path, *, code: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            _fail(f"{code}-not-regular")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except DeploymentContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail(f"{code}-invalid")
    if not isinstance(raw, dict):
        _fail(f"{code}-invalid")
    return raw


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


def load_contract(root: Path = REPOSITORY_ROOT) -> DeploymentContract:
    root = root.absolute()
    manifest_path = root / "deploy" / "hermes-production.json"
    raw = _read_json_file(manifest_path, code="manifest")
    if set(raw) != {"version", "compose", "runtime", "secrets", "deployment", "rollback", "feature_gates"}:
        _fail("manifest-fields")
    if raw["version"] != 1:
        _fail("manifest-version")

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
    if runtime_directory != Path("/run/hermes") or secret_override != runtime_directory / "hermes-secrets-override.yml":
        _fail("runtime-path")
    _mode(runtime.get("directory_mode"), expected="0700", code="runtime-directory-mode")
    _mode(runtime.get("secret_override_mode"), expected="0600", code="secret-override-mode")
    if runtime.get("owner") != "deployment-operator":
        _fail("runtime-owner")

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
    required = secrets.get("required_variables")
    if not isinstance(required, list) or required != ["TELEGRAM_BOT_TOKEN"]:
        _fail("required-secret-names")
    if not all(isinstance(name, str) and ENV_NAME_RE.fullmatch(name) for name in required):
        _fail("required-secret-names")

    deployment = _mapping(raw["deployment"], code="manifest-deployment")
    if (
        deployment.get("image_reference_policy") != "digest-only"
        or deployment.get("revision_required") is not True
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
    expected_gates = {
        "HEALBITE_SHOPPING_LIST_ENABLED": "false",
        "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
    }
    if feature_gates != expected_gates:
        _fail("feature-gate-policy")

    return DeploymentContract(
        root=root,
        manifest_path=manifest_path,
        base_compose=root / base_relative,
        production_override=root / production_relative,
        runtime_directory=runtime_directory,
        secret_override=secret_override,
        approved_secret_source=approved_source,
        approved_source_owner_uids=frozenset(owner_uids),
        required_secret_names=tuple(required),
        project_name="hermes-agent",
        target_service="hermes-bot",
        feature_gates=dict(expected_gates),
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
    if _git_output(contract, "rev-parse", "HEAD") != expected_sha:
        _fail("head-mismatch")
    if _git_output(contract, "status", "--porcelain=v1"):
        _fail("dirty-worktree")
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


def _read_protected_file(path: Path, *, expected: os.stat_result) -> str:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        _fail("secret-source-open")
    try:
        opened = os.fstat(fd)
        if opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino or not stat.S_ISREG(opened.st_mode):
            _fail("secret-source-race")
        data = bytearray()
        while len(data) <= MAX_SECRET_SOURCE_BYTES:
            chunk = os.read(fd, min(65536, MAX_SECRET_SOURCE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > MAX_SECRET_SOURCE_BYTES:
            _fail("secret-source-too-large")
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            _fail("secret-source-encoding")
    finally:
        os.close(fd)


def _parse_dotenv(text: str) -> dict[str, str]:
    if "\x00" in text or "\r" in text:
        _fail("secret-source-control-character")
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            _fail("secret-source-syntax")
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not ENV_NAME_RE.fullmatch(name) or name in values:
            _fail("secret-source-variable")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[name] = value
    return values


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
    selected: dict[str, str] = {}
    for name in contract.required_secret_names:
        value = values.get(name)
        if value is None or not value:
            _fail("required-secret-missing")
        selected[name] = value
    if set(values) != set(contract.required_secret_names):
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
    if metadata.st_uid != os.geteuid():
        _fail("runtime-directory-owner")


def validate_secret_override(contract: DeploymentContract) -> None:
    if contract.secret_override.parent != contract.runtime_directory:
        _fail("override-path")
    _validate_runtime_directory(contract, create=False)
    _validate_regular_file(
        contract.secret_override,
        mode=0o600,
        allowed_uids=frozenset({os.geteuid()}),
        code="secret-override",
    )


def _override_document(contract: DeploymentContract, secrets: dict[str, str]) -> bytes:
    document = {
        "services": {
            contract.target_service: {
                "environment": {name: secrets[name] for name in contract.required_secret_names}
            }
        }
    }
    return (json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def prepare_secret_override(contract: DeploymentContract, source: Path) -> None:
    secrets = read_required_secrets(contract, source)
    _validate_runtime_directory(contract, create=True)
    if contract.secret_override.exists() or contract.secret_override.is_symlink():
        validate_secret_override(contract)

    temp_path: Path | None = None
    fd: int | None = None
    old_umask = os.umask(0o077)
    try:
        try:
            fd, raw_path = tempfile.mkstemp(prefix=".hermes-secrets-override.", dir=contract.runtime_directory)
            temp_path = Path(raw_path)
            os.fchmod(fd, 0o600)
            payload = _override_document(contract, secrets)
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
                allowed_uids=frozenset({os.geteuid()}),
                code="temporary-override",
            )
            os.replace(temp_path, contract.secret_override)
            temp_path = None
            directory_fd = os.open(contract.runtime_directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            validate_secret_override(contract)
        except DeploymentContractError:
            raise
        except OSError:
            _fail("override-atomic-write")
    finally:
        os.umask(old_umask)
        if fd is not None:
            os.close(fd)
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                _fail("override-partial-cleanup")


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


def validate_compose_render(contract: DeploymentContract, image: str, revision: str) -> None:
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


def inspect_local_image(image: str) -> str:
    validate_immutable_image(image)
    result = _run(("docker", "image", "inspect", "--format", "{{.Id}}", image), timeout=30)
    image_id = result.stdout.strip()
    if result.returncode != 0 or not IMAGE_ID_RE.fullmatch(image_id):
        _fail("local-image-missing")
    return image_id


def _print_plan(contract: DeploymentContract, image: str, *, rollback: bool) -> None:
    action = "ROLLBACK" if rollback else "DEPLOY"
    command = (*compose_command(contract), "up", "-d", "--no-deps", "--force-recreate", contract.target_service)
    print(f"PLAN={action}")
    print(f"IMAGE={image}")
    print(f"COMPOSE_PROJECT={contract.project_name}")
    print(f"TARGET_SERVICE={contract.target_service}")
    print(f"COMMAND={shlex.join(command)}")
    print("DEPLOYMENT_ACTIONS_PERFORMED=false")


def plan_operation(
    contract: DeploymentContract,
    *,
    source: Path,
    image: str,
    revision: str,
    rollback_from: str | None = None,
) -> None:
    validate_immutable_image(image)
    validate_revision(revision)
    if rollback_from is not None:
        validate_immutable_image(rollback_from)
        if inspect_local_image(image) == inspect_local_image(rollback_from):
            _fail("rollback-image-not-distinct")
    else:
        inspect_local_image(image)
    prepare_secret_override(contract, source)
    try:
        validate_compose_render(contract, image, revision)
        _print_plan(contract, image, rollback=rollback_from is not None)
    finally:
        cleanup_secret_override(contract)


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
    expected_image_id = inspect_local_image(image)
    if rollback:
        if current_image is None:
            _fail("current-image-required")
        if inspect_local_image(current_image) == expected_image_id:
            _fail("rollback-image-not-distinct")
    prepare_secret_override(contract, source)
    try:
        validate_compose_render(contract, image, revision)
        environment = _compose_environment(image, revision)
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
    finally:
        cleanup_secret_override(contract)


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
            print("SOURCE_REQUIRED_VARIABLES_PRESENT=true")
            print("SOURCE_DUPLICATE_ASSIGNMENTS=false")
            print("SOURCE_MALFORMED_ASSIGNMENTS=false")
            print("SOURCE_STRUCTURALLY_VALID=true")
            print("SECRET_VALUES_OUTPUT=false")
        elif args.command == "prepare-override":
            prepare_secret_override(contract, _secret_source_argument(contract, args.secret_source))
            print("SECRET_OVERRIDE_PREPARED=true")
            print("REQUIRED_VARIABLES=" + ",".join(contract.required_secret_names))
        elif args.command == "cleanup":
            cleanup_secret_override(contract)
            print("SECRET_OVERRIDE_PRESENT=false")
        elif args.command == "check-render":
            validate_compose_render(contract, args.image, args.revision)
            print("CHECK_COMPOSE_RENDER=PASS")
            print("DEPLOYMENT_ACTIONS_PERFORMED=false")
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
