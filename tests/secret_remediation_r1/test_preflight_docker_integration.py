import os
import tempfile
import subprocess
import shutil
import json
import pytest

from ops.secret_remediation_r1.preflight import run_compose_preflight, PreflightError

# Only run if docker is available
try:
    subprocess.run(
        ["docker", "info"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False


@pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker is not available")
def test_real_docker_preflight(monkeypatch):
    """
    Test real docker compose behavior for preflight, using a local scratch image.
    This prevents mock-only coverage from masking docker compose behavior changes.
    """
    # 1. Build a synthetic test image
    image_tag = "hermes-preflight-test-image:latest"
    build_dir = tempfile.mkdtemp(prefix="hermes_preflight_build_")
    try:
        with open(os.path.join(build_dir, "Dockerfile"), "w") as f:
            f.write('FROM scratch\nCMD ["/bin/true"]\n')

        subprocess.run(
            ["docker", "build", "-t", image_tag, "."],
            cwd=build_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    finally:
        shutil.rmtree(build_dir, ignore_errors=True)

    # Patch the image reference inside preflight
    def mock_write(self, content):
        if isinstance(content, str):
            content = content.replace(
                "healbite-hermes:pr99-main-273b0a6cccaf", image_tag
            )
        # Original write
        return type(self).write(self, content)

    # We patch the built-in open so the compose_path gets the test image
    original_open = __builtins__["open"]

    def patched_open(
        file,
        mode="r",
        buffering=-1,
        encoding=None,
        errors=None,
        newline=None,
        closefd=True,
        opener=None,
    ):
        f = original_open(
            file, mode, buffering, encoding, errors, newline, closefd, opener
        )
        if "docker-compose.yml" in str(file) and "w" in mode:
            # Override write method for this file object
            orig_write = f.write

            def new_write(data):
                if isinstance(data, str):
                    data = data.replace(
                        "healbite-hermes:pr99-main-273b0a6cccaf", image_tag
                    )
                return orig_write(data)

            f.write = new_write
        return f

    monkeypatch.setattr("builtins.open", patched_open)

    # Run the preflight! If successful, the container is created,
    # its env semantics are verified, and cleanup is robust.
    try:
        run_compose_preflight()
    finally:
        # Cleanup the synthetic image
        subprocess.run(
            ["docker", "rmi", "-f", image_tag],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
