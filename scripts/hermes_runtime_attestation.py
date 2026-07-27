"""Hash-bound pre-stop runtime attestation for production migration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts import hermes_execution_authority as authority


RUNTIME_ATTESTATION_VERSION = 1
MAX_ATTESTATION_LIFETIME = timedelta(hours=1)
RUNTIME_ATTESTATION_FIELDS = frozenset(
    {
        "RUNTIME_ATTESTATION_VERSION",
        "CREATED_AT",
        "EXPIRES_AT",
        "PLAN_PATH",
        "PLAN_SHA256",
        "OPERATION_ID",
        "SOURCE_SHA",
        "TARGET_IMAGE_ID",
        "CURRENT_RUNTIME_IMAGE_ID",
        "CANONICAL_PRODUCTION_DB_PATH",
        "SOURCE_DB_DEVICE",
        "SOURCE_DB_INODE",
        "SOURCE_DB_SIZE",
        "SOURCE_DB_SHA256",
        "FINAL_AUTHORITY_SHA256",
        "OPERATIONS_ROOT_APPROVAL_SHA256",
        "CLEAN_START_POLICY_SHA256",
        "RUNTIME_STATE_ATTESTED",
        "EXPECTED_RUNTIME_TRANSITION",
        "IMMUTABLE_RUNTIME_IDENTITY",
        "CONTAINS_SECRETS",
    }
)


def _canonical_digest(payload: object) -> str:
    data = (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")
    return hashlib.sha256(data).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _runtime_file_record(path_value: str) -> dict[str, int | str]:
    path = authority._absolute_path(
        path_value,
        "RUNTIME_COMPOSE_PATH_INVALID",
    )
    expected_uid, _expected_gid = authority._effective_identity()
    trusted_parent = authority.validate_trusted_parent_chain(
        path.parent,
        expected_uid=expected_uid,
    )
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > authority.MAX_ARTIFACT_BYTES
        ):
            raise authority.ExecutionAuthorityError(
                "RUNTIME_COMPOSE_FILE_INVALID"
            )
        data = authority._read_exact(
            fd, int(before.st_size), "RUNTIME_COMPOSE_FILE_INVALID"
        )
        path_metadata = os.stat(path, follow_symlinks=False)
        after = os.fstat(fd)
        final_parent = authority.validate_trusted_parent_chain(
            path.parent,
            expected_uid=expected_uid,
        )
        identity = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_uid),
            int(before.st_gid),
            stat.S_IMODE(before.st_mode),
            int(before.st_nlink),
        )
        if (
            (
                int(path_metadata.st_dev),
                int(path_metadata.st_ino),
                int(path_metadata.st_size),
                int(path_metadata.st_uid),
                int(path_metadata.st_gid),
                stat.S_IMODE(path_metadata.st_mode),
                int(path_metadata.st_nlink),
            )
            != identity
            or (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_uid),
                int(after.st_gid),
                stat.S_IMODE(after.st_mode),
                int(after.st_nlink),
            )
            != identity
            or final_parent != trusted_parent
        ):
            raise authority.ExecutionAuthorityError(
                "RUNTIME_COMPOSE_FILE_DRIFT"
            )
        return {
            "PATH": str(path),
            "DEVICE": identity[0],
            "INODE": identity[1],
            "SIZE": identity[2],
            "UID": identity[3],
            "GID": identity[4],
            "MODE": identity[5],
            "SHA256": hashlib.sha256(data).hexdigest(),
        }
    finally:
        os.close(fd)


def capture_runtime_identity(
    bundle: authority.ExecutionAuthorityBundle,
    *,
    expected_running: bool,
) -> dict[str, Any]:
    descriptor = bundle.invocation_descriptor.payload
    runtime = authority._inspect_runtime(
        str(descriptor["APPLICATION_SERVICE"])
    )
    authority._validate_runtime_payload(
        runtime,
        descriptor,
        bundle.runtime_image_id,
        expected_running=expected_running,
    )
    container_id = runtime.get("Id")
    name = runtime.get("Name")
    config = runtime.get("Config")
    host_config = runtime.get("HostConfig")
    mounts = runtime.get("Mounts")
    if (
        not isinstance(container_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
        or not isinstance(name, str)
        or not name
        or not isinstance(config, dict)
        or not isinstance(host_config, dict)
        or not isinstance(mounts, list)
    ):
        raise authority.ExecutionAuthorityError(
            "RUNTIME_ATTESTATION_METADATA_INVALID"
        )
    labels = config.get("Labels")
    environment = config.get("Env")
    if not isinstance(labels, dict) or not isinstance(environment, list):
        raise authority.ExecutionAuthorityError(
            "RUNTIME_ATTESTATION_METADATA_INVALID"
        )
    if not all(isinstance(item, str) and "=" in item for item in environment):
        raise authority.ExecutionAuthorityError(
            "RUNTIME_ATTESTATION_ENVIRONMENT_INVALID"
        )
    project_directory = labels.get(
        "com.docker.compose.project.working_dir"
    )
    config_files_value = labels.get(
        "com.docker.compose.project.config_files"
    )
    if (
        not isinstance(project_directory, str)
        or not project_directory
        or not isinstance(config_files_value, str)
        or not config_files_value
    ):
        raise authority.ExecutionAuthorityError(
            "RUNTIME_COMPOSE_IDENTITY_INCOMPLETE"
        )
    authority._absolute_path(
        project_directory,
        "RUNTIME_COMPOSE_PATH_INVALID",
    )
    config_files = config_files_value.split(",")
    if (
        not config_files
        or any(not item for item in config_files)
        or len(set(config_files)) != len(config_files)
    ):
        raise authority.ExecutionAuthorityError(
            "RUNTIME_COMPOSE_FILE_ORDER_INVALID"
        )
    compose_records = [_runtime_file_record(item) for item in config_files]
    mount_records: list[dict[str, Any]] = []
    for item in mounts:
        if not isinstance(item, dict):
            raise authority.ExecutionAuthorityError(
                "CURRENT_RUNTIME_MOUNT_METADATA_INVALID"
            )
        destination = str(
            authority._normalize_container_mount_destination(
                item.get("Destination")
            )
        )
        source = item.get("Source")
        mount_type = item.get("Type")
        rw = item.get("RW")
        if (
            not isinstance(source, str)
            or not isinstance(mount_type, str)
            or not isinstance(rw, bool)
        ):
            raise authority.ExecutionAuthorityError(
                "CURRENT_RUNTIME_MOUNT_METADATA_INVALID"
            )
        mount_records.append(
            {
                "TYPE": mount_type,
                "SOURCE": source,
                "DESTINATION": destination,
                "RW": rw,
                "MODE": str(item.get("Mode") or ""),
                "PROPAGATION": str(item.get("Propagation") or ""),
            }
        )
    mount_records.sort(
        key=lambda item: (item["DESTINATION"], item["SOURCE"])
    )
    credential_entries = sorted(
        item
        for item in environment
        if re.search(
            r"(?:TOKEN|SECRET|PASSWORD|API_KEY|CREDENTIAL|AUTH)",
            item.split("=", 1)[0],
            flags=re.IGNORECASE,
        )
    )
    revision = authority._inspect_image(bundle.runtime_image_id, None)
    if authority.REVISION_RE.fullmatch(revision) is None:
        raise authority.ExecutionAuthorityError(
            "RUNTIME_IMAGE_REVISION_INVALID"
        )
    return {
        "CONTAINER_ID": container_id,
        "CONTAINER_NAME": name,
        "IMAGE_ID": bundle.runtime_image_id,
        "IMAGE_REVISION": revision,
        "COMPOSE_PROJECT": labels["com.docker.compose.project"],
        "COMPOSE_SERVICE": labels["com.docker.compose.service"],
        "COMPOSE_PROJECT_DIRECTORY": project_directory,
        "COMPOSE_CONFIG_FILES": config_files,
        "COMPOSE_CONFIG_FILE_RECORDS": compose_records,
        "MOUNTS": mount_records,
        "CONTAINER_CONFIG_SHA256": _canonical_digest(
            {"Config": config, "HostConfig": host_config}
        ),
        "CREDENTIAL_FINGERPRINT_SHA256": _canonical_digest(
            credential_entries
        ),
        "CREDENTIAL_ENTRY_COUNT": len(credential_entries),
    }


def build_runtime_attestation_payload(
    *,
    bundle: authority.ExecutionAuthorityBundle,
    plan_path: Path,
    plan_sha256: str,
    plan: dict[str, Any],
    now: datetime | None = None,
) -> dict[str, Any]:
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    plan_expiry = authority._timestamp(
        plan["EXPIRES_AT"],
        "PLAN_EXPIRY_INVALID",
    )
    expires = min(
        bundle.expires_at,
        plan_expiry,
        created + MAX_ATTESTATION_LIFETIME,
    )
    if created >= expires:
        raise authority.ExecutionAuthorityError(
            "RUNTIME_ATTESTATION_EXPIRED"
        )
    return {
        "RUNTIME_ATTESTATION_VERSION": RUNTIME_ATTESTATION_VERSION,
        "CREATED_AT": _timestamp(created),
        "EXPIRES_AT": _timestamp(expires),
        "PLAN_PATH": str(plan_path),
        "PLAN_SHA256": plan_sha256,
        "OPERATION_ID": plan["OPERATION_ID"],
        "SOURCE_SHA": plan["MIGRATION_IMAGE_REVISION"],
        "TARGET_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
        "CURRENT_RUNTIME_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
        "CANONICAL_PRODUCTION_DB_PATH": plan["DB_CANONICAL_PATH"],
        "SOURCE_DB_DEVICE": plan["SOURCE_DEVICE"],
        "SOURCE_DB_INODE": plan["SOURCE_INODE"],
        "SOURCE_DB_SIZE": plan["SOURCE_SIZE"],
        "SOURCE_DB_SHA256": plan["SOURCE_SHA256"],
        "FINAL_AUTHORITY_SHA256": bundle.final_authority.sha256,
        "OPERATIONS_ROOT_APPROVAL_SHA256": plan[
            "OPERATIONS_ROOT_APPROVAL_SHA256"
        ],
        "CLEAN_START_POLICY_SHA256": plan[
            "CLEAN_START_POLICY_SHA256"
        ],
        "RUNTIME_STATE_ATTESTED": "running",
        "EXPECTED_RUNTIME_TRANSITION": "running_to_stopped",
        "IMMUTABLE_RUNTIME_IDENTITY": capture_runtime_identity(
            bundle,
            expected_running=True,
        ),
        "CONTAINS_SECRETS": False,
    }


def open_runtime_attestation(
    path: str,
    expected_sha256: str,
) -> authority.BoundJsonArtifact:
    return authority._open_bound_json(
        path,
        expected_sha256,
        code_prefix="RUNTIME_ATTESTATION",
        fields=RUNTIME_ATTESTATION_FIELDS,
    )


def validate_stopped_runtime_attestation(
    *,
    artifact: authority.BoundJsonArtifact,
    bundle: authority.ExecutionAuthorityBundle,
    plan_path: Path,
    plan_sha256: str,
    plan: dict[str, Any],
) -> None:
    payload = artifact.payload
    created = authority._timestamp(
        payload["CREATED_AT"],
        "RUNTIME_ATTESTATION_CREATED_AT_INVALID",
    )
    expires = authority._timestamp(
        payload["EXPIRES_AT"],
        "RUNTIME_ATTESTATION_EXPIRES_AT_INVALID",
    )
    now = datetime.now(timezone.utc)
    if (
        created > now
        or expires <= created
        or expires - created > MAX_ATTESTATION_LIFETIME
        or expires > bundle.expires_at
        or now >= expires
    ):
        raise authority.ExecutionAuthorityError(
            "RUNTIME_ATTESTATION_EXPIRED"
        )
    expected = {
        "RUNTIME_ATTESTATION_VERSION": RUNTIME_ATTESTATION_VERSION,
        "PLAN_PATH": str(plan_path),
        "PLAN_SHA256": plan_sha256,
        "OPERATION_ID": plan["OPERATION_ID"],
        "SOURCE_SHA": plan["MIGRATION_IMAGE_REVISION"],
        "TARGET_IMAGE_ID": plan["MIGRATION_IMAGE_ID"],
        "CURRENT_RUNTIME_IMAGE_ID": plan["PREVIOUS_IMAGE_ID"],
        "CANONICAL_PRODUCTION_DB_PATH": plan["DB_CANONICAL_PATH"],
        "SOURCE_DB_DEVICE": plan["SOURCE_DEVICE"],
        "SOURCE_DB_INODE": plan["SOURCE_INODE"],
        "SOURCE_DB_SIZE": plan["SOURCE_SIZE"],
        "SOURCE_DB_SHA256": plan["SOURCE_SHA256"],
        "FINAL_AUTHORITY_SHA256": bundle.final_authority.sha256,
        "OPERATIONS_ROOT_APPROVAL_SHA256": plan[
            "OPERATIONS_ROOT_APPROVAL_SHA256"
        ],
        "CLEAN_START_POLICY_SHA256": plan[
            "CLEAN_START_POLICY_SHA256"
        ],
        "RUNTIME_STATE_ATTESTED": "running",
        "EXPECTED_RUNTIME_TRANSITION": "running_to_stopped",
        "CONTAINS_SECRETS": False,
    }
    authority._typed_fields(
        payload,
        expected,
        "RUNTIME_ATTESTATION_PLAN_BINDING_MISMATCH",
    )
    current_identity = capture_runtime_identity(
        bundle,
        expected_running=False,
    )
    if payload["IMMUTABLE_RUNTIME_IDENTITY"] != current_identity:
        raise authority.ExecutionAuthorityError(
            "RUNTIME_IDENTITY_DRIFT_AFTER_STOP"
        )
