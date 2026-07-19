from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import build_verified_playwright_image as build_helper
from tests.playwright_supply_chain_support import (
    manifest_document,
    write_browser_archive,
    write_lockfile,
    write_manifest,
    write_wheel,
)
from scripts.playwright_artifact_contract import load_verified_wheel_contract


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


def _repository_and_context(
    tmp_path: Path,
) -> tuple[Path, str, Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _run("git", "init", "--quiet", cwd=root)
    _run("git", "config", "user.name", "Synthetic Test", cwd=root)
    _run("git", "config", "user.email", "synthetic@example.invalid", cwd=root)
    (root / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n*.diff\n.pytest-cache/\n*.egg-info/\n",
        encoding="utf-8",
    )
    (root / "tracked.txt").write_text("exact source\n", encoding="utf-8")
    (root / "Dockerfile").write_text(
        "FROM scratch\nCOPY tracked.txt /tracked.txt\n",
        encoding="utf-8",
    )

    context = tmp_path / "approved-artifact"
    context.mkdir(mode=0o700)
    wheel = context / "playwright-wheel"
    wheel_bytes = write_wheel(wheel)
    wheel.chmod(0o600)
    lockfile = root / "uv.lock"
    write_lockfile(lockfile, wheel_bytes)
    verified = load_verified_wheel_contract(
        lockfile_path=lockfile,
        wheel_path=wheel,
        platform="linux/amd64",
    )
    archive = context / "browser-archive"
    write_browser_archive(archive, verified)
    archive.chmod(0o600)
    document = manifest_document(verified, archive)
    manifest = context / "manifest.json"
    manifest_sha = write_manifest(manifest, document)
    manifest.chmod(0o600)

    _run("git", "add", ".gitignore", "tracked.txt", "Dockerfile", "uv.lock", cwd=root)
    _run("git", "commit", "--quiet", "-m", "synthetic exact source", cwd=root)
    source_sha = _run("git", "rev-parse", "HEAD", cwd=root)
    return root, source_sha, context, manifest_sha


@contextlib.contextmanager
def _prepared(tmp_path: Path):
    repository, source_sha, context, manifest_sha = _repository_and_context(
        tmp_path
    )
    with build_helper.prepared_build_inputs(
        repository_root=repository,
        expected_source_sha=source_sha,
        artifact_context=context,
        expected_manifest_sha256=manifest_sha,
        image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
        platform="linux/amd64",
    ) as inputs:
        yield inputs


def test_build_command_uses_exact_git_export_not_raw_worktree(
    tmp_path: Path,
) -> None:
    with _prepared(tmp_path) as inputs:
        command = build_helper.docker_build_command(inputs)

        assert command[:2] == ["docker", "build"]
        assert command[command.index("--build-context") + 1] == (
            f"playwright_artifact={inputs.artifact_context}"
        )
        assert f"HERMES_GIT_SHA={inputs.source_sha}" in command
        assert (
            f"org.opencontainers.image.revision={inputs.source_sha}" in command
        )
        assert command[-1] == str(inputs.build_context)
        assert inputs.build_context != inputs.repository_root
        assert inputs.source_tree_sha
        assert inputs.context_manifest_sha256


@pytest.mark.parametrize(
    "relative_path",
    [
        "ignored/__pycache__/module.pyc",
        "local-review.diff",
        ".pytest-cache/state",
        "local-package.egg-info/PKG-INFO",
    ],
)
def test_ignored_local_artifact_is_absent_from_exported_context(
    tmp_path: Path,
    relative_path: str,
) -> None:
    repository, source_sha, context, manifest_sha = _repository_and_context(
        tmp_path
    )
    local_path = repository.joinpath(*relative_path.split("/"))
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text("local only\n", encoding="utf-8")

    with build_helper.prepared_build_inputs(
        repository_root=repository,
        expected_source_sha=source_sha,
        artifact_context=context,
        expected_manifest_sha256=manifest_sha,
        image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
        platform="linux/amd64",
    ) as inputs:
        assert not inputs.build_context.joinpath(*relative_path.split("/")).exists()


def test_untracked_source_like_file_is_absent_from_exported_context(
    tmp_path: Path,
) -> None:
    repository, source_sha, context, manifest_sha = _repository_and_context(
        tmp_path
    )
    (repository / "untracked.py").write_text(
        "raise RuntimeError('must not enter context')\n",
        encoding="utf-8",
    )

    with build_helper.prepared_build_inputs(
        repository_root=repository,
        expected_source_sha=source_sha,
        artifact_context=context,
        expected_manifest_sha256=manifest_sha,
        image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
        platform="linux/amd64",
    ) as inputs:
        assert not (inputs.build_context / "untracked.py").exists()


def test_exact_git_tree_and_exported_manifest_match(tmp_path: Path) -> None:
    with _prepared(tmp_path) as inputs:
        document = json.loads(inputs.context_manifest.read_text(encoding="ascii"))
        paths = {record["path"] for record in document["files"]}

        assert document["source_sha"] == inputs.source_sha
        assert document["tree_sha"] == inputs.source_tree_sha
        assert paths == {".gitignore", "Dockerfile", "tracked.txt", "uv.lock"}
        assert build_helper.inspect_exported_context(
            context_root=inputs.build_context,
            manifest_path=inputs.context_manifest,
            expected_source_sha=inputs.source_sha,
            expected_tree_sha=inputs.source_tree_sha,
        ) == 4


def test_dirty_tracked_file_is_denied_before_context_export(
    tmp_path: Path,
) -> None:
    repository, source_sha, context, manifest_sha = _repository_and_context(
        tmp_path
    )
    (repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(
        build_helper.BuildContractError,
        match="^SOURCE_TRACKED_WORKTREE_DIRTY$",
    ):
        build_helper.validate_build_inputs(
            repository_root=repository,
            expected_source_sha=source_sha,
            artifact_context=context,
            expected_manifest_sha256=manifest_sha,
            image_tag=f"healbite-hermes:p3a-{source_sha[:12]}",
            platform="linux/amd64",
        )


def test_submodule_tree_entry_fails_closed(tmp_path: Path) -> None:
    repository, _, context, manifest_sha = _repository_and_context(tmp_path)
    commit_sha = _run("git", "rev-parse", "HEAD", cwd=repository)
    _run(
        "git",
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit_sha},vendor/submodule",
        cwd=repository,
    )
    _run("git", "commit", "--quiet", "-m", "synthetic submodule", cwd=repository)
    source_sha = _run("git", "rev-parse", "HEAD", cwd=repository)
    tree_sha = _run("git", "rev-parse", f"{source_sha}^{{tree}}", cwd=repository)
    operation_root = tmp_path / "operation"
    operation_root.mkdir()

    with pytest.raises(
        build_helper.BuildContractError,
        match="^GIT_SUBMODULE_UNSUPPORTED$",
    ):
        build_helper.export_exact_git_context(
            repository_root=repository,
            source_sha=source_sha,
            source_tree_sha=tree_sha,
            operation_root=operation_root,
        )
    assert context.is_dir()
    assert manifest_sha


def test_git_lfs_pointer_fails_closed(tmp_path: Path) -> None:
    repository, _, _, _ = _repository_and_context(tmp_path)
    (repository / "large.bin").write_bytes(
        b"version https://git-lfs.github.com/spec/v1\n"
        b"oid sha256:" + b"0" * 64 + b"\nsize 1\n"
    )
    _run("git", "add", "large.bin", cwd=repository)
    _run("git", "commit", "--quiet", "-m", "synthetic lfs", cwd=repository)
    source_sha = _run("git", "rev-parse", "HEAD", cwd=repository)
    tree_sha = _run("git", "rev-parse", f"{source_sha}^{{tree}}", cwd=repository)
    operation_root = tmp_path / "operation"
    operation_root.mkdir()

    with pytest.raises(
        build_helper.BuildContractError,
        match="^GIT_LFS_POINTER_UNSUPPORTED$",
    ):
        build_helper.export_exact_git_context(
            repository_root=repository,
            source_sha=source_sha,
            source_tree_sha=tree_sha,
            operation_root=operation_root,
        )


def test_artifact_context_inside_repository_is_denied(tmp_path: Path) -> None:
    repository, source_sha, external_context, _ = _repository_and_context(tmp_path)
    context = repository / "approved-artifact"
    context.mkdir()
    for source in external_context.iterdir():
        (context / source.name).write_bytes(source.read_bytes())
        (context / source.name).chmod(0o600)
    (repository / ".git" / "info" / "exclude").write_text(
        "/approved-artifact/\n",
        encoding="utf-8",
    )
    manifest_sha = hashlib.sha256((context / "manifest.json").read_bytes()).hexdigest()

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
        ("missing_wheel", "ARTIFACT_CONTEXT_CONTENTS_INVALID"),
        ("wrong_manifest_sha", "MANIFEST_SHA256_MISMATCH"),
        ("archive_tamper", "ARTIFACT_ARCHIVE_SIZE_MISMATCH"),
    ],
)
def test_invalid_artifact_context_is_denied(
    tmp_path: Path,
    mutation: str,
    code: str,
) -> None:
    repository, source_sha, context, manifest_sha = _repository_and_context(
        tmp_path
    )
    if mutation == "missing_archive":
        (context / "browser-archive").unlink()
    elif mutation == "missing_wheel":
        (context / "playwright-wheel").unlink()
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
    repository, source_sha, context, manifest_sha = _repository_and_context(
        tmp_path
    )
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
    repository, source_sha, context, manifest_sha = _repository_and_context(
        tmp_path
    )

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
    with _prepared(tmp_path) as inputs:

        @contextlib.contextmanager
        def prepared_fixture(**_kwargs: object):
            yield inputs

        monkeypatch.setattr(
            build_helper,
            "prepared_build_inputs",
            prepared_fixture,
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
                inputs.manifest_sha256,
                "--image-tag",
                inputs.image_tag,
                "--platform",
                inputs.platform,
            ]
        )

    assert result == 0
    output = capsys.readouterr().out
    assert "PLAYWRIGHT_IMAGE_BUILD_CONTRACT=PASS" in output
    assert "BUILD_CONTEXT_SOURCE=EXACT_GIT_TREE_EXPORT" in output
    assert "IMAGE_BUILD_PERFORMED=false" in output


def test_build_helper_has_no_network_or_skip_verification_path() -> None:
    source = Path(build_helper.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "requests",
        "httpx",
        "playwright.dev",
        "--skip",
        "--force",
        "latest-playwright",
    ):
        assert forbidden not in source
