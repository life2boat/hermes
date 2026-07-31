"""Fail-closed policy tests for the Docker integration-test fixture."""
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from tests.docker import conftest as docker_conftest


def _call_built_image_fixture() -> str:
    return docker_conftest.built_image.__wrapped__()


def _unset_fixture_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_TEST_IMAGE", raising=False)
    monkeypatch.delenv("HERMES_TEST_LOCAL_BUILD_ALLOWED", raising=False)


def test_missing_image_and_authorization_skip_before_docker_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unset_fixture_environment(monkeypatch)
    buildkit_probe = Mock()
    docker_run = Mock()
    monkeypatch.setattr(docker_conftest, "_docker_buildkit_probe", buildkit_probe)
    monkeypatch.setattr(docker_conftest.subprocess, "run", docker_run)

    with pytest.raises(pytest.skip.Exception, match="local Docker builds are disabled"):
        _call_built_image_fixture()

    buildkit_probe.assert_not_called()
    docker_run.assert_not_called()


def test_prebuilt_image_is_returned_exactly_without_build_or_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unset_fixture_environment(monkeypatch)
    monkeypatch.setenv("HERMES_TEST_IMAGE", "registry.example/hermes@sha256:abc")
    buildkit_probe = Mock()
    docker_run = Mock()
    monkeypatch.setattr(docker_conftest, "_docker_buildkit_probe", buildkit_probe)
    monkeypatch.setattr(docker_conftest.subprocess, "run", docker_run)

    assert _call_built_image_fixture() == "registry.example/hermes@sha256:abc"
    buildkit_probe.assert_not_called()
    docker_run.assert_not_called()


def test_exact_authorization_allows_one_build_with_exact_git_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unset_fixture_environment(monkeypatch)
    monkeypatch.setenv("HERMES_TEST_LOCAL_BUILD_ALLOWED", "1")
    monkeypatch.setattr(docker_conftest, "_docker_buildkit_probe", lambda: (True, "ready"))
    monkeypatch.setattr(docker_conftest, "_exact_git_sha", lambda _root: "a" * 40)
    docker_run = Mock(return_value=CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(docker_conftest.subprocess, "run", docker_run)

    assert _call_built_image_fixture() == docker_conftest.IMAGE_TAG
    docker_run.assert_called_once()
    command = docker_run.call_args.args[0]
    assert command[:2] == ["docker", "build"]
    assert f"HERMES_GIT_SHA={'a' * 40}" in command


@pytest.mark.parametrize("value", ["true", "yes", "on", "TRUE", "0", " 1"])
def test_invalid_authorization_fails_before_docker_mutation(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    _unset_fixture_environment(monkeypatch)
    monkeypatch.setenv("HERMES_TEST_LOCAL_BUILD_ALLOWED", value)
    buildkit_probe = Mock()
    docker_run = Mock()
    monkeypatch.setattr(docker_conftest, "_docker_buildkit_probe", buildkit_probe)
    monkeypatch.setattr(docker_conftest.subprocess, "run", docker_run)

    with pytest.raises(pytest.fail.Exception, match="must be unset, empty, or exactly '1'"):
        _call_built_image_fixture()

    buildkit_probe.assert_not_called()
    docker_run.assert_not_called()


def test_unavailable_buildkit_skips_after_explicit_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unset_fixture_environment(monkeypatch)
    monkeypatch.setenv("HERMES_TEST_LOCAL_BUILD_ALLOWED", "1")
    buildkit_probe = Mock(return_value=(False, "not available"))
    docker_run = Mock()
    monkeypatch.setattr(docker_conftest, "_docker_buildkit_probe", buildkit_probe)
    monkeypatch.setattr(docker_conftest.subprocess, "run", docker_run)

    with pytest.raises(pytest.skip.Exception, match="BuildKit unavailable"):
        _call_built_image_fixture()

    buildkit_probe.assert_called_once()
    docker_run.assert_not_called()


def test_docker_integration_consumers_use_shared_fixture_without_direct_builds() -> None:
    docker_tests = Path(__file__).parent / "docker"
    consumers = sorted(docker_tests.glob("test_*.py"))
    assert consumers
    for consumer in consumers:
        source = consumer.read_text(encoding="utf-8")
        assert "built_image" in source, f"{consumer} bypasses the shared image fixture"
        assert '["docker", "build"' not in source
        assert '["docker", "buildx", "build"' not in source
