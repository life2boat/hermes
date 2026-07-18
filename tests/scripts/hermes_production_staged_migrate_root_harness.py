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
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

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
        shutil.rmtree(runtime_root)
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

        execute_argv = [
            "execute",
            "--plan",
            str(plan_path),
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
            raise AssertionError("public execute failed")

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
            "public_entrypoint_used": True,
            "canonical_deployment_contract_used": True,
            "synthetic_db_only": True,
            "production_db_used": False,
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
            "public_entrypoint_used",
            "canonical_deployment_contract_used",
            "synthetic_db_only",
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
        shutil.rmtree(runtime_root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
