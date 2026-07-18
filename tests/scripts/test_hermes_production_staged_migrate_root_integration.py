from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


TARGET_IMAGE_ENV = "HERMES_ROOT_INTEGRATION_TARGET_IMAGE_ID"
TARGET_REVISION_ENV = "HERMES_ROOT_INTEGRATION_TARGET_REVISION"
PREVIOUS_IMAGE_ENV = "HERMES_ROOT_INTEGRATION_PREVIOUS_IMAGE_ID"
OUTER_IMAGE_ENV = "HERMES_ROOT_INTEGRATION_OUTER_IMAGE"


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        pytest.skip(f"root integration policy skip: {name} is not set")
    return value


def _image_available(image: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _git_common_directory(repository_root: Path) -> Path:
    result = subprocess.run(
        ["git", "-C", str(repository_root), "rev-parse", "--git-common-dir"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise AssertionError("repository common Git directory unavailable")
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = repository_root / path
    return path.resolve(strict=True)


def test_public_plan_inspect_execute_runs_as_real_root_without_network() -> None:
    if shutil.which("docker") is None:
        pytest.skip("root integration capability skip: docker CLI unavailable")
    if not Path("/var/run/docker.sock").exists():
        pytest.skip("root integration capability skip: docker socket unavailable")
    if not Path("/usr/bin/docker").is_file():
        pytest.skip("root integration capability skip: docker binary path unavailable")

    target_image = _required_environment(TARGET_IMAGE_ENV)
    target_revision = _required_environment(TARGET_REVISION_ENV)
    previous_image = _required_environment(PREVIOUS_IMAGE_ENV)
    outer_image = os.environ.get(
        OUTER_IMAGE_ENV, target_image
    ).strip()
    for image in (target_image, previous_image, outer_image):
        if not _image_available(image):
            pytest.skip(
                "root integration capability skip: required immutable "
                f"image unavailable ({image[:19]})"
            )

    repository_root = Path(__file__).resolve().parents[2]
    git_common_directory = _git_common_directory(repository_root)
    git_mount: list[str] = []
    if not git_common_directory.is_relative_to(repository_root):
        git_mount = [
            "-v", f"{git_common_directory}:{git_common_directory}:ro"
        ]
    runtime_root = Path(
        f"/tmp/hermes-root-gate-{uuid.uuid4().hex}"
    )
    command = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--user",
        "0:0",
        "--entrypoint",
        "/opt/hermes/.venv/bin/python",
        "-e",
        "PYTHONPATH=/repo",
        "-v",
        f"{repository_root}:/repo:ro",
        *git_mount,
        "-v",
        "/tmp:/tmp:rw",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        "/usr/bin/docker:/usr/bin/docker:ro",
        outer_image,
        "/repo/tests/scripts/hermes_production_staged_migrate_root_harness.py",
        "--runtime-root",
        str(runtime_root),
        "--repository-root",
        "/repo",
        "--target-image-id",
        target_image,
        "--target-revision",
        target_revision,
        "--previous-image-id",
        previous_image,
    ]
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (
        f"root integration failed: {result.stderr[-2000:]}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == {
        "backfill_rows_created": 0,
        "backup_durable": True,
        "canonical_deployment_contract_used": True,
        "deployment_contract_sha_revalidated": True,
        "final_evidence_valid": True,
        "final_schema_matches_planned_target": True,
        "foreign_key_check": 0,
        "integrity_check": "ok",
        "network_none": True,
        "plan_creator_gid_recorded": True,
        "plan_creator_uid": 0,
        "production_db_used": False,
        "production_secrets_used": False,
        "public_entrypoint_used": True,
        "quiescence_before_execution_evidence": True,
        "root_check_monkeypatched": False,
        "root_context_real": True,
        "status": "PASS",
        "synthetic_db_only": True,
    }
