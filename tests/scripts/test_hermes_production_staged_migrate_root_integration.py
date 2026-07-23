from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Iterator

import pytest

from scripts import hermes_execution_authority as authority


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


@contextlib.contextmanager
def _trusted_operation_parent() -> Iterator[Path]:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("root integration capability skip: POSIX root required")
    operation_parent = Path(
        f"/run/hermes-integration-tests-{uuid.uuid4().hex}"
    )
    operation_parent.mkdir(mode=0o700)
    try:
        metadata = operation_parent.lstat()
        assert stat.S_ISDIR(metadata.st_mode)
        assert metadata.st_uid == 0
        assert metadata.st_gid == 0
        assert stat.S_IMODE(metadata.st_mode) == 0o700
        authority.validate_trusted_parent_chain(
            operation_parent,
            expected_uid=0,
        )
        yield operation_parent
    finally:
        shutil.rmtree(operation_parent)


def test_world_writable_tmp_parent_remains_untrusted() -> None:
    tmp = Path("/tmp")
    if (
        os.name != "posix"
        or not tmp.is_dir()
        or stat.S_IMODE(tmp.stat().st_mode) & 0o022 == 0
    ):
        pytest.skip("world-writable /tmp contract is unavailable")

    with pytest.raises(authority.ExecutionAuthorityError) as exc_info:
        authority.validate_trusted_parent_chain(
            tmp,
            expected_uid=os.geteuid(),
        )

    assert exc_info.value.code == "AUTHORITY_PARENT_CHAIN_UNTRUSTED"


@pytest.mark.timeout(240)
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
    with _trusted_operation_parent() as operation_parent:
        runtime_root = operation_parent / "runtime"
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
            f"{operation_parent}:{operation_parent}:rw",
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

    assert not operation_parent.exists()
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
        "runtime_inspection_redirected": True,
        "status": "PASS",
        "synthetic_db_only": True,
        "synthetic_runtime_only": True,
        "production_runtime_inspected": False,
    }
