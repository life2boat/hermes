from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import hermes_execution_authority as authority


REVISION = "1" * 40
TARGET_IMAGE = "sha256:" + "2" * 64
CURRENT_IMAGE = "sha256:" + "3" * 64
SOURCE_SHA = "4" * 64
SCHEMA_SHA = "5" * 64


@dataclass
class AuthorityContext:
    repository: Path
    plan_path: Path
    plan_sha256: str
    plan: dict[str, Any]
    final_path: Path
    final_sha256: str
    final_payload: dict[str, Any]
    envelope_path: Path
    descriptor_path: Path
    override_path: Path
    runtime_payload: dict[str, Any]


def _canonical(payload: dict[str, Any]) -> bytes:
    return authority._canonical_json(payload)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _private(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write(path: Path, data: bytes, mode: int = 0o600) -> Path:
    path.write_bytes(data)
    os.chmod(path, mode)
    return path


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    return _write(path, _canonical(payload))


def _directory_identity(path: Path) -> dict[str, int | str]:
    metadata = path.stat()
    return {
        "PATH": str(path),
        "DEVICE": int(metadata.st_dev),
        "INODE": int(metadata.st_ino),
        "UID": int(metadata.st_uid),
        "GID": int(metadata.st_gid),
        "MODE": stat.S_IMODE(metadata.st_mode),
    }


def _secret_identity(path: Path) -> dict[str, int | str]:
    metadata = path.stat()
    return {
        "PATH": str(path),
        "DEVICE": int(metadata.st_dev),
        "INODE": int(metadata.st_ino),
        "SIZE": int(metadata.st_size),
        "UID": int(metadata.st_uid),
        "GID": int(metadata.st_gid),
        "MODE": stat.S_IMODE(metadata.st_mode),
        "SHA256": _sha256(path),
    }


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _authority_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AuthorityContext:
    repository = _private(tmp_path / "operations-root")
    _write(repository / ".gitignore", b"*.pyc\n__pycache__/\nops/\n", 0o644)
    _write(repository / "docker-compose.yml", b"services: {}\n", 0o644)
    _write(repository / "tracked.txt", b"tracked\n", 0o644)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Authority Tests")
    _git(repository, "add", ".gitignore", "docker-compose.yml", "tracked.txt")
    _git(repository, "commit", "-q", "-m", "synthetic authority tree")
    head = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")

    artifacts = _private(tmp_path / "artifacts")
    plan_dir = _private(tmp_path / "plans")
    database_parent = _private(tmp_path / "database")
    database = _write(database_parent / "healbite.db", b"synthetic-db")
    approval = _write(artifacts / "operations-root-approval.json", b"approval\n")
    policy = _write(artifacts / "clean-start-policy.json", b"policy\n")
    p5b = _write(artifacts / "p5b-evidence.md", b"p5b\n")
    p6a_f1 = _write(artifacts / "p6a-f1-evidence.md", b"p6a-f1\n")
    override_payload = {
        "services": {
            "hermes-bot": {
                "volumes": [
                    {
                        "bind": {"create_host_path": True},
                        "source": str(database),
                        "target": "/home/hermes/healbite.db",
                        "type": "bind",
                    }
                ]
            }
        }
    }
    override = _write_json(artifacts / "production-db-override.yml", override_payload)
    secret = _write(artifacts / "secrets-override.yml", b"services: {}\n")

    plan = {
        "OPERATIONS_ROOT_APPROVAL_PATH": str(approval),
        "OPERATIONS_ROOT_APPROVAL_SHA256": _sha256(approval),
        "CLEAN_START_POLICY_PATH": str(policy),
        "CLEAN_START_POLICY_SHA256": _sha256(policy),
        "MIGRATION_IMAGE_REVISION": head,
        "MIGRATION_IMAGE_ID": TARGET_IMAGE,
        "PREVIOUS_IMAGE_ID": CURRENT_IMAGE,
        "DB_CANONICAL_PATH": str(database),
        "SOURCE_DEVICE": int(database.stat().st_dev),
        "SOURCE_INODE": int(database.stat().st_ino),
        "SOURCE_SIZE": int(database.stat().st_size),
        "SOURCE_SHA256": SOURCE_SHA,
        "SOURCE_USER_VERSION": 7,
        "SOURCE_SCHEMA_FINGERPRINT": SCHEMA_SHA,
        "SOURCE_PARENT_IDENTITY": _directory_identity(database_parent),
    }
    plan_path = _write_json(plan_dir / "plan.json", plan)
    plan_sha = _sha256(plan_path)

    descriptor_payload = {
        "DESCRIPTOR_VERSION": authority.INVOCATION_DESCRIPTOR_VERSION,
        "CREATED_AT": "2026-07-22T13:00:00Z",
        "COMPOSE_PROJECT_NAME": "hermes-agent",
        "PROJECT_DIRECTORY": str(repository),
        "COMPOSE_FILE_ORDER": [
            str(repository / "docker-compose.yml"),
            str(override),
            str(secret),
        ],
        "NON_SECRET_COMPOSE_SHA256": {
            str(repository / "docker-compose.yml"): _sha256(
                repository / "docker-compose.yml"
            ),
            str(override): _sha256(override),
        },
        "SECRETS_OVERRIDE": _secret_identity(secret),
        "ENVIRONMENT_SOURCE_CLASS": "EXISTING_PRODUCTION_ENV_FILE_METADATA_ONLY",
        "APPLICATION_SERVICE": "hermes-bot",
        "CANONICAL_DB_SOURCE": str(database),
        "CANONICAL_DB_TARGET": "/home/hermes/healbite.db",
        "CURRENT_PRODUCTION_IMAGE_ID": CURRENT_IMAGE,
        "TARGET_IMAGE_ID": TARGET_IMAGE,
        "SOURCE_SHA": head,
        "TREE_SHA": tree,
        "CONTAINS_SECRET_VALUES": False,
    }
    descriptor = _write_json(
        artifacts / "invocation-descriptor.json", descriptor_payload
    )
    root_metadata = repository.stat()
    envelope_payload = {
        "ENVELOPE_VERSION": 1,
        "CREATED_AT": "2026-07-22T13:00:00Z",
        "PUBLIC_OPERATIONS_ROOT_APPROVAL_PATH": str(approval),
        "PUBLIC_OPERATIONS_ROOT_APPROVAL_SHA256": _sha256(approval),
        "OPERATIONS_ROOT_PATH": str(repository),
        "OPERATIONS_ROOT_HEAD_SHA": head,
        "OPERATIONS_ROOT_TREE_SHA": tree,
        "OPERATIONS_ROOT_MODE": stat.S_IMODE(root_metadata.st_mode),
        "OPERATIONS_ROOT_UID": int(root_metadata.st_uid),
        "OPERATIONS_ROOT_GID": int(root_metadata.st_gid),
        "OPERATIONS_ROOT_CLEAN": True,
        "OBJECT_ALTERNATES_ABSENT": True,
        "P5B_EVIDENCE_SHA256": _sha256(p5b),
        "P6A_F1_EVIDENCE_SHA256": _sha256(p6a_f1),
        "EXACT_MAIN_IMAGE_ID": TARGET_IMAGE,
        "CANONICAL_DB_PATH": str(database),
        "CANONICAL_DB_DEVICE": int(database.stat().st_dev),
        "CANONICAL_DB_INODE": int(database.stat().st_ino),
        "CANONICAL_DB_SIZE": int(database.stat().st_size),
        "CANONICAL_DB_SHA256": SOURCE_SHA,
        "PERSISTENT_DB_OVERRIDE_SHA256": _sha256(override),
        "INVOCATION_DESCRIPTOR_SHA256": _sha256(descriptor),
        "CLEAN_START_POLICY_SHA256": _sha256(policy),
        "PLAN_ONLY_AUTHORIZED": True,
        "EXECUTION_AUTHORIZED": False,
        "DEPLOY_AUTHORIZED": False,
        "CONTAINS_SECRETS": False,
    }
    envelope = _write_json(artifacts / "approval-envelope.json", envelope_payload)
    now = authority.datetime.now(authority.timezone.utc)
    final_payload = {
        "EXECUTION_AUTHORITY_VERSION": authority.EXECUTION_AUTHORITY_VERSION,
        "CREATED_AT": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "EXPIRES_AT": (now + timedelta(hours=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "PLAN_PATH": str(plan_path),
        "PLAN_SHA256": plan_sha,
        "OPERATIONS_ROOT_APPROVAL_PATH": str(approval),
        "OPERATIONS_ROOT_APPROVAL_SHA256": _sha256(approval),
        "CLEAN_START_POLICY_PATH": str(policy),
        "CLEAN_START_POLICY_SHA256": _sha256(policy),
        "APPROVAL_ENVELOPE_PATH": str(envelope),
        "APPROVAL_ENVELOPE_SHA256": _sha256(envelope),
        "INVOCATION_DESCRIPTOR_PATH": str(descriptor),
        "INVOCATION_DESCRIPTOR_SHA256": _sha256(descriptor),
        "PERSISTENT_DB_OVERRIDE_PATH": str(override),
        "PERSISTENT_DB_OVERRIDE_SHA256": _sha256(override),
        "P5B_EVIDENCE_PATH": str(p5b),
        "P5B_EVIDENCE_SHA256": _sha256(p5b),
        "P6A_F1_EVIDENCE_PATH": str(p6a_f1),
        "P6A_F1_EVIDENCE_SHA256": _sha256(p6a_f1),
        "SOURCE_SHA": head,
        "SOURCE_TREE_SHA": tree,
        "TARGET_IMAGE_ID": TARGET_IMAGE,
        "CURRENT_RUNTIME_IMAGE_ID": CURRENT_IMAGE,
        "CANONICAL_PRODUCTION_DB_PATH": str(database),
        "SOURCE_DB_SHA256": SOURCE_SHA,
        "SOURCE_DB_SIZE": int(database.stat().st_size),
        "SOURCE_DB_USER_VERSION": 7,
        "SOURCE_DB_SCHEMA_FINGERPRINT": SCHEMA_SHA,
        "SOURCE_DB_PARENT_IDENTITY": _directory_identity(database_parent),
        "OPERATIONS_ROOT_PATH": str(repository),
        "OPERATIONS_ROOT_HEAD_SHA": head,
        "OPERATIONS_ROOT_TREE_SHA": tree,
        "EXECUTION_AUTHORIZED": True,
        "DEPLOY_AUTHORIZED": False,
        "CONTAINS_SECRETS": False,
    }
    final = _write_json(artifacts / "final-authority.json", final_payload)
    runtime_payload = {
        "State": {"Running": True},
        "Image": CURRENT_IMAGE,
        "Mounts": [
            {
                "Source": str(database),
                "Destination": "/home/hermes/healbite.db",
            }
        ],
    }
    monkeypatch.setattr(authority, "_inspect_image", lambda *_args: None)
    monkeypatch.setattr(authority, "_inspect_runtime", lambda _service: runtime_payload)
    return AuthorityContext(
        repository=repository,
        plan_path=plan_path,
        plan_sha256=plan_sha,
        plan=plan,
        final_path=final,
        final_sha256=_sha256(final),
        final_payload=final_payload,
        envelope_path=envelope,
        descriptor_path=descriptor,
        override_path=override,
        runtime_payload=runtime_payload,
    )


def _load(context: AuthorityContext) -> authority.ExecutionAuthorityBundle:
    return authority.load_execution_authority(
        authority_path=str(context.final_path),
        authority_sha256=context.final_sha256,
        plan_path=context.plan_path,
        plan_sha256=context.plan_sha256,
        plan=context.plan,
        repository_root=context.repository,
    )


def _rewrite_final(context: AuthorityContext) -> None:
    _write_json(context.final_path, context.final_payload)
    context.final_sha256 = _sha256(context.final_path)


def _rewrite_descriptor_chain(
    context: AuthorityContext,
    descriptor_payload: dict[str, Any],
) -> None:
    _write_json(context.descriptor_path, descriptor_payload)
    descriptor_sha = _sha256(context.descriptor_path)
    envelope_payload = json.loads(context.envelope_path.read_text(encoding="ascii"))
    envelope_payload["INVOCATION_DESCRIPTOR_SHA256"] = descriptor_sha
    _write_json(context.envelope_path, envelope_payload)
    context.final_payload["INVOCATION_DESCRIPTOR_SHA256"] = descriptor_sha
    context.final_payload["APPROVAL_ENVELOPE_SHA256"] = _sha256(context.envelope_path)
    _rewrite_final(context)


def test_complete_execution_authority_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _authority_context(tmp_path, monkeypatch)
    bundle = _load(context)
    try:
        bundle.validate_source(
            identity={
                "SOURCE_DEVICE": context.plan["SOURCE_DEVICE"],
                "SOURCE_INODE": context.plan["SOURCE_INODE"],
                "SOURCE_SIZE": context.plan["SOURCE_SIZE"],
                "SOURCE_SHA256": SOURCE_SHA,
                "SOURCE_USER_VERSION": 7,
            },
            schema_fingerprint=SCHEMA_SHA,
            parent_identity=context.plan["SOURCE_PARENT_IDENTITY"],
        )
        assert bundle.path_matches()
        assert bundle.runtime_matches()
    finally:
        bundle.close()


AUTHORITY_DENIAL_CASES = (
    "final_missing",
    "final_empty",
    "final_modified",
    "final_expired",
    "plan_sha_mismatch",
    "approval_sha_mismatch",
    "policy_sha_mismatch",
    "envelope_missing",
    "envelope_modified",
    "descriptor_missing",
    "descriptor_modified",
    "override_missing",
    "override_modified",
    "compose_order_changed",
    "target_image_changed",
    "runtime_image_changed",
    "source_sha_changed",
    "source_size_changed",
    "source_user_version_changed",
    "source_schema_changed",
    "ignored_pyc",
    "ignored_pycache",
    "excluded_override",
    "untracked_file",
    "repository_symlink",
    "repository_special_file",
    "repository_hardlink",
    "authority_symlink",
    "authority_wrong_mode",
    "authority_unknown_field",
)


@pytest.mark.parametrize("case", AUTHORITY_DENIAL_CASES)
def test_execution_authority_denial_matrix(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _authority_context(tmp_path, monkeypatch)
    load_kwargs: dict[str, Any] = {}
    validate_identity = {
        "SOURCE_DEVICE": context.plan["SOURCE_DEVICE"],
        "SOURCE_INODE": context.plan["SOURCE_INODE"],
        "SOURCE_SIZE": context.plan["SOURCE_SIZE"],
        "SOURCE_SHA256": SOURCE_SHA,
        "SOURCE_USER_VERSION": 7,
    }
    validate_schema = SCHEMA_SHA
    validate_only = False

    if case == "final_missing":
        context.final_path.unlink()
    elif case == "final_empty":
        load_kwargs["authority_path"] = ""
    elif case == "final_modified":
        context.final_path.write_bytes(context.final_path.read_bytes() + b" ")
    elif case == "final_expired":
        now = authority.datetime.now(authority.timezone.utc)
        context.final_payload["CREATED_AT"] = (
            (now - timedelta(hours=2))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        context.final_payload["EXPIRES_AT"] = (
            (now - timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        _rewrite_final(context)
    elif case == "plan_sha_mismatch":
        load_kwargs["plan_sha256"] = "f" * 64
    elif case == "approval_sha_mismatch":
        context.final_payload["OPERATIONS_ROOT_APPROVAL_SHA256"] = "f" * 64
        _rewrite_final(context)
    elif case == "policy_sha_mismatch":
        context.final_payload["CLEAN_START_POLICY_SHA256"] = "f" * 64
        _rewrite_final(context)
    elif case == "envelope_missing":
        context.envelope_path.unlink()
    elif case == "envelope_modified":
        context.envelope_path.write_bytes(context.envelope_path.read_bytes() + b" ")
    elif case == "descriptor_missing":
        context.descriptor_path.unlink()
    elif case == "descriptor_modified":
        context.descriptor_path.write_bytes(context.descriptor_path.read_bytes() + b" ")
    elif case == "override_missing":
        context.override_path.unlink()
    elif case == "override_modified":
        context.override_path.write_bytes(context.override_path.read_bytes() + b" ")
    elif case == "compose_order_changed":
        descriptor = json.loads(context.descriptor_path.read_text(encoding="ascii"))
        descriptor["COMPOSE_FILE_ORDER"] = list(
            reversed(descriptor["COMPOSE_FILE_ORDER"])
        )
        _rewrite_descriptor_chain(context, descriptor)
    elif case == "target_image_changed":
        context.plan = dict(context.plan)
        context.plan["MIGRATION_IMAGE_ID"] = "sha256:" + "9" * 64
    elif case == "runtime_image_changed":
        context.runtime_payload["Image"] = "sha256:" + "8" * 64
    elif case == "source_sha_changed":
        validate_identity["SOURCE_SHA256"] = "8" * 64
        validate_only = True
    elif case == "source_size_changed":
        validate_identity["SOURCE_SIZE"] = int(validate_identity["SOURCE_SIZE"]) + 1
        validate_only = True
    elif case == "source_user_version_changed":
        validate_identity["SOURCE_USER_VERSION"] = 8
        validate_only = True
    elif case == "source_schema_changed":
        validate_schema = "8" * 64
        validate_only = True
    elif case == "ignored_pyc":
        _write(context.repository / "ignored.pyc", b"bytecode", 0o644)
    elif case == "ignored_pycache":
        cache = context.repository / "__pycache__"
        cache.mkdir()
        _write(cache / "ignored.pyc", b"bytecode", 0o644)
    elif case == "excluded_override":
        info_exclude = context.repository / ".git" / "info" / "exclude"
        info_exclude.write_text("ops/\n", encoding="ascii")
        ops = context.repository / "ops"
        ops.mkdir()
        _write(ops / "override.yml", b"services: {}\n", 0o644)
    elif case == "untracked_file":
        _write(context.repository / "untracked.txt", b"untracked\n", 0o644)
    elif case == "repository_symlink":
        (context.repository / "link").symlink_to("tracked.txt")
    elif case == "repository_special_file":
        os.mkfifo(context.repository / "special")
    elif case == "repository_hardlink":
        os.link(context.repository / "tracked.txt", context.repository / "hardlink")
    elif case == "authority_symlink":
        link = context.final_path.with_name("final-link.json")
        link.symlink_to(context.final_path)
        load_kwargs["authority_path"] = str(link)
    elif case == "authority_wrong_mode":
        os.chmod(context.final_path, 0o644)
    elif case == "authority_unknown_field":
        context.final_payload["UNKNOWN"] = True
        _rewrite_final(context)
    else:
        raise AssertionError(case)

    bundle: authority.ExecutionAuthorityBundle | None = None
    with pytest.raises(authority.ExecutionAuthorityError):
        bundle = authority.load_execution_authority(
            authority_path=load_kwargs.get("authority_path", str(context.final_path)),
            authority_sha256=context.final_sha256,
            plan_path=context.plan_path,
            plan_sha256=load_kwargs.get("plan_sha256", context.plan_sha256),
            plan=context.plan,
            repository_root=context.repository,
        )
        if validate_only:
            bundle.validate_source(
                identity=validate_identity,
                schema_fingerprint=validate_schema,
                parent_identity=context.plan["SOURCE_PARENT_IDENTITY"],
            )
    if bundle is not None:
        bundle.close()


def test_git_environment_does_not_override_repository_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _authority_context(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "untrusted-git-dir"))
    assert authority.exact_repository_provenance(context.repository) == (
        context.final_payload["OPERATIONS_ROOT_HEAD_SHA"],
        context.final_payload["OPERATIONS_ROOT_TREE_SHA"],
    )


def test_no_bytecode_invocation_contract_is_explicit() -> None:
    entrypoint = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "hermes_production_staged_migrate.py"
    )
    source = entrypoint.read_text(encoding="utf-8")
    assert "sys.dont_write_bytecode = True" in source
