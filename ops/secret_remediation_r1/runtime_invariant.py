"""Verify runtime container invariants post-recreation."""
from __future__ import annotations
import json
import subprocess
from ops.secret_remediation_r1.constants import (
    CONTAINER_NAME, COMPOSE_PROJECT, COMPOSE_SERVICE,
    LEGACY_IMAGE_REF, LEGACY_IMAGE_ID,
    DB_MOUNT_SOURCE, DB_MOUNT_DESTINATION,
    QDRANT_COLLECTION,
)
from ops.secret_remediation_r1.process_identity import DockerBackend


class RuntimeInvariantError(Exception):
    pass


def verify_runtime_invariants(docker: DockerBackend | None = None) -> None:
    from ops.secret_remediation_r1.process_identity import RealDockerBackend
    if docker is None:
        docker = RealDockerBackend()

    containers = docker.inspect(CONTAINER_NAME)
    if not containers:
        raise RuntimeInvariantError(f"Container {CONTAINER_NAME} not found")

    data = containers[0]

    # Image
    labels = data.get("Config", {}).get("Labels", {})
    image_ref = labels.get("com.docker.compose.image", "") or data.get("Config", {}).get("Image", "")
    if not image_ref:
        # Try from Name pattern
        pass

    actual_image_id = data.get("Image", "")
    if actual_image_id != LEGACY_IMAGE_ID:
        raise RuntimeInvariantError(
            f"Image ID mismatch: {actual_image_id!r} != {LEGACY_IMAGE_ID!r}"
        )

    # Compose labels
    project = labels.get("com.docker.compose.project", "")
    service = labels.get("com.docker.compose.service", "")
    if project != COMPOSE_PROJECT:
        raise RuntimeInvariantError(f"Wrong project: {project!r}")
    if service != COMPOSE_SERVICE:
        raise RuntimeInvariantError(f"Wrong service: {service!r}")

    # DB mount
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
