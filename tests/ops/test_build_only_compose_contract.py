from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "ops" / "build" / "docker-compose.build.yml"
WRAPPER = ROOT / "ops" / "build" / "build_hermes_image.sh"
IMAGE_TAG = "healbite-hermes:contract-test-aaaaaaaaaaaa"
REVISION = "a" * 40
OTHER_REVISION = "b" * 40

RUNTIME_FIELDS = {
    "command",
    "configs",
    "container_name",
    "depends_on",
    "entrypoint",
    "env_file",
    "environment",
    "healthcheck",
    "networks",
    "ports",
    "restart",
    "secrets",
    "volumes",
}


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_tool_environment(
    tmp_path: Path,
    *,
    git_head: str = REVISION,
    git_dirty: bool = False,
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_path = tmp_path / "docker-capture.txt"

    _write_executable(
        bin_dir / "docker",
        """#!/usr/bin/env bash
set -euo pipefail
{
  printf 'COMPOSE_DISABLE_ENV_FILE=%s\n' "${COMPOSE_DISABLE_ENV_FILE:-}"
  printf 'HERMES_IMAGE=%s\n' "${HERMES_IMAGE:-}"
  printf 'HERMES_GIT_SHA=%s\n' "${HERMES_GIT_SHA:-}"
  printf 'ARG=%s\n' "$@"
} > "$CAPTURE_PATH"
""",
    )
    _write_executable(
        bin_dir / "git",
        """#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *"rev-parse --verify HEAD")
    printf '%s\n' "$FAKE_GIT_HEAD"
    ;;
  *"status --porcelain=v1")
    if [ "${FAKE_GIT_DIRTY:-0}" = 1 ]; then
      printf '%s\n' ' M synthetic'
    fi
    ;;
  *)
    exit 97
    ;;
esac
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["CAPTURE_PATH"] = str(capture_path)
    env["FAKE_GIT_HEAD"] = git_head
    env["FAKE_GIT_DIRTY"] = "1" if git_dirty else "0"
    return env, capture_path


def _run_wrapper(
    tmp_path: Path,
    *arguments: str,
    git_head: str = REVISION,
    git_dirty: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    env, capture_path = _fake_tool_environment(
        tmp_path,
        git_head=git_head,
        git_dirty=git_dirty,
    )
    result = subprocess.run(
        [str(WRAPPER), *arguments],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
        check=False,
    )
    return result, capture_path


def test_build_only_compose_contains_only_build_fields() -> None:
    source = COMPOSE_FILE.read_text(encoding="utf-8")
    contract = yaml.safe_load(source)

    assert set(contract) == {"services"}
    assert set(contract["services"]) == {"hermes-bot"}

    service = contract["services"]["hermes-bot"]
    assert set(service) == {"build", "image"}
    assert RUNTIME_FIELDS.isdisjoint(service)

    build = service["build"]
    assert set(build) == {"context", "dockerfile", "args"}
    assert build["context"] == "."
    assert build["dockerfile"] == "Dockerfile"
    assert set(build["args"]) == {"HERMES_GIT_SHA"}
    assert "HERMES_GIT_SHA:?" in build["args"]["HERMES_GIT_SHA"]
    assert "HERMES_IMAGE:?" in service["image"]

    lowered = source.lower()
    assert ".env" not in lowered
    assert "env_file" not in lowered
    assert "hermes-secrets-override" not in lowered
    assert "/tmp/" not in lowered


def test_wrapper_static_contract_is_single_build_and_dotenv_disabled() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    assert source.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")
    assert "COMPOSE_DISABLE_ENV_FILE=1" in source
    assert "--env-file /dev/null" in normalized
    assert "docker compose" in normalized
    assert len(re.findall(r"\bdocker\s+compose\b", normalized)) == 1
    assert len(re.findall(r"\bbuild\s+\\?\s*hermes-bot\b", source)) == 1
    assert "eval " not in source
    assert ".env" not in source
    assert "hermes-secrets-override" not in source

    for forbidden in (
        "docker pull",
        "docker push",
        "docker run",
        "docker tag",
        "docker compose up",
        "docker compose down",
        "docker image rm",
        "docker system prune",
    ):
        assert forbidden not in normalized


def test_wrapper_passes_exact_non_secret_build_contract(tmp_path: Path) -> None:
    result, capture_path = _run_wrapper(tmp_path, IMAGE_TAG, REVISION)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert capture_path.read_text(encoding="utf-8").splitlines() == [
        "COMPOSE_DISABLE_ENV_FILE=1",
        f"HERMES_IMAGE={IMAGE_TAG}",
        f"HERMES_GIT_SHA={REVISION}",
        "ARG=compose",
        "ARG=--env-file",
        "ARG=/dev/null",
        "ARG=--project-directory",
        f"ARG={ROOT}",
        "ARG=-f",
        f"ARG={COMPOSE_FILE}",
        "ARG=--project-name",
        "ARG=hermes-build",
        "ARG=build",
        "ARG=hermes-bot",
    ]


@pytest.mark.parametrize(
    "image_tag",
    (
        "",
        "latest",
        "production",
        "healbite-hermes:latest",
        "healbite-hermes:PRODUCTION",
        "HealBite:immutable",
        "healbite-hermes",
        "healbite-hermes:tag;touch",
        "healbite-hermes@sha256:" + "a" * 64,
    ),
)
def test_wrapper_rejects_unsafe_or_mutable_image_tags(
    tmp_path: Path,
    image_tag: str,
) -> None:
    result, capture_path = _run_wrapper(tmp_path, image_tag, REVISION)

    assert result.returncode == 64
    assert not capture_path.exists()


@pytest.mark.parametrize(
    "revision",
    (
        "",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "a" * 39 + ";",
    ),
)
def test_wrapper_rejects_invalid_revisions(tmp_path: Path, revision: str) -> None:
    result, capture_path = _run_wrapper(tmp_path, IMAGE_TAG, revision)

    assert result.returncode == 64
    assert not capture_path.exists()


def test_wrapper_rejects_unknown_arguments(tmp_path: Path) -> None:
    result, capture_path = _run_wrapper(tmp_path, IMAGE_TAG, REVISION, "extra")

    assert result.returncode == 64
    assert not capture_path.exists()


def test_wrapper_rejects_revision_that_differs_from_worktree_head(tmp_path: Path) -> None:
    result, capture_path = _run_wrapper(
        tmp_path,
        IMAGE_TAG,
        REVISION,
        git_head=OTHER_REVISION,
    )

    assert result.returncode == 65
    assert not capture_path.exists()


def test_wrapper_rejects_dirty_worktree(tmp_path: Path) -> None:
    result, capture_path = _run_wrapper(
        tmp_path,
        IMAGE_TAG,
        REVISION,
        git_dirty=True,
    )

    assert result.returncode == 65
    assert not capture_path.exists()
