"""Shared fixtures for docker-image integration tests.

Tests in this directory build the image with the current ``Dockerfile``
and exercise it via ``docker run``. They skip when Docker is unavailable
(e.g. on developer laptops without a daemon).

Override the image with ``HERMES_TEST_IMAGE`` env var to point at a pre-built
image (faster local iteration); otherwise the ``built_image`` fixture builds
the repo's Dockerfile once per session.

Docker tests need longer timeouts than the suite default (30s), so every
test under this directory is granted a 180s default via
``pytest.mark.timeout`` applied at collection time.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator

import pytest

IMAGE_TAG = os.environ.get("HERMES_TEST_IMAGE", "hermes-agent-harness:latest")
_DOCKER_BUILD_TIMEOUT_SECONDS = max(300, int(os.environ.get("HERMES_DOCKER_BUILD_TIMEOUT", "1200")))
_DOCKER_SUITE_TIMEOUT_SECONDS = max(_DOCKER_BUILD_TIMEOUT_SECONDS + 120, 180)


def _docker_build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DOCKER_BUILDKIT", "1")
    return env


def _docker_buildkit_probe() -> tuple[bool, str]:
    if shutil.which("docker") is None:
        return False, "docker CLI not found"
    try:
        version = subprocess.run(
            ["docker", "buildx", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"docker buildx probe failed: {exc}"
    if version.returncode != 0:
        detail = (version.stderr or version.stdout).strip()
        return False, detail or "docker buildx unavailable"
    return True, (version.stdout or version.stderr).strip()


def _docker_available() -> bool:
    """Return True iff a docker CLI is on PATH and the daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def pytest_collection_modifyitems(config, items):  # noqa: D401 - pytest hook
    """Apply docker-suite policy: timeout bump + skip on missing docker."""
    docker_ok = _docker_available()
    skip_docker = pytest.mark.skip(
        reason="Docker not available or daemon not running",
    )
    extend_timeout = pytest.mark.timeout(_DOCKER_SUITE_TIMEOUT_SECONDS)
    for item in items:
        if "tests/docker/" not in str(item.fspath).replace(os.sep, "/"):
            continue
        item.add_marker(extend_timeout)
        if not docker_ok:
            item.add_marker(skip_docker)


@pytest.fixture(scope="session")
def built_image() -> str:
    """Build the image once per test session.

    Override with ``HERMES_TEST_IMAGE`` env var to point at a pre-built
    image (faster local iteration).
    """
    if os.environ.get("HERMES_TEST_IMAGE"):
        return IMAGE_TAG
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", ".."),
    )
    buildkit_ok, buildkit_detail = _docker_buildkit_probe()
    if not buildkit_ok:
        pytest.skip(f"Docker BuildKit unavailable for integration tests: {buildkit_detail}")
    try:
        result = subprocess.run(
            ["docker", "build", "--progress=plain", "-t", IMAGE_TAG, repo_root],
            capture_output=True,
            text=True,
            timeout=_DOCKER_BUILD_TIMEOUT_SECONDS,
            env=_docker_build_env(),
        )
    except subprocess.TimeoutExpired as exc:
        stdout_tail = (exc.stdout or "")[-2000:]
        stderr_tail = (exc.stderr or "")[-2000:]
        pytest.fail(
            "docker build timed out after "
            f"{_DOCKER_BUILD_TIMEOUT_SECONDS}s with BuildKit available. "
            f"stdout_tail={stdout_tail!r} stderr_tail={stderr_tail!r}"
        )
    if result.returncode != 0:
        stderr_tail = result.stderr[-2000:]
        buildkit_missing = (
            "requires BuildKit" in stderr_tail
            or "Install the buildx component" in stderr_tail
            or "unknown command: docker buildx" in stderr_tail
        )
        if buildkit_missing:
            pytest.skip("Docker BuildKit unavailable for integration tests")
    assert result.returncode == 0, (
        f"docker build failed:\n{result.stderr[-2000:]}"
    )
    return IMAGE_TAG


@pytest.fixture
def container_name(request) -> Iterator[str]:
    """Generate a unique container name and ensure cleanup on test exit."""
    safe = request.node.name.replace("[", "_").replace("]", "_")
    name = f"hermes-test-{safe}"
    yield name
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True, timeout=10,
    )


# ---------------------------------------------------------------------------
# docker_exec — default to the unprivileged hermes user
# ---------------------------------------------------------------------------
#
# Background: every Hermes runtime path inside the container drops to UID
# 10000 (the ``hermes`` user) via ``s6-setuidgid hermes``. ``docker exec``
# without ``-u`` runs as root, which is **not** representative of how
# production code executes. PR #30136 review caught a real regression
# this way — ``Path('/proc/1/exe').resolve()`` works as root and silently
# fails (PermissionError swallowed) for hermes, so a test that ran as root
# couldn't catch a feature that was inert for the actual runtime user.
#
# Tests in this directory MUST exercise the realistic user context. The
# helpers below run every probe under ``-u hermes`` unless a specific
# test explicitly opts into ``user="root"`` (rare — e.g. inspecting
# /proc/1/exe itself, chowning a volume).
# ---------------------------------------------------------------------------


def docker_exec(
    container: str,
    *args: str,
    user: str = "hermes",
    timeout: int = 30,
    extra_docker_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run a command inside ``container`` as ``user`` (default: hermes).

    Returns the CompletedProcess with text=True, capture_output=True.

    Pass ``user="root"`` only when the test specifically needs root
    capabilities (e.g. reading /proc/1/exe, manipulating ownership).
    Most tests should use the default.
    """
    cmd = ["docker", "exec", "-u", user, *extra_docker_args, container, *args]
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )


def docker_exec_sh(
    container: str,
    command: str,
    *,
    user: str = "hermes",
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    """Run ``sh -c <command>`` inside the container as ``user``."""
    return docker_exec(
        container, "sh", "-c", command, user=user, timeout=timeout,
    )
