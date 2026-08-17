"""Child-only Compose interpolation environment contracts."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from ops.secret_remediation_r1 import compose_command
from ops.secret_remediation_r1.compose_command import (
    ComposeCommandError,
    build_clean_env,
    build_recreate_argv,
    run_recreate,
)
from ops.secret_remediation_r1.constants import (
    COMPOSE_FILES,
    COMPOSE_PROJECT,
    LEGACY_IMAGE_REF,
    PROTECTED_NAMES,
)


VALID_SHA = "1" * 40


def _git_result(sha: str = VALID_SHA, returncode: int = 0):
    return SimpleNamespace(returncode=returncode, stdout=f"{sha}\n", stderr="")


def test_child_env_scrubs_protected_and_binds_exact_values(monkeypatch):
    for name in PROTECTED_NAMES:
        monkeypatch.setenv(name, "synthetic-ambient")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _git_result())

    child = build_clean_env("/canonical/worktree")

    assert not (set(child) & PROTECTED_NAMES)
    assert child["HERMES_IMAGE"] == LEGACY_IMAGE_REF
    assert child["HERMES_GIT_SHA"] == VALID_SHA


def test_child_env_does_not_mutate_global_environment(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-ambient")
    monkeypatch.setenv("HERMES_IMAGE", "ambient:image")
    monkeypatch.setenv("HERMES_GIT_SHA", "2" * 40)
    before = os.environ.copy()
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _git_result())

    build_clean_env("/canonical/worktree")

    assert os.environ == before


def test_git_sha_is_resolved_from_requested_worktree(monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _git_result()

    monkeypatch.setattr(subprocess, "run", fake_run)
    child = build_clean_env("/canonical/worktree")
    assert child["HERMES_GIT_SHA"] == VALID_SHA
    assert captured["argv"] == [
        "git",
        "-C",
        "/canonical/worktree",
        "rev-parse",
        "HEAD",
    ]
    assert captured["kwargs"]["timeout"] == 10


@pytest.mark.parametrize("sha", ["", "0" * 39, "G" * 40, "A" * 40])
def test_invalid_git_sha_rejected(monkeypatch, sha):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: _git_result(sha))
    with pytest.raises(ComposeCommandError, match="invalid data"):
        build_clean_env("/canonical/worktree")


def test_git_lookup_failure_rejected(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: _git_result("", returncode=1)
    )
    with pytest.raises(ComposeCommandError, match="invalid data"):
        build_clean_env("/canonical/worktree")


def test_git_lookup_missing_binary_rejected(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ComposeCommandError, match="resolution failed"):
        build_clean_env("/canonical/worktree")


def test_recreate_passes_bindings_only_to_child(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "git":
            return _git_result()
        return SimpleNamespace(returncode=0)

    monkeypatch.setenv("HERMES_IMAGE", "ambient:image")
    monkeypatch.setenv("HERMES_GIT_SHA", "2" * 40)
    before = os.environ.copy()
    monkeypatch.setattr(subprocess, "run", fake_run)

    run_recreate("/canonical/worktree")

    assert calls[1][0] == build_recreate_argv()
    assert calls[1][1]["env"]["HERMES_IMAGE"] == LEGACY_IMAGE_REF
    assert calls[1][1]["env"]["HERMES_GIT_SHA"] == VALID_SHA
    assert os.environ == before


def test_recreate_argv_retains_exact_stack_project_and_safety_flags():
    argv = build_recreate_argv()
    expected = ["docker", "compose"]
    for compose_file in COMPOSE_FILES:
        expected.extend(["-f", compose_file])
    expected.extend(["-p", COMPOSE_PROJECT])
    expected.extend([
        "up",
        "-d",
        "--no-deps",
        "--force-recreate",
        "--no-build",
        "--pull",
        "never",
        "hermes-bot",
    ])
    assert argv == expected
