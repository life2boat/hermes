#!/usr/bin/env python3
"""Real-root public-entrypoint matrix for migration evidence bindings."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts import hermes_execution_authority as execution_authority
from scripts import hermes_production_staged_migrate as production


TARGET_IMAGE_ID = "sha256:" + "2" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "3" * 64
OTHER_IMAGE_ID = "sha256:" + "9" * 64
OTHER_REVISION = "8" * 40

PLAN_CASES = (
    ("missing_root_approval_path", 2, "ARGUMENT_ERROR"),
    ("missing_root_approval_expected_sha", 2, "ARGUMENT_ERROR"),
    ("missing_policy_path", 2, "ARGUMENT_ERROR"),
    ("missing_policy_expected_sha", 2, "ARGUMENT_ERROR"),
    (
        "root_approval_sha_mismatch",
        1,
        "OPERATIONS_ROOT_APPROVAL_SHA256_MISMATCH",
    ),
    ("policy_sha_mismatch", 1, "CLEAN_START_POLICY_SHA256_MISMATCH"),
    ("root_approval_symlink", 1, "SYMLINK_PATH_REFUSED"),
    ("policy_symlink", 1, "SYMLINK_PATH_REFUSED"),
    (
        "root_approval_gid_not_zero",
        1,
        "OPERATIONS_ROOT_APPROVAL_FILE_METADATA_INVALID",
    ),
    (
        "policy_gid_not_zero",
        1,
        "CLEAN_START_POLICY_FILE_METADATA_INVALID",
    ),
    (
        "root_approval_uid_not_zero",
        1,
        "OPERATIONS_ROOT_APPROVAL_FILE_METADATA_INVALID",
    ),
    (
        "policy_uid_not_zero",
        1,
        "CLEAN_START_POLICY_FILE_METADATA_INVALID",
    ),
    (
        "root_approval_mode_not_0600",
        1,
        "OPERATIONS_ROOT_APPROVAL_FILE_METADATA_INVALID",
    ),
    (
        "policy_mode_not_0600",
        1,
        "CLEAN_START_POLICY_FILE_METADATA_INVALID",
    ),
    (
        "expired_root_approval",
        1,
        "OPERATIONS_ROOT_APPROVAL_EXPIRED",
    ),
    (
        "wrong_target_main_sha",
        1,
        "OPERATIONS_ROOT_APPROVAL_MISMATCH",
    ),
    (
        "wrong_image_id",
        1,
        "OPERATIONS_ROOT_APPROVAL_MISMATCH",
    ),
    (
        "wrong_image_revision",
        1,
        "OPERATIONS_ROOT_APPROVAL_MISMATCH",
    ),
    (
        "wrong_repository_root",
        1,
        "OPERATIONS_ROOT_APPROVAL_MISMATCH",
    ),
    (
        "wrong_repository_tree_sha",
        1,
        "OPERATIONS_ROOT_APPROVAL_MISMATCH",
    ),
    (
        "deployment_contract_sha_mismatch",
        1,
        "OPERATIONS_ROOT_APPROVAL_MISMATCH",
    ),
    (
        "policy_source_sha_mismatch",
        1,
        "CLEAN_START_POLICY_MISMATCH",
    ),
    ("backfill_enabled", 1, "CLEAN_START_POLICY_MISMATCH"),
    (
        "memory_os_preservation_false",
        1,
        "CLEAN_START_POLICY_MISMATCH",
    ),
    (
        "nutrition_diary_preservation_false",
        1,
        "CLEAN_START_POLICY_MISMATCH",
    ),
    (
        "telegram_admin_preservation_false",
        1,
        "CLEAN_START_POLICY_MISMATCH",
    ),
    (
        "out_of_scope_preservation_false",
        1,
        "CLEAN_START_POLICY_MISMATCH",
    ),
    (
        "policy_execution_authorized_true",
        1,
        "CLEAN_START_POLICY_MISMATCH",
    ),
    ("deletion_performed_true", 1, "CLEAN_START_POLICY_MISMATCH"),
)

EXECUTE_CASES = (
    (
        "root_approval_replacement_after_plan",
        1,
        "OPERATIONS_ROOT_APPROVAL_SHA256_MISMATCH",
    ),
    (
        "policy_replacement_after_plan",
        1,
        "CLEAN_START_POLICY_SHA256_MISMATCH",
    ),
    (
        "root_approval_inode_drift",
        1,
        "OPERATIONS_ROOT_APPROVAL_IDENTITY_DRIFT",
    ),
    (
        "policy_inode_drift",
        1,
        "CLEAN_START_POLICY_IDENTITY_DRIFT",
    ),
    (
        "root_approval_group_drift",
        1,
        "OPERATIONS_ROOT_APPROVAL_FILE_METADATA_INVALID",
    ),
    (
        "policy_group_drift",
        1,
        "CLEAN_START_POLICY_FILE_METADATA_INVALID",
    ),
    ("legacy_plan_v1", 1, "PLAN_VERSION_OR_STATE_INVALID"),
    ("legacy_plan_v2", 1, "PLAN_VERSION_OR_STATE_INVALID"),
    ("legacy_plan_v3", 1, "PLAN_VERSION_OR_STATE_INVALID"),
    (
        "root_approval_confirmation_mismatch",
        1,
        "PLAN_OPERATIONS_ROOT_APPROVAL_SHA256_CONFIRMATION_MISMATCH",
    ),
    (
        "policy_confirmation_mismatch",
        1,
        "PLAN_CLEAN_START_POLICY_SHA256_CONFIRMATION_MISMATCH",
    ),
)

NEGATIVE_CASE_COUNT = len(PLAN_CASES) + len(EXECUTE_CASES)


@dataclass(frozen=True)
class CaseContext:
    runtime: Path
    source: Path
    backup: Path
    staging: Path
    evidence: Path
    evidence_inputs: Path
    operations_root_approval: Path
    clean_start_policy: Path


@dataclass
class PlanContext:
    path: Path
    payload: dict[str, Any]
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    os.chown(path, 0, 0)
    return path


def _write_document(path: Path, payload: dict[str, Any]) -> None:
    path.write_bytes(production._canonical_json(payload))
    os.chmod(path, 0o600)
    os.chown(path, 0, 0)


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)
    os.chown(path, 0, 0)


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


def _atomic_replace(path: Path, data: bytes) -> None:
    replacement = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    replacement.write_bytes(data)
    os.chmod(replacement, 0o600)
    os.chown(replacement, 0, 0)
    os.replace(replacement, path)


def _run_git(repository: Path, *arguments: str) -> str:
    environment = dict(os.environ)
    environment.update({
        "GIT_AUTHOR_NAME": "Hermes Test",
        "GIT_AUTHOR_EMAIL": "hermes-test@example.invalid",
        "GIT_COMMITTER_NAME": "Hermes Test",
        "GIT_COMMITTER_EMAIL": "hermes-test@example.invalid",
    })
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError("synthetic repository command failed")
    return result.stdout.strip()


def _copy_repository(source_root: Path, target: Path) -> tuple[str, str]:
    _private_directory(target)
    for relative in (
        Path("deploy/hermes-production.json"),
        Path("deploy/docker-compose.production.yml"),
        Path("docker-compose.yml"),
        Path("scripts/hermes_production_staged_migrate.py"),
        Path("scripts/hermes_staged_schema_migrate.py"),
        Path("docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"),
    ):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
        os.chmod(destination, 0o644)
        os.chown(destination, 0, 0)
    _run_git(target, "init", "--quiet")
    _run_git(target, "add", "--all")
    _run_git(target, "commit", "--quiet", "-m", "synthetic evidence root")
    head = _run_git(target, "rev-parse", "HEAD")
    tree = _run_git(target, "rev-parse", "HEAD^{tree}")
    if len(head) != 40 or len(tree) != 40:
        raise AssertionError("synthetic repository identity invalid")
    return head, tree


def _create_source(path: Path) -> None:
    _private_directory(path.parent)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    os.chmod(path, 0o600)
    os.chown(path, 0, 0)


def _new_context(root: Path, name: str) -> CaseContext:
    runtime = _private_directory(root / name)
    source = runtime / "source" / "database.sqlite"
    _create_source(source)
    backup = _private_directory(runtime / "backup")
    staging = _private_directory(runtime / "staging")
    evidence = _private_directory(runtime / "evidence")
    evidence_inputs = _private_directory(runtime / "evidence-inputs")
    return CaseContext(
        runtime=runtime,
        source=source,
        backup=backup,
        staging=staging,
        evidence=evidence,
        evidence_inputs=evidence_inputs,
        operations_root_approval=(evidence_inputs / "approved-operations-root.json"),
        clean_start_policy=evidence_inputs / "clean-start-data-policy.json",
    )


def _write_valid_evidence(
    context: CaseContext,
    repository: Path,
    revision: str,
    tree: str,
) -> None:
    root_record = production._directory_record(repository, private=True)
    contract_path = repository / production.CANONICAL_CONTRACT_RELATIVE_PATH
    contract_metadata = contract_path.stat()
    created_at = production._now()
    approval = {
        "APPROVAL_VERSION": 1,
        "CREATED_AT": production._timestamp(created_at),
        "EXPIRES_AT": production._timestamp(created_at + timedelta(hours=1)),
        "TARGET_MAIN_SHA": revision,
        "APPROVED_REPOSITORY_ROOT": str(repository),
        "REPOSITORY_ROOT_DEVICE": root_record["DEVICE"],
        "REPOSITORY_ROOT_INODE": root_record["INODE"],
        "REPOSITORY_ROOT_UID": root_record["UID"],
        "REPOSITORY_ROOT_GID": root_record["GID"],
        "REPOSITORY_ROOT_MODE": root_record["MODE"],
        "REPOSITORY_ROOT_TREE_SHA": tree,
        "DEPLOYMENT_CONTRACT_PATH": str(contract_path),
        "DEPLOYMENT_CONTRACT_DEVICE": contract_metadata.st_dev,
        "DEPLOYMENT_CONTRACT_INODE": contract_metadata.st_ino,
        "DEPLOYMENT_CONTRACT_SHA256": _sha256(contract_path),
        "PRODUCTION_MIGRATION_ENTRYPOINT_SHA256": _sha256(
            repository / "scripts/hermes_production_staged_migrate.py"
        ),
        "STAGED_IMPLEMENTATION_SHA256": _sha256(
            repository / "scripts/hermes_staged_schema_migrate.py"
        ),
        "RUNBOOK_SHA256": _sha256(
            repository / "docs/runbooks/"
            "RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
        ),
        "MIGRATION_IMAGE_ID": TARGET_IMAGE_ID,
        "MIGRATION_IMAGE_REVISION": revision,
        "DIRTY_LEGACY_ROOT_PRESERVED": True,
        "PRODUCTION_DB_ACCESS_AUTHORIZED": False,
        "PRODUCTION_PLAN_ONLY_AUTHORIZED": True,
        "PRODUCTION_EXECUTE_AUTHORIZED": False,
        "DEPLOY_AUTHORIZED": False,
    }
    policy = {
        "POLICY_VERSION": 1,
        "DATA_POLICY": "NO_CLIENTS_CLEAN_START",
        "CREATED_AT": production._timestamp(created_at),
        "TARGET_MAIN_SHA": revision,
        "MIGRATION_IMAGE_ID": TARGET_IMAGE_ID,
        "PRODUCTION_DB_SOURCE_SHA256": _sha256(context.source),
        "FAMILY_SHOPPING_BACKFILL_REQUIRED": False,
        "LEGACY_FAMILY_SHOPPING_DATA_MAY_BE_RESET": True,
        "MEMORY_OS_DATA_MUST_BE_PRESERVED": True,
        "NUTRITION_DIARY_DATA_MUST_BE_PRESERVED": True,
        "TELEGRAM_ADMIN_CONFIGURATION_MUST_BE_PRESERVED": True,
        "OUT_OF_SCOPE_TABLES_MUST_BE_PRESERVED": True,
        "EXECUTION_AUTHORIZED": False,
        "DELETION_PERFORMED": False,
    }
    _write_document(context.operations_root_approval, approval)
    _write_document(context.clean_start_policy, policy)


def _plan_argv(
    context: CaseContext,
    repository: Path,
    revision: str,
) -> list[str]:
    metadata = context.source.stat()
    return [
        "plan",
        "--repository-root",
        str(repository),
        "--db-path",
        str(context.source),
        "--backup-parent",
        str(context.backup),
        "--staging-parent",
        str(context.staging),
        "--evidence-parent",
        str(context.evidence),
        "--operations-root-approval",
        str(context.operations_root_approval),
        "--expected-operations-root-approval-sha256",
        _sha256(context.operations_root_approval),
        "--clean-start-policy",
        str(context.clean_start_policy),
        "--expected-clean-start-policy-sha256",
        _sha256(context.clean_start_policy),
        "--migration-image-id",
        TARGET_IMAGE_ID,
        "--migration-image-revision",
        revision,
        "--previous-image-id",
        PREVIOUS_IMAGE_ID,
        "--expected-hostname",
        socket.gethostname(),
        "--expected-source-device",
        str(metadata.st_dev),
        "--expected-source-inode",
        str(metadata.st_ino),
        "--expected-source-size",
        str(metadata.st_size),
        "--expected-source-sha256",
        _sha256(context.source),
        "--expected-free-bytes",
        "1",
        "--expires-in-seconds",
        "3600",
    ]


def _execute_argv(
    plan: PlanContext,
    *,
    final_authority_path: Path,
    final_authority_sha256: str,
    **overrides: str,
) -> list[str]:
    values = {
        "expected_plan_sha256": plan.sha256,
        "confirm_operation_id": str(plan.payload["OPERATION_ID"]),
        "confirm_source_sha256": str(plan.payload["SOURCE_SHA256"]),
        "confirm_image_revision": str(plan.payload["MIGRATION_IMAGE_REVISION"]),
        "confirm_operations_root_approval_sha256": str(
            plan.payload["OPERATIONS_ROOT_APPROVAL_SHA256"]
        ),
        "confirm_clean_start_policy_sha256": str(
            plan.payload["CLEAN_START_POLICY_SHA256"]
        ),
    }
    values.update(overrides)
    return [
        "execute",
        "--plan",
        str(plan.path),
        "--final-authority",
        str(final_authority_path),
        "--expected-final-authority-sha256",
        final_authority_sha256,
        "--expected-plan-sha256",
        values["expected_plan_sha256"],
        "--confirm-operation-id",
        values["confirm_operation_id"],
        "--confirm-source-sha256",
        values["confirm_source_sha256"],
        "--confirm-image-revision",
        values["confirm_image_revision"],
        "--confirm-operations-root-approval-sha256",
        values["confirm_operations_root_approval_sha256"],
        "--confirm-clean-start-policy-sha256",
        values["confirm_clean_start_policy_sha256"],
    ]


def _public_main(argv: list[str]) -> tuple[int, dict[str, Any]]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        return_code = production.main(argv)
    payloads: list[dict[str, Any]] = []
    for stream in (stdout.getvalue(), stderr.getvalue()):
        for line in stream.splitlines():
            if line.strip():
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise AssertionError("public result is not an object")
                payloads.append(payload)
    if len(payloads) != 1:
        raise AssertionError("public entrypoint emitted unexpected output")
    return return_code, payloads[0]


def _create_plan(
    context: CaseContext,
    repository: Path,
    revision: str,
) -> PlanContext:
    return_code, result = _public_main(_plan_argv(context, repository, revision))
    if return_code != 0 or result.get("status") != "PASS":
        raise AssertionError("synthetic public plan failed")
    path = Path(str(result["plan_path"]))
    payload = json.loads(path.read_text(encoding="ascii"))
    return PlanContext(
        path=path,
        payload=payload,
        sha256=str(result["plan_sha256"]),
    )


def _create_final_authority(
    context: CaseContext,
    repository: Path,
    plan: PlanContext,
) -> tuple[Path, str]:
    artifacts = context.evidence_inputs
    revision = str(plan.payload["MIGRATION_IMAGE_REVISION"])
    tree = _run_git(repository, "rev-parse", "HEAD^{tree}")
    created_at = production._now()
    base_compose = repository / "docker-compose.yml"
    p5b = artifacts / "p5b-evidence.md"
    p6a_f1 = artifacts / "p6a-f1-evidence.md"
    override = artifacts / "production-db-override.yml"
    secret = artifacts / "secrets-override.yml"
    descriptor = artifacts / "invocation-descriptor.json"
    envelope = artifacts / "approval-envelope.json"
    final = artifacts / "final-authority.json"

    _write_bytes(p5b, b"synthetic p5b evidence\n")
    _write_bytes(p6a_f1, b"synthetic p6a-f1 evidence\n")
    _write_document(
        override,
        {
            "services": {
                "hermes-bot": {
                    "volumes": [
                        {
                            "bind": {"create_host_path": True},
                            "source": str(context.source),
                            "target": "/home/hermes/healbite.db",
                            "type": "bind",
                        }
                    ]
                }
            }
        },
    )
    _write_bytes(secret, b"services: {}\n")
    _write_document(
        descriptor,
        {
            "DESCRIPTOR_VERSION": (execution_authority.INVOCATION_DESCRIPTOR_VERSION),
            "CREATED_AT": production._timestamp(created_at),
            "COMPOSE_PROJECT_NAME": "hermes-agent",
            "PROJECT_DIRECTORY": str(repository),
            "COMPOSE_FILE_ORDER": [
                str(base_compose),
                str(override),
                str(secret),
            ],
            "NON_SECRET_COMPOSE_SHA256": {
                str(base_compose): _sha256(base_compose),
                str(override): _sha256(override),
            },
            "SECRETS_OVERRIDE": _secret_identity(secret),
            "ENVIRONMENT_SOURCE_CLASS": ("EXISTING_PRODUCTION_ENV_FILE_METADATA_ONLY"),
            "APPLICATION_SERVICE": "hermes-bot",
            "CANONICAL_DB_SOURCE": str(context.source),
            "CANONICAL_DB_TARGET": "/home/hermes/healbite.db",
            "CURRENT_PRODUCTION_IMAGE_ID": PREVIOUS_IMAGE_ID,
            "TARGET_IMAGE_ID": TARGET_IMAGE_ID,
            "SOURCE_SHA": revision,
            "TREE_SHA": tree,
            "CONTAINS_SECRET_VALUES": False,
        },
    )
    root_metadata = repository.stat()
    _write_document(
        envelope,
        {
            "ENVELOPE_VERSION": 1,
            "CREATED_AT": production._timestamp(created_at),
            "PUBLIC_OPERATIONS_ROOT_APPROVAL_PATH": str(
                context.operations_root_approval
            ),
            "PUBLIC_OPERATIONS_ROOT_APPROVAL_SHA256": _sha256(
                context.operations_root_approval
            ),
            "OPERATIONS_ROOT_PATH": str(repository),
            "OPERATIONS_ROOT_HEAD_SHA": revision,
            "OPERATIONS_ROOT_TREE_SHA": tree,
            "OPERATIONS_ROOT_MODE": stat.S_IMODE(root_metadata.st_mode),
            "OPERATIONS_ROOT_UID": int(root_metadata.st_uid),
            "OPERATIONS_ROOT_GID": int(root_metadata.st_gid),
            "OPERATIONS_ROOT_CLEAN": True,
            "OBJECT_ALTERNATES_ABSENT": True,
            "P5B_EVIDENCE_SHA256": _sha256(p5b),
            "P6A_F1_EVIDENCE_SHA256": _sha256(p6a_f1),
            "EXACT_MAIN_IMAGE_ID": TARGET_IMAGE_ID,
            "CANONICAL_DB_PATH": str(context.source),
            "CANONICAL_DB_DEVICE": int(plan.payload["SOURCE_DEVICE"]),
            "CANONICAL_DB_INODE": int(plan.payload["SOURCE_INODE"]),
            "CANONICAL_DB_SIZE": int(plan.payload["SOURCE_SIZE"]),
            "CANONICAL_DB_SHA256": str(plan.payload["SOURCE_SHA256"]),
            "PERSISTENT_DB_OVERRIDE_SHA256": _sha256(override),
            "INVOCATION_DESCRIPTOR_SHA256": _sha256(descriptor),
            "CLEAN_START_POLICY_SHA256": _sha256(context.clean_start_policy),
            "PLAN_ONLY_AUTHORIZED": True,
            "EXECUTION_AUTHORIZED": False,
            "DEPLOY_AUTHORIZED": False,
            "CONTAINS_SECRETS": False,
        },
    )
    _write_document(
        final,
        {
            "EXECUTION_AUTHORITY_VERSION": (
                execution_authority.EXECUTION_AUTHORITY_VERSION
            ),
            "CREATED_AT": production._timestamp(created_at),
            "EXPIRES_AT": production._timestamp(created_at + timedelta(hours=1)),
            "PLAN_PATH": str(plan.path),
            "PLAN_SHA256": plan.sha256,
            "OPERATIONS_ROOT_APPROVAL_PATH": str(context.operations_root_approval),
            "OPERATIONS_ROOT_APPROVAL_SHA256": _sha256(
                context.operations_root_approval
            ),
            "CLEAN_START_POLICY_PATH": str(context.clean_start_policy),
            "CLEAN_START_POLICY_SHA256": _sha256(context.clean_start_policy),
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
            "SOURCE_SHA": revision,
            "SOURCE_TREE_SHA": tree,
            "TARGET_IMAGE_ID": TARGET_IMAGE_ID,
            "CURRENT_RUNTIME_IMAGE_ID": PREVIOUS_IMAGE_ID,
            "CANONICAL_PRODUCTION_DB_PATH": str(context.source),
            "SOURCE_DB_SHA256": str(plan.payload["SOURCE_SHA256"]),
            "SOURCE_DB_SIZE": int(plan.payload["SOURCE_SIZE"]),
            "SOURCE_DB_USER_VERSION": int(plan.payload["SOURCE_USER_VERSION"]),
            "SOURCE_DB_SCHEMA_FINGERPRINT": str(
                plan.payload["SOURCE_SCHEMA_FINGERPRINT"]
            ),
            "SOURCE_DB_PARENT_IDENTITY": plan.payload["SOURCE_PARENT_IDENTITY"],
            "OPERATIONS_ROOT_PATH": str(repository),
            "OPERATIONS_ROOT_HEAD_SHA": revision,
            "OPERATIONS_ROOT_TREE_SHA": tree,
            "EXECUTION_AUTHORIZED": True,
            "DEPLOY_AUTHORIZED": False,
            "CONTAINS_SECRETS": False,
        },
    )
    return final, _sha256(final)


def _rewrite_plan(plan: PlanContext) -> None:
    production._write_json_durable(plan.path, plan.payload)
    plan.sha256 = _sha256(plan.path)


def _database_snapshot(path: Path) -> tuple[int, int, int, int, str]:
    metadata = path.stat()
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        _sha256(path),
    )


def _tree_snapshot(context: CaseContext) -> dict[str, tuple[Any, ...]]:
    roots = {
        "source": context.source.parent,
        "backup": context.backup,
        "staging": context.staging,
        "evidence": context.evidence,
        "evidence_inputs": context.evidence_inputs,
    }
    snapshot: dict[str, tuple[Any, ...]] = {}
    for label, root in roots.items():
        paths = [root, *sorted(root.rglob("*"))]
        for path in paths:
            metadata = path.lstat()
            relative = "." if path == root else str(path.relative_to(root))
            digest = _sha256(path) if stat.S_ISREG(metadata.st_mode) else ""
            link = os.readlink(path) if stat.S_ISLNK(metadata.st_mode) else ""
            snapshot[f"{label}:{relative}"] = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                int(metadata.st_mode),
                int(metadata.st_uid),
                int(metadata.st_gid),
                int(metadata.st_size),
                int(metadata.st_mtime_ns),
                digest,
                link,
            )
    return snapshot


def _remove_option(argv: list[str], option: str) -> None:
    index = argv.index(option)
    del argv[index : index + 2]


def _replace_option(argv: list[str], option: str, value: str) -> None:
    argv[argv.index(option) + 1] = value


def _mutate_document(path: Path, field: str, value: Any) -> None:
    payload = json.loads(path.read_text(encoding="ascii"))
    payload[field] = value
    _write_document(path, payload)


def _prepare_plan_case(
    case: str,
    context: CaseContext,
    repository: Path,
    revision: str,
) -> list[str]:
    if case == "expired_root_approval":
        _mutate_document(
            context.operations_root_approval,
            "CREATED_AT",
            production._timestamp(production._now() - timedelta(hours=2)),
        )
        _mutate_document(
            context.operations_root_approval,
            "EXPIRES_AT",
            production._timestamp(production._now() - timedelta(hours=1)),
        )
    elif case == "wrong_target_main_sha":
        _mutate_document(
            context.operations_root_approval,
            "TARGET_MAIN_SHA",
            "7" * 40,
        )
    elif case == "wrong_image_id":
        _mutate_document(
            context.operations_root_approval,
            "MIGRATION_IMAGE_ID",
            OTHER_IMAGE_ID,
        )
    elif case == "wrong_image_revision":
        _mutate_document(
            context.operations_root_approval,
            "MIGRATION_IMAGE_REVISION",
            OTHER_REVISION,
        )
    elif case == "wrong_repository_root":
        _mutate_document(
            context.operations_root_approval,
            "APPROVED_REPOSITORY_ROOT",
            str(context.runtime / "not-approved"),
        )
    elif case == "wrong_repository_tree_sha":
        _mutate_document(
            context.operations_root_approval,
            "REPOSITORY_ROOT_TREE_SHA",
            "6" * 40,
        )
    elif case == "deployment_contract_sha_mismatch":
        _mutate_document(
            context.operations_root_approval,
            "DEPLOYMENT_CONTRACT_SHA256",
            "5" * 64,
        )
    elif case == "policy_source_sha_mismatch":
        _mutate_document(
            context.clean_start_policy,
            "PRODUCTION_DB_SOURCE_SHA256",
            "4" * 64,
        )
    elif case == "backfill_enabled":
        _mutate_document(
            context.clean_start_policy,
            "FAMILY_SHOPPING_BACKFILL_REQUIRED",
            True,
        )
    elif case == "memory_os_preservation_false":
        _mutate_document(
            context.clean_start_policy,
            "MEMORY_OS_DATA_MUST_BE_PRESERVED",
            False,
        )
    elif case == "nutrition_diary_preservation_false":
        _mutate_document(
            context.clean_start_policy,
            "NUTRITION_DIARY_DATA_MUST_BE_PRESERVED",
            False,
        )
    elif case == "telegram_admin_preservation_false":
        _mutate_document(
            context.clean_start_policy,
            "TELEGRAM_ADMIN_CONFIGURATION_MUST_BE_PRESERVED",
            False,
        )
    elif case == "out_of_scope_preservation_false":
        _mutate_document(
            context.clean_start_policy,
            "OUT_OF_SCOPE_TABLES_MUST_BE_PRESERVED",
            False,
        )
    elif case == "policy_execution_authorized_true":
        _mutate_document(
            context.clean_start_policy,
            "EXECUTION_AUTHORIZED",
            True,
        )
    elif case == "deletion_performed_true":
        _mutate_document(
            context.clean_start_policy,
            "DELETION_PERFORMED",
            True,
        )
    elif case == "root_approval_symlink":
        target = context.operations_root_approval.with_suffix(".target")
        os.replace(context.operations_root_approval, target)
        os.symlink(target.name, context.operations_root_approval)
    elif case == "policy_symlink":
        target = context.clean_start_policy.with_suffix(".target")
        os.replace(context.clean_start_policy, target)
        os.symlink(target.name, context.clean_start_policy)
    elif case == "root_approval_gid_not_zero":
        os.chown(context.operations_root_approval, 0, 1)
    elif case == "policy_gid_not_zero":
        os.chown(context.clean_start_policy, 0, 1)
    elif case == "root_approval_uid_not_zero":
        os.chown(context.operations_root_approval, 1, 0)
    elif case == "policy_uid_not_zero":
        os.chown(context.clean_start_policy, 1, 0)
    elif case == "root_approval_mode_not_0600":
        os.chmod(context.operations_root_approval, 0o640)
    elif case == "policy_mode_not_0600":
        os.chmod(context.clean_start_policy, 0o640)

    argv = _plan_argv(context, repository, revision)
    if case == "missing_root_approval_path":
        _remove_option(argv, "--operations-root-approval")
    elif case == "missing_root_approval_expected_sha":
        _remove_option(argv, "--expected-operations-root-approval-sha256")
    elif case == "missing_policy_path":
        _remove_option(argv, "--clean-start-policy")
    elif case == "missing_policy_expected_sha":
        _remove_option(argv, "--expected-clean-start-policy-sha256")
    elif case == "root_approval_sha_mismatch":
        _replace_option(
            argv,
            "--expected-operations-root-approval-sha256",
            "f" * 64,
        )
    elif case == "policy_sha_mismatch":
        _replace_option(
            argv,
            "--expected-clean-start-policy-sha256",
            "f" * 64,
        )
    return argv


def _prepare_execute_case(
    case: str,
    context: CaseContext,
    repository: Path,
    revision: str,
) -> list[str]:
    global _ACTIVE_SOURCE
    _ACTIVE_SOURCE = context.source
    plan = _create_plan(context, repository, revision)
    overrides: dict[str, str] = {}
    authority_before_mutation = case not in {
        "legacy_plan_v1",
        "legacy_plan_v2",
        "legacy_plan_v3",
    }
    if authority_before_mutation:
        final_path, final_sha256 = _create_final_authority(context, repository, plan)
    if case == "root_approval_replacement_after_plan":
        payload = json.loads(
            context.operations_root_approval.read_text(encoding="ascii")
        )
        payload["DEPLOY_AUTHORIZED"] = True
        _atomic_replace(
            context.operations_root_approval,
            production._canonical_json(payload),
        )
    elif case == "policy_replacement_after_plan":
        payload = json.loads(context.clean_start_policy.read_text(encoding="ascii"))
        payload["DELETION_PERFORMED"] = True
        _atomic_replace(
            context.clean_start_policy,
            production._canonical_json(payload),
        )
    elif case == "root_approval_inode_drift":
        _atomic_replace(
            context.operations_root_approval,
            context.operations_root_approval.read_bytes(),
        )
    elif case == "policy_inode_drift":
        _atomic_replace(
            context.clean_start_policy,
            context.clean_start_policy.read_bytes(),
        )
    elif case == "root_approval_group_drift":
        os.chown(context.operations_root_approval, 0, 1)
    elif case == "policy_group_drift":
        os.chown(context.clean_start_policy, 0, 1)
    elif case == "legacy_plan_v1":
        plan.payload["PLAN_VERSION"] = 1
        _rewrite_plan(plan)
    elif case == "legacy_plan_v2":
        plan.payload["PLAN_VERSION"] = 2
        _rewrite_plan(plan)
    elif case == "legacy_plan_v3":
        plan.payload["PLAN_VERSION"] = 3
        _rewrite_plan(plan)
    elif case == "root_approval_confirmation_mismatch":
        overrides["confirm_operations_root_approval_sha256"] = "f" * 64
    elif case == "policy_confirmation_mismatch":
        overrides["confirm_clean_start_policy_sha256"] = "f" * 64
    else:
        raise AssertionError(f"unknown execute case: {case}")
    if not authority_before_mutation:
        final_path, final_sha256 = _create_final_authority(
            context,
            repository,
            plan,
        )
    return _execute_argv(
        plan,
        final_authority_path=final_path,
        final_authority_sha256=final_sha256,
        **overrides,
    )


def _assert_rejection(
    case: str,
    expected_return_code: int,
    expected_class: str,
    context: CaseContext,
    argv: list[str],
    prepare_calls_before: int,
) -> None:
    database_before = _database_snapshot(context.source)
    filesystem_before = _tree_snapshot(context)
    return_code, result = _public_main(argv)
    database_after = _database_snapshot(context.source)
    filesystem_after = _tree_snapshot(context)
    if return_code != expected_return_code:
        raise AssertionError(f"{case}: unexpected exit code")
    if result.get("exit_classification") != expected_class:
        raise AssertionError(f"{case}: unexpected exit classification")
    if result.get("status") != "FAILED":
        raise AssertionError(f"{case}: failure status missing")
    if result.get("publish_state") != "BEFORE_EXCHANGE":
        raise AssertionError(f"{case}: unsafe publish state")
    if result.get("target_may_have_changed") is not False:
        raise AssertionError(f"{case}: target uncertainty reported")
    if result.get("automatic_retry_allowed") is not False:
        raise AssertionError(f"{case}: retry incorrectly allowed")
    if result.get("database_mutated") not in (None, False):
        raise AssertionError(f"{case}: database mutation reported")
    if result.get("durable_evidence_updated") not in (None, False):
        raise AssertionError(f"{case}: execution evidence reported")
    if database_after != database_before:
        raise AssertionError(f"{case}: database identity changed")
    if filesystem_after != filesystem_before:
        raise AssertionError(f"{case}: execution filesystem changed")
    if _PREPARE_CALLS != prepare_calls_before:
        raise AssertionError(f"{case}: migration preparation reached")
    if any(path.name == "execution.json" for path in context.evidence.rglob("*")):
        raise AssertionError(f"{case}: execution evidence created")


def _positive_plan_control(
    root: Path,
    repository: Path,
    revision: str,
    tree: str,
) -> None:
    context = _new_context(root, "positive-control")
    _write_valid_evidence(context, repository, revision, tree)
    database_before = _database_snapshot(context.source)
    plan = _create_plan(context, repository, revision)
    if _database_snapshot(context.source) != database_before:
        raise AssertionError("positive plan mutated the source database")
    expected = {
        "OPERATIONS_ROOT_APPROVAL_UID": 0,
        "OPERATIONS_ROOT_APPROVAL_GID": 0,
        "OPERATIONS_ROOT_APPROVAL_MODE": 0o600,
        "CLEAN_START_POLICY_UID": 0,
        "CLEAN_START_POLICY_GID": 0,
        "CLEAN_START_POLICY_MODE": 0o600,
    }
    if any(plan.payload.get(name) != value for name, value in expected.items()):
        raise AssertionError("positive plan did not bind root:root evidence")
    if list(context.backup.iterdir()) or list(context.staging.iterdir()):
        raise AssertionError("positive plan created execution artifacts")


_ACTIVE_REVISION = ""
_ACTIVE_SOURCE: Path | None = None
_PREPARE_CALLS = 0


def _strict_image_inspect(
    image_id: str,
    expected_revision: str | None = None,
) -> str:
    if image_id == TARGET_IMAGE_ID and expected_revision == _ACTIVE_REVISION:
        return _ACTIVE_REVISION
    if image_id == PREVIOUS_IMAGE_ID and expected_revision is None:
        return _ACTIVE_REVISION
    raise production.ProductionGateError("IMAGE_NOT_AVAILABLE")


def _synthetic_runtime(service_name: str) -> dict[str, Any]:
    if service_name != "hermes-bot" or _ACTIVE_SOURCE is None:
        raise execution_authority.ExecutionAuthorityError("CURRENT_RUNTIME_UNAVAILABLE")
    return {
        "State": {"Running": True},
        "Image": PREVIOUS_IMAGE_ID,
        "Mounts": [
            {
                "Source": str(_ACTIVE_SOURCE),
                "Destination": "/home/hermes/healbite.db",
            }
        ],
    }


def _forbid_migration_prepare(*_args: Any, **_kwargs: Any) -> Any:
    global _PREPARE_CALLS
    _PREPARE_CALLS += 1
    raise AssertionError("negative evidence case reached migration preparation")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", required=True)
    return parser.parse_args()


def main() -> int:
    global _ACTIVE_REVISION
    args = _arguments()
    if os.geteuid() != 0 or os.getegid() != 0:  # windows-footgun: ok
        raise AssertionError("evidence matrix requires real root:root")
    source_root = Path(args.repository_root).resolve(strict=True)
    original_repository_root = production.REPO_ROOT
    original_inspect = production._inspect_image
    original_prepare = production._prepare_authorized_production_execution
    original_authority_inspect = execution_authority._inspect_image
    original_runtime_inspect = execution_authority._inspect_runtime
    with tempfile.TemporaryDirectory(prefix="hermes-evidence-matrix-") as value:
        root = Path(value)
        os.chmod(root, 0o700)
        repository = root / "repository"
        revision, tree = _copy_repository(source_root, repository)
        production.REPO_ROOT = repository
        production._inspect_image = _strict_image_inspect
        production._prepare_authorized_production_execution = _forbid_migration_prepare
        _ACTIVE_REVISION = revision
        execution_authority._inspect_image = _strict_image_inspect
        execution_authority._inspect_runtime = _synthetic_runtime
        try:
            for case, expected_code, expected_class in PLAN_CASES:
                context = _new_context(root, f"plan-{case}")
                _write_valid_evidence(context, repository, revision, tree)
                argv = _prepare_plan_case(
                    case,
                    context,
                    repository,
                    revision,
                )
                _assert_rejection(
                    case,
                    expected_code,
                    expected_class,
                    context,
                    argv,
                    _PREPARE_CALLS,
                )
            for case, expected_code, expected_class in EXECUTE_CASES:
                context = _new_context(root, f"execute-{case}")
                _write_valid_evidence(context, repository, revision, tree)
                argv = _prepare_execute_case(
                    case,
                    context,
                    repository,
                    revision,
                )
                _assert_rejection(
                    case,
                    expected_code,
                    expected_class,
                    context,
                    argv,
                    _PREPARE_CALLS,
                )
            _positive_plan_control(root, repository, revision, tree)
            bytecode_sidecars = [
                path
                for path in repository.rglob("*")
                if path.name == "__pycache__" or path.suffix == ".pyc"
            ]
            if bytecode_sidecars:
                raise AssertionError("plan or execute created bytecode sidecars")
        finally:
            production.REPO_ROOT = original_repository_root
            production._inspect_image = original_inspect
            production._prepare_authorized_production_execution = original_prepare
            execution_authority._inspect_image = original_authority_inspect
            execution_authority._inspect_runtime = original_runtime_inspect
    print(
        json.dumps(
            {
                "automatic_retry_allowed": False,
                "cases_with_missing_delta_assertions": 0,
                "database_delta_asserted_per_case": True,
                "filesystem_delta_asserted_per_case": True,
                "fstat_monkeypatched": False,
                "gid_check_from_pinned_fd": True,
                "migration_container_started": False,
                "no_bytecode_files_created": True,
                "negative_evidence_cases": NEGATIVE_CASE_COUNT,
                "production_database_used": False,
                "public_execute_main_used": True,
                "public_plan_main_used": True,
                "root_identity_monkeypatched": False,
                "secure_loader_real": True,
                "status": "PASS",
                "synthetic_database_only": True,
                "target_may_have_changed": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
