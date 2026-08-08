#!/usr/bin/env python3
"""Canonical producer for production staged-migration authority packages."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

from scripts import hermes_execution_authority as execution
from scripts import hermes_production_staged_migrate as migration


PLAN_ONLY_CONFIRMATION = "PREPARE_PLAN_ONLY_AUTHORITY"
CLEAN_START_CONFIRMATION = "CONFIRM_NO_CLIENTS_CLEAN_START"
FINAL_AUTHORITY_CONFIRMATION = "AUTHORIZE_EXACT_PLAN_EXECUTION"


def _artifact_sha256(path: Path) -> str:
    return migration._sha256(path)


def _artifact_result(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _artifact_sha256(path)}


def _timestamp(value: Any) -> str:
    return migration._timestamp(value)


def _operation_components() -> list[str]:
    return [str(item["component"]) for item in migration._target_migration_registry()]


def _require_exact_components(values: Sequence[str]) -> list[str]:
    expected = _operation_components()
    if list(values) != expected:
        raise migration.ProductionGateError("MIGRATION_COMPONENT_SELECTION_MISMATCH")
    return expected


def _outside_repository(path: Path, repository_root: Path) -> None:
    try:
        path.relative_to(repository_root)
    except ValueError:
        return
    raise migration.ProductionGateError(
        "AUTHORITY_ARTIFACT_INSIDE_OPERATIONS_ROOT_DENIED"
    )


def _private_parent(value: str, *, repository_root: Path) -> Path:
    parent = migration._absolute_path(value, "AUTHORITY_PARENT")
    migration._directory_record(parent, private=True)
    _outside_repository(parent, repository_root)
    return parent


def _authority_directory(
    value: str,
    *,
    operation_id: str,
    repository_root: Path,
) -> Path:
    path = migration._absolute_path(value, "AUTHORITY_DIRECTORY")
    if path.name != operation_id:
        raise migration.ProductionGateError("AUTHORITY_OPERATION_DIRECTORY_MISMATCH")
    migration._directory_record(path, private=True)
    _outside_repository(path, repository_root)
    return path


def _create_authority_directory(
    parent: Path,
    *,
    operation_id: str,
    repository_root: Path,
) -> Path:
    if migration.OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise migration.ProductionGateError("OPERATION_ID_INVALID")
    directory = parent / operation_id
    _outside_repository(directory, repository_root)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(parent, flags)
    try:
        try:
            os.mkdir(operation_id, 0o700, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise migration.ProductionGateError(
                "AUTHORITY_OPERATION_DIRECTORY_COLLISION"
            ) from exc
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    os.chmod(directory, 0o700)
    migration._assert_root_private_directory(
        directory,
        "AUTHORITY_OPERATION_DIRECTORY_UNSAFE",
    )
    return directory


def _write_new_json(
    path: Path,
    payload: dict[str, Any],
    *,
    fields: frozenset[str],
) -> str:
    if set(payload) != fields:
        raise migration.ProductionGateError("AUTHORITY_PRODUCER_SCHEMA_DRIFT")
    encoded = migration._canonical_json(payload)
    parent_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = os.open(path.parent, parent_flags)
    temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary_created = False
    try:
        fd = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        temporary_created = True
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        try:
            os.link(
                temporary,
                path.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            raise migration.ProductionGateError("AUTHORITY_ARTIFACT_COLLISION") from exc
        os.unlink(temporary, dir_fd=parent_fd)
        temporary_created = False
        os.fsync(parent_fd)
        metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != len(encoded)
        ):
            raise migration.ProductionGateError("AUTHORITY_ARTIFACT_METADATA_INVALID")
    finally:
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
    return hashlib.sha256(encoded).hexdigest()


def _open_bound_input(
    path: str,
    expected_sha256: str,
    *,
    code_prefix: str,
) -> execution.BoundArtifact:
    try:
        return execution._open_bound_artifact(
            path,
            expected_sha256,
            code_prefix=code_prefix,
        )
    except execution.ExecutionAuthorityError as exc:
        raise migration.ProductionGateError(exc.code) from exc


def _secret_identity(
    artifact: execution.BoundArtifact,
) -> dict[str, int | str]:
    return {
        "PATH": str(artifact.path),
        "DEVICE": artifact.identity[0],
        "INODE": artifact.identity[1],
        "SIZE": artifact.identity[2],
        "UID": artifact.identity[3],
        "GID": artifact.identity[4],
        "MODE": artifact.identity[5],
        "SHA256": artifact.sha256,
    }


def prepare_initial_authority(args: argparse.Namespace) -> int:
    migration._root_identity()
    if args.confirm_plan_only_authority != PLAN_ONLY_CONFIRMATION:
        raise migration.ProductionGateError("PLAN_ONLY_OPERATOR_AUTHORIZATION_REQUIRED")
    if args.confirm_clean_start_policy != CLEAN_START_CONFIRMATION:
        raise migration.ProductionGateError(
            "CLEAN_START_OPERATOR_CONFIRMATION_REQUIRED"
        )
    if args.expires_in_seconds < 60 or args.expires_in_seconds > 86400:
        raise migration.ProductionGateError("OPERATIONS_ROOT_APPROVAL_EXPIRY_INVALID")

    repository_root = migration._canonical_repository_root(args.repository_root)
    operation_id = args.operation_id
    if migration.OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise migration.ProductionGateError("OPERATION_ID_INVALID")
    components = _require_exact_components(args.migration_component)
    db_path = migration._absolute_path(args.db_path, "DB_PATH")
    if migration.SHA_RE.fullmatch(args.expected_source_sha256) is None:
        raise migration.ProductionGateError("EXPECTED_SOURCE_SHA256_INVALID")
    migration._inspect_image(
        args.migration_image_id,
        args.migration_image_revision,
    )
    identity, _schema, integrity, foreign_keys = migration._read_only_source(db_path)
    if (
        identity["SOURCE_SHA256"] != args.expected_source_sha256
        or integrity != "ok"
        or foreign_keys != 0
    ):
        raise migration.ProductionGateError("AUTHORITY_SOURCE_DATABASE_MISMATCH")

    authority_parent = _private_parent(
        args.authority_parent,
        repository_root=repository_root,
    )
    deployment_contract: migration.PinnedDeploymentContract | None = None
    approval: migration.PinnedEvidenceDocument | None = None
    policy: migration.PinnedEvidenceDocument | None = None
    try:
        deployment_contract = migration._open_canonical_deployment_contract(
            repository_root
        )
        head, tree = migration._repository_provenance(repository_root)
        if head != args.migration_image_revision:
            raise migration.ProductionGateError("AUTHORITY_SOURCE_REVISION_MISMATCH")
        canonical = migration._canonical_repository_binding(
            repository_root,
            head=head,
        )
        root_record = migration._directory_record(
            repository_root,
            private=False,
        )
        directory = _create_authority_directory(
            authority_parent,
            operation_id=operation_id,
            repository_root=repository_root,
        )
        created_at = migration._now()
        approval_payload: dict[str, Any] = {
            "APPROVAL_VERSION": migration.OPERATIONS_ROOT_APPROVAL_VERSION,
            "OPERATION_ID": operation_id,
            "OPERATION_CLASS": migration.AUTHORITY_OPERATION_CLASS,
            "CREATED_AT": _timestamp(created_at),
            "EXPIRES_AT": _timestamp(
                created_at + timedelta(seconds=args.expires_in_seconds)
            ),
            **canonical,
            "TARGET_MAIN_SHA": head,
            "MIGRATION_COMPONENTS": components,
            "APPROVED_REPOSITORY_ROOT": str(repository_root),
            "REPOSITORY_ROOT_DEVICE": root_record["DEVICE"],
            "REPOSITORY_ROOT_INODE": root_record["INODE"],
            "REPOSITORY_ROOT_UID": root_record["UID"],
            "REPOSITORY_ROOT_GID": root_record["GID"],
            "REPOSITORY_ROOT_MODE": root_record["MODE"],
            "REPOSITORY_ROOT_TREE_SHA": tree,
            "DEPLOYMENT_CONTRACT_PATH": str(deployment_contract.path),
            "DEPLOYMENT_CONTRACT_DEVICE": deployment_contract.device,
            "DEPLOYMENT_CONTRACT_INODE": deployment_contract.inode,
            "DEPLOYMENT_CONTRACT_SHA256": deployment_contract.sha256,
            "PRODUCTION_MIGRATION_ENTRYPOINT_SHA256": migration._sha256(
                repository_root / "scripts/hermes_production_staged_migrate.py"
            ),
            "STAGED_IMPLEMENTATION_SHA256": migration._sha256(
                repository_root / "scripts/hermes_staged_schema_migrate.py"
            ),
            "RUNBOOK_SHA256": migration._sha256(
                repository_root / "docs/runbooks/"
                "RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
            ),
            "MIGRATION_IMAGE_ID": args.migration_image_id,
            "MIGRATION_IMAGE_REVISION": args.migration_image_revision,
            "DIRTY_LEGACY_ROOT_PRESERVED": True,
            "PRODUCTION_DB_ACCESS_AUTHORIZED": False,
            "PRODUCTION_PLAN_ONLY_AUTHORIZED": True,
            "PRODUCTION_EXECUTE_AUTHORIZED": False,
            "DEPLOY_AUTHORIZED": False,
        }
        policy_payload: dict[str, Any] = {
            "POLICY_VERSION": migration.CLEAN_START_POLICY_VERSION,
            "OPERATION_ID": operation_id,
            "DATA_POLICY": "NO_CLIENTS_CLEAN_START",
            "CREATED_AT": _timestamp(created_at),
            "TARGET_MAIN_SHA": head,
            "MIGRATION_COMPONENTS": components,
            "MIGRATION_IMAGE_ID": args.migration_image_id,
            "PRODUCTION_DB_SOURCE_SHA256": identity["SOURCE_SHA256"],
            "FAMILY_SHOPPING_BACKFILL_REQUIRED": False,
            "LEGACY_FAMILY_SHOPPING_DATA_MAY_BE_RESET": True,
            "MEMORY_OS_DATA_MUST_BE_PRESERVED": True,
            "NUTRITION_DIARY_DATA_MUST_BE_PRESERVED": True,
            "TELEGRAM_ADMIN_CONFIGURATION_MUST_BE_PRESERVED": True,
            "OUT_OF_SCOPE_TABLES_MUST_BE_PRESERVED": True,
            "EXECUTION_AUTHORIZED": False,
            "DELETION_PERFORMED": False,
        }
        approval_path = directory / "operations-root-approval.json"
        policy_path = directory / "clean-start-policy.json"
        approval_sha = _write_new_json(
            approval_path,
            approval_payload,
            fields=migration.OPERATIONS_ROOT_APPROVAL_FIELDS,
        )
        policy_sha = _write_new_json(
            policy_path,
            policy_payload,
            fields=migration.CLEAN_START_POLICY_FIELDS,
        )

        approval = migration._open_evidence_document(
            str(approval_path),
            approval_sha,
            code_prefix="OPERATIONS_ROOT_APPROVAL",
            expected_fields=migration.OPERATIONS_ROOT_APPROVAL_FIELDS,
        )
        migration._validate_operations_root_approval(
            approval,
            repository_root=repository_root,
            operation_id=operation_id,
            migration_components=components,
            migration_image_id=args.migration_image_id,
            migration_revision=args.migration_image_revision,
            deployment_contract=deployment_contract,
        )
        policy = migration._open_evidence_document(
            str(policy_path),
            policy_sha,
            code_prefix="CLEAN_START_POLICY",
            expected_fields=migration.CLEAN_START_POLICY_FIELDS,
        )
        migration._validate_clean_start_policy(
            policy,
            operation_id=operation_id,
            migration_components=components,
            source_sha256=str(identity["SOURCE_SHA256"]),
            migration_image_id=args.migration_image_id,
            migration_revision=args.migration_image_revision,
        )
        migration._json_emit({
            "status": "PASS",
            "mode": "PREPARE_AUTHORITY",
            "operation_id": operation_id,
            "authority_directory": str(directory),
            "operations_root_approval": _artifact_result(approval_path),
            "clean_start_policy": _artifact_result(policy_path),
            "operator_authorization_was_input": True,
            "execution_authorized": False,
            "production_execution_enabled": False,
            "contains_secrets": False,
        })
        return 0
    finally:
        if policy is not None:
            policy.close()
        if approval is not None:
            approval.close()
        if deployment_contract is not None:
            deployment_contract.close()


def _validate_plan_shape(
    plan: dict[str, Any],
    *,
    operation_id: str,
) -> None:
    components = _operation_components()
    if (
        set(plan) != migration.PLAN_FIELDS
        or plan.get("PLAN_VERSION") != migration.PLAN_VERSION
        or plan.get("PLAN_STATE") != "PLANNED"
        or plan.get("OPERATION_ID") != operation_id
        or plan.get("OPERATION_CLASS") != migration.AUTHORITY_OPERATION_CLASS
        or plan.get("MIGRATION_COMPONENTS") != components
        or plan.get("MIGRATION_REGISTRY") != migration._target_migration_registry()
    ):
        raise migration.ProductionGateError("AUTHORITY_PLAN_CONTRACT_INVALID")
    migration._parse_timestamp(
        plan.get("EXPIRES_AT"),
        "PLAN_EXPIRY_INVALID",
    )
    if migration._now() >= migration._parse_timestamp(
        plan.get("EXPIRES_AT"),
        "PLAN_EXPIRY_INVALID",
    ):
        raise migration.ProductionGateError("PLAN_EXPIRED")


def _validation_args(
    *,
    plan: dict[str, Any],
    final_path: Path,
    final_sha256: str,
) -> argparse.Namespace:
    return argparse.Namespace(
        confirm_operation_id=plan["OPERATION_ID"],
        confirm_source_sha256=plan["SOURCE_SHA256"],
        confirm_image_revision=plan["MIGRATION_IMAGE_REVISION"],
        confirm_operations_root_approval_sha256=plan["OPERATIONS_ROOT_APPROVAL_SHA256"],
        confirm_clean_start_policy_sha256=plan["CLEAN_START_POLICY_SHA256"],
        final_authority=str(final_path),
        expected_final_authority_sha256=final_sha256,
    )


def finalize_authority_package(args: argparse.Namespace) -> int:
    root_identity = migration._root_identity()
    if args.confirm_execution_authority != FINAL_AUTHORITY_CONFIRMATION:
        raise migration.ProductionGateError("FINAL_OPERATOR_AUTHORIZATION_REQUIRED")
    if args.expires_in_seconds < 60 or args.expires_in_seconds > 86400:
        raise migration.ProductionGateError("FINAL_AUTHORITY_EXPIRY_INVALID")
    if migration.SHA_RE.fullmatch(args.expected_plan_sha256) is None:
        raise migration.ProductionGateError("EXPECTED_PLAN_SHA256_INVALID")

    pinned = migration._open_plan(
        args.plan,
        args.expected_plan_sha256,
    )
    inputs: list[execution.BoundArtifact] = []
    validated: migration.ValidatedExecution | None = None
    try:
        plan = pinned.payload
        operation_id = args.confirm_operation_id
        _validate_plan_shape(plan, operation_id=operation_id)
        repository_root = migration._canonical_repository_root(
            str(plan["REPOSITORY_ROOT"])
        )
        directory = _authority_directory(
            args.authority_directory,
            operation_id=operation_id,
            repository_root=repository_root,
        )
        expected_initial = {
            "OPERATIONS_ROOT_APPROVAL_PATH": str(
                directory / "operations-root-approval.json"
            ),
            "CLEAN_START_POLICY_PATH": str(directory / "clean-start-policy.json"),
        }
        if not migration._typed_mapping_matches(plan, expected_initial):
            raise migration.ProductionGateError("AUTHORITY_INITIAL_PACKAGE_MISMATCH")

        p5b = _open_bound_input(
            args.p5b_evidence,
            args.expected_p5b_evidence_sha256,
            code_prefix="P5B_EVIDENCE",
        )
        inputs.append(p5b)
        p6a_f1 = _open_bound_input(
            args.p6a_f1_evidence,
            args.expected_p6a_f1_evidence_sha256,
            code_prefix="P6A_F1_EVIDENCE",
        )
        inputs.append(p6a_f1)
        secret = _open_bound_input(
            args.secrets_override,
            args.expected_secrets_override_sha256,
            code_prefix="SECRETS_OVERRIDE",
        )
        inputs.append(secret)

        try:
            contract = migration.deployment.load_contract(repository_root)
        except migration.deployment.DeploymentContractError as exc:
            raise migration.ProductionGateError(
                "CANONICAL_REPOSITORY_CONTRACT_INVALID"
            ) from exc
        head, tree = migration._repository_provenance(repository_root)
        if head != plan["MIGRATION_IMAGE_REVISION"]:
            raise migration.ProductionGateError("AUTHORITY_SOURCE_REVISION_MISMATCH")
        canonical = migration._canonical_repository_binding(
            repository_root,
            head=head,
        )
        if not migration._typed_mapping_matches(plan, canonical):
            raise migration.ProductionGateError("PLAN_REPOSITORY_BINDING_DRIFT")

        override_path = directory / "persistent-db-override.json"
        override_payload = {
            "services": {
                "hermes-bot": {
                    "volumes": [
                        {
                            "bind": {"create_host_path": True},
                            "source": plan["DB_CANONICAL_PATH"],
                            "target": str(contract.database_target),
                            "type": "bind",
                        }
                    ]
                }
            }
        }
        override_sha = _write_new_json(
            override_path,
            override_payload,
            fields=frozenset({"services"}),
        )

        descriptor_path = directory / "invocation-descriptor.json"
        descriptor_payload = {
            "DESCRIPTOR_VERSION": execution.INVOCATION_DESCRIPTOR_VERSION,
            "CREATED_AT": _timestamp(migration._now()),
            "COMPOSE_PROJECT_NAME": contract.project_name,
            "PROJECT_DIRECTORY": str(repository_root),
            "COMPOSE_FILE_ORDER": [
                str(contract.base_compose),
                str(override_path),
                str(secret.path),
            ],
            "NON_SECRET_COMPOSE_SHA256": {
                str(contract.base_compose): migration._sha256(contract.base_compose),
                str(override_path): override_sha,
            },
            "SECRETS_OVERRIDE": _secret_identity(secret),
            "ENVIRONMENT_SOURCE_CLASS": ("EXISTING_PRODUCTION_ENV_FILE_METADATA_ONLY"),
            "APPLICATION_SERVICE": contract.target_service,
            "CANONICAL_DB_SOURCE": plan["DB_CANONICAL_PATH"],
            "CANONICAL_DB_TARGET": str(contract.database_target),
            "CURRENT_PRODUCTION_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
            "TARGET_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
            "SOURCE_SHA": head,
            "TREE_SHA": tree,
            "CONTAINS_SECRET_VALUES": False,
        }
        descriptor_sha = _write_new_json(
            descriptor_path,
            descriptor_payload,
            fields=execution.INVOCATION_DESCRIPTOR_FIELDS,
        )

        database = Path(str(plan["DB_CANONICAL_PATH"]))
        root_metadata = repository_root.stat()
        envelope_path = directory / "approval-envelope.json"
        envelope_payload = {
            "ENVELOPE_VERSION": 1,
            "CREATED_AT": _timestamp(migration._now()),
            "PUBLIC_OPERATIONS_ROOT_APPROVAL_PATH": plan[
                "OPERATIONS_ROOT_APPROVAL_PATH"
            ],
            "PUBLIC_OPERATIONS_ROOT_APPROVAL_SHA256": plan[
                "OPERATIONS_ROOT_APPROVAL_SHA256"
            ],
            "OPERATIONS_ROOT_PATH": str(repository_root),
            "OPERATIONS_ROOT_HEAD_SHA": head,
            "OPERATIONS_ROOT_TREE_SHA": tree,
            "OPERATIONS_ROOT_MODE": stat.S_IMODE(root_metadata.st_mode),
            "OPERATIONS_ROOT_UID": int(root_metadata.st_uid),
            "OPERATIONS_ROOT_GID": int(root_metadata.st_gid),
            "OPERATIONS_ROOT_CLEAN": True,
            "OBJECT_ALTERNATES_ABSENT": True,
            "P5B_EVIDENCE_SHA256": p5b.sha256,
            "P6A_F1_EVIDENCE_SHA256": p6a_f1.sha256,
            "EXACT_MAIN_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
            "CANONICAL_DB_PATH": str(database),
            "CANONICAL_DB_DEVICE": plan["SOURCE_DEVICE"],
            "CANONICAL_DB_INODE": plan["SOURCE_INODE"],
            "CANONICAL_DB_SIZE": plan["SOURCE_SIZE"],
            "CANONICAL_DB_SHA256": plan["SOURCE_SHA256"],
            "PERSISTENT_DB_OVERRIDE_SHA256": override_sha,
            "INVOCATION_DESCRIPTOR_SHA256": descriptor_sha,
            "CLEAN_START_POLICY_SHA256": plan["CLEAN_START_POLICY_SHA256"],
            "PLAN_ONLY_AUTHORIZED": True,
            "EXECUTION_AUTHORIZED": False,
            "DEPLOY_AUTHORIZED": False,
            "CONTAINS_SECRETS": False,
        }
        envelope_sha = _write_new_json(
            envelope_path,
            envelope_payload,
            fields=execution.APPROVAL_ENVELOPE_FIELDS,
        )

        created_at = migration._now()
        plan_expires = migration._parse_timestamp(
            plan["EXPIRES_AT"],
            "PLAN_EXPIRY_INVALID",
        )
        approval_expires = migration._parse_timestamp(
            plan["OPERATIONS_ROOT_APPROVAL_EXPIRES_AT"],
            "OPERATIONS_ROOT_APPROVAL_EXPIRY_INVALID",
        )
        expires_at = min(
            created_at + timedelta(seconds=args.expires_in_seconds),
            plan_expires,
            approval_expires,
        )
        if expires_at <= created_at:
            raise migration.ProductionGateError("FINAL_AUTHORITY_EXPIRY_INVALID")
        final_path = directory / "final-authority.json"
        final_payload = {
            "EXECUTION_AUTHORITY_VERSION": (execution.EXECUTION_AUTHORITY_VERSION),
            "CREATED_AT": _timestamp(created_at),
            "EXPIRES_AT": _timestamp(expires_at),
            "PLAN_PATH": str(pinned.path),
            "PLAN_SHA256": pinned.sha256,
            "OPERATIONS_ROOT_APPROVAL_PATH": plan["OPERATIONS_ROOT_APPROVAL_PATH"],
            "OPERATIONS_ROOT_APPROVAL_SHA256": plan["OPERATIONS_ROOT_APPROVAL_SHA256"],
            "CLEAN_START_POLICY_PATH": plan["CLEAN_START_POLICY_PATH"],
            "CLEAN_START_POLICY_SHA256": plan["CLEAN_START_POLICY_SHA256"],
            "APPROVAL_ENVELOPE_PATH": str(envelope_path),
            "APPROVAL_ENVELOPE_SHA256": envelope_sha,
            "INVOCATION_DESCRIPTOR_PATH": str(descriptor_path),
            "INVOCATION_DESCRIPTOR_SHA256": descriptor_sha,
            "PERSISTENT_DB_OVERRIDE_PATH": str(override_path),
            "PERSISTENT_DB_OVERRIDE_SHA256": override_sha,
            "P5B_EVIDENCE_PATH": str(p5b.path),
            "P5B_EVIDENCE_SHA256": p5b.sha256,
            "P6A_F1_EVIDENCE_PATH": str(p6a_f1.path),
            "P6A_F1_EVIDENCE_SHA256": p6a_f1.sha256,
            "SOURCE_SHA": head,
            "SOURCE_TREE_SHA": tree,
            "TARGET_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
            "CURRENT_RUNTIME_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
            "CANONICAL_PRODUCTION_DB_PATH": plan["DB_CANONICAL_PATH"],
            "SOURCE_DB_SHA256": plan["SOURCE_SHA256"],
            "SOURCE_DB_SIZE": plan["SOURCE_SIZE"],
            "SOURCE_DB_USER_VERSION": plan["SOURCE_USER_VERSION"],
            "SOURCE_DB_SCHEMA_FINGERPRINT": plan["SOURCE_SCHEMA_FINGERPRINT"],
            "SOURCE_DB_PARENT_IDENTITY": plan["SOURCE_PARENT_IDENTITY"],
            "OPERATIONS_ROOT_PATH": str(repository_root),
            "OPERATIONS_ROOT_HEAD_SHA": head,
            "OPERATIONS_ROOT_TREE_SHA": tree,
            "EXECUTION_AUTHORIZED": True,
            "DEPLOY_AUTHORIZED": False,
            "CONTAINS_SECRETS": False,
        }
        final_sha = _write_new_json(
            final_path,
            final_payload,
            fields=execution.EXECUTION_AUTHORITY_FIELDS,
        )

        validated = migration._revalidate_plan(
            _validation_args(
                plan=plan,
                final_path=final_path,
                final_sha256=final_sha,
            ),
            pinned,
            root_identity,
            expected_runtime_running=True,
        )
        migration._json_emit({
            "status": "PASS",
            "mode": "FINALIZE_AUTHORITY",
            "operation_id": operation_id,
            "plan_path": str(pinned.path),
            "plan_sha256": pinned.sha256,
            "approval_envelope": _artifact_result(envelope_path),
            "invocation_descriptor": _artifact_result(descriptor_path),
            "persistent_db_override": _artifact_result(override_path),
            "final_authority": _artifact_result(final_path),
            "operator_authorization_was_input": True,
            "execution_authorized": True,
            "deploy_authorized": False,
            "production_execution_enabled": False,
            "contains_secrets": False,
        })
        return 0
    finally:
        if validated is not None:
            validated.close()
        for artifact in reversed(inputs):
            artifact.close()
        pinned.close()


def validate_authority_package(args: argparse.Namespace) -> int:
    root_identity = migration._root_identity()
    pinned = migration._open_plan(
        args.plan,
        args.expected_plan_sha256,
    )
    validated: migration.ValidatedExecution | None = None
    try:
        plan = pinned.payload
        _validate_plan_shape(
            plan,
            operation_id=args.confirm_operation_id,
        )
        validated = migration._revalidate_plan(
            _validation_args(
                plan=plan,
                final_path=Path(args.final_authority),
                final_sha256=args.expected_final_authority_sha256,
            ),
            pinned,
            root_identity,
            expected_runtime_running=True,
        )
        migration._json_emit({
            "status": "PASS",
            "mode": "VALIDATE_AUTHORITY_PACKAGE",
            "operation_id": plan["OPERATION_ID"],
            "plan_sha256": pinned.sha256,
            "final_authority_sha256": (args.expected_final_authority_sha256),
            "production_execution_enabled": False,
            "contains_secrets": False,
        })
        return 0
    finally:
        if validated is not None:
            validated.close()
        pinned.close()
