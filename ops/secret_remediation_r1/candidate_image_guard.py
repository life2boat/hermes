"""Verify the effective hermes-bot image matches the legacy reference before recreate."""

from __future__ import annotations
import json
import subprocess
from ops.secret_remediation_r1.constants import LEGACY_IMAGE_REF, LEGACY_IMAGE_ID


class CandidateImageGuardError(Exception):
    pass


class DockerImageBackend:
    def inspect_image(self, ref: str) -> dict:
        r = subprocess.run(
            ["docker", "image", "inspect", ref],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            raise CandidateImageGuardError(f"docker image inspect failed: {r.stderr}")
        data = json.loads(r.stdout)
        if not data:
            raise CandidateImageGuardError(f"No image data for {ref!r}")
        return data[0]


def verify_legacy_image(
    effective_image_ref: str,
    backend: DockerImageBackend | None = None,
) -> None:
    """
    Verify the effective image reference exactly matches the legacy reference
    and that the local image ID matches the expected digest.
    """
    if effective_image_ref != LEGACY_IMAGE_REF:
        raise CandidateImageGuardError(
            f"Effective image {effective_image_ref!r} != expected {LEGACY_IMAGE_REF!r}"
        )

    if backend is None:
        backend = DockerImageBackend()

    image_data = backend.inspect_image(effective_image_ref)
    actual_id = image_data.get("Id", "")
    if actual_id != LEGACY_IMAGE_ID:
        raise CandidateImageGuardError(
            f"Image ID {actual_id!r} != expected {LEGACY_IMAGE_ID!r}"
        )
