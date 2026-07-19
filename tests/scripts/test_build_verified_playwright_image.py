from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from scripts import build_verified_playwright_image as build_helper
from scripts.install_pinned_playwright_artifact import canonical_json


def _run(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        list(arguments),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _run("git", "init", "--quiet", cwd=root)
    _run("git", "config", "user.name", "Synthetic Test", cwd=root)
    _run("git", "config", "user.email", "synthetic@example.invalid", cwd=root)
    (root / "tracked.txt").write_text("exact source\n", encoding="utf-8")
    _run("git", "add", "tracked.txt", cwd=root)
    _run("git", "commit", "--quiet", "-m", "synthetic exact source", cwd=root)
    return root, _run("git", "rev-parse", "HEAD", cwd=root)


def _artifact_context(tmp_path: Path) -> tuple[Path, str]:
    context = tmp_path / "approved-artifact"
    context.mkdir(mode=0o700)
    archive = context / "browser-archive"
    archive.write_bytes(b"synthetic browser archive")
    archive.chmod(0o600)
    document: dict[str, object] = {
        "archive_filename": "browser-archive",
        "archive_format": "zip",
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_size": archive.stat().st_size,
        "browser_family": "chromium-headless-shell",
        "browser_revision": "9876",
        "cache_root": "/opt/hermes/.playwright",
        "expected_executable_relative_path": (
            "chrome-headless-shell-linux64/chrome-headless-shell"
        ),
        "manifest_version": 1,
        "platform": "linux/amd64",
        "playwright_package": "playwright",
        "playwright_package_version": "1.61.0",
        "source_kind": "operator-approved-offline-artifact",
        "source_reference": "approved-synthetic-fixture",
    }
    manifest = context / "manifest.json"
    manifest.write_bytes(canonical_json(document))
    manifest.chmod(0o600)
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    return context, digest


def _validate(tmp_path: Path) -> tuple[build_helper.BuildInputs, str]:
    repository, source_sha = _repository(tmp_path)
    context, manifest_sha = _artifact_context(tmp_path)
    inputs = build_helper.validate_build_inputs(
        repository_root=repository,
        expected_source_sha=source_sha,
        artifact_context=context,
        expected_manifest_sha256=manifest_sha,
        image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
        platform="linux/amd64",
    )
    return inputs, manifest_sha


def test_check_contract_accepts_exact_clean_source_and_approved_context(
    tmp_path: Path,
) -> None:
    inputs, manifest_sha = _validate(tmp_path)

    command = build_helper.docker_build_command(inputs)

    assert command[:2] == ["docker", "build"]
    assert command[command.index("--build-context") + 1] == (
        f"playwright_artifact={inputs.artifact_context}"
    )
    assert (
        f"PLAYWRIGHT_ARTIFACT_MANIFEST_SHA256={manifest_sha}" in command
    )
    assert f"HERMES_GIT_SHA={inputs.source_sha}" in command
    assert (
        f"org.opencontainers.image.revision={inputs.source_sha}" in command
    )
    assert command[-1] == str(inputs.repository_root)


def test_dirty_source_is_denied_before_build(tmp_path: Path) -> None:
    repository, source_sha = _repository(tmp_path)
    context, manifest_sha = _artifact_context(tmp_path)
    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(build_helper.BuildContractError, match="^SOURCE_WORKTREE_DIRTY$"):
        build_helper.validate_build_inputs(
            repository_root=repository,
            expected_source_sha=source_sha,
            artifact_context=context,
            expected_manifest_sha256=manifest_sha,
            image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
            platform="linux/amd64",
        )


def test_artifact_context_inside_repository_is_denied(tmp_path: Path) -> None:
    repository, source_sha = _repository(tmp_path)
    context, manifest_sha = _artifact_context(repository)
    (repository / ".git" / "info" / "exclude").write_text(
        "/approved-artifact/\n", encoding="utf-8"
    )

    with pytest.raises(
        build_helper.BuildContractError,
        match="^ARTIFACT_CONTEXT_INSIDE_REPOSITORY$",
    ):
        build_helper.validate_build_inputs(
            repository_root=repository,
            expected_source_sha=source_sha,
            artifact_context=context,
            expected_manifest_sha256=manifest_sha,
            image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
            platform="linux/amd64",
        )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("missing_archive", "ARTIFACT_CONTEXT_CONTENTS_INVALID"),
        ("wrong_manifest_sha", "MANIFEST_SHA256_MISMATCH"),
        ("archive_tamper", "ARTIFACT_ARCHIVE_SIZE_MISMATCH"),
    ],
)
def test_invalid_artifact_context_is_denied(
    tmp_path: Path, mutation: str, code: str
) -> None:
    repository, source_sha = _repository(tmp_path)
    context, manifest_sha = _artifact_context(tmp_path)
    if mutation == "missing_archive":
        (context / "browser-archive").unlink()
    elif mutation == "wrong_manifest_sha":
        manifest_sha = "0" * 64
    else:
        with (context / "browser-archive").open("ab") as handle:
            handle.write(b"tamper")

    with pytest.raises(build_helper.BuildContractError, match=f"^{code}$"):
        build_helper.validate_build_inputs(
            repository_root=repository,
            expected_source_sha=source_sha,
            artifact_context=context,
            expected_manifest_sha256=manifest_sha,
            image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
            platform="linux/amd64",
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX artifact permission contract")
def test_group_or_world_writable_artifact_context_is_denied(tmp_path: Path) -> None:
    repository, source_sha = _repository(tmp_path)
    context, manifest_sha = _artifact_context(tmp_path)
    context.chmod(0o770)

    with pytest.raises(
        build_helper.BuildContractError,
        match="^ARTIFACT_CONTEXT_METADATA_INVALID$",
    ):
        build_helper.validate_build_inputs(
            repository_root=repository,
            expected_source_sha=source_sha,
            artifact_context=context,
            expected_manifest_sha256=manifest_sha,
            image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
            platform="linux/amd64",
        )


def test_mutable_or_unrelated_image_tag_is_denied(tmp_path: Path) -> None:
    repository, source_sha = _repository(tmp_path)
    context, manifest_sha = _artifact_context(tmp_path)

    for image_tag in ("healbite-hermes:latest", "healbite-hermes:p3a-other"):
        with pytest.raises(
            build_helper.BuildContractError,
            match="^IMAGE_TAG_NOT_IMMUTABLE$",
        ):
            build_helper.validate_build_inputs(
                repository_root=repository,
                expected_source_sha=source_sha,
                artifact_context=context,
                expected_manifest_sha256=manifest_sha,
                image_tag=image_tag,
                platform="linux/amd64",
            )


def test_check_mode_never_invokes_docker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs, manifest_sha = _validate(tmp_path)
    monkeypatch.setattr(
        build_helper,
        "validate_build_inputs",
        lambda **_kwargs: inputs,
    )

    def deny_subprocess(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("check mode must not invoke Docker")

    monkeypatch.setattr(build_helper.subprocess, "run", deny_subprocess)

    result = build_helper.main(
        [
            "check",
            "--expected-source-sha",
            inputs.source_sha,
            "--artifact-context",
            str(inputs.artifact_context),
            "--expected-manifest-sha256",
            manifest_sha,
            "--image-tag",
            inputs.image_tag,
            "--platform",
            inputs.platform,
        ]
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "PLAYWRIGHT_IMAGE_BUILD_CONTRACT=PASS" in output
    assert "IMAGE_BUILD_PERFORMED=false" in output


def test_build_helper_has_no_network_or_skip_verification_path() -> None:
    source = Path(build_helper.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "requests",
        "urllib",
        "httpx",
        "playwright.dev",
        "--skip",
        "--force",
        "latest-playwright",
    ):
        assert forbidden not in source
