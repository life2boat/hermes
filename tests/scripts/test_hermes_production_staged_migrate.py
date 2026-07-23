from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import hermes_execution_authority as authority
from scripts import hermes_production_staged_migrate as production
from scripts import hermes_staged_schema_migrate as staged


REVISION = "1" * 40
IMAGE_ID = "sha256:" + "2" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "3" * 64
TREE_SHA = "4" * 40

PARSER_CASES = (
    "missing_subcommand",
    "missing_repository_root",
    "arbitrary_deployment_contract",
    "caller_target_schema_version",
    "production_callback_argument",
    "root_bypass_argument",
    "force_argument",
    "crash_hook_argument",
)

PLAN_GATE_CASES = (
    ("non_root_plan", "ROOT_EUID_REQUIRED"),
    ("repository_root_mismatch", "REPOSITORY_ROOT_MISMATCH"),
    ("canonical_contract_symlink", "SYMLINK_PATH_REFUSED"),
    ("canonical_contract_not_regular", "DEPLOYMENT_CONTRACT_INVALID"),
    ("malformed_contract", "DEPLOYMENT_CONTRACT_MANIFEST_FIELDS"),
    ("household_flag_missing", "DEPLOYMENT_CONTRACT_FEATURE_GATE_POLICY"),
    ("household_flag_enabled", "DEPLOYMENT_CONTRACT_FEATURE_GATE_POLICY"),
    ("household_flag_string_false", "DEPLOYMENT_CONTRACT_FEATURE_GATE_POLICY"),
    ("shopping_flag_missing", "DEPLOYMENT_CONTRACT_FEATURE_GATE_POLICY"),
    ("shopping_flag_enabled", "DEPLOYMENT_CONTRACT_FEATURE_GATE_POLICY"),
    ("shopping_flag_string_false", "DEPLOYMENT_CONTRACT_FEATURE_GATE_POLICY"),
    ("invalid_source_sha", "EXPECTED_SOURCE_SHA256_INVALID"),
    ("invalid_image_revision", "MIGRATION_IMAGE_REVISION_INVALID"),
    ("expiry_too_short", "PLAN_EXPIRY_INVALID"),
    ("expiry_too_long", "PLAN_EXPIRY_INVALID"),
    ("hostname_mismatch", "HOSTNAME_MISMATCH"),
    ("expected_source_device_mismatch", "EXPECTED_SOURCE_IDENTITY_MISMATCH"),
    ("expected_source_inode_mismatch", "EXPECTED_SOURCE_IDENTITY_MISMATCH"),
    ("expected_source_size_mismatch", "EXPECTED_SOURCE_IDENTITY_MISMATCH"),
    ("expected_source_hash_mismatch", "EXPECTED_SOURCE_IDENTITY_MISMATCH"),
    ("insufficient_free_space", "INSUFFICIENT_FREE_SPACE"),
    ("migration_image_missing", "IMAGE_NOT_AVAILABLE"),
    ("previous_image_missing", "IMAGE_NOT_AVAILABLE"),
    ("unsupported_sidecar", "UNSUPPORTED_SQLITE_SIDECAR"),
    ("source_mode_unsafe", "SOURCE_METADATA_INVALID"),
    ("evidence_parent_mode_unsafe", "OPERATION_PARENT_MODE_UNSAFE"),
    ("cross_filesystem_staging", "CROSS_FILESYSTEM_STAGING"),
)

EXECUTE_GATE_CASES = (
    ("non_root_execute", "ROOT_EUID_REQUIRED"),
    ("plan_sha_invalid", "EXPECTED_PLAN_SHA256_INVALID"),
    ("plan_sha_mismatch", "PLAN_SHA256_MISMATCH"),
    ("operation_id_mismatch", "PLAN_OPERATION_ID_MISMATCH"),
    ("source_confirmation_mismatch", "PLAN_SOURCE_SHA256_CONFIRMATION_MISMATCH"),
    ("image_confirmation_mismatch", "PLAN_IMAGE_REVISION_CONFIRMATION_MISMATCH"),
    ("expired_plan", "PLAN_EXPIRED"),
    ("plan_fields_extra", "PLAN_FIELDS_INVALID"),
    ("plan_creator_uid_mismatch", "PLAN_CREATOR_IDENTITY_MISMATCH"),
    ("plan_creator_gid_mismatch", "PLAN_CREATOR_IDENTITY_MISMATCH"),
    ("plan_symlink_substitution", "SYMLINK_PATH_REFUSED"),
    ("plan_path_substitution", "PLAN_FILE_METADATA_INVALID"),
    ("source_inode_drift", "SOURCE_IDENTITY_DRIFT"),
    ("source_sha_drift", "SOURCE_IDENTITY_DRIFT"),
    ("source_schema_drift", "SOURCE_SCHEMA_DRIFT"),
    ("source_mode_drift", "SOURCE_METADATA_INVALID"),
    ("deployment_contract_replaced", "DEPLOYMENT_CONTRACT_DRIFT"),
    ("target_schema_version_mismatch", "TARGET_SCHEMA_CONTRACT_MISMATCH"),
    ("target_schema_fingerprint_mismatch", "TARGET_SCHEMA_CONTRACT_MISMATCH"),
    ("active_sqlite_reader", "QUIESCENCE_FAILED"),
    ("active_sqlite_writer", "QUIESCENCE_FAILED"),
    ("unsupported_sidecar_after_plan", "UNSUPPORTED_SQLITE_SIDECAR"),
    ("operation_artifact_collision", "OPERATION_ARTIFACT_COLLISION"),
    ("image_revision_drift", "IMAGE_REVISION_MISMATCH"),
)

POST_EXCHANGE_CASES = (
    "internal_manifest_read_failure",
    "internal_manifest_close_failure",
    "final_target_validation_failure",
    "external_evidence_write_failure",
    "completed_transition_failure",
    "plan_path_revalidation_failure",
    "operation_cleanup_failure",
    "final_target_hash_failure",
    "final_result_emit_failure",
)

SCHEMA_FAILURE_CASES = (
    "migration_missing_required_table",
    "migration_incompatible_schema",
)

CLOSE_FAILURE_CASES = (
    "validated_close_before_exchange",
    "validated_close_after_exchange_started",
    "validated_close_after_final_verification",
    "pinned_plan_close_before_exchange",
    "pinned_plan_close_after_exchange_started",
    "pinned_plan_close_after_final_validation",
    "primary_post_exchange_plus_cleanup_failure",
    "successful_body_evidence_finalization_failure",
)

NEGATIVE_MATRIX_CASES = (
    len(PARSER_CASES)
    + len(PLAN_GATE_CASES)
    + len(EXECUTE_GATE_CASES)
    + len(POST_EXCHANGE_CASES)
    + len(SCHEMA_FAILURE_CASES)
    + len(CLOSE_FAILURE_CASES)
)


@dataclass
class UnitContext:
    root: Path
    repository: Path
    runtime: Path
    source: Path
    backup: Path
    staging: Path
    evidence: Path
    operations_root_approval: Path
    clean_start_policy: Path

    @property
    def manifest(self) -> Path:
        return self.repository / "deploy" / "hermes-production.json"


@dataclass
class PlannedContext:
    unit: UnitContext
    path: Path
    payload: dict[str, Any]
    sha256: str


class _UnitExecutionAuthority:
    def __init__(self) -> None:
        self.final_authority = type(
            "FinalAuthority",
            (),
            {"sha256": "a" * 64},
        )()

    def path_matches(self) -> bool:
        return True

    def runtime_matches(self) -> bool:
        return True

    def validate_not_expired(self) -> None:
        return None

    def revalidate_operations_root(self) -> None:
        return None

    def validate_runtime(self) -> None:
        return None

    def validate_source(
        self,
        *,
        identity: dict[str, int | str],
        schema_fingerprint: str,
        parent_identity: dict[str, int | str],
    ) -> None:
        assert identity["SOURCE_USER_VERSION"] == 0
        assert len(schema_fingerprint) == 64
        assert parent_identity["PATH"]

    def close(self) -> None:
        return None


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _source(path: Path) -> Path:
    _private_directory(path.parent)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    os.chmod(path, 0o600)
    return path


def _copy_repository(path: Path) -> Path:
    _private_directory(path)
    _private_directory(path / "deploy")
    source_root = Path(__file__).resolve().parents[2]
    for relative in (
        Path("deploy/hermes-production.json"),
        Path("deploy/docker-compose.production.yml"),
        Path("docker-compose.yml"),
        Path("scripts/hermes_production_staged_migrate.py"),
        Path("scripts/hermes_staged_schema_migrate.py"),
        Path("docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"),
    ):
        destination = path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
        os.chmod(destination, 0o600)
    return path


def _write_canonical_document(path: Path, payload: dict[str, Any]) -> None:
    path.write_bytes(production._canonical_json(payload))
    os.chmod(path, 0o600)


def _write_unit_evidence(context: UnitContext) -> None:
    root_record = _fake_directory_record(context.repository, private=True)
    contract_metadata = context.manifest.stat()
    created_at = production._now()
    approval = {
        "APPROVAL_VERSION": 1,
        "CREATED_AT": production._timestamp(created_at),
        "EXPIRES_AT": production._timestamp(created_at + timedelta(hours=1)),
        "TARGET_MAIN_SHA": REVISION,
        "APPROVED_REPOSITORY_ROOT": str(context.repository),
        "REPOSITORY_ROOT_DEVICE": root_record["DEVICE"],
        "REPOSITORY_ROOT_INODE": root_record["INODE"],
        "REPOSITORY_ROOT_UID": root_record["UID"],
        "REPOSITORY_ROOT_GID": root_record["GID"],
        "REPOSITORY_ROOT_MODE": root_record["MODE"],
        "REPOSITORY_ROOT_TREE_SHA": TREE_SHA,
        "DEPLOYMENT_CONTRACT_PATH": str(context.manifest),
        "DEPLOYMENT_CONTRACT_DEVICE": contract_metadata.st_dev,
        "DEPLOYMENT_CONTRACT_INODE": contract_metadata.st_ino,
        "DEPLOYMENT_CONTRACT_SHA256": production._sha256(context.manifest),
        "PRODUCTION_MIGRATION_ENTRYPOINT_SHA256": production._sha256(
            context.repository / "scripts/hermes_production_staged_migrate.py"
        ),
        "STAGED_IMPLEMENTATION_SHA256": production._sha256(
            context.repository / "scripts/hermes_staged_schema_migrate.py"
        ),
        "RUNBOOK_SHA256": production._sha256(
            context.repository
            / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
        ),
        "MIGRATION_IMAGE_ID": IMAGE_ID,
        "MIGRATION_IMAGE_REVISION": REVISION,
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
        "TARGET_MAIN_SHA": REVISION,
        "MIGRATION_IMAGE_ID": IMAGE_ID,
        "PRODUCTION_DB_SOURCE_SHA256": production._sha256(context.source),
        "FAMILY_SHOPPING_BACKFILL_REQUIRED": False,
        "LEGACY_FAMILY_SHOPPING_DATA_MAY_BE_RESET": True,
        "MEMORY_OS_DATA_MUST_BE_PRESERVED": True,
        "NUTRITION_DIARY_DATA_MUST_BE_PRESERVED": True,
        "TELEGRAM_ADMIN_CONFIGURATION_MUST_BE_PRESERVED": True,
        "OUT_OF_SCOPE_TABLES_MUST_BE_PRESERVED": True,
        "EXECUTION_AUTHORIZED": False,
        "DELETION_PERFORMED": False,
    }
    _write_canonical_document(context.operations_root_approval, approval)
    _write_canonical_document(context.clean_start_policy, policy)


def _path_set(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
    }


def _fake_directory_record(path: Path, *, private: bool) -> dict[str, int | str]:
    production._no_symlink_chain(path)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise production.ProductionGateError("OPERATION_PARENT_NOT_DIRECTORY")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o022 or (private and mode != 0o700):
        raise production.ProductionGateError("OPERATION_PARENT_MODE_UNSAFE")
    return {
        "PATH": str(path),
        "DEVICE": int(metadata.st_dev),
        "INODE": int(metadata.st_ino),
        "UID": 0,
        "GID": 0,
        "MODE": mode,
    }


def _unit_write_json_at(
    parent_fd: int,
    name: str,
    payload: dict[str, Any],
) -> None:
    encoded = production._canonical_json(payload)
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temporary,
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _unit_open_plan(path_value: str, expected_sha: str) -> production.PinnedPlan:
    path = production._absolute_path(path_value, "PLAN_PATH")
    production._no_symlink_chain(path)
    if production.SHA_RE.fullmatch(expected_sha) is None:
        raise production.ProductionGateError("EXPECTED_PLAN_SHA256_INVALID")
    parent_fd = os.open(
        path.parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        file_fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
    except Exception:
        os.close(parent_fd)
        raise
    try:
        parent_metadata = os.fstat(parent_fd)
        metadata = os.fstat(file_fd)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > production.MAX_DOCUMENT_BYTES
        ):
            raise production.ProductionGateError("PLAN_FILE_METADATA_INVALID")
        data = production._read_fd_bytes(
            file_fd,
            maximum=production.MAX_DOCUMENT_BYTES,
            code="PLAN_FILE_TOO_LARGE",
        )
        actual_sha = production._sha256_bytes(data)
        if actual_sha != expected_sha:
            raise production.ProductionGateError("PLAN_SHA256_MISMATCH")
        try:
            payload = json.loads(data.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise production.ProductionGateError("PLAN_JSON_INVALID") from exc
        if not isinstance(payload, dict) or production._canonical_json(payload) != data:
            raise production.ProductionGateError("PLAN_JSON_NOT_CANONICAL")
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (path_metadata.st_dev, path_metadata.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise production.ProductionGateError("PLAN_PATH_SUBSTITUTION")
        parent_identity = (
            int(parent_metadata.st_dev),
            int(parent_metadata.st_ino),
        )
        identity = (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
            int(metadata.st_uid),
            int(metadata.st_gid),
            stat.S_IMODE(metadata.st_mode),
            int(metadata.st_nlink),
        )
        return production.PinnedPlan(
            path=path,
            parent_fd=parent_fd,
            file_fd=file_fd,
            parent_identity=parent_identity,
            identity=identity,
            expected_uid=int(metadata.st_uid),
            payload=payload,
            sha256=actual_sha,
        )
    except Exception:
        os.close(file_fd)
        os.close(parent_fd)
        raise


def _fake_image_inspect(
    image_id: str,
    expected_revision: str | None = None,
) -> str:
    if image_id not in {IMAGE_ID, PREVIOUS_IMAGE_ID}:
        raise production.ProductionGateError("IMAGE_NOT_AVAILABLE")
    if expected_revision is not None and expected_revision != REVISION:
        raise production.ProductionGateError("IMAGE_REVISION_MISMATCH")
    return expected_revision or REVISION


def _install_unit_root(
    monkeypatch: pytest.MonkeyPatch,
    repository: Path,
) -> None:
    monkeypatch.setattr(production, "REPO_ROOT", repository)
    monkeypatch.setattr(
        production,
        "validate_trusted_parent_chain",
        lambda path, *, expected_uid=None: (
            authority.validate_trusted_parent_chain(
                path,
                expected_uid=os.geteuid(),
            )
        ),
    )
    monkeypatch.setattr(
        production,
        "_root_identity",
        lambda: production.RootIdentity(
            effective_uid=0,
            effective_gid=0,
            username="root",
            process_uid=0,
            process_gid=0,
        ),
    )
    monkeypatch.setattr(production, "_directory_record", _fake_directory_record)
    monkeypatch.setattr(
        production,
        "_assert_root_private_directory",
        lambda _path, _code: None,
    )
    monkeypatch.setattr(
        production,
        "_assert_source_parent_controlled",
        lambda _path: None,
    )
    monkeypatch.setattr(production, "_write_json_durable_at", _unit_write_json_at)
    monkeypatch.setattr(production, "_open_plan", _unit_open_plan)
    monkeypatch.setattr(production, "_inspect_image", _fake_image_inspect)
    monkeypatch.setattr(
        production,
        "_repository_provenance",
        lambda _repository: (REVISION, TREE_SHA),
    )
    monkeypatch.setattr(
        production,
        "load_execution_authority",
        lambda **_kwargs: _UnitExecutionAuthority(),
    )
    monkeypatch.setattr(staged, "_inspect_image", _fake_image_inspect)


def _unit_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    root_context: bool = True,
) -> UnitContext:
    tmp_path.mkdir(parents=True, exist_ok=True)
    os.chmod(tmp_path, 0o700)
    monkeypatch.setattr(authority, "TRUSTED_FILESYSTEM_ANCHOR", tmp_path)
    repository = _copy_repository(tmp_path / "repository")
    runtime = _private_directory(tmp_path / "runtime")
    source = _source(runtime / "source" / "database.sqlite")
    backup = _private_directory(runtime / "backups")
    staging = _private_directory(runtime / "staging")
    evidence = _private_directory(runtime / "evidence")
    evidence_inputs = _private_directory(runtime / "evidence-inputs")
    monkeypatch.setattr(production, "REPO_ROOT", repository)
    if root_context:
        _install_unit_root(monkeypatch, repository)
    context = UnitContext(
        root=tmp_path,
        repository=repository,
        runtime=runtime,
        source=source,
        backup=backup,
        staging=staging,
        evidence=evidence,
        operations_root_approval=(
            evidence_inputs / "approved-operations-root.json"
        ),
        clean_start_policy=evidence_inputs / "clean-start-data-policy.json",
    )
    _write_unit_evidence(context)
    return context


def _plan_argv(context: UnitContext) -> list[str]:
    metadata = context.source.stat()
    return [
        "plan",
        "--repository-root",
        str(context.repository),
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
        production._sha256(context.operations_root_approval),
        "--clean-start-policy",
        str(context.clean_start_policy),
        "--expected-clean-start-policy-sha256",
        production._sha256(context.clean_start_policy),
        "--migration-image-id",
        IMAGE_ID,
        "--migration-image-revision",
        REVISION,
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
        production._sha256(context.source),
        "--expected-free-bytes",
        "1",
        "--expires-in-seconds",
        "3600",
    ]


def _json_results(
    capfd: pytest.CaptureFixture[str],
) -> list[tuple[dict[str, Any], str]]:
    captured = capfd.readouterr()
    results: list[tuple[dict[str, Any], str]] = []
    for stream_name, value in (("stderr", captured.err), ("stdout", captured.out)):
        for line in value.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                results.append((payload, stream_name))
    if not results:
        raise AssertionError(
            f"no JSON result; stdout={captured.out!r}; stderr={captured.err!r}"
        )
    return results


def _json_result(
    capfd: pytest.CaptureFixture[str],
) -> tuple[dict[str, Any], str]:
    results = _json_results(capfd)
    for stream_name in ("stderr", "stdout"):
        matching = [
            result
            for result in results
            if result[1] == stream_name
        ]
        if matching:
            return matching[-1]
    raise AssertionError("unreachable")


def _create_plan(
    context: UnitContext,
    capfd: pytest.CaptureFixture[str],
) -> PlannedContext:
    assert production.main(_plan_argv(context)) == 0
    output, stream = _json_result(capfd)
    assert stream == "stdout"
    path = Path(str(output["plan_path"]))
    payload = json.loads(path.read_text(encoding="ascii"))
    return PlannedContext(
        unit=context,
        path=path,
        payload=payload,
        sha256=str(output["plan_sha256"]),
    )


def _rewrite_plan(plan: PlannedContext) -> None:
    production._write_json_durable(plan.path, plan.payload)
    plan.sha256 = production._sha256(plan.path)


def _execute_argv(
    plan: PlannedContext,
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
        "final_authority": str(
            plan.unit.root / "synthetic-final-authority.json"
        ),
        "expected_final_authority_sha256": "a" * 64,
    }
    values.update(overrides)
    return [
        "execute",
        "--plan",
        str(plan.path),
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
        "--final-authority",
        values["final_authority"],
        "--expected-final-authority-sha256",
        values["expected_final_authority_sha256"],
    ]


def _set_manifest_feature(
    context: UnitContext,
    name: str,
    value: object,
    *,
    remove: bool = False,
) -> None:
    payload = json.loads(context.manifest.read_text(encoding="utf-8"))
    if remove:
        payload["feature_gates"].pop(name)
    else:
        payload["feature_gates"][name] = value
    context.manifest.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(context.manifest, 0o600)


def _install_staged_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error_type: str,
    publish_state: str,
    target_may_have_changed: bool,
) -> None:
    def fail(
        _args: Any,
        *,
        prepared: Any,
    ) -> int:
        prepared.close()
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "error_type": error_type,
                    "exit_classification": error_type,
                    "publish_state": publish_state,
                    "target_may_have_changed": target_may_have_changed,
                    "automatic_retry_allowed": False,
                    "manual_recovery_required": target_may_have_changed,
                    "backup_available": False,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1

    monkeypatch.setattr(production, "_execute_authorized_staged", fail)


def _install_staged_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def succeed(
        _args: Any,
        *,
        prepared: Any,
    ) -> int:
        prepared.close()
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "publish_state": "FINAL_VERIFIED",
                    "target_may_have_changed": True,
                    "automatic_retry_allowed": False,
                    "manual_recovery_required": False,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0

    monkeypatch.setattr(production, "_execute_authorized_staged", succeed)
    monkeypatch.setattr(
        production,
        "_read_internal_manifest",
        lambda *_args, **_kwargs: {
            "STATE": "VERIFIED",
            "PUBLISH_STATE": "FINAL_VERIFIED",
            "BACKUP_SHA256": "4" * 64,
            "STAGING_SHA256": "5" * 64,
        },
    )
    target = staged._target_schema_contract()
    monkeypatch.setattr(
        production,
        "_target_schema_fingerprint",
        lambda _path: target.fingerprint,
    )


@pytest.mark.parametrize("helper", ("sha256", "document"))
def test_descriptor_reads_reject_premature_eof(
    helper: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "descriptor.bin"
    path.write_bytes(b"complete-descriptor")
    fd = os.open(path, os.O_RDONLY)
    original_pread = os.pread
    calls = 0

    def premature_eof(
        target_fd: int,
        size: int,
        offset: int,
    ) -> bytes:
        nonlocal calls
        if target_fd != fd:
            return original_pread(target_fd, size, offset)
        calls += 1
        if calls == 1:
            return original_pread(target_fd, min(4, size), offset)
        return b""

    monkeypatch.setattr(production.os, "pread", premature_eof)
    try:
        with pytest.raises(
            production.ProductionGateError,
            match=(
                "SOURCE_READ_CONTRACT_VIOLATION"
                if helper == "sha256"
                else "DOCUMENT_READ_CONTRACT_VIOLATION"
            ),
        ):
            if helper == "sha256":
                production._sha256_fd(
                    fd,
                    code="SOURCE_READ_CONTRACT_VIOLATION",
                )
            else:
                production._read_fd_bytes(
                    fd,
                    maximum=1024,
                    code="DOCUMENT_READ_CONTRACT_VIOLATION",
                )
    finally:
        os.close(fd)


def test_public_contract_has_one_production_surface_and_no_callback() -> None:
    parser = production.build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert set(choices) == {"plan", "execute"}
    plan_help = choices["plan"].format_help()
    execute_help = choices["execute"].format_help()
    assert "--repository-root" in plan_help
    assert "--operations-root-approval" in plan_help
    assert "--expected-operations-root-approval-sha256" in plan_help
    assert "--clean-start-policy" in plan_help
    assert "--expected-clean-start-policy-sha256" in plan_help
    assert "--confirm-operations-root-approval-sha256" in execute_help
    assert "--confirm-clean-start-policy-sha256" in execute_help
    assert "--final-authority" in execute_help
    assert "--expected-final-authority-sha256" in execute_help
    assert "--deployment-contract" not in plan_help
    assert "--target-schema-version" not in plan_help
    combined = f"{plan_help}\n{execute_help}"
    for forbidden in (
        "--phase-callback",
        "--failure-hook",
        "--crash-hook",
        "--allow-non-root",
        "--skip-root-check",
        "--force",
    ):
        assert forbidden not in combined
    assert not hasattr(staged, "execute_production_staged")
    with pytest.raises(staged.OrchestratorError, match="PRODUCTION_AUTHORIZATION_REQUIRED"):
        staged._execute_staged(object(), synthetic=False)


def test_production_entrypoint_has_no_environment_or_test_hook_bypass() -> None:
    source = Path(production.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv(" not in source
    assert "phase_callback" not in source
    assert "failure_callback" not in source
    assert "before_exchange_callback" not in source
    assert "shell=True" not in source
    assert "execute_production_staged" not in source


def test_runbook_documents_exact_evidence_binding_contract() -> None:
    runbook = (
        Path(__file__).resolve().parents[2]
        / "docs/runbooks/RUNBOOK_WEEKLY_SHOPPING_FEATURE_DISABLED_ROLLOUT.md"
    ).read_text(encoding="utf-8")
    for required in (
        "--operations-root-approval",
        "--expected-operations-root-approval-sha256",
        "--clean-start-policy",
        "--expected-clean-start-policy-sha256",
        "--confirm-operations-root-approval-sha256",
        "--confirm-clean-start-policy-sha256",
        "--final-authority",
        "--expected-final-authority-sha256",
        "NO_CLIENTS_CLEAN_START",
        "plan schema version 4",
        "no generic force or skip-validation flag exists",
    ):
        assert required in runbook


def test_valid_public_plan_records_root_and_canonical_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    before_hash = production._sha256(context.source)
    plan = _create_plan(context, capfd)
    payload = plan.payload
    target = staged._target_schema_contract()
    assert payload["PLAN_CREATOR_UID"] == 0
    assert payload["PLAN_CREATOR_GID"] == 0
    assert payload["PLAN_CREATOR_USERNAME"] == "root"
    assert payload["PLAN_CREATOR_PROCESS_UID"] == 0
    assert payload["PLAN_CREATOR_PROCESS_GID"] == 0
    assert payload["DEPLOYMENT_CONTRACT_CANONICAL_PATH"] == str(context.manifest)
    assert payload["DEPLOYMENT_CONTRACT_SHA256"] == production._sha256(context.manifest)
    assert payload["DEPLOYMENT_CONTRACT_VERSION"] == 1
    assert payload["OPERATIONS_ROOT_APPROVAL_PATH"] == str(
        context.operations_root_approval
    )
    assert payload["OPERATIONS_ROOT_APPROVAL_SHA256"] == production._sha256(
        context.operations_root_approval
    )
    assert payload["OPERATIONS_ROOT_APPROVAL_MODE"] == 0o600
    assert payload["OPERATIONS_ROOT_APPROVAL_TREE_SHA"] == TREE_SHA
    assert payload["CLEAN_START_POLICY_PATH"] == str(
        context.clean_start_policy
    )
    assert payload["CLEAN_START_POLICY_SHA256"] == production._sha256(
        context.clean_start_policy
    )
    assert payload["CLEAN_START_POLICY_MODE"] == 0o600
    assert payload["CLEAN_START_POLICY_VERSION"] == 1
    assert payload["CLEAN_START_DATA_POLICY"] == "NO_CLIENTS_CLEAN_START"
    assert payload["EXPECTED_FEATURE_FLAGS"] == production.EXPECTED_FEATURE_FLAGS
    assert payload["TARGET_SCHEMA_VERSION"] == target.version
    assert payload["TARGET_SCHEMA_FINGERPRINT"] == target.fingerprint
    assert payload["SOURCE_USER_VERSION"] == 0
    assert payload["SOURCE_PARENT_IDENTITY"]["PATH"] == str(
        context.source.parent
    )
    assert payload["PLAN_READ_ONLY"] is True
    assert payload["PLAN_DATABASE_MUTATION"] is False
    assert list(context.backup.iterdir()) == []
    assert list(context.staging.iterdir()) == []
    assert production._sha256(context.source) == before_hash


@pytest.mark.parametrize(
    "option",
    (
        "--operations-root-approval",
        "--expected-operations-root-approval-sha256",
        "--clean-start-policy",
        "--expected-clean-start-policy-sha256",
    ),
)
def test_plan_parser_requires_each_evidence_binding(
    option: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    argv = _plan_argv(context)
    index = argv.index(option)
    del argv[index : index + 2]
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(argv) == 2
    result, stream = _json_result(capfd)
    assert stream == "stderr"
    assert result["exit_classification"] == "ARGUMENT_ERROR"
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) == before_paths


@pytest.mark.parametrize(
    "option",
    ("--final-authority", "--expected-final-authority-sha256"),
)
def test_execute_parser_requires_final_authority_binding(
    option: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    argv = _execute_argv(plan)
    index = argv.index(option)
    del argv[index : index + 2]
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(argv) == 2
    result, stream = _json_result(capfd)
    assert stream == "stderr"
    assert result["exit_classification"] == "ARGUMENT_ERROR"
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) == before_paths


def test_execution_authority_failure_precedes_source_and_quiescence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    source_reads = 0
    quiescence_calls = 0

    def deny_authority(**_kwargs: Any) -> Any:
        raise production.ExecutionAuthorityError("FINAL_AUTHORITY_SHA256_MISMATCH")

    def forbidden_source_read(_path: Path) -> Any:
        nonlocal source_reads
        source_reads += 1
        raise AssertionError("source DB opened after static authority denial")

    def forbidden_quiescence(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal quiescence_calls
        quiescence_calls += 1
        raise AssertionError("quiescence reached after static authority denial")

    monkeypatch.setattr(production, "load_execution_authority", deny_authority)
    monkeypatch.setattr(production, "_read_only_source", forbidden_source_read)
    monkeypatch.setattr(
        production,
        "_prepare_authorized_production_execution",
        forbidden_quiescence,
    )
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(_execute_argv(plan)) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == "FINAL_AUTHORITY_SHA256_MISMATCH"
    assert source_reads == 0
    assert quiescence_calls == 0
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) == before_paths


def test_plan_rejects_expired_root_approval_before_source_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    approval = json.loads(
        context.operations_root_approval.read_text(encoding="ascii")
    )
    approval["CREATED_AT"] = production._timestamp(
        production._now() - timedelta(hours=2)
    )
    approval["EXPIRES_AT"] = production._timestamp(
        production._now() - timedelta(hours=1)
    )
    _write_canonical_document(context.operations_root_approval, approval)
    source_calls = 0

    def forbidden_source_open(_path: Path) -> Any:
        nonlocal source_calls
        source_calls += 1
        raise AssertionError("source DB must not open before approval")

    monkeypatch.setattr(production, "_read_only_source", forbidden_source_open)
    assert production.main(_plan_argv(context)) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == "OPERATIONS_ROOT_APPROVAL_EXPIRED"
    assert source_calls == 0


@pytest.mark.parametrize(
    ("document_name", "field", "expected_class"),
    (
        (
            "operations_root_approval",
            "PRODUCTION_DB_ACCESS_AUTHORIZED",
            "OPERATIONS_ROOT_APPROVAL_MISMATCH",
        ),
        (
            "clean_start_policy",
            "DELETION_PERFORMED",
            "CLEAN_START_POLICY_MISMATCH",
        ),
    ),
)
def test_evidence_boolean_fields_reject_integer_aliases(
    document_name: str,
    field: str,
    expected_class: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    path = getattr(context, document_name)
    payload = json.loads(path.read_text(encoding="ascii"))
    payload[field] = int(bool(payload[field]))
    _write_canonical_document(path, payload)
    before_hash = production._sha256(context.source)
    assert production.main(_plan_argv(context)) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == expected_class
    assert production._sha256(context.source) == before_hash
    assert list(context.evidence.iterdir()) == []


@pytest.mark.parametrize(
    ("override", "expected_class"),
    (
        (
            "confirm_operations_root_approval_sha256",
            "PLAN_OPERATIONS_ROOT_APPROVAL_SHA256_CONFIRMATION_MISMATCH",
        ),
        (
            "confirm_clean_start_policy_sha256",
            "PLAN_CLEAN_START_POLICY_SHA256_CONFIRMATION_MISMATCH",
        ),
    ),
)
def test_execute_requires_exact_evidence_confirmation_before_source_open(
    override: str,
    expected_class: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    source_calls = 0

    def forbidden_source_open(_path: Path) -> Any:
        nonlocal source_calls
        source_calls += 1
        raise AssertionError("source DB must not open before confirmations")

    monkeypatch.setattr(production, "_read_only_source", forbidden_source_open)
    assert production.main(_execute_argv(plan, **{override: "f" * 64})) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == expected_class
    assert source_calls == 0


def test_execute_rejects_policy_replacement_before_source_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    policy = json.loads(context.clean_start_policy.read_text(encoding="ascii"))
    policy["DELETION_PERFORMED"] = True
    _write_canonical_document(context.clean_start_policy, policy)
    monkeypatch.setattr(
        production,
        "_read_only_source",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("source DB must not open after policy replacement")
        ),
    )
    assert production.main(_execute_argv(plan)) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == "CLEAN_START_POLICY_SHA256_MISMATCH"


def test_execute_revalidates_expired_approval_before_source_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    approval = json.loads(
        context.operations_root_approval.read_text(encoding="ascii")
    )
    approval["CREATED_AT"] = production._timestamp(
        production._now() - timedelta(hours=2)
    )
    approval["EXPIRES_AT"] = production._timestamp(
        production._now() - timedelta(hours=1)
    )
    _write_canonical_document(context.operations_root_approval, approval)
    metadata = context.operations_root_approval.stat()
    approval_sha = production._sha256(context.operations_root_approval)
    plan.payload.update(
        {
            "OPERATIONS_ROOT_APPROVAL_SIZE": metadata.st_size,
            "OPERATIONS_ROOT_APPROVAL_SHA256": approval_sha,
            "OPERATIONS_ROOT_APPROVAL_EXPIRES_AT": approval["EXPIRES_AT"],
        }
    )
    _rewrite_plan(plan)
    monkeypatch.setattr(
        production,
        "_read_only_source",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("source DB must not open after approval expiry")
        ),
    )
    assert production.main(
        _execute_argv(
            plan,
            confirm_operations_root_approval_sha256=approval_sha,
        )
    ) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == "OPERATIONS_ROOT_APPROVAL_EXPIRED"


@pytest.mark.parametrize("case", PARSER_CASES)
def test_public_parser_negative_matrix_has_zero_deltas(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    argv = _plan_argv(context)
    if case == "missing_subcommand":
        argv = []
    elif case == "missing_repository_root":
        argv = ["plan"]
    elif case == "arbitrary_deployment_contract":
        argv += ["--deployment-contract", str(context.manifest)]
    elif case == "caller_target_schema_version":
        argv += ["--target-schema-version", "caller-controlled"]
    elif case == "production_callback_argument":
        argv = [
            "execute",
            "--plan",
            str(context.runtime / "plan.json"),
            "--expected-plan-sha256",
            "0" * 64,
            "--confirm-operation-id",
            "0" * 32,
            "--confirm-source-sha256",
            "0" * 64,
            "--confirm-image-revision",
            REVISION,
            "--phase-callback",
            "unsafe",
        ]
    elif case == "root_bypass_argument":
        argv += ["--allow-non-root"]
    elif case == "force_argument":
        argv += ["--force"]
    elif case == "crash_hook_argument":
        argv += ["--crash-hook", "after-exchange"]
    else:
        raise AssertionError(case)
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(argv) == 2
    results = _json_results(capfd)
    assert len(results) == 1
    result, stream = results[0]
    assert stream == "stderr"
    assert result["exit_classification"] == "ARGUMENT_ERROR"
    assert result["publish_state"] == "BEFORE_EXCHANGE"
    assert result["target_may_have_changed"] is False
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is False
    assert result["durable_evidence_updated"] is False
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) == before_paths


@pytest.mark.parametrize(("case", "expected_class"), PLAN_GATE_CASES)
def test_public_plan_gate_matrix_is_fail_closed_with_exact_deltas(
    case: str,
    expected_class: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(
        tmp_path,
        monkeypatch,
        root_context=case != "non_root_plan",
    )
    argv = _plan_argv(context)
    if case == "non_root_plan":
        monkeypatch.setattr(
            production,
            "_root_identity",
            lambda: (_ for _ in ()).throw(
                production.ProductionGateError("ROOT_EUID_REQUIRED")
            ),
        )
    elif case == "repository_root_mismatch":
        other = _private_directory(tmp_path / "other-repository")
        argv[argv.index("--repository-root") + 1] = str(other)
    elif case == "canonical_contract_symlink":
        target = context.manifest.with_name("manifest-copy.json")
        shutil.copy2(context.manifest, target)
        context.manifest.unlink()
        context.manifest.symlink_to(target)
    elif case == "canonical_contract_not_regular":
        context.manifest.unlink()
        context.manifest.mkdir(mode=0o700)
    elif case == "malformed_contract":
        context.manifest.write_text("{}", encoding="utf-8")
    elif case == "household_flag_missing":
        _set_manifest_feature(
            context,
            "HEALBITE_HOUSEHOLDS_ENABLED",
            False,
            remove=True,
        )
    elif case == "household_flag_enabled":
        _set_manifest_feature(context, "HEALBITE_HOUSEHOLDS_ENABLED", True)
    elif case == "household_flag_string_false":
        _set_manifest_feature(context, "HEALBITE_HOUSEHOLDS_ENABLED", "false")
    elif case == "shopping_flag_missing":
        _set_manifest_feature(
            context,
            "HEALBITE_SHOPPING_LIST_ENABLED",
            False,
            remove=True,
        )
    elif case == "shopping_flag_enabled":
        _set_manifest_feature(context, "HEALBITE_SHOPPING_LIST_ENABLED", True)
    elif case == "shopping_flag_string_false":
        _set_manifest_feature(context, "HEALBITE_SHOPPING_LIST_ENABLED", "false")
    elif case == "invalid_source_sha":
        argv[argv.index("--expected-source-sha256") + 1] = "invalid"
    elif case == "invalid_image_revision":
        argv[argv.index("--migration-image-revision") + 1] = "main"
    elif case == "expiry_too_short":
        argv[argv.index("--expires-in-seconds") + 1] = "59"
    elif case == "expiry_too_long":
        argv[argv.index("--expires-in-seconds") + 1] = "86401"
    elif case == "hostname_mismatch":
        argv[argv.index("--expected-hostname") + 1] = "other-host"
    elif case == "expected_source_device_mismatch":
        index = argv.index("--expected-source-device") + 1
        argv[index] = str(int(argv[index]) + 1)
    elif case == "expected_source_inode_mismatch":
        index = argv.index("--expected-source-inode") + 1
        argv[index] = str(int(argv[index]) + 1)
    elif case == "expected_source_size_mismatch":
        index = argv.index("--expected-source-size") + 1
        argv[index] = str(int(argv[index]) + 1)
    elif case == "expected_source_hash_mismatch":
        argv[argv.index("--expected-source-sha256") + 1] = "f" * 64
    elif case == "insufficient_free_space":
        argv[argv.index("--expected-free-bytes") + 1] = str(2**63)
    elif case == "migration_image_missing":
        argv[argv.index("--migration-image-id") + 1] = "sha256:" + "8" * 64
    elif case == "previous_image_missing":
        argv[argv.index("--previous-image-id") + 1] = "sha256:" + "9" * 64
    elif case == "unsupported_sidecar":
        Path(f"{context.source}-journal").touch()
    elif case == "source_mode_unsafe":
        os.chmod(context.source, 0o640)
    elif case == "evidence_parent_mode_unsafe":
        os.chmod(context.evidence, 0o750)
    elif case == "cross_filesystem_staging":
        original = production._directory_record

        def drift(path: Path, *, private: bool) -> dict[str, int | str]:
            result = original(path, private=private)
            if path == context.staging:
                result["DEVICE"] = int(result["DEVICE"]) + 1
            return result

        monkeypatch.setattr(production, "_directory_record", drift)
    else:
        raise AssertionError(case)

    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(argv) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == expected_class
    assert result["publish_state"] == "BEFORE_EXCHANGE"
    assert result["target_may_have_changed"] is False
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is False
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) == before_paths


@pytest.mark.parametrize(("case", "expected_class"), EXECUTE_GATE_CASES)
def test_public_execute_gate_matrix_is_fail_closed_with_exact_deltas(
    case: str,
    expected_class: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    argv_overrides: dict[str, str] = {}
    reader: sqlite3.Connection | None = None
    writer: sqlite3.Connection | None = None

    try:
        if case == "non_root_execute":
            monkeypatch.setattr(
                production,
                "_root_identity",
                lambda: (_ for _ in ()).throw(
                    production.ProductionGateError("ROOT_EUID_REQUIRED")
                ),
            )
        elif case == "plan_sha_invalid":
            argv_overrides["expected_plan_sha256"] = "invalid"
        elif case == "plan_sha_mismatch":
            argv_overrides["expected_plan_sha256"] = "f" * 64
        elif case == "operation_id_mismatch":
            argv_overrides["confirm_operation_id"] = "f" * 32
        elif case == "source_confirmation_mismatch":
            argv_overrides["confirm_source_sha256"] = "f" * 64
        elif case == "image_confirmation_mismatch":
            argv_overrides["confirm_image_revision"] = "f" * 40
        elif case == "expired_plan":
            plan.payload["CREATED_AT"] = production._timestamp(
                production._now() - timedelta(hours=2)
            )
            plan.payload["EXPIRES_AT"] = production._timestamp(
                production._now() - timedelta(hours=1)
            )
            _rewrite_plan(plan)
        elif case == "plan_fields_extra":
            plan.payload["UNTRUSTED"] = True
            _rewrite_plan(plan)
        elif case == "plan_creator_uid_mismatch":
            plan.payload["PLAN_CREATOR_UID"] = 1
            _rewrite_plan(plan)
        elif case == "plan_creator_gid_mismatch":
            plan.payload["PLAN_CREATOR_GID"] = 1
            _rewrite_plan(plan)
        elif case == "plan_symlink_substitution":
            link = plan.path.parent / "plan-link.json"
            link.symlink_to(plan.path)
            plan.path = link
        elif case == "plan_path_substitution":
            alias = plan.path.parent / "plan-alias.json"
            os.link(plan.path, alias)
            plan.path = alias
        elif case == "source_inode_drift":
            replacement = context.source.with_name("replacement.sqlite")
            shutil.copy2(context.source, replacement)
            os.chmod(replacement, 0o600)
            os.replace(replacement, context.source)
        elif case == "source_sha_drift":
            with sqlite3.connect(context.source) as connection:
                connection.execute("INSERT INTO legacy_rows VALUES ('drift')")
        elif case == "source_schema_drift":
            old_schema = plan.payload["SOURCE_SCHEMA_FINGERPRINT"]
            with sqlite3.connect(context.source) as connection:
                connection.execute("CREATE TABLE schema_drift (value TEXT)")
            identity, _schema, _integrity, _fk = production._read_only_source(
                context.source
            )
            plan.payload.update(identity)
            plan.payload["SOURCE_SCHEMA_FINGERPRINT"] = old_schema
            _rewrite_plan(plan)
            argv_overrides["confirm_source_sha256"] = str(
                plan.payload["SOURCE_SHA256"]
            )
            policy = json.loads(
                context.clean_start_policy.read_text(encoding="ascii")
            )
            policy["PRODUCTION_DB_SOURCE_SHA256"] = str(
                plan.payload["SOURCE_SHA256"]
            )
            _write_canonical_document(context.clean_start_policy, policy)
            policy_sha = production._sha256(context.clean_start_policy)
            plan.payload["CLEAN_START_POLICY_SHA256"] = policy_sha
            _rewrite_plan(plan)
            argv_overrides["confirm_clean_start_policy_sha256"] = policy_sha
        elif case == "source_mode_drift":
            os.chmod(context.source, 0o640)
        elif case == "deployment_contract_replaced":
            context.manifest.write_text(
                context.manifest.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )
        elif case == "target_schema_version_mismatch":
            plan.payload["TARGET_SCHEMA_VERSION"] = "untrusted-version"
            _rewrite_plan(plan)
        elif case == "target_schema_fingerprint_mismatch":
            plan.payload["TARGET_SCHEMA_FINGERPRINT"] = "f" * 64
            _rewrite_plan(plan)
        elif case == "active_sqlite_reader":
            reader = sqlite3.connect(context.source, timeout=0)
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM legacy_rows").fetchall()
        elif case == "active_sqlite_writer":
            writer = sqlite3.connect(context.source, timeout=0)
            writer.execute("BEGIN IMMEDIATE")
        elif case == "unsupported_sidecar_after_plan":
            Path(f"{context.source}-journal").touch()
        elif case == "operation_artifact_collision":
            (plan.path.parent / "execution.json").write_text(
                "{}",
                encoding="ascii",
            )
        elif case == "image_revision_drift":
            def image_drift(
                image_id: str,
                expected_revision: str | None,
            ) -> str:
                if image_id == IMAGE_ID and expected_revision is not None:
                    raise production.ProductionGateError(
                        "IMAGE_REVISION_MISMATCH"
                    )
                return REVISION

            monkeypatch.setattr(production, "_inspect_image", image_drift)
        else:
            raise AssertionError(case)

        before_hash = production._sha256(context.source)
        before_paths = _path_set(context.runtime)
        assert production.main(_execute_argv(plan, **argv_overrides)) == 1
        result, _stream = _json_result(capfd)
        assert result["exit_classification"] == expected_class
        assert result["publish_state"] == "BEFORE_EXCHANGE"
        assert result["target_may_have_changed"] is False
        assert result["automatic_retry_allowed"] is False
        assert result["manual_recovery_required"] is False
        assert production._sha256(context.source) == before_hash
        assert _path_set(context.runtime) == before_paths
        assert not (plan.path.parent / "execution.json").exists() or (
            case == "operation_artifact_collision"
        )
    finally:
        if reader is not None:
            reader.rollback()
            reader.close()
        if writer is not None:
            writer.rollback()
            writer.close()


@pytest.mark.parametrize("case", SCHEMA_FAILURE_CASES)
def test_public_schema_failure_cases_preserve_database_and_report_exact_delta(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    error_type = "TARGET_SCHEMA_CONTRACT_MISMATCH"
    _install_staged_failure(
        monkeypatch,
        error_type=error_type,
        publish_state="BEFORE_EXCHANGE",
        target_may_have_changed=False,
    )
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(_execute_argv(plan)) == 1
    result, stream = _json_result(capfd)
    assert stream == "stdout"
    assert result["exit_classification"] == error_type
    assert result["publish_state"] == "BEFORE_EXCHANGE"
    assert result["target_may_have_changed"] is False
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is False
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) - before_paths == {
        str(
            (plan.path.parent / "execution.json").relative_to(context.runtime)
        )
    }


@pytest.mark.parametrize("case", POST_EXCHANGE_CASES)
def test_public_post_exchange_faults_are_always_publish_uncertain(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    expected_publish_state = "FINAL_VERIFIED"

    if case == "external_evidence_write_failure":
        _install_staged_failure(
            monkeypatch,
            error_type="PUBLISH_UNCERTAIN",
            publish_state="PARENT_FSYNCED",
            target_may_have_changed=True,
        )
        expected_publish_state = "PARENT_FSYNCED"
        original_writer = production._write_json_durable_at
        writes = 0

        def fail_later(
            parent_fd: int,
            name: str,
            payload: dict[str, Any],
        ) -> None:
            nonlocal writes
            if name == "execution.json":
                writes += 1
                if writes >= 2:
                    raise OSError("synthetic evidence failure")
            original_writer(parent_fd, name, payload)

        monkeypatch.setattr(
            production,
            "_write_json_durable_at",
            fail_later,
        )
    elif case == "operation_cleanup_failure":
        expected_publish_state = "EXCHANGE_STARTED"

        def fail_after_authorization(
            _args: Any,
            *,
            prepared: Any,
        ) -> int:
            prepared.close()
            raise OSError("synthetic cleanup failure")

        monkeypatch.setattr(
            production,
            "_execute_authorized_staged",
            fail_after_authorization,
        )
    else:
        actual_manifest_reader = production._read_internal_manifest
        _install_staged_success(monkeypatch)
        if case == "internal_manifest_read_failure":
            monkeypatch.setattr(
                production,
                "_read_internal_manifest",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    production.ProductionGateError(
                        "INTERNAL_MANIFEST_INVALID"
                    )
                ),
            )
        elif case == "internal_manifest_close_failure":
            manifest_path = context.backup / (
                f"manifest-{plan.payload['OPERATION_ID']}.json"
            )
            def succeed_with_manifest(
                _args: Any,
                *,
                prepared: Any,
            ) -> int:
                prepared.close()
                production._write_json_durable(
                    manifest_path,
                    {
                        "OPERATION_ID": plan.payload["OPERATION_ID"],
                        "STATE": "VERIFIED",
                        "PUBLISH_STATE": "FINAL_VERIFIED",
                        "BACKUP_SHA256": "4" * 64,
                        "STAGING_SHA256": "5" * 64,
                    },
                )
                os.chmod(manifest_path, 0o600)
                print(
                    json.dumps(
                        {
                            "status": "PASS",
                            "publish_state": "FINAL_VERIFIED",
                            "target_may_have_changed": True,
                            "automatic_retry_allowed": False,
                            "manual_recovery_required": False,
                        },
                        ensure_ascii=True,
                        sort_keys=True,
                    )
                )
                return 0

            monkeypatch.setattr(
                production,
                "_execute_authorized_staged",
                succeed_with_manifest,
            )
            monkeypatch.setattr(
                production,
                "_read_internal_manifest",
                actual_manifest_reader,
            )
            original_reader = production._read_fd_bytes
            original_close = production.os.close
            manifest_fd: int | None = None

            def track_manifest_fd(
                fd: int,
                *,
                maximum: int,
                code: str,
            ) -> bytes:
                nonlocal manifest_fd
                if code == "INTERNAL_MANIFEST_INVALID":
                    manifest_fd = fd
                return original_reader(
                    fd,
                    maximum=maximum,
                    code=code,
                )

            def fail_manifest_close(fd: int) -> None:
                nonlocal manifest_fd
                if fd == manifest_fd:
                    manifest_fd = None
                    original_close(fd)
                    raise OSError(
                        "synthetic manifest descriptor close failure"
                    )
                original_close(fd)

            monkeypatch.setattr(
                production,
                "_read_fd_bytes",
                track_manifest_fd,
            )
            monkeypatch.setattr(
                production.os,
                "close",
                fail_manifest_close,
            )
        elif case == "final_target_validation_failure":
            def install_validation_failure(
                _args: Any,
                *,
                prepared: Any,
            ) -> int:
                prepared.close()
                monkeypatch.setattr(
                    production,
                    "_read_only_source",
                    lambda _path: (_ for _ in ()).throw(
                        production.ProductionGateError(
                            "FINAL_DATABASE_VALIDATION_FAILED"
                        )
                    ),
                )
                print(
                    json.dumps(
                        {
                            "status": "PASS",
                            "publish_state": "FINAL_VERIFIED",
                        }
                    )
                )
                return 0

            monkeypatch.setattr(
                production,
                "_execute_authorized_staged",
                install_validation_failure,
            )
        elif case == "completed_transition_failure":
            original_transition = production.ExecutionEvidence.transition

            def fail_completed(
                self: production.ExecutionEvidence,
                state: str,
                **updates: Any,
            ) -> None:
                if state == "COMPLETED":
                    raise OSError("synthetic completed transition failure")
                original_transition(self, state, **updates)

            monkeypatch.setattr(
                production.ExecutionEvidence,
                "transition",
                fail_completed,
            )
        elif case == "plan_path_revalidation_failure":
            original_path_matches = production.PinnedPlan.path_matches
            calls = 0

            def fail_final_path_check(
                self: production.PinnedPlan,
            ) -> bool:
                nonlocal calls
                calls += 1
                if calls >= 4:
                    return False
                return original_path_matches(self)

            monkeypatch.setattr(
                production.PinnedPlan,
                "path_matches",
                fail_final_path_check,
            )
        elif case == "final_target_hash_failure":
            monkeypatch.setattr(
                production,
                "_target_schema_fingerprint",
                lambda _path: (_ for _ in ()).throw(
                    production.ProductionGateError(
                        "FINAL_TARGET_HASH_FAILED"
                    )
                ),
            )
        elif case == "final_result_emit_failure":
            original_emit = production._json_emit

            def fail_final_result(
                payload: dict[str, Any],
                *,
                stream: Any = None,
            ) -> None:
                if (
                    payload.get("status") == "PASS"
                    and payload.get("mode") == "EXECUTE"
                ):
                    raise OSError("synthetic final result failure")
                original_emit(payload, stream=stream)

            monkeypatch.setattr(production, "_json_emit", fail_final_result)
        else:
            raise AssertionError(case)

    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(_execute_argv(plan)) == 1
    result, stream = _json_result(capfd)
    assert result["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert result["publish_state"] == expected_publish_state
    assert result["target_may_have_changed"] is True
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is True
    assert production._sha256(context.source) == before_hash
    expected_path_delta = {
        str(
            (plan.path.parent / "execution.json").relative_to(context.runtime)
        )
    }
    if case == "internal_manifest_close_failure":
        expected_path_delta.add(
            str(manifest_path.relative_to(context.runtime))
        )
    assert _path_set(context.runtime) - before_paths == expected_path_delta
    if case == "external_evidence_write_failure":
        assert stream == "stderr"
        assert result["durable_evidence_persisted"] is False
    if case == "internal_manifest_close_failure":
        assert stream == "stderr"
        assert result["cleanup_exception_recorded"] is True
        assert result["primary_exception_preserved"] is False
        assert result["cleanup_failure_codes"] == [
            "INTERNAL_MANIFEST_CLOSE_FAILED"
        ]
        assert "synthetic manifest" not in json.dumps(result)


@pytest.mark.parametrize("case", CLOSE_FAILURE_CASES)
def test_public_close_failure_matrix_preserves_primary_and_state(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    expected_class = "PUBLISH_UNCERTAIN"
    expected_state = "EXCHANGE_STARTED"
    expected_target_changed = True
    expected_manual_recovery = True
    expected_durable_evidence = True
    expected_primary_preserved = True
    expected_primary_error: str | None = (
        "PRIMARY_POST_EXCHANGE_FAILURE"
    )
    expected_stream = "stderr"
    expected_path_delta = {
        str(
            (plan.path.parent / "execution.json").relative_to(
                context.runtime
            )
        )
    }

    def fail_after_close(
        owner: type[Any],
        code: str,
    ) -> None:
        original_close = owner.close

        def close_then_fail(self: Any) -> None:
            original_close(self)
            raise OSError(code)

        monkeypatch.setattr(owner, "close", close_then_fail)

    def fail_before_exchange(
        *_args: Any,
        **_kwargs: Any,
    ) -> Any:
        raise staged.OrchestratorError("SOURCE_NOT_QUIESCENT")

    def fail_after_exchange(
        _args: Any,
        *,
        prepared: Any,
    ) -> int:
        prepared.close()
        raise production.ProductionGateError(
            "PRIMARY_POST_EXCHANGE_FAILURE"
        )

    if case == "validated_close_before_exchange":
        fail_after_close(
            production.ValidatedExecution,
            "validated-close-before-exchange",
        )
        monkeypatch.setattr(
            production,
            "_prepare_authorized_production_execution",
            fail_before_exchange,
        )
        expected_class = "QUIESCENCE_FAILED"
        expected_state = "BEFORE_EXCHANGE"
        expected_target_changed = False
        expected_manual_recovery = False
        expected_durable_evidence = False
        expected_primary_error = "QUIESCENCE_FAILED"
        expected_stream = "stdout"
        expected_path_delta = set()
    elif case == "validated_close_after_exchange_started":
        fail_after_close(
            production.ValidatedExecution,
            "validated-close-after-exchange",
        )
        monkeypatch.setattr(
            production,
            "_execute_authorized_staged",
            fail_after_exchange,
        )
    elif case == "validated_close_after_final_verification":
        fail_after_close(
            production.ValidatedExecution,
            "validated-close-after-verify",
        )
        _install_staged_success(monkeypatch)
        expected_state = "FINAL_VERIFIED"
        expected_primary_preserved = False
        expected_primary_error = None
    elif case == "pinned_plan_close_before_exchange":
        fail_after_close(
            production.PinnedPlan,
            "pinned-close-before-exchange",
        )
        monkeypatch.setattr(
            production,
            "_prepare_authorized_production_execution",
            fail_before_exchange,
        )
        expected_class = "QUIESCENCE_FAILED"
        expected_state = "BEFORE_EXCHANGE"
        expected_target_changed = False
        expected_manual_recovery = False
        expected_durable_evidence = False
        expected_primary_error = "QUIESCENCE_FAILED"
        expected_stream = "stdout"
        expected_path_delta = set()
    elif case == "pinned_plan_close_after_exchange_started":
        fail_after_close(
            production.PinnedPlan,
            "pinned-close-after-exchange",
        )
        monkeypatch.setattr(
            production,
            "_execute_authorized_staged",
            fail_after_exchange,
        )
        expected_durable_evidence = False
    elif case == "pinned_plan_close_after_final_validation":
        fail_after_close(
            production.PinnedPlan,
            "pinned-close-after-validation",
        )
        _install_staged_success(monkeypatch)
        expected_state = "FINAL_VERIFIED"
        expected_durable_evidence = False
        expected_primary_preserved = False
        expected_primary_error = None
    elif case == "primary_post_exchange_plus_cleanup_failure":
        fail_after_close(
            production.ValidatedExecution,
            "cleanup-alongside-primary",
        )
        monkeypatch.setattr(
            production,
            "_execute_authorized_staged",
            fail_after_exchange,
        )
    elif case == "successful_body_evidence_finalization_failure":
        _install_staged_success(monkeypatch)

        def fail_evidence_finalization(
            self: production.ExecutionEvidence,
            **_updates: Any,
        ) -> None:
            raise OSError("synthetic evidence finalization failure")

        monkeypatch.setattr(
            production.ExecutionEvidence,
            "checkpoint",
            fail_evidence_finalization,
        )
        expected_state = "FINAL_VERIFIED"
        expected_durable_evidence = False
        expected_primary_preserved = False
        expected_primary_error = None
    else:
        raise AssertionError(case)

    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)
    assert production.main(_execute_argv(plan)) == 1
    results = _json_results(capfd)
    assert len(results) == 1
    result, stream = results[0]
    assert stream == expected_stream
    assert result["exit_classification"] == expected_class
    assert result["publish_state"] == expected_state
    assert result["target_may_have_changed"] is expected_target_changed
    assert result["automatic_retry_allowed"] is False
    assert (
        result["manual_recovery_required"]
        is expected_manual_recovery
    )
    assert (
        result["durable_evidence_updated"]
        is expected_durable_evidence
    )
    assert (
        result["primary_exception_preserved"]
        is expected_primary_preserved
    )
    assert result.get("primary_error_type") == expected_primary_error
    assert result["cleanup_exception_recorded"] is True
    assert result["status"] == "FAILED"
    assert result["cleanup_failure_codes"]
    serialized_result = json.dumps(result, ensure_ascii=True)
    assert "synthetic" not in serialized_result
    assert "validated-close" not in serialized_result
    assert "pinned-close" not in serialized_result
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) - before_paths == expected_path_delta


def test_execute_main_generic_fallback_never_reports_before_exchange(
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def fail_outside_boundary(_args: Any) -> int:
        raise OSError("synthetic-sensitive-exception-body")

    monkeypatch.setattr(production, "execute_plan", fail_outside_boundary)
    argv = [
        "execute",
        "--plan",
        "/synthetic/plan.json",
        "--expected-plan-sha256",
        "0" * 64,
        "--confirm-operation-id",
        "0" * 32,
        "--confirm-source-sha256",
        "0" * 64,
        "--confirm-image-revision",
        REVISION,
        "--confirm-operations-root-approval-sha256",
        "0" * 64,
        "--confirm-clean-start-policy-sha256",
        "0" * 64,
        "--final-authority",
        "/synthetic/final-authority.json",
        "--expected-final-authority-sha256",
        "0" * 64,
    ]
    assert production.main(argv) == 1
    results = _json_results(capfd)
    assert len(results) == 1
    result, stream = results[0]
    assert stream == "stderr"
    assert result["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert result["publish_state"] == "EXECUTION_STATE_UNKNOWN"
    assert result["target_may_have_changed"] is True
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is True
    assert result["durable_evidence_updated"] is False
    assert "synthetic-sensitive" not in json.dumps(result)


def test_negative_matrix_contract_is_large_and_public() -> None:
    assert NEGATIVE_MATRIX_CASES >= 78
    source = Path(__file__).read_text(encoding="utf-8")
    assert "production.main(" in source
    direct_parser_call = "production.build_parser()" + ".parse_args("
    assert direct_parser_call not in source



class _RealBoundaryCloseFailure:
    def __init__(self, wrapped: Any, attempts: list[str]) -> None:
        self._wrapped = wrapped
        self._attempts = attempts

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def close(self) -> None:
        self._attempts.append("close")
        self._wrapped.close()
        raise OSError("sensitive-real-public-cleanup")


PUBLIC_REAL_CLEANUP_RESOURCE_CLASSES = (
    ("copy_descriptors_files", "COPY_SOURCE_DESCRIPTOR"),
    ("fsync_descriptors_handles", "FSYNC_FILE_HANDLE"),
    ("manifest_temporary_resources", "MANIFEST_TEMP_FILE_HANDLE"),
    ("recovery_manifest", "RECOVERY_MANIFEST_FILE_HANDLE"),
    ("sqlite_connections", "SQLITE_VALIDATION_CONNECTION"),
)


def _actual_cleanup_record_for_public_class(
    resource_class: str,
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    attempts: list[str] = []
    with monkeypatch.context() as scoped:
        if resource_class == "copy_descriptors_files":
            source = root / "copy-source.sqlite"
            source.write_bytes(b"synthetic")
            destination = root / "copy-target.sqlite"
            real_open = os.open
            real_close = os.close
            source_descriptors: set[int] = set()

            def tracking_open(
                path: object,
                *args: Any,
                **kwargs: Any,
            ) -> int:
                descriptor = real_open(path, *args, **kwargs)
                if Path(path) == source:
                    source_descriptors.add(descriptor)
                    attempts.append("acquired")
                return descriptor

            def close_then_fail(descriptor: int) -> None:
                if descriptor not in source_descriptors:
                    real_close(descriptor)
                    return
                source_descriptors.remove(descriptor)
                attempts.append("close")
                real_close(descriptor)
                raise OSError("sensitive-real-public-cleanup")

            scoped.setattr(os, "open", tracking_open)
            scoped.setattr(os, "close", close_then_fail)
            uid_getter = getattr(os, "getuid", None)
            gid_getter = getattr(os, "getgid", None)
            assert callable(uid_getter)
            assert callable(gid_getter)
            with pytest.raises(staged._CleanupAggregateError) as captured:
                staged._copy_durable(
                    source,
                    destination,
                    uid=int(uid_getter()),
                    gid=int(gid_getter()),
                )
        elif resource_class == "fsync_descriptors_handles":
            target = root / "fsync-target.bin"
            target.write_bytes(b"synthetic")
            real_open = Path.open

            def open_with_failing_close(
                path: Path,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                handle = real_open(path, *args, **kwargs)
                if path == target:
                    attempts.append("acquired")
                    return _RealBoundaryCloseFailure(handle, attempts)
                return handle

            scoped.setattr(Path, "open", open_with_failing_close)
            with pytest.raises(staged._CleanupAggregateError) as captured:
                staged._fsync_file(target)
        elif resource_class == "manifest_temporary_resources":
            real_fdopen = os.fdopen

            def fdopen_with_failing_close(
                descriptor: int,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                attempts.append("acquired")
                return _RealBoundaryCloseFailure(
                    real_fdopen(descriptor, *args, **kwargs),
                    attempts,
                )

            scoped.setattr(os, "fdopen", fdopen_with_failing_close)
            manifest = staged.DurableManifest(
                path=root / "manifest.json",
                payload={
                    "STATE": "PLANNED",
                    "PUBLISH_STATE": "BEFORE_EXCHANGE",
                },
            )
            with pytest.raises(staged._CleanupAggregateError) as captured:
                manifest.transition("FAILED")
        elif resource_class == "recovery_manifest":
            manifest_path = root / "recovery.json"
            manifest_path.write_text(
                json.dumps(
                    {"PUBLISH_STATE": "EXCHANGE_COMPLETED_NOT_VERIFIED"}
                ),
                encoding="ascii",
            )
            staging_root = _private_directory(root / "staging")
            real_open = Path.open

            def open_with_failing_close(
                path: Path,
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                handle = real_open(path, *args, **kwargs)
                if path == manifest_path:
                    attempts.append("acquired")
                    return _RealBoundaryCloseFailure(handle, attempts)
                return handle

            scoped.setattr(Path, "open", open_with_failing_close)
            with pytest.raises(staged._CleanupAggregateError) as captured:
                staged._recover_pre_publish_staging(
                    manifest_path,
                    staging_root,
                )
        elif resource_class == "sqlite_connections":
            source = _source(root / "sqlite" / "validation.sqlite")
            real_connect = sqlite3.connect

            def connect_with_failing_close(
                *args: Any,
                **kwargs: Any,
            ) -> Any:
                attempts.append("acquired")
                return _RealBoundaryCloseFailure(
                    real_connect(*args, **kwargs),
                    attempts,
                )

            scoped.setattr(sqlite3, "connect", connect_with_failing_close)
            with pytest.raises(staged._CleanupAggregateError) as captured:
                staged._sqlite_validation(source)
        else:
            raise AssertionError(resource_class)

    primary, records = staged._split_primary_cleanup(captured.value)
    assert isinstance(primary, staged.OrchestratorError)
    assert primary.code == "CLEANUP_FAILED"
    assert len(records) == 1
    assert attempts == ["acquired", "close"]
    serialized = json.dumps(records[0].as_payload(), ensure_ascii=True)
    assert "sensitive" not in serialized
    return records[0].as_payload()


def _post_exchange_result_with_cleanup(
    record: dict[str, str],
) -> dict[str, Any]:
    return {
        "status": "FAILED",
        "error_type": "PUBLISH_UNCERTAIN",
        "exit_classification": "PUBLISH_UNCERTAIN",
        "failure_reason": "PRIMARY_POST_EXCHANGE_FAILURE",
        "primary_error_type": "OrchestratorError",
        "primary_error_code": "PRIMARY_POST_EXCHANGE_FAILURE",
        "publish_state": "EXCHANGE_COMPLETED_NOT_VERIFIED",
        "target_may_have_changed": True,
        "automatic_retry_allowed": False,
        "manual_recovery_required": True,
        "primary_exit_classification": "PRIMARY_POST_EXCHANGE_FAILURE",
        "primary_publish_state": "EXCHANGE_COMPLETED_NOT_VERIFIED",
        "primary_target_may_have_changed": True,
        "primary_automatic_retry_allowed": False,
        "primary_manual_recovery_required": True,
        "primary_exception_present": True,
        "primary_exception_preserved": True,
        "cleanup_exception_recorded": True,
        "cleanup_exception_count": 1,
        "cleanup_failures": [record],
        "cleanup_failure_codes": [record["error_code"]],
        "backup_available": True,
        "manifest_write_failed": True,
        "durable_evidence_updated": False,
    }


PUBLIC_INTERNAL_CLEANUP_CASES = (
    (
        "pre_exchange_primary",
        {
            "status": "FAILED",
            "error_type": "PRIMARY_PRE_EXCHANGE_FAILURE",
            "exit_classification": "PRIMARY_PRE_EXCHANGE_FAILURE",
            "publish_state": "BEFORE_EXCHANGE",
            "target_may_have_changed": False,
            "automatic_retry_allowed": False,
            "manual_recovery_required": True,
            "primary_exit_classification": "PRIMARY_PRE_EXCHANGE_FAILURE",
            "primary_publish_state": "BEFORE_EXCHANGE",
            "primary_target_may_have_changed": False,
            "primary_automatic_retry_allowed": False,
            "primary_manual_recovery_required": False,
            "primary_exception_present": True,
            "primary_exception_preserved": True,
            "cleanup_exception_recorded": True,
            "cleanup_exception_count": 1,
            "cleanup_failures": [
                {
                    "resource_kind": "SOURCE_SQLITE_LEASE",
                    "cleanup_phase": "FINAL_RESOURCE_RELEASE",
                    "error_type": "OSError",
                    "error_code": "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
                }
            ],
            "cleanup_failure_codes": [
                "SOURCE_SQLITE_LEASE_CLOSE_FAILED"
            ],
            "backup_available": False,
        },
        "PRIMARY_PRE_EXCHANGE_FAILURE",
        "BEFORE_EXCHANGE",
        False,
        True,
        1,
    ),
    (
        "post_exchange_primary",
        {
            "status": "FAILED",
            "error_type": "PUBLISH_UNCERTAIN",
            "exit_classification": "PUBLISH_UNCERTAIN",
            "failure_reason": "PRIMARY_POST_EXCHANGE_FAILURE",
            "publish_state": "EXCHANGE_COMPLETED_NOT_VERIFIED",
            "target_may_have_changed": True,
            "automatic_retry_allowed": False,
            "manual_recovery_required": True,
            "primary_exit_classification": "PRIMARY_POST_EXCHANGE_FAILURE",
            "primary_publish_state": "EXCHANGE_COMPLETED_NOT_VERIFIED",
            "primary_target_may_have_changed": True,
            "primary_automatic_retry_allowed": False,
            "primary_manual_recovery_required": True,
            "primary_exception_present": True,
            "primary_exception_preserved": True,
            "cleanup_exception_recorded": True,
            "cleanup_exception_count": 2,
            "cleanup_failures": [
                {
                    "resource_kind": "STAGING_SQLITE_LEASE",
                    "cleanup_phase": "FINAL_RESOURCE_RELEASE",
                    "error_type": "OSError",
                    "error_code": "STAGING_SQLITE_LEASE_CLOSE_FAILED",
                },
                {
                    "resource_kind": "SOURCE_SQLITE_LEASE",
                    "cleanup_phase": "FINAL_RESOURCE_RELEASE",
                    "error_type": "OSError",
                    "error_code": "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
                },
            ],
            "cleanup_failure_codes": [
                "STAGING_SQLITE_LEASE_CLOSE_FAILED",
                "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
            ],
            "backup_available": True,
        },
        "PUBLISH_UNCERTAIN",
        "EXCHANGE_COMPLETED_NOT_VERIFIED",
        True,
        True,
        2,
    ),
    (
        "successful_body_cleanup_failure",
        {
            "status": "FAILED",
            "error_type": "PUBLISH_UNCERTAIN",
            "exit_classification": "PUBLISH_UNCERTAIN",
            "publish_state": "FINAL_VERIFIED",
            "target_may_have_changed": True,
            "automatic_retry_allowed": False,
            "manual_recovery_required": True,
            "primary_exit_classification": "SUCCESS",
            "primary_publish_state": "FINAL_VERIFIED",
            "primary_target_may_have_changed": True,
            "primary_automatic_retry_allowed": False,
            "primary_manual_recovery_required": False,
            "primary_exception_present": False,
            "primary_exception_preserved": False,
            "cleanup_exception_recorded": True,
            "cleanup_exception_count": 1,
            "cleanup_failures": [
                {
                    "resource_kind": "SOURCE_PINNED_DATABASE",
                    "cleanup_phase": "FINAL_RESOURCE_RELEASE",
                    "error_type": "OSError",
                    "error_code": "SOURCE_PINNED_DATABASE_CLOSE_FAILED",
                }
            ],
            "cleanup_failure_codes": [
                "SOURCE_PINNED_DATABASE_CLOSE_FAILED"
            ],
            "backup_available": True,
        },
        "PUBLISH_UNCERTAIN",
        "FINAL_VERIFIED",
        True,
        False,
        1,
    ),
)


@pytest.mark.parametrize(
    (
        "case",
        "staged_result",
        "expected_classification",
        "expected_publish_state",
        "expected_target_changed",
        "expected_primary_present",
        "expected_cleanup_count",
    ),
    PUBLIC_INTERNAL_CLEANUP_CASES,
)
def test_public_entrypoint_preserves_internal_primary_and_cleanup(
    case: str,
    staged_result: dict[str, Any],
    expected_classification: str,
    expected_publish_state: str,
    expected_target_changed: bool,
    expected_primary_present: bool,
    expected_cleanup_count: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)

    def return_structured_failure(
        _args: Any,
        *,
        prepared: Any,
    ) -> int:
        prepared.close()
        print(json.dumps(staged_result, ensure_ascii=True, sort_keys=True))
        return 1

    monkeypatch.setattr(
        production,
        "_execute_authorized_staged",
        return_structured_failure,
    )
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)

    assert production.main(_execute_argv(plan)) == 1, case
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == expected_classification
    assert result["publish_state"] == expected_publish_state
    assert result["target_may_have_changed"] is expected_target_changed
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is True
    assert result["primary_exception_present"] is expected_primary_present
    assert result["primary_exception_preserved"] is expected_primary_present
    assert result["cleanup_exception_recorded"] is True
    assert result["cleanup_exception_count"] == expected_cleanup_count
    assert len(result["cleanup_failures"]) == expected_cleanup_count
    assert result["durable_evidence_updated"] is True
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) - before_paths == {
        str(
            (plan.path.parent / "execution.json").relative_to(
                context.runtime
            )
        )
    }

    evidence = json.loads(
        (plan.path.parent / "execution.json").read_text(encoding="ascii")
    )
    assert evidence["CLEANUP_EXCEPTION_COUNT"] == expected_cleanup_count
    assert len(evidence["CLEANUP_FAILURES"]) == expected_cleanup_count
    assert evidence["PRIMARY_EXCEPTION_PRESENT"] is expected_primary_present
    serialized = json.dumps(result, ensure_ascii=True)
    assert "sensitive" not in serialized


@pytest.mark.parametrize(
    ("resource_class", "expected_resource_kind"),
    PUBLIC_REAL_CLEANUP_RESOURCE_CLASSES,
)
def test_public_entrypoint_transports_real_cleanup_resource_class(
    resource_class: str,
    expected_resource_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    boundary_root = _private_directory(tmp_path / "boundary")
    record = _actual_cleanup_record_for_public_class(
        resource_class,
        boundary_root,
        monkeypatch,
    )
    assert record["resource_kind"] == expected_resource_kind
    context = _unit_context(tmp_path / "gate", monkeypatch)
    plan = _create_plan(context, capfd)
    staged_result = _post_exchange_result_with_cleanup(record)

    def return_structured_failure(
        _args: Any,
        *,
        prepared: Any,
    ) -> int:
        prepared.close()
        print(json.dumps(staged_result, ensure_ascii=True, sort_keys=True))
        return 1

    monkeypatch.setattr(
        production,
        "_execute_authorized_staged",
        return_structured_failure,
    )
    before_hash = production._sha256(context.source)

    assert production.main(_execute_argv(plan)) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert result["publish_state"] == "EXCHANGE_COMPLETED_NOT_VERIFIED"
    assert result["target_may_have_changed"] is True
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is True
    assert result["primary_exception_preserved"] is True
    assert result["primary_error_type"] == "OrchestratorError"
    assert result["primary_error_code"] == "PRIMARY_POST_EXCHANGE_FAILURE"
    assert result["cleanup_exception_count"] == 1
    assert result["cleanup_resource_kinds"] == [expected_resource_kind]
    assert result["cleanup_failures"] == [record]
    assert result["durable_evidence_updated"] is True
    assert production._sha256(context.source) == before_hash

    evidence = json.loads(
        (plan.path.parent / "execution.json").read_text(encoding="ascii")
    )
    assert evidence["PRIMARY_EXCEPTION_PRESERVED"] is True
    assert evidence["PRIMARY_ERROR_TYPE"] == "OrchestratorError"
    assert evidence["PRIMARY_ERROR_CODE"] == "PRIMARY_POST_EXCHANGE_FAILURE"
    assert evidence["CLEANUP_EXCEPTION_COUNT"] == 1
    assert evidence["CLEANUP_RESOURCE_KINDS"] == [expected_resource_kind]
    serialized = json.dumps([result, evidence], ensure_ascii=True)
    assert "sensitive" not in serialized


def test_durable_evidence_failure_preserves_real_manifest_cleanup_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    boundary_root = _private_directory(tmp_path / "boundary")
    record = _actual_cleanup_record_for_public_class(
        "manifest_temporary_resources",
        boundary_root,
        monkeypatch,
    )
    context = _unit_context(tmp_path / "gate", monkeypatch)
    plan = _create_plan(context, capfd)
    staged_result = _post_exchange_result_with_cleanup(record)

    def return_structured_failure(
        _args: Any,
        *,
        prepared: Any,
    ) -> int:
        prepared.close()
        print(json.dumps(staged_result, ensure_ascii=True, sort_keys=True))
        return 1

    monkeypatch.setattr(
        production,
        "_execute_authorized_staged",
        return_structured_failure,
    )
    original_writer = production._write_json_durable_at
    evidence_writes = 0

    def fail_evidence_update(
        parent_fd: int,
        name: str,
        payload: dict[str, Any],
    ) -> None:
        nonlocal evidence_writes
        if name == "execution.json":
            evidence_writes += 1
            if evidence_writes >= 2:
                raise OSError("sensitive-evidence-write-failure")
        original_writer(parent_fd, name, payload)

    monkeypatch.setattr(
        production,
        "_write_json_durable_at",
        fail_evidence_update,
    )

    assert production.main(_execute_argv(plan)) == 1
    result, stream = _json_result(capfd)
    assert stream == "stderr"
    assert result["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert result["publish_state"] == "EXCHANGE_COMPLETED_NOT_VERIFIED"
    assert result["target_may_have_changed"] is True
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is True
    assert result["durable_evidence_updated"] is False
    assert result["primary_exception_present"] is True
    assert result["primary_exception_preserved"] is True
    assert result["primary_error_type"] == "OrchestratorError"
    assert result["primary_error_code"] == "PRIMARY_POST_EXCHANGE_FAILURE"
    assert result["cleanup_exception_count"] == 3
    assert result["cleanup_resource_kinds"] == [
        "MANIFEST_TEMP_FILE_HANDLE",
        "PRODUCTION_GATE_RESOURCE",
        "PRODUCTION_GATE_RESOURCE",
    ]
    assert result["cleanup_failure_codes"] == [
        "MANIFEST_TEMP_FILE_CLOSE_FAILED",
        "EXECUTION_EVIDENCE_UPDATE_FAILED",
        "EXECUTION_EVIDENCE_FINALIZATION_FAILED",
    ]
    serialized = json.dumps(result, ensure_ascii=True)
    assert "sensitive" not in serialized


def test_public_entrypoint_accepts_exceptional_preparation_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    record = staged._CleanupFailureRecord(
        resource_kind="MANIFEST_PARENT_DESCRIPTOR",
        cleanup_phase="PREPARATION_FAILURE_RELEASE",
        error_type="OSError",
        error_code="MANIFEST_PARENT_CLOSE_FAILED",
    )

    def fail_preparation(*_args: Any, **_kwargs: Any) -> Any:
        raise staged._StagedCleanupTransport(
            result_snapshot={
                "status": "FAILED",
                "error_type": "PRIMARY_PREPARATION_FAILURE",
                "exit_classification": "PRIMARY_PREPARATION_FAILURE",
                "publish_state": "BEFORE_EXCHANGE",
                "target_may_have_changed": False,
                "automatic_retry_allowed": False,
                "manual_recovery_required": False,
                "backup_available": False,
            },
            publish_state="BEFORE_EXCHANGE",
            exchange_started=False,
            cleanup_failures=(record,),
            durable_evidence_updated=False,
        )

    monkeypatch.setattr(
        production,
        "_prepare_authorized_production_execution",
        fail_preparation,
    )
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)

    assert production.main(_execute_argv(plan)) == 1
    result, stream = _json_result(capfd)
    assert stream == "stdout"
    assert result["exit_classification"] == "PRIMARY_PREPARATION_FAILURE"
    assert result["publish_state"] == "BEFORE_EXCHANGE"
    assert result["target_may_have_changed"] is False
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is True
    assert result["primary_exception_present"] is True
    assert result["primary_exception_preserved"] is True
    assert result["primary_exit_classification"] == (
        "PRIMARY_PREPARATION_FAILURE"
    )
    assert result["cleanup_exception_count"] == 1
    assert result["cleanup_failure_codes"] == [
        "MANIFEST_PARENT_CLOSE_FAILED"
    ]
    assert result["durable_evidence_updated"] is False
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) == before_paths


def test_sanitized_stderr_fallback_includes_cleanup_count(
    capfd: pytest.CaptureFixture[str],
) -> None:
    outcome = production._ExecutionOutcome(
        exit_code=1,
        payload={
            "publish_state": "EXCHANGE_STARTED",
            "cleanup_exception_count": 2,
            "cleanup_failures": [
                {
                    "resource_kind": "MANIFEST_TEMP_FILE_HANDLE",
                    "cleanup_phase": "FAILED_MANIFEST_TRANSITION",
                    "error_type": "OSError",
                    "error_code": "MANIFEST_TEMP_FILE_CLOSE_FAILED",
                },
                {
                    "resource_kind": "SOURCE_SQLITE_LEASE",
                    "cleanup_phase": "FINAL_RESOURCE_RELEASE",
                    "error_type": "OSError",
                    "error_code": "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
                },
            ],
        },
        stream=production.sys.stdout,
        primary_error_type="PRIMARY_POST_EXCHANGE_FAILURE",
    )
    production._sanitized_stderr_fallback(outcome)
    result, stream = _json_result(capfd)
    assert stream == "stderr"
    assert result["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert result["publish_state"] == "EXCHANGE_STARTED"
    assert result["target_may_have_changed"] is True
    assert result["automatic_retry_allowed"] is False
    assert result["manual_recovery_required"] is True
    assert result["durable_evidence_updated"] is False
    assert result["cleanup_exception_count"] == 3
    assert result["cleanup_resource_kinds"] == [
        "MANIFEST_TEMP_FILE_HANDLE",
        "SOURCE_SQLITE_LEASE",
        "FINAL_RESULT_STREAM",
    ]
    assert result["cleanup_failure_codes"] == [
        "MANIFEST_TEMP_FILE_CLOSE_FAILED",
        "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
        "FINAL_RESULT_EMIT_FAILED",
    ]


@pytest.mark.parametrize(
    "drift",
    ("same_size", "different_size", "atomic", "symlink", "hardlink"),
)
def test_pinned_plan_final_byte_revalidation_denies_drift(
    drift: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    pinned = _unit_open_plan(str(plan.path), plan.sha256)
    try:
        original = plan.path.read_bytes()
        if drift == "same_size":
            replacement = bytearray(original)
            replacement[len(replacement) // 2] ^= 1
            plan.path.write_bytes(bytes(replacement))
        elif drift == "different_size":
            plan.path.write_bytes(original + b" ")
        elif drift == "atomic":
            replacement_path = plan.path.with_name("replacement.json")
            replacement_path.write_bytes(original)
            os.chmod(replacement_path, 0o600)
            os.replace(replacement_path, plan.path)
        elif drift == "symlink":
            target = plan.path.with_name("plan-target.json")
            target.write_bytes(original)
            os.chmod(target, 0o600)
            plan.path.unlink()
            plan.path.symlink_to(target)
        elif drift == "hardlink":
            linked = plan.path.with_name("plan-hardlink.json")
            os.link(plan.path, linked)
        else:
            raise AssertionError(drift)
        assert not pinned.path_matches()
    finally:
        pinned.close()


def test_plan_modification_during_descriptor_read_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    original_pread = production.os.pread
    modified = False

    def changing_pread(fd: int, size: int, offset: int) -> bytes:
        nonlocal modified
        chunk = original_pread(fd, size, offset)
        if not modified:
            modified = True
            with plan.path.open("ab") as handle:
                handle.write(b" ")
        return chunk

    monkeypatch.setattr(production.os, "pread", changing_pread)
    with pytest.raises(production.ProductionGateError):
        _unit_open_plan(str(plan.path), plan.sha256)


def test_complete_final_mutation_checkpoint_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    pinned = _unit_open_plan(str(plan.path), plan.sha256)
    calls: list[str] = []

    class FinalAuthority(_UnitExecutionAuthority):
        def validate_not_expired(self) -> None:
            calls.append("expiry")

        def revalidate_operations_root(self) -> None:
            calls.append("operations_root")

        def validate_runtime(self) -> None:
            calls.append("runtime")

    source_identity = object()
    validated = type(
        "Validated",
        (),
        {
            "execution_authority": FinalAuthority(),
            "source_identity": source_identity,
        },
    )()
    prepared = type("Prepared", (), {"source_identity": source_identity})()
    monkeypatch.setattr(
        production,
        "_pinned_authorities_match",
        lambda _pinned, _validated: True,
    )
    try:
        production._final_mutation_checkpoint(pinned, validated, prepared)
    finally:
        pinned.close()
    assert calls == ["expiry", "operations_root", "runtime"]


def test_public_execute_final_checkpoint_failure_preserves_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    context = _unit_context(tmp_path, monkeypatch)
    plan = _create_plan(context, capfd)
    before_hash = production._sha256(context.source)
    before_paths = _path_set(context.runtime)

    def deny_final_checkpoint(*_args: Any, **_kwargs: Any) -> None:
        raise production.ProductionGateError("FINAL_CHECKPOINT_TEST_DENIAL")

    monkeypatch.setattr(
        production,
        "_final_mutation_checkpoint",
        deny_final_checkpoint,
    )
    assert production.main(_execute_argv(plan)) == 1
    result, _stream = _json_result(capfd)
    assert result["exit_classification"] == "FINAL_CHECKPOINT_TEST_DENIAL"
    assert production._sha256(context.source) == before_hash
    assert _path_set(context.runtime) == before_paths


def test_public_execute_has_one_final_checkpoint_call() -> None:
    source = Path(production.__file__).read_text(encoding="utf-8")
    execute_source = source.split("def _execute_plan_outcome(", 1)[1]
    execute_source = execute_source.split("def execute_plan(", 1)[0]
    assert execute_source.count("_final_mutation_checkpoint(") == 1
