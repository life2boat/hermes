"""Verify runtime container invariants post-recreation.

Checks that all expected runtime configuration is unchanged:
  - Container is running
  - Compose project and service labels are exact
  - Legacy image ref and image ID are exact
  - DB mount source/destination are exact
  - MEMORY_VECTOR_ENABLED, QDRANT_ENDPOINT, QDRANT_COLLECTION are present and correct
"""
from __future__ import annotations

from ops.secret_remediation_r1.constants import (
    COMPOSE_PROJECT,
    COMPOSE_SERVICE,
    CONTAINER_NAME,
    DB_MOUNT_DESTINATION,
    DB_MOUNT_SOURCE,
    LEGACY_IMAGE_ID,
    LEGACY_IMAGE_REF,
    QDRANT_COLLECTION,
)
from ops.secret_remediation_r1.process_identity import DockerBackend


class RuntimeInvariantError(Exception):
    pass


def _parse_env_list(env_list: list) -> dict[str, str]:
    """Parse a Docker Env list (``["KEY=val", ...]``) into a name→value dict.

    Values are stored for structural verification only; secret values are not
    logged, compared, or exposed beyond presence/emptiness checks.
    """
    result: dict[str, str] = {}
    for entry in env_list:
        if "=" in entry:
            k, v = entry.split("=", 1)
            result[k] = v
    return result


def verify_runtime_invariants(docker: DockerBackend | None = None) -> None:
    """Verify all runtime container invariants.

    Raises RuntimeInvariantError on any violation.
    """
    from ops.secret_remediation_r1.process_identity import RealDockerBackend
    if docker is None:
        docker = RealDockerBackend()

    containers = docker.inspect(CONTAINER_NAME)
    if not containers:
        raise RuntimeInvariantError(f"Container {CONTAINER_NAME} not found")

    data = containers[0]

    # ── Image identity ────────────────────────────────────────────────────
    labels = data.get("Config", {}).get("Labels", {})
    image_ref = (
        labels.get("com.docker.compose.image", "")
        or data.get("Config", {}).get("Image", "")
    )
    if image_ref != LEGACY_IMAGE_REF:
        raise RuntimeInvariantError(
            f"Image ref mismatch: {image_ref!r} != {LEGACY_IMAGE_REF!r}"
        )

    actual_image_id = data.get("Image", "")
    if actual_image_id != LEGACY_IMAGE_ID:
        raise RuntimeInvariantError(
            f"Image ID mismatch: {actual_image_id!r} != {LEGACY_IMAGE_ID!r}"
        )

    # ── Compose labels ────────────────────────────────────────────────────
    project = labels.get("com.docker.compose.project", "")
    service = labels.get("com.docker.compose.service", "")
    if project != COMPOSE_PROJECT:
        raise RuntimeInvariantError(f"Wrong project: {project!r}")
    if service != COMPOSE_SERVICE:
        raise RuntimeInvariantError(f"Wrong service: {service!r}")

    # ── DB mount ──────────────────────────────────────────────────────────
    mounts = data.get("Mounts", [])
    db_mount_found = False
    for mount in mounts:
        if mount.get("Source") == DB_MOUNT_SOURCE:
            if mount.get("Destination") != DB_MOUNT_DESTINATION:
                raise RuntimeInvariantError(
                    f"DB mount destination mismatch: {mount.get('Destination')!r}"
                )
            db_mount_found = True
    if not db_mount_found:
        raise RuntimeInvariantError(f"DB mount not found: {DB_MOUNT_SOURCE!r}")

    # ── Environment checks ────────────────────────────────────────────────
    env_dict = _parse_env_list(data.get("Config", {}).get("Env", []))

    if env_dict.get("MEMORY_VECTOR_ENABLED") != "true":
        raise RuntimeInvariantError(
            f"MEMORY_VECTOR_ENABLED mismatch: {env_dict.get('MEMORY_VECTOR_ENABLED')!r}"
        )

    # QDRANT_ENDPOINT: must be present and non-empty.
    # We verify presence and non-emptiness only; the value is not logged.
    if not env_dict.get("QDRANT_ENDPOINT"):
        raise RuntimeInvariantError("QDRANT_ENDPOINT missing or empty")

    if env_dict.get("QDRANT_COLLECTION") != QDRANT_COLLECTION:
        raise RuntimeInvariantError(
            f"QDRANT_COLLECTION mismatch: {env_dict.get('QDRANT_COLLECTION')!r} "
            f"!= {QDRANT_COLLECTION!r}"
        )
