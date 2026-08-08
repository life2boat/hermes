#!/usr/bin/env python3
"""One-time, fail-closed bridge from an unlabelled legacy Hermes runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import hermes_deploy_preflight as preflight  # noqa: E402
from scripts import hermes_post_deploy_attestation as attestation  # noqa: E402
from scripts import hermes_production_deploy as deploy  # noqa: E402


BOOTSTRAP_PLAN_VERSION = 1
OPERATION_CLASS = "LEGACY_RUNTIME_PROVENANCE_BOOTSTRAP"
LEGACY_CLASSIFICATION = "LEGACY_BASELINE"
UNKNOWN_REVISION = "UNKNOWN"
EXECUTE_CONFIRMATION = "BOOTSTRAP_LEGACY_HERMES_RUNTIME"
OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
MAX_PLAN_BYTES = 1024 * 1024

PLAN_FIELDS = frozenset({
    "BOOTSTRAP_PLAN_VERSION",
    "OPERATION_ID",
    "OPERATION_CLASS",
    "CREATED_AT",
    "EXPIRES_AT",
    "REPOSITORY_ROOT",
    "SECRET_SOURCE_PATH",
    "CANDIDATE_IMAGE_ID",
    "CANDIDATE_OCI_REVISION",
    "LEGACY_BASELINE_CLASSIFICATION",
    "SOURCE_REVISION",
    "LEGACY_IMAGE_ID",
    "LEGACY_RUNTIME_BASELINE",
    "ROLLBACK_ARTIFACT_PATH",
    "ROLLBACK_ARTIFACT_SIZE",
    "ROLLBACK_ARTIFACT_SHA256",
    "ALLOWED_MUTATION",
    "SCHEMA_MIGRATION_ALLOWED",
    "FEATURE_ACTIVATION_ALLOWED",
    "SECRET_MUTATION_ALLOWED",
    "QDRANT_MUTATION_ALLOWED",
    "PLAN_STATE",
})


class BootstrapError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class BootstrapRolledBack(BootstrapError):
    pass


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def _read_private_file(
    path: Path,
    *,
    maximum: int | None,
) -> tuple[bytes | None, int, str]:
    _private_directory(path.parent)
    parent_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    parent_fd = os.open(path.parent, parent_flags)
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != 0
                or before.st_gid != 0
                or stat.S_IMODE(before.st_mode) != 0o600
                or (maximum is not None and before.st_size > maximum)
            ):
                raise BootstrapError("PRIVATE_ARTIFACT_METADATA_INVALID")
            digest = hashlib.sha256()
            chunks: list[bytes] | None = [] if maximum is not None else None
            total = 0
            while True:
                block = os.read(descriptor, 1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > before.st_size or (
                    maximum is not None and total > maximum
                ):
                    raise BootstrapError("PRIVATE_ARTIFACT_READ_INVALID")
                digest.update(block)
                if chunks is not None:
                    chunks.append(block)
            path_metadata = os.stat(
                path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            after = os.fstat(descriptor)
            identity = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_uid,
                before.st_gid,
                stat.S_IMODE(before.st_mode),
                before.st_nlink,
            )
            if total != before.st_size or identity != (
                path_metadata.st_dev,
                path_metadata.st_ino,
                path_metadata.st_size,
                path_metadata.st_uid,
                path_metadata.st_gid,
                stat.S_IMODE(path_metadata.st_mode),
                path_metadata.st_nlink,
            ) or identity != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_uid,
                after.st_gid,
                stat.S_IMODE(after.st_mode),
                after.st_nlink,
            ):
                raise BootstrapError("PRIVATE_ARTIFACT_SUBSTITUTION")
            data = b"".join(chunks) if chunks is not None else None
            return data, total, digest.hexdigest()
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)


def _root_required() -> None:
    getter = getattr(os, "geteuid", None)
    if not callable(getter) or getter() != 0:
        raise BootstrapError("ROOT_EXECUTION_REQUIRED")


def _private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise BootstrapError("PRIVATE_DIRECTORY_CONTRACT_INVALID")


def _outside_repository(path: Path, repository_root: Path) -> None:
    try:
        path.resolve().relative_to(repository_root.resolve())
    except ValueError:
        return
    raise BootstrapError("BOOTSTRAP_ARTIFACT_INSIDE_REPOSITORY_DENIED")


def _write_new_private_json(path: Path, payload: dict[str, Any]) -> str:
    data = _canonical_json(payload)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return hashlib.sha256(data).hexdigest()


def _open_plan(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.is_absolute() or SHA256_RE.fullmatch(expected_sha256) is None:
        raise BootstrapError("BOOTSTRAP_PLAN_BINDING_INVALID")
    try:
        data, _size, actual_sha256 = _read_private_file(
            path,
            maximum=MAX_PLAN_BYTES,
        )
    except (BootstrapError, OSError) as exc:
        raise BootstrapError("BOOTSTRAP_PLAN_METADATA_INVALID") from exc
    if data is None or actual_sha256 != expected_sha256:
        raise BootstrapError("BOOTSTRAP_PLAN_SHA256_MISMATCH")
    try:
        payload = json.loads(data.decode("ascii"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("BOOTSTRAP_PLAN_JSON_INVALID") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != PLAN_FIELDS
        or _canonical_json(payload) != data
        or path.name != "bootstrap-plan.json"
        or path.parent.name != payload.get("OPERATION_ID")
        or payload.get("ROLLBACK_ARTIFACT_PATH")
        != str(path.parent / "legacy-image.tar")
    ):
        raise BootstrapError("BOOTSTRAP_PLAN_CONTRACT_INVALID")
    return payload


def _inspect_legacy_image(
    contract: deploy.DeploymentContract,
    image_id: str,
) -> str:
    if IMAGE_ID_RE.fullmatch(image_id) is None:
        raise BootstrapError("LEGACY_IMAGE_ID_INVALID")
    result = deploy._run(("docker", "image", "inspect", image_id), timeout=30)
    if result.returncode != 0:
        raise BootstrapError("LEGACY_IMAGE_MISSING")
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapError("LEGACY_IMAGE_INSPECT_INVALID") from exc
    if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
        raise BootstrapError("LEGACY_IMAGE_INSPECT_INVALID")
    record = records[0]
    config = record.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    revision = labels.get(contract.image_revision_label) if isinstance(labels, dict) else None
    if record.get("Id") != image_id:
        raise BootstrapError("LEGACY_IMAGE_IDENTITY_MISMATCH")
    if isinstance(revision, str) and REVISION_RE.fullmatch(revision):
        raise BootstrapError("BOOTSTRAP_DENIED_USE_ORDINARY_DEPLOY")
    return image_id


def _baseline_contract(baseline: attestation.RuntimeBaseline) -> dict[str, Any]:
    document = {
        "hermes": asdict(baseline.hermes),
        "qdrant": asdict(baseline.qdrant),
        "database": asdict(baseline.database),
        "telegram_health": baseline.telegram_health,
        "gateway_health": baseline.gateway_health,
        "provider_request_count": baseline.provider_request_count,
    }
    return json.loads(json.dumps(document, ensure_ascii=True, sort_keys=True))


def _capture_eligible_baseline(
    contract: deploy.DeploymentContract,
) -> attestation.RuntimeBaseline:
    baseline = attestation.capture_pre_mutation_baseline(
        contract.attestation_policy,
        hermes_service=contract.target_service,
        qdrant_service="qdrant",
        database_path=contract.database_source,
        database_target=contract.database_target,
        revision_label=None,
        protected_secret_names=contract.protected_secret_names,
        run=deploy._run,
    )
    _inspect_legacy_image(contract, baseline.hermes.image_id)
    if baseline.hermes.revision:
        raise BootstrapError("LEGACY_SOURCE_REVISION_MUST_BE_UNKNOWN")
    return baseline


def _candidate_and_baseline(
    contract: deploy.DeploymentContract,
    *,
    source: Path,
    candidate_image: str,
    candidate_revision: str,
    lease: preflight.DeploymentLease | None = None,
) -> tuple[deploy.InspectedImage, attestation.RuntimeBaseline]:
    target, _secrets, _head = deploy._ordinary_deploy_pre_mutation_barrier(
        contract,
        source=source,
        image=candidate_image,
        revision=candidate_revision,
        lease=lease,
        lease_operation_class="legacy-bootstrap" if lease is not None else None,
    )
    baseline = _capture_eligible_baseline(contract)
    return target, baseline


def _create_rollback_artifact(path: Path, legacy_image_id: str) -> tuple[int, str]:
    if path.exists() or path.is_symlink():
        raise BootstrapError("ROLLBACK_ARTIFACT_COLLISION")
    previous_umask = os.umask(0o077)
    try:
        result = deploy._run(
            ("docker", "image", "save", "--output", str(path), legacy_image_id),
            timeout=300,
        )
    finally:
        os.umask(previous_umask)
    if result.returncode != 0:
        if path.exists() and path.is_file():
            path.unlink()
        raise BootstrapError("ROLLBACK_ARTIFACT_CREATE_FAILED")
    os.chmod(path, 0o600)
    try:
        _data, size, digest = _read_private_file(path, maximum=None)
    except (BootstrapError, OSError) as exc:
        raise BootstrapError("ROLLBACK_ARTIFACT_DRIFT") from exc
    if size <= 0:
        raise BootstrapError("ROLLBACK_ARTIFACT_METADATA_INVALID")
    return size, digest


def _validate_artifact(plan: dict[str, Any]) -> Path:
    path = Path(str(plan["ROLLBACK_ARTIFACT_PATH"]))
    if not path.is_absolute():
        raise BootstrapError("ROLLBACK_ARTIFACT_PATH_INVALID")
    _data, size, digest = _read_private_file(path, maximum=None)
    if (
        size != plan["ROLLBACK_ARTIFACT_SIZE"]
        or digest != plan["ROLLBACK_ARTIFACT_SHA256"]
    ):
        raise BootstrapError("ROLLBACK_ARTIFACT_DRIFT")
    return path


def _validate_plan_runtime(
    plan: dict[str, Any],
    *,
    lease: preflight.DeploymentLease | None = None,
) -> tuple[deploy.DeploymentContract, attestation.RuntimeBaseline, Path]:
    operation_id = plan.get("OPERATION_ID")
    repository_value = plan.get("REPOSITORY_ROOT")
    source_value = plan.get("SECRET_SOURCE_PATH")
    if (
        not isinstance(operation_id, str)
        or OPERATION_ID_RE.fullmatch(operation_id) is None
        or not isinstance(repository_value, str)
        or not Path(repository_value).is_absolute()
        or not isinstance(source_value, str)
        or not Path(source_value).is_absolute()
        or not isinstance(plan.get("CANDIDATE_IMAGE_ID"), str)
        or IMAGE_ID_RE.fullmatch(plan["CANDIDATE_IMAGE_ID"]) is None
        or not isinstance(plan.get("CANDIDATE_OCI_REVISION"), str)
        or REVISION_RE.fullmatch(plan["CANDIDATE_OCI_REVISION"]) is None
        or not isinstance(plan.get("LEGACY_IMAGE_ID"), str)
        or IMAGE_ID_RE.fullmatch(plan["LEGACY_IMAGE_ID"]) is None
        or not isinstance(plan.get("LEGACY_RUNTIME_BASELINE"), dict)
        or not isinstance(plan.get("ROLLBACK_ARTIFACT_SIZE"), int)
        or isinstance(plan.get("ROLLBACK_ARTIFACT_SIZE"), bool)
        or plan["ROLLBACK_ARTIFACT_SIZE"] <= 0
        or not isinstance(plan.get("ROLLBACK_ARTIFACT_SHA256"), str)
        or SHA256_RE.fullmatch(plan["ROLLBACK_ARTIFACT_SHA256"]) is None
    ):
        raise BootstrapError("BOOTSTRAP_PLAN_BINDING_INVALID")
    safety = {
        "OPERATION_CLASS": OPERATION_CLASS,
        "LEGACY_BASELINE_CLASSIFICATION": LEGACY_CLASSIFICATION,
        "SOURCE_REVISION": UNKNOWN_REVISION,
        "ALLOWED_MUTATION": "HERMES_RUNTIME_IMAGE_IDENTITY_ONLY",
        "SCHEMA_MIGRATION_ALLOWED": False,
        "FEATURE_ACTIVATION_ALLOWED": False,
        "SECRET_MUTATION_ALLOWED": False,
        "QDRANT_MUTATION_ALLOWED": False,
        "PLAN_STATE": "PLANNED",
    }
    if plan.get("BOOTSTRAP_PLAN_VERSION") != BOOTSTRAP_PLAN_VERSION or any(
        type(plan.get(name)) is not type(value) or plan.get(name) != value
        for name, value in safety.items()
    ):
        raise BootstrapError("BOOTSTRAP_PLAN_SAFETY_CONTRACT_INVALID")
    try:
        created_at = datetime.fromisoformat(
            str(plan["CREATED_AT"]).replace("Z", "+00:00")
        )
        expires_at = datetime.fromisoformat(str(plan["EXPIRES_AT"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise BootstrapError("BOOTSTRAP_PLAN_EXPIRY_INVALID") from exc
    now = datetime.now(timezone.utc)
    if (
        created_at > now
        or expires_at <= created_at
        or expires_at - created_at > timedelta(days=1)
        or now >= expires_at
    ):
        raise BootstrapError("BOOTSTRAP_PLAN_EXPIRED")
    repository_root = Path(str(plan["REPOSITORY_ROOT"]))
    source = Path(str(plan["SECRET_SOURCE_PATH"]))
    artifact_path = Path(str(plan["ROLLBACK_ARTIFACT_PATH"]))
    if artifact_path.parent.name != operation_id or artifact_path.name != "legacy-image.tar":
        raise BootstrapError("ROLLBACK_ARTIFACT_PATH_INVALID")
    _outside_repository(artifact_path, repository_root)
    contract = deploy.load_contract(repository_root)
    target, baseline = _candidate_and_baseline(
        contract,
        source=source,
        candidate_image=str(plan["CANDIDATE_IMAGE_ID"]),
        candidate_revision=str(plan["CANDIDATE_OCI_REVISION"]),
        lease=lease,
    )
    if target.image_id != plan["CANDIDATE_IMAGE_ID"]:
        raise BootstrapError("CANDIDATE_IMAGE_IDENTITY_DRIFT")
    if (
        baseline.hermes.image_id != plan["LEGACY_IMAGE_ID"]
        or _baseline_contract(baseline) != plan["LEGACY_RUNTIME_BASELINE"]
    ):
        raise BootstrapError("LEGACY_RUNTIME_BASELINE_DRIFT")
    return contract, baseline, _validate_artifact(plan)


def plan_bootstrap(args: argparse.Namespace) -> int:
    _root_required()
    if OPERATION_ID_RE.fullmatch(args.operation_id) is None:
        raise BootstrapError("OPERATION_ID_INVALID")
    if args.expires_in_seconds < 60 or args.expires_in_seconds > 86400:
        raise BootstrapError("BOOTSTRAP_PLAN_EXPIRY_INVALID")
    repository_root = Path(args.repository_root).resolve()
    source = Path(args.secret_source).resolve()
    parent = Path(args.artifact_parent).resolve()
    _private_directory(parent)
    _outside_repository(parent, repository_root)
    contract = deploy.load_contract(repository_root)
    target, baseline = _candidate_and_baseline(
        contract,
        source=source,
        candidate_image=args.candidate_image,
        candidate_revision=args.candidate_revision,
    )
    operation_directory = parent / args.operation_id
    operation_directory.mkdir(mode=0o700)
    os.chmod(operation_directory, 0o700)
    _private_directory(operation_directory)
    artifact = operation_directory / "legacy-image.tar"
    artifact_size, artifact_sha = _create_rollback_artifact(
        artifact,
        baseline.hermes.image_id,
    )
    created_at = datetime.now(timezone.utc)
    payload = {
        "BOOTSTRAP_PLAN_VERSION": BOOTSTRAP_PLAN_VERSION,
        "OPERATION_ID": args.operation_id,
        "OPERATION_CLASS": OPERATION_CLASS,
        "CREATED_AT": created_at.isoformat(),
        "EXPIRES_AT": (
            created_at + timedelta(seconds=args.expires_in_seconds)
        ).isoformat(),
        "REPOSITORY_ROOT": str(repository_root),
        "SECRET_SOURCE_PATH": str(source),
        "CANDIDATE_IMAGE_ID": target.image_id,
        "CANDIDATE_OCI_REVISION": args.candidate_revision,
        "LEGACY_BASELINE_CLASSIFICATION": LEGACY_CLASSIFICATION,
        "SOURCE_REVISION": UNKNOWN_REVISION,
        "LEGACY_IMAGE_ID": baseline.hermes.image_id,
        "LEGACY_RUNTIME_BASELINE": _baseline_contract(baseline),
        "ROLLBACK_ARTIFACT_PATH": str(artifact),
        "ROLLBACK_ARTIFACT_SIZE": artifact_size,
        "ROLLBACK_ARTIFACT_SHA256": artifact_sha,
        "ALLOWED_MUTATION": "HERMES_RUNTIME_IMAGE_IDENTITY_ONLY",
        "SCHEMA_MIGRATION_ALLOWED": False,
        "FEATURE_ACTIVATION_ALLOWED": False,
        "SECRET_MUTATION_ALLOWED": False,
        "QDRANT_MUTATION_ALLOWED": False,
        "PLAN_STATE": "PLANNED",
    }
    plan_path = operation_directory / "bootstrap-plan.json"
    plan_sha = _write_new_private_json(plan_path, payload)
    print(json.dumps({
        "status": "PASS",
        "mode": "PLAN_BOOTSTRAP",
        "plan_path": str(plan_path),
        "plan_sha256": plan_sha,
        "legacy_image_id": baseline.hermes.image_id,
        "source_revision": UNKNOWN_REVISION,
        "rollback_artifact_path": str(artifact),
        "rollback_artifact_sha256": artifact_sha,
        "production_mutated": False,
    }, sort_keys=True))
    return 0


def validate_bootstrap(args: argparse.Namespace) -> int:
    _root_required()
    plan = _open_plan(Path(args.plan), args.expected_plan_sha256)
    _validate_plan_runtime(plan)
    print(json.dumps({
        "status": "PASS",
        "mode": "VALIDATE_BOOTSTRAP",
        "operation_id": plan["OPERATION_ID"],
        "production_mutated": False,
    }, sort_keys=True))
    return 0


def _write_execution_evidence(
    plan_path: Path,
    *,
    status: str,
    original_error: str | None,
    rollback_error: str | None,
) -> None:
    _write_new_private_json(
        plan_path.parent / "bootstrap-execution.json",
        {
            "STATUS": status,
            "OPERATION_ID": plan_path.parent.name,
            "LEGACY_BASELINE_CLASSIFICATION": LEGACY_CLASSIFICATION,
            "SOURCE_REVISION": UNKNOWN_REVISION,
            "ORIGINAL_ERROR": original_error,
            "ROLLBACK_ERROR": rollback_error,
            "COMPLETED_AT": datetime.now(timezone.utc).isoformat(),
        },
    )


def _ensure_legacy_image(
    contract: deploy.DeploymentContract,
    image_id: str,
    artifact: Path,
) -> None:
    try:
        _inspect_legacy_image(contract, image_id)
        return
    except BootstrapError as exc:
        if exc.code != "LEGACY_IMAGE_MISSING":
            raise
    result = deploy._run(("docker", "image", "load", "--input", str(artifact)), timeout=300)
    if result.returncode != 0:
        raise BootstrapError("LEGACY_IMAGE_RESTORE_FAILED")
    _inspect_legacy_image(contract, image_id)


def execute_bootstrap(args: argparse.Namespace) -> int:
    _root_required()
    if args.confirm != EXECUTE_CONFIRMATION:
        raise BootstrapError("EXPLICIT_BOOTSTRAP_CONFIRMATION_REQUIRED")
    plan_path = Path(args.plan)
    plan = _open_plan(plan_path, args.expected_plan_sha256)
    contract = deploy.load_contract(Path(str(plan["REPOSITORY_ROOT"])))
    preflight.validate_deployment_lease_owner(
        allowed_owner_uids=contract.lease_owner_uids,
    )
    deploy._validate_runtime_directory(contract, create=True)
    lease = preflight.acquire_deployment_lease(
        path=contract.lease_path,
        allowed_owner_uids=contract.lease_owner_uids,
        operation_class="legacy-bootstrap",
        canonical_repository=contract.canonical_repository,
        target_sha=str(plan["CANDIDATE_OCI_REVISION"]),
        target_image_id=str(plan["CANDIDATE_IMAGE_ID"]),
        timeout_seconds=contract.lease_timeout_seconds,
    )
    primary: BaseException | None = None
    mutation_started = False
    try:
        contract, baseline, artifact = _validate_plan_runtime(plan, lease=lease)
        mutation_started = True
        deploy._compose_recreate_hermes(
            contract,
            image_id=str(plan["CANDIDATE_IMAGE_ID"]),
            revision=str(plan["CANDIDATE_OCI_REVISION"]),
        )
        result = attestation.post_deploy_attestation(
            contract.attestation_policy,
            baseline,
            hermes_service=contract.target_service,
            qdrant_service="qdrant",
            database_path=contract.database_source,
            revision_label=contract.image_revision_label,
            target_image_id=str(plan["CANDIDATE_IMAGE_ID"]),
            target_revision=str(plan["CANDIDATE_OCI_REVISION"]),
            protected_secret_names=contract.protected_secret_names,
            run=deploy._run,
        )
        if result.database_delta_result != "UNCHANGED" or result.qdrant_result != "UNCHANGED":
            raise BootstrapError("BOOTSTRAP_STATE_DELTA")
        _write_execution_evidence(
            plan_path,
            status="PASS",
            original_error=None,
            rollback_error=None,
        )
        print(json.dumps({"status": "PASS", "mode": "EXECUTE_BOOTSTRAP"}, sort_keys=True))
        return 0
    except BaseException as exc:
        primary = exc
        if not mutation_started:
            raise
        original_code = getattr(exc, "code", "BOOTSTRAP_POST_CHECK_FAILED")
        rollback_error: str | None = None
        try:
            _ensure_legacy_image(
                contract,
                str(plan["LEGACY_IMAGE_ID"]),
                artifact,
            )
            deploy._compose_recreate_hermes(
                contract,
                image_id=str(plan["LEGACY_IMAGE_ID"]),
                # Compose requires a valid interpolation SHA; this is the candidate
                # SHA, never an attribution of provenance to the legacy image.
                revision=str(plan["CANDIDATE_OCI_REVISION"]),
            )
            rollback_baseline = attestation.rollback_log_baseline(baseline)
            attestation.post_deploy_attestation(
                contract.attestation_policy,
                rollback_baseline,
                hermes_service=contract.target_service,
                qdrant_service="qdrant",
                database_path=contract.database_source,
                revision_label=None,
                target_image_id=str(plan["LEGACY_IMAGE_ID"]),
                target_revision="",
                protected_secret_names=contract.protected_secret_names,
                run=deploy._run,
            )
        except BaseException as rollback_exc:
            rollback_error = getattr(rollback_exc, "code", "LEGACY_ROLLBACK_FAILED")
        status = "ROLLED_BACK" if rollback_error is None else "FAIL"
        try:
            _write_execution_evidence(
                plan_path,
                status=status,
                original_error=str(original_code),
                rollback_error=rollback_error,
            )
        except BaseException:
            if rollback_error is None:
                rollback_error = "BOOTSTRAP_EVIDENCE_WRITE_FAILED"
                status = "FAIL"
        if status == "ROLLED_BACK":
            raise BootstrapRolledBack("BOOTSTRAP_CANDIDATE_REJECTED_ROLLED_BACK") from exc
        raise BootstrapError(rollback_error or "LEGACY_ROLLBACK_FAILED") from exc
    finally:
        try:
            preflight.release_deployment_lease(
                lease,
                allowed_owner_uids=contract.lease_owner_uids,
            )
        except BaseException:
            if primary is None:
                raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan-bootstrap")
    plan.add_argument("--repository-root", required=True)
    plan.add_argument("--operation-id", required=True)
    plan.add_argument("--artifact-parent", required=True)
    plan.add_argument("--secret-source", required=True)
    plan.add_argument("--candidate-image", required=True)
    plan.add_argument("--candidate-revision", required=True)
    plan.add_argument("--expires-in-seconds", type=int, default=3600)
    validate = subparsers.add_parser("validate-bootstrap")
    validate.add_argument("--plan", required=True)
    validate.add_argument("--expected-plan-sha256", required=True)
    execute = subparsers.add_parser("execute-bootstrap")
    execute.add_argument("--plan", required=True)
    execute.add_argument("--expected-plan-sha256", required=True)
    execute.add_argument("--confirm", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan-bootstrap":
            return plan_bootstrap(args)
        if args.command == "validate-bootstrap":
            return validate_bootstrap(args)
        return execute_bootstrap(args)
    except BootstrapRolledBack as exc:
        print(json.dumps({"status": "ROLLED_BACK", "error": exc.code}, sort_keys=True))
        return 2
    except (
        BootstrapError,
        OSError,
        deploy.DeploymentContractError,
        attestation.RuntimeAttestationError,
        preflight.DeployPreflightError,
    ) as exc:
        print(json.dumps({"status": "FAIL", "error": getattr(exc, "code", "BOOTSTRAP_FAILED")}, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
