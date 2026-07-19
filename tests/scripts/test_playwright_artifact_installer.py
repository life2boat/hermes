from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import install_pinned_playwright_artifact as installer
from scripts.playwright_artifact_contract import VerifiedBrowserContract
from tests.playwright_supply_chain_support import (
    manifest_document,
    verified_contract,
    write_browser_archive,
    write_manifest,
)


def _fixture(
    tmp_path: Path,
    *,
    executable: bool = True,
    include_executable: bool = True,
    extra_entries: list[tuple[str, bytes, int, int]] | None = None,
) -> tuple[
    VerifiedBrowserContract,
    Path,
    Path,
    dict[str, object],
    str,
]:
    verified, _, _ = verified_contract(tmp_path, cache_root=tmp_path / "cache")
    archive = tmp_path / "browser-archive"
    write_browser_archive(
        archive,
        verified,
        executable=executable,
        include_executable=include_executable,
        extra_entries=extra_entries,
    )
    document = manifest_document(verified, archive)
    manifest = tmp_path / "manifest.json"
    manifest_sha = write_manifest(manifest, document)
    return verified, manifest, archive, document, manifest_sha


def _assert_failure(
    code: str,
    *,
    verified: VerifiedBrowserContract,
    manifest: Path,
    archive: Path,
    manifest_sha: str,
) -> None:
    contract = verified.browser
    destination = Path(contract.cache_root) / contract.cache_directory
    with pytest.raises(installer.ArtifactContractError, match=f"^{code}$"):
        installer.install_artifact(
            manifest_path=manifest,
            archive_path=archive,
            expected_manifest_sha256=manifest_sha,
            verified_contract=verified,
        )
    assert not destination.exists()
    assert not destination.is_symlink()


def test_valid_real_layout_archive_is_durably_published(tmp_path: Path) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(tmp_path)

    destination = installer.install_artifact(
        manifest_path=manifest,
        archive_path=archive,
        expected_manifest_sha256=manifest_sha,
        verified_contract=verified,
    )

    contract = verified.browser
    executable = destination.joinpath(
        *contract.expected_executable_relative_path.split("/")
    )
    marker = destination / "INSTALLATION_COMPLETE"
    assert destination == Path(contract.cache_root) / contract.cache_directory
    assert executable.is_file()
    assert stat.S_IMODE(executable.stat().st_mode) & 0o111
    assert marker.read_bytes()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o444
    assert not list(Path(contract.cache_root).glob(f".{contract.cache_directory}.*"))


def test_missing_expected_manifest_sha_is_denied(tmp_path: Path) -> None:
    _, manifest, _, _, _ = _fixture(tmp_path)
    with pytest.raises(
        installer.ArtifactContractError,
        match="^EXPECTED_MANIFEST_SHA256_INVALID$",
    ):
        installer.load_verified_manifest(manifest, "")


def test_manifest_sha_mismatch_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, _, _ = _fixture(tmp_path)
    _assert_failure(
        "MANIFEST_SHA256_MISMATCH",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha="0" * 64,
    )


def test_noncanonical_manifest_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    data = json.dumps(document, indent=2, sort_keys=True).encode("ascii")
    manifest.write_bytes(data)
    _assert_failure(
        "MANIFEST_NOT_CANONICAL",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=hashlib.sha256(data).hexdigest(),
    )


def test_unknown_manifest_field_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    document["unexpected"] = "denied"
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        "MANIFEST_FIELDS_INVALID",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_latest_source_reference_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    document["source_reference"] = "approved-latest-fixture"
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        "SOURCE_REFERENCE_INVALID",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("playwright_package_version", "1.60.0", "PACKAGE_VERSION_MISMATCH"),
        ("playwright_wheel_filename", "other.whl", "WHEEL_FILENAME_MISMATCH"),
        ("playwright_wheel_sha256", "0" * 64, "WHEEL_SHA256_MISMATCH"),
        ("browser_family", "chromium", "BROWSER_FAMILY_MISMATCH"),
        ("browser_revision", "9999", "BROWSER_REVISION_MISMATCH"),
        ("platform", "linux/arm64", "PLATFORM_MISMATCH"),
        ("archive_root", "other-root", "ARCHIVE_ROOT_MISMATCH"),
    ],
)
def test_manifest_identity_mismatch_is_denied(
    tmp_path: Path,
    field: str,
    value: str,
    code: str,
) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    document[field] = value
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        code,
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_archive_size_mismatch_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    document["archive_size"] = archive.stat().st_size + 1
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        "ARCHIVE_SIZE_MISMATCH",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_archive_sha_mismatch_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    document["archive_sha256"] = "0" * 64
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        "ARCHIVE_SHA256_MISMATCH",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("../escape", "ARCHIVE_PATH_TRAVERSAL"),
        ("/absolute", "ARCHIVE_ABSOLUTE_OR_INVALID_PATH"),
        ("C:/absolute", "ARCHIVE_ABSOLUTE_OR_INVALID_PATH"),
        (
            "chrome-headless-shell-linux64/trailing.",
            "ARCHIVE_PATH_NORMALIZATION_AMBIGUOUS",
        ),
    ],
)
def test_unsafe_zip_path_is_denied(
    tmp_path: Path,
    name: str,
    code: str,
) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_entries=[(name, b"unsafe", 0o644, stat.S_IFREG)],
    )
    _assert_failure(
        code,
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_extra_top_level_file_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_entries=[("unexpected.txt", b"x", 0o644, stat.S_IFREG)],
    )
    _assert_failure(
        "ARCHIVE_UNEXPECTED_TOP_LEVEL",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_extra_top_level_directory_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_entries=[("unexpected/", b"", 0o755, stat.S_IFDIR)],
    )
    _assert_failure(
        "ARCHIVE_UNEXPECTED_TOP_LEVEL",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_expected_executable_outside_declared_root_is_denied(
    tmp_path: Path,
) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    document["expected_executable_relative_path"] = "other-root/browser"
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        "EXECUTABLE_PATH_MISMATCH",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_normalization_alias_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_entries=[
            (
                "chrome-headless-shell-linux64/e\u0301.txt",
                b"x",
                0o644,
                stat.S_IFREG,
            )
        ],
    )
    _assert_failure(
        "ARCHIVE_PATH_NORMALIZATION_AMBIGUOUS",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_case_collision_is_denied(tmp_path: Path) -> None:
    root = "chrome-headless-shell-linux64"
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_entries=[
            (f"{root}/Case.txt", b"a", 0o644, stat.S_IFREG),
            (f"{root}/case.txt", b"b", 0o644, stat.S_IFREG),
        ],
    )
    _assert_failure(
        "ARCHIVE_CASE_COLLISION",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_duplicate_zip_path_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, document, _ = _fixture(tmp_path)
    executable = verified.browser.expected_executable_relative_path
    with zipfile.ZipFile(archive, "a") as zip_archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            info = zipfile.ZipInfo(executable)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            zip_archive.writestr(info, b"duplicate")
    document.update(manifest_document(verified, archive))
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        "ARCHIVE_DUPLICATE_PATH",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_symlink_zip_entry_is_denied(tmp_path: Path) -> None:
    root = "chrome-headless-shell-linux64"
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_entries=[
            (f"{root}/link", b"../../escape", 0o777, stat.S_IFLNK)
        ],
    )
    _assert_failure(
        "ARCHIVE_SYMLINK_DENIED",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def _write_tar_with_special_entry(
    path: Path,
    verified: VerifiedBrowserContract,
    kind: bytes,
) -> None:
    contract = verified.browser
    with tarfile.open(path, "w:gz") as archive:
        executable = tarfile.TarInfo(contract.expected_executable_relative_path)
        payload = b"#!/bin/sh\nexit 0\n"
        executable.size = len(payload)
        executable.mode = 0o755
        archive.addfile(executable, io.BytesIO(payload))
        special = tarfile.TarInfo(f"{contract.archive_root}/special")
        special.type = kind
        special.mode = 0o600
        if kind == tarfile.LNKTYPE:
            special.linkname = "../../escape"
        archive.addfile(special)


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        (tarfile.LNKTYPE, "ARCHIVE_HARDLINK_DENIED"),
        (tarfile.FIFOTYPE, "ARCHIVE_FIFO_DENIED"),
        (tarfile.CHRTYPE, "ARCHIVE_DEVICE_DENIED"),
    ],
)
def test_special_tar_entry_is_denied(
    tmp_path: Path,
    kind: bytes,
    code: str,
) -> None:
    verified, _, _ = verified_contract(tmp_path, cache_root=tmp_path / "cache")
    archive = tmp_path / "browser-archive"
    _write_tar_with_special_entry(archive, verified, kind)
    document = manifest_document(verified, archive, archive_format="tar.gz")
    manifest = tmp_path / "manifest.json"
    manifest_sha = write_manifest(manifest, document)
    _assert_failure(
        code,
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_missing_executable_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        include_executable=False,
    )
    _assert_failure(
        "EXPECTED_EXECUTABLE_MISSING",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_non_executable_browser_file_is_denied(tmp_path: Path) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        executable=False,
    )
    _assert_failure(
        "EXPECTED_EXECUTABLE_INVALID",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_preexisting_partial_cache_is_preserved_and_denied(tmp_path: Path) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(tmp_path)
    contract = verified.browser
    destination = Path(contract.cache_root) / contract.cache_directory
    destination.mkdir(parents=True)
    sentinel = destination / "partial"
    sentinel.write_bytes(b"unchanged")

    with pytest.raises(
        installer.ArtifactContractError,
        match="^TARGET_CACHE_ALREADY_EXISTS$",
    ):
        installer.install_artifact(
            manifest_path=manifest,
            archive_path=archive,
            expected_manifest_sha256=manifest_sha,
            verified_contract=verified,
        )

    assert sentinel.read_bytes() == b"unchanged"


def test_file_fsync_failure_leaves_no_published_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(tmp_path)

    def fail_file_fsync(_path: Path) -> None:
        raise installer.ArtifactContractError("FILE_FSYNC_FAILED")

    monkeypatch.setattr(installer, "_fsync_regular_file", fail_file_fsync)
    _assert_failure(
        "FILE_FSYNC_FAILED",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_directory_fsync_failure_leaves_no_published_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(tmp_path)
    Path(verified.browser.cache_root).mkdir()

    def fail_directory_fsync(
        _path: Path,
        code: str = "DIRECTORY_FSYNC_FAILED",
    ) -> None:
        raise installer.ArtifactContractError(code)

    monkeypatch.setattr(installer, "_fsync_directory", fail_directory_fsync)
    _assert_failure(
        "DIRECTORY_FSYNC_FAILED",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_atomic_rename_failure_leaves_no_published_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(tmp_path)

    def fail_publish(_source: object, _target: object) -> None:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(installer.os, "replace", fail_publish)
    _assert_failure(
        "ATOMIC_PUBLISH_FAILED",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_final_parent_fsync_failure_invalidates_published_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(tmp_path)
    original = installer._fsync_directory

    def fail_final_parent(path: Path, code: str = "DIRECTORY_FSYNC_FAILED") -> None:
        if code == "FINAL_PARENT_FSYNC_FAILED":
            raise installer.ArtifactContractError(code)
        original(path, code)

    monkeypatch.setattr(installer, "_fsync_directory", fail_final_parent)
    _assert_failure(
        "FINAL_PARENT_FSYNC_FAILED",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_post_publish_identity_mismatch_invalidates_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(tmp_path)

    def fail_revalidation(*_args: object, **_kwargs: object) -> None:
        raise installer.ArtifactContractError("PUBLISHED_CACHE_IDENTITY_MISMATCH")

    monkeypatch.setattr(
        installer,
        "_revalidate_published_cache",
        fail_revalidation,
    )
    _assert_failure(
        "PUBLISHED_CACHE_IDENTITY_MISMATCH",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_cleanup_failure_does_not_mask_primary_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        include_executable=False,
    )

    def fail_cleanup(_path: object) -> None:
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(installer.shutil, "rmtree", fail_cleanup)
    _assert_failure(
        "EXPECTED_EXECUTABLE_MISSING",
        verified=verified,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_cli_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    verified, _, archive, _, _ = _fixture(tmp_path)
    canary = "PRIVATE-MANIFEST-CANARY"
    manifest = tmp_path / canary
    manifest.write_bytes(b"not-json")
    expected_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    lockfile = tmp_path / "uv.lock"
    wheel = tmp_path / "playwright-wheel"
    monkeypatch.setattr(
        installer,
        "load_verified_wheel_contract",
        lambda **_kwargs: verified,
    )

    result = installer.main(
        [
            "--manifest",
            str(manifest),
            "--archive",
            str(archive),
            "--lockfile",
            str(lockfile),
            "--wheel",
            str(wheel),
            "--expected-manifest-sha256",
            expected_sha,
            "--platform",
            "linux/amd64",
        ]
    )

    output = capsys.readouterr()
    assert result == 2
    assert "ERROR_CLASS=MANIFEST_JSON_INVALID" in output.err
    assert canary not in output.err
    assert "not-json" not in output.err


def test_installer_has_no_network_client_or_public_skip_path() -> None:
    source = Path(installer.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "requests",
        "httpx",
        "socket",
        "playwright.dev",
        "--skip",
        "--force",
    ):
        assert forbidden not in source
