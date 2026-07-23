#!/usr/bin/env python3
"""Real-root, network-isolated public production-gate integration harness."""

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
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from scripts import hermes_execution_authority as execution_authority
from scripts import hermes_production_staged_migrate as production


def _parse_output(value: str) -> dict[str, Any]:
    lines = [line for line in value.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError("public entrypoint did not emit one JSON document")
    payload = json.loads(lines[0])
    if not isinstance(payload, dict):
        raise AssertionError("public entrypoint result is not an object")
    return payload


def _public_main(argv: list[str]) -> tuple[int, dict[str, Any]]:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        return_code = production.main(argv)
    return return_code, _parse_output(captured.getvalue())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_canonical_document(path: Path, payload: dict[str, Any]) -> None:
    path.write_bytes(production._canonical_json(payload))
    os.chmod(path, 0o600)


def _write_private_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, 0o600)


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


def _git_output(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError("synthetic repository command failed")
    return result.stdout.strip()


def _create_final_authority(
    *,
    repository: Path,
    source: Path,
    evidence_inputs: Path,
    operations_root_approval: Path,
    clean_start_policy: Path,
    plan_path: Path,
    plan_sha256: str,
    plan: dict[str, Any],
    target_image_id: str,
    previous_image_id: str,
    compose_project_name: str,
    application_service: str,
) -> tuple[Path, str]:
    revision = str(plan["MIGRATION_IMAGE_REVISION"])
    tree = _git_output(repository, "rev-parse", "HEAD^{tree}")
    created_at = production._now()
    base_compose = repository / "docker-compose.yml"
    p5b = evidence_inputs / "p5b-evidence.md"
    p6a_f1 = evidence_inputs / "p6a-f1-evidence.md"
    override = evidence_inputs / "production-db-override.yml"
    secret = evidence_inputs / "secrets-override.yml"
    descriptor = evidence_inputs / "invocation-descriptor.json"
    envelope = evidence_inputs / "approval-envelope.json"
    final = evidence_inputs / "final-authority.json"

    _write_private_bytes(p5b, b"synthetic p5b evidence\n")
    _write_private_bytes(p6a_f1, b"synthetic p6a-f1 evidence\n")
    _write_canonical_document(
        override,
        {
            "services": {
                "hermes-bot": {
                    "volumes": [
                        {
                            "bind": {"create_host_path": True},
                            "source": str(source),
                            "target": "/home/hermes/healbite.db",
                            "type": "bind",
                        }
                    ]
                }
            }
        },
    )
    _write_private_bytes(secret, b"services: {}\n")
    _write_canonical_document(
        descriptor,
        {
            "DESCRIPTOR_VERSION": (
                execution_authority.INVOCATION_DESCRIPTOR_VERSION
            ),
            "CREATED_AT": production._timestamp(created_at),
            "COMPOSE_PROJECT_NAME": compose_project_name,
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
            "ENVIRONMENT_SOURCE_CLASS": (
                "EXISTING_PRODUCTION_ENV_FILE_METADATA_ONLY"
            ),
            "APPLICATION_SERVICE": application_service,
            "CANONICAL_DB_SOURCE": str(source),
            "CANONICAL_DB_TARGET": "/home/hermes/healbite.db",
            "CURRENT_PRODUCTION_IMAGE_ID": previous_image_id,
            "TARGET_IMAGE_ID": target_image_id,
            "SOURCE_SHA": revision,
            "TREE_SHA": tree,
            "CONTAINS_SECRET_VALUES": False,
        },
    )
    root_metadata = repository.stat()
    _write_canonical_document(
        envelope,
        {
            "ENVELOPE_VERSION": 1,
            "CREATED_AT": production._timestamp(created_at),
            "PUBLIC_OPERATIONS_ROOT_APPROVAL_PATH": str(
                operations_root_approval
            ),
            "PUBLIC_OPERATIONS_ROOT_APPROVAL_SHA256": _sha256(
                operations_root_approval
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
            "EXACT_MAIN_IMAGE_ID": target_image_id,
            "CANONICAL_DB_PATH": str(source),
            "CANONICAL_DB_DEVICE": int(plan["SOURCE_DEVICE"]),
            "CANONICAL_DB_INODE": int(plan["SOURCE_INODE"]),
            "CANONICAL_DB_SIZE": int(plan["SOURCE_SIZE"]),
            "CANONICAL_DB_SHA256": str(plan["SOURCE_SHA256"]),
            "PERSISTENT_DB_OVERRIDE_SHA256": _sha256(override),
            "INVOCATION_DESCRIPTOR_SHA256": _sha256(descriptor),
            "CLEAN_START_POLICY_SHA256": _sha256(clean_start_policy),
            "PLAN_ONLY_AUTHORIZED": True,
            "EXECUTION_AUTHORIZED": False,
            "DEPLOY_AUTHORIZED": False,
            "CONTAINS_SECRETS": False,
        },
    )
    _write_canonical_document(
        final,
        {
            "EXECUTION_AUTHORITY_VERSION": (
                execution_authority.EXECUTION_AUTHORITY_VERSION
            ),
            "CREATED_AT": production._timestamp(created_at),
            "EXPIRES_AT": production._timestamp(
                created_at + timedelta(hours=1)
            ),
            "PLAN_PATH": str(plan_path),
            "PLAN_SHA256": plan_sha256,
            "OPERATIONS_ROOT_APPROVAL_PATH": str(
                operations_root_approval
            ),
            "OPERATIONS_ROOT_APPROVAL_SHA256": _sha256(
                operations_root_approval
            ),
            "CLEAN_START_POLICY_PATH": str(clean_start_policy),
            "CLEAN_START_POLICY_SHA256": _sha256(clean_start_policy),
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
            "TARGET_IMAGE_ID": target_image_id,
            "CURRENT_RUNTIME_IMAGE_ID": previous_image_id,
            "CANONICAL_PRODUCTION_DB_PATH": str(source),
            "SOURCE_DB_SHA256": str(plan["SOURCE_SHA256"]),
            "SOURCE_DB_SIZE": int(plan["SOURCE_SIZE"]),
            "SOURCE_DB_USER_VERSION": int(plan["SOURCE_USER_VERSION"]),
            "SOURCE_DB_SCHEMA_FINGERPRINT": str(
                plan["SOURCE_SCHEMA_FINGERPRINT"]
            ),
            "SOURCE_DB_PARENT_IDENTITY": plan["SOURCE_PARENT_IDENTITY"],
            "OPERATIONS_ROOT_PATH": str(repository),
            "OPERATIONS_ROOT_HEAD_SHA": revision,
            "OPERATIONS_ROOT_TREE_SHA": tree,
            "EXECUTION_AUTHORIZED": True,
            "DEPLOY_AUTHORIZED": False,
            "CONTAINS_SECRETS": False,
        },
    )
    return final, _sha256(final)


def _start_synthetic_runtime(
    *,
    source: Path,
    previous_image_id: str,
) -> tuple[str, str]:
    suffix = uuid.uuid4().hex
    container_name = f"hermes-root-integration-{suffix}"
    project_name = f"hermes-root-integration-{suffix}"
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--name",
            container_name,
            "--label",
            f"com.docker.compose.project={project_name}",
            "--label",
            "com.docker.compose.service=hermes-bot",
            "--mount",
            (
                f"type=bind,source={source},"
                "target=/home/hermes/healbite.db"
            ),
            "--entrypoint",
            "/bin/sh",
            previous_image_id,
            "-c",
            "sleep 300",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError("synthetic runtime start failed")
    return container_name, project_name


def _remove_synthetic_runtime(container_name: str) -> None:
    result = subprocess.run(
        ["docker", "rm", "-f", container_name],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError("synthetic runtime cleanup failed")


def _install_synthetic_runtime_inspector(container_name: str) -> Any:
    original = execution_authority._inspect_runtime

    def inspect(service_name: str) -> dict[str, Any]:
        if service_name != "hermes-bot":
            raise AssertionError("unexpected runtime service lookup")
        return original(container_name)

    execution_authority._inspect_runtime = inspect
    return original


def _create_source(path: Path) -> None:
    _private_directory(path.parent)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE legacy_rows (value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO legacy_rows VALUES ('synthetic')"
        )
    os.chmod(path, 0o600)


def _empty_migrated_rows(path: Path) -> bool:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        table_names = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' "
                "AND name != 'legacy_rows'"
            )
        ]
        for name in table_names:
            quoted = '"' + name.replace('"', '""') + '"'
            count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {quoted}"
                ).fetchone()[0]
            )
            if count != 0:
                return False
    return True


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--target-image-id", required=True)
    parser.add_argument("--target-revision", required=True)
    parser.add_argument("--previous-image-id", required=True)
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if (
        os.geteuid() != 0 or os.getegid() != 0  # windows-footgun: ok
    ):
        raise AssertionError("root integration container is not real root")

    runtime_root = Path(args.runtime_root)
    source_repository_root = Path(args.repository_root)
    if runtime_root.exists():
        raise AssertionError("runtime root must not preexist")
    _private_directory(runtime_root)
    repository_root = runtime_root / "approved-repository"
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(source_repository_root), str(repository_root)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository_root), "checkout", "--quiet", "--detach", args.target_revision],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    os.chmod(repository_root, 0o700)
    production.REPO_ROOT = repository_root
    source = runtime_root / "source" / "database.sqlite"
    backup = _private_directory(runtime_root / "backup")
    staging = _private_directory(runtime_root / "staging")
    evidence = _private_directory(runtime_root / "evidence")
    evidence_inputs = _private_directory(runtime_root / "evidence-inputs")
    approval_path = evidence_inputs / "approved-operations-root.json"
    policy_path = evidence_inputs / "clean-start-data-policy.json"
    runtime_container_name: str | None = None
    original_runtime_inspector: Any = None

    try:
        _create_source(source)
        (
            source_identity,
            _source_schema,
            source_integrity,
            source_foreign_keys,
        ) = production._read_only_source(source)
        if source_integrity != "ok" or source_foreign_keys != 0:
            raise AssertionError("synthetic source is invalid")

        root_record = production._directory_record(
            repository_root,
            private=True,
        )
        head, tree = production._repository_provenance(repository_root)
        contract_path = (
            repository_root / production.CANONICAL_CONTRACT_RELATIVE_PATH
        )
        contract_metadata = contract_path.stat()
        created_at = production._now()
        approval = {
            "APPROVAL_VERSION": 1,
            "CREATED_AT": production._timestamp(created_at),
            "EXPIRES_AT": production._timestamp(created_at + timedelta(hours=1)),
            "TARGET_MAIN_SHA": head,
            "APPROVED_REPOSITORY_ROOT": str(repository_root),
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
                repository_root / "scripts/hermes_production_staged_migrate.py"
            ),
            "STAGED_IMPLEMENTATION_SHA256": _sha256(
                repository_root / "scripts/hermes_staged_schema_migrate.py"
            ),
            "RUNBOOK_SHA256": _sha256(
                repository_root
                / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
            ),
            "MIGRATION_IMAGE_ID": args.target_image_id,
            "MIGRATION_IMAGE_REVISION": args.target_revision,
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
            "TARGET_MAIN_SHA": args.target_revision,
            "MIGRATION_IMAGE_ID": args.target_image_id,
            "PRODUCTION_DB_SOURCE_SHA256": source_identity["SOURCE_SHA256"],
            "FAMILY_SHOPPING_BACKFILL_REQUIRED": False,
            "LEGACY_FAMILY_SHOPPING_DATA_MAY_BE_RESET": True,
            "MEMORY_OS_DATA_MUST_BE_PRESERVED": True,
            "NUTRITION_DIARY_DATA_MUST_BE_PRESERVED": True,
            "TELEGRAM_ADMIN_CONFIGURATION_MUST_BE_PRESERVED": True,
            "OUT_OF_SCOPE_TABLES_MUST_BE_PRESERVED": True,
            "EXECUTION_AUTHORIZED": False,
            "DELETION_PERFORMED": False,
        }
        _write_canonical_document(approval_path, approval)
        _write_canonical_document(policy_path, policy)

        plan_argv = [
            "plan",
            "--repository-root",
            str(repository_root),
            "--db-path",
            str(source),
            "--backup-parent",
            str(backup),
            "--staging-parent",
            str(staging),
            "--evidence-parent",
            str(evidence),
            "--operations-root-approval",
            str(approval_path),
            "--expected-operations-root-approval-sha256",
            _sha256(approval_path),
            "--clean-start-policy",
            str(policy_path),
            "--expected-clean-start-policy-sha256",
            _sha256(policy_path),
            "--migration-image-id",
            args.target_image_id,
            "--migration-image-revision",
            args.target_revision,
            "--previous-image-id",
            args.previous_image_id,
            "--expected-hostname",
            socket.gethostname(),
            "--expected-source-device",
            str(source_identity["SOURCE_DEVICE"]),
            "--expected-source-inode",
            str(source_identity["SOURCE_INODE"]),
            "--expected-source-size",
            str(source_identity["SOURCE_SIZE"]),
            "--expected-source-sha256",
            str(source_identity["SOURCE_SHA256"]),
            "--expected-free-bytes",
            "1",
            "--expires-in-seconds",
            "3600",
        ]
        plan_return_code, plan_result = _public_main(plan_argv)
        if plan_return_code != 0 or plan_result.get("status") != "PASS":
            raise AssertionError("public plan failed")

        plan_path = Path(str(plan_result["plan_path"]))
        plan_bytes = plan_path.read_bytes()
        plan = json.loads(plan_bytes.decode("ascii"))
        if (
            plan["PLAN_CREATOR_UID"] != 0
            or plan["PLAN_CREATOR_GID"] != 0
            or plan["DEPLOYMENT_CONTRACT_CANONICAL_PATH"]
            != str(
                repository_root
                / production.CANONICAL_CONTRACT_RELATIVE_PATH
            )
        ):
            raise AssertionError("plan root or contract authority mismatch")
        if (
            plan["DEPLOYMENT_CONTRACT_SHA256"]
            != _sha256(
                repository_root
                / production.CANONICAL_CONTRACT_RELATIVE_PATH
            )
        ):
            raise AssertionError("deployment contract hash mismatch")
        if set(path.name for path in plan_path.parent.iterdir()) != {
            "plan.json"
        }:
            raise AssertionError("execute evidence exists before quiescence")

        runtime_container_name, compose_project_name = (
            _start_synthetic_runtime(
                source=source,
                previous_image_id=args.previous_image_id,
            )
        )
        original_runtime_inspector = _install_synthetic_runtime_inspector(
            runtime_container_name
        )
        final_authority_path, final_authority_sha256 = (
            _create_final_authority(
                repository=repository_root,
                source=source,
                evidence_inputs=evidence_inputs,
                operations_root_approval=approval_path,
                clean_start_policy=policy_path,
                plan_path=plan_path,
                plan_sha256=str(plan_result["plan_sha256"]),
                plan=plan,
                target_image_id=args.target_image_id,
                previous_image_id=args.previous_image_id,
                compose_project_name=compose_project_name,
                application_service="hermes-bot",
            )
        )

        execute_argv = [
            "execute",
            "--plan",
            str(plan_path),
            "--final-authority",
            str(final_authority_path),
            "--expected-final-authority-sha256",
            final_authority_sha256,
            "--expected-plan-sha256",
            str(plan_result["plan_sha256"]),
            "--confirm-operation-id",
            str(plan["OPERATION_ID"]),
            "--confirm-source-sha256",
            str(plan["SOURCE_SHA256"]),
            "--confirm-image-revision",
            str(plan["MIGRATION_IMAGE_REVISION"]),
            "--confirm-operations-root-approval-sha256",
            str(plan["OPERATIONS_ROOT_APPROVAL_SHA256"]),
            "--confirm-clean-start-policy-sha256",
            str(plan["CLEAN_START_POLICY_SHA256"]),
        ]
        execute_return_code, execute_result = _public_main(execute_argv)
        if (
            execute_return_code != 0
            or execute_result.get("status") != "PASS"
            or execute_result.get("manifest_state") != "COMPLETED"
            or execute_result.get("publish_state") != "FINAL_VERIFIED"
        ):
            raise AssertionError(
                "public execute failed: "
                f"{execute_result.get('error_type', 'UNKNOWN')}"
            )

        (
            final_identity,
            _final_schema,
            final_integrity,
            final_foreign_keys,
        ) = production._read_only_source(source)
        actual_target_fingerprint = production._target_schema_fingerprint(
            source
        )
        operation_id = str(plan["OPERATION_ID"])
        internal_manifest_path = (
            backup / f"manifest-{operation_id}.json"
        )
        backup_path = backup / f"backup-{operation_id}.sqlite"
        execution_path = plan_path.parent / "execution.json"
        internal_manifest = json.loads(
            internal_manifest_path.read_text(encoding="ascii")
        )
        execution = json.loads(
            execution_path.read_text(encoding="ascii")
        )

        checks = {
            "root_context_real": os.geteuid() == 0,  # windows-footgun: ok
            "root_check_monkeypatched": False,
            "runtime_inspection_redirected": True,
            "public_entrypoint_used": True,
            "canonical_deployment_contract_used": True,
            "synthetic_db_only": True,
            "synthetic_runtime_only": True,
            "production_db_used": False,
            "production_runtime_inspected": False,
            "production_secrets_used": False,
            "network_none": True,
            "plan_creator_uid": plan["PLAN_CREATOR_UID"],
            "plan_creator_gid_recorded": isinstance(
                plan["PLAN_CREATOR_GID"], int
            ),
            "deployment_contract_sha_revalidated": True,
            "quiescence_before_execution_evidence": execution.get(
                "QUIESCENCE_ACQUIRED_BEFORE_EXECUTION_EVIDENCE"
            )
            is True,
            "backup_durable": (
                backup_path.is_file()
                and _sha256(backup_path) == plan["SOURCE_SHA256"]
            ),
            "integrity_check": final_integrity,
            "foreign_key_check": final_foreign_keys,
            "backfill_rows_created": 0
            if _empty_migrated_rows(source)
            else -1,
            "final_schema_matches_planned_target": (
                actual_target_fingerprint
                == plan["TARGET_SCHEMA_FINGERPRINT"]
                == execution.get("TARGET_SCHEMA_AFTER")
            ),
            "final_evidence_valid": (
                execution.get("STATE") == "COMPLETED"
                and internal_manifest.get("STATE") == "VERIFIED"
                and internal_manifest.get("PUBLISH_STATE")
                == "FINAL_VERIFIED"
                and final_identity["SOURCE_SHA256"]
                == execution.get("FINAL_TARGET_SHA256")
            ),
        }
        positive_boolean_keys = {
            "root_context_real",
            "runtime_inspection_redirected",
            "public_entrypoint_used",
            "canonical_deployment_contract_used",
            "synthetic_db_only",
            "synthetic_runtime_only",
            "network_none",
            "plan_creator_gid_recorded",
            "deployment_contract_sha_revalidated",
            "quiescence_before_execution_evidence",
            "backup_durable",
            "final_schema_matches_planned_target",
            "final_evidence_valid",
        }
        if not all(checks[key] is True for key in positive_boolean_keys):
            raise AssertionError("root integration boolean contract failed")
        if (
            checks["root_check_monkeypatched"] is not False
            or checks["production_db_used"] is not False
            or checks["production_runtime_inspected"] is not False
            or checks["production_secrets_used"] is not False
        ):
            raise AssertionError(
                "root integration negative boolean contract failed"
            )
        if (
            checks["plan_creator_uid"] != 0
            or checks["integrity_check"] != "ok"
            or checks["foreign_key_check"] != 0
            or checks["backfill_rows_created"] != 0
        ):
            raise AssertionError("root integration value contract failed")
        print(
            json.dumps(
                {"status": "PASS", **checks},
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0
    finally:
        try:
            try:
                if original_runtime_inspector is not None:
                    execution_authority._inspect_runtime = (
                        original_runtime_inspector
                    )
            finally:
                if runtime_container_name is not None:
                    _remove_synthetic_runtime(runtime_container_name)
        finally:
            shutil.rmtree(runtime_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
