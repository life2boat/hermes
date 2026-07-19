from __future__ import annotations

import io
from importlib import metadata
from pathlib import Path
import stat
import subprocess
import tarfile

import pytest

from scripts import build_verified_playwright_image as build_helper
from scripts import install_pinned_playwright_artifact as installer
from scripts import playwright_artifact_contract as contract_module
from tests.secret_scanner_support import synthetic_private_key_block
from tests.playwright_supply_chain_support import (
    manifest_document,
    verified_contract,
    write_browser_archive,
    write_lockfile,
    write_manifest,
    write_wheel,
)


def _git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _synthetic_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git("init", "--quiet", cwd=repository)
    _git("config", "user.name", "Synthetic Test", cwd=repository)
    _git(
        "config",
        "user.email",
        "synthetic@example.invalid",
        cwd=repository,
    )
    (repository / "tracked.txt").write_text("exact source\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "synthetic source", cwd=repository)
    source_sha = _git("rev-parse", "HEAD", cwd=repository)
    tree_sha = _git("rev-parse", f"{source_sha}^{{tree}}", cwd=repository)
    return repository, source_sha, tree_sha


def test_real_reporter_ignores_tampered_installed_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wheel = tmp_path / "playwright-wheel"
    wheel_bytes = write_wheel(wheel, revision="9876")
    lockfile = tmp_path / "uv.lock"
    write_lockfile(lockfile, wheel_bytes)

    class TamperedDistribution:
        version = "1.61.0"
        files = [Path("playwright/driver/package/browsers.json")]

        @staticmethod
        def locate_file(_file: object) -> Path:
            return tmp_path / "tampered-metadata.json"

    (tmp_path / "tampered-metadata.json").write_text(
        '{"browsers":[{"name":"chromium-headless-shell","revision":"9999"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: TamperedDistribution(),
    )

    result = contract_module.main([
        "--lockfile",
        str(lockfile),
        "--wheel",
        str(wheel),
        "--platform",
        "linux/amd64",
    ])

    output = capsys.readouterr().out
    assert result == 0
    assert "BROWSER_REVISION=9876" in output
    assert "9999" not in output


def test_pax_path_override_is_denied_at_installer_boundary(
    tmp_path: Path,
) -> None:
    verified, _, _ = verified_contract(tmp_path, cache_root=tmp_path / "cache")
    contract = verified.browser
    archive_path = tmp_path / "browser-archive"
    executable_payload = b"#!/bin/sh\nexit 0\n"
    with tarfile.open(
        archive_path,
        mode="w:gz",
        format=tarfile.PAX_FORMAT,
    ) as archive:
        executable = tarfile.TarInfo(contract.expected_executable_relative_path)
        executable.size = len(executable_payload)
        executable.mode = 0o755
        archive.addfile(executable, io.BytesIO(executable_payload))

        overridden = tarfile.TarInfo(f"{contract.archive_root}/safe.txt")
        overridden.pax_headers = {"path": "../escape"}
        overridden.size = 1
        overridden.mode = 0o644
        archive.addfile(overridden, io.BytesIO(b"x"))

    manifest_path = tmp_path / "manifest.json"
    manifest_sha = write_manifest(
        manifest_path,
        manifest_document(verified, archive_path, archive_format="tar.gz"),
    )

    with pytest.raises(
        installer.ArtifactContractError,
        match="^ARCHIVE_PATH_TRAVERSAL$",
    ):
        installer.install_artifact(
            manifest_path=manifest_path,
            archive_path=archive_path,
            expected_manifest_sha256=manifest_sha,
            verified_contract=verified,
        )
    destination = Path(contract.cache_root) / contract.cache_directory
    assert not destination.exists()


def test_marker_and_final_parent_are_fsynced_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, _, _ = verified_contract(tmp_path, cache_root=tmp_path / "cache")
    archive_path = tmp_path / "browser-archive"
    write_browser_archive(archive_path, verified)
    manifest_path = tmp_path / "manifest.json"
    manifest_sha = write_manifest(
        manifest_path,
        manifest_document(verified, archive_path),
    )
    events: list[tuple[str, str]] = []
    original_file_fsync = installer._fsync_regular_file
    original_directory_fsync = installer._fsync_directory

    def record_file_fsync(path: Path) -> None:
        events.append(("file", path.name))
        original_file_fsync(path)

    def record_directory_fsync(
        path: Path,
        code: str = "DIRECTORY_FSYNC_FAILED",
    ) -> None:
        events.append(("directory", code))
        original_directory_fsync(path, code)

    monkeypatch.setattr(installer, "_fsync_regular_file", record_file_fsync)
    monkeypatch.setattr(installer, "_fsync_directory", record_directory_fsync)

    installer.install_artifact(
        manifest_path=manifest_path,
        archive_path=archive_path,
        expected_manifest_sha256=manifest_sha,
        verified_contract=verified,
    )

    marker_event = ("file", contract_module.INSTALLATION_MARKER)
    parent_event = ("directory", "FINAL_PARENT_FSYNC_FAILED")
    assert marker_event in events
    assert parent_event in events
    assert events.index(marker_event) < events.index(parent_event)


def test_git_archive_mode_is_independent_of_repository_tar_umask(
    tmp_path: Path,
) -> None:
    repository, source_sha, tree_sha = _synthetic_repository(tmp_path)
    _git("config", "tar.umask", "0000", cwd=repository)
    operation_root = tmp_path / "operation"
    operation_root.mkdir()

    context_root, manifest_path, _, count = build_helper.export_exact_git_context(
        repository_root=repository,
        source_sha=source_sha,
        source_tree_sha=tree_sha,
        approved_base_sha=source_sha,
        approved_base_tree_sha=tree_sha,
        operation_root=operation_root,
    )

    assert count == 1
    assert manifest_path.is_file()
    assert stat.S_IMODE((context_root / "tracked.txt").stat().st_mode) == 0o644


def test_private_key_marker_in_git_blob_is_denied(
    tmp_path: Path,
) -> None:
    repository, _, _ = _synthetic_repository(tmp_path)
    (repository / "credential.txt").write_text(
        synthetic_private_key_block(),
        encoding="utf-8",
    )
    _git("add", "credential.txt", cwd=repository)
    _git("commit", "--quiet", "-m", "synthetic forbidden marker", cwd=repository)
    source_sha = _git("rev-parse", "HEAD", cwd=repository)
    tree_sha = _git("rev-parse", f"{source_sha}^{{tree}}", cwd=repository)
    operation_root = tmp_path / "operation"
    operation_root.mkdir()

    with pytest.raises(
        build_helper.BuildContractError,
        match="^GIT_SECRET_CONTENT_DENIED$",
    ):
        build_helper.export_exact_git_context(
            repository_root=repository,
            source_sha=source_sha,
            source_tree_sha=tree_sha,
            approved_base_sha=_git("rev-parse", f"{source_sha}^", cwd=repository),
            approved_base_tree_sha=_git(
                "rev-parse", f"{source_sha}^^{{tree}}", cwd=repository
            ),
            operation_root=operation_root,
        )
