from dataclasses import dataclass
from typing import Any

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


@dataclass
class RuntimePrestate:
    container_id: str
    compose_project: str
    compose_service: str
    image_ref: str
    image_id: str
    db_mount_source: str
    db_mount_destination: str
    memory_vector_enabled: str | None
    qdrant_endpoint: str | None
    qdrant_collection: str | None


def _parse_env_list(env_list: list) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in env_list:
        if "=" in entry:
            k, v = entry.split("=", 1)
            result[k] = v
    return result


def capture_runtime_prestate(docker: DockerBackend | None = None) -> RuntimePrestate:
    from ops.secret_remediation_r1.process_identity import RealDockerBackend

    if docker is None:
        docker = RealDockerBackend()

    containers = docker.inspect(CONTAINER_NAME)
    if not containers:
        raise RuntimeInvariantError(
            f"Container {CONTAINER_NAME} not found during prestate capture"
        )

    data = containers[0]

    container_id = data.get("Id", "")
    labels = data.get("Config", {}).get("Labels", {})
    image_ref = labels.get("com.docker.compose.image", "") or data.get(
        "Config", {}
    ).get("Image", "")
    image_id = data.get("Image", "")
    compose_project = labels.get("com.docker.compose.project", "")
    compose_service = labels.get("com.docker.compose.service", "")

    db_mount_src = ""
    db_mount_dst = ""
    for mount in data.get("Mounts", []):
        if mount.get("Source") == DB_MOUNT_SOURCE:
            db_mount_src = mount.get("Source", "")
            db_mount_dst = mount.get("Destination", "")
            break

    env_dict = _parse_env_list(data.get("Config", {}).get("Env", []))

    return RuntimePrestate(
        container_id=container_id,
        compose_project=compose_project,
        compose_service=compose_service,
        image_ref=image_ref,
        image_id=image_id,
        db_mount_source=db_mount_src,
        db_mount_destination=db_mount_dst,
        memory_vector_enabled=env_dict.get("MEMORY_VECTOR_ENABLED"),
        qdrant_endpoint=env_dict.get("QDRANT_ENDPOINT"),
        qdrant_collection=env_dict.get("QDRANT_COLLECTION"),
    )


def verify_runtime_invariants(
    expected: RuntimePrestate, docker: DockerBackend | None = None
) -> None:
    from ops.secret_remediation_r1.process_identity import RealDockerBackend

    if docker is None:
        docker = RealDockerBackend()

    containers = docker.inspect(CONTAINER_NAME)
    if not containers:
        raise RuntimeInvariantError(f"Container {CONTAINER_NAME} not found")

    data = containers[0]

    if not data.get("State", {}).get("Running"):
        raise RuntimeInvariantError("Container is not running")

    # Image identity
    labels = data.get("Config", {}).get("Labels", {})
    image_ref = labels.get("com.docker.compose.image", "") or data.get(
        "Config", {}
    ).get("Image", "")

    # Must match prestate and constants
    if image_ref != expected.image_ref or image_ref != LEGACY_IMAGE_REF:
        raise RuntimeInvariantError(f"Image ref mismatch: {image_ref!r}")

    actual_image_id = data.get("Image", "")
    if actual_image_id != expected.image_id or actual_image_id != LEGACY_IMAGE_ID:
        raise RuntimeInvariantError(f"Image ID mismatch: {actual_image_id!r}")

    # Compose labels
    project = labels.get("com.docker.compose.project", "")
    service = labels.get("com.docker.compose.service", "")
    if project != expected.compose_project or project != COMPOSE_PROJECT:
        raise RuntimeInvariantError(f"Wrong project: {project!r}")
    if service != expected.compose_service or service != COMPOSE_SERVICE:
        raise RuntimeInvariantError(f"Wrong service: {service!r}")

    # DB mount
    mounts = data.get("Mounts", [])
    db_mount_found = False
    for mount in mounts:
        if (
            mount.get("Source") == expected.db_mount_source
            or mount.get("Source") == DB_MOUNT_SOURCE
        ):
            if (
                mount.get("Destination") != expected.db_mount_destination
                or mount.get("Destination") != DB_MOUNT_DESTINATION
            ):
                raise RuntimeInvariantError(
                    f"DB mount destination mismatch: {mount.get('Destination')!r}"
                )
            db_mount_found = True
    if not db_mount_found:
        raise RuntimeInvariantError(f"DB mount not found: {DB_MOUNT_SOURCE!r}")

    # Environment checks
    env_dict = _parse_env_list(data.get("Config", {}).get("Env", []))

    if env_dict.get("MEMORY_VECTOR_ENABLED") != expected.memory_vector_enabled:
        raise RuntimeInvariantError(
            f"MEMORY_VECTOR_ENABLED mismatch: {env_dict.get('MEMORY_VECTOR_ENABLED')!r}"
        )

    # For QDRANT_ENDPOINT, we only check presence and structure if it was present before,
    # but the instructions say "actual canonical Qdrant runtime configuration field(s)".
    # If it changed, that's a violation.
    if env_dict.get("QDRANT_ENDPOINT") != expected.qdrant_endpoint:
        raise RuntimeInvariantError("QDRANT_ENDPOINT mismatch")

    if (
        env_dict.get("QDRANT_COLLECTION") != expected.qdrant_collection
        or env_dict.get("QDRANT_COLLECTION") != QDRANT_COLLECTION
    ):
        raise RuntimeInvariantError(f"QDRANT_COLLECTION mismatch")
