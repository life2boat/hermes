from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import warnings
import zipfile
from pathlib import Path

import pytest

from scripts import install_pinned_playwright_artifact as installer
from scripts.playwright_artifact_contract import (
    BrowserContract,
    contract_from_metadata,
)


EXECUTABLE = "chrome-headless-shell-linux64/chrome-headless-shell"


def _contract(tmp_path: Path) -> BrowserContract:
    return BrowserContract(
        package="playwright",
        package_version="1.61.0",
        browser_family="chromium-headless-shell",
        browser_revision="9876",
        platform="linux/amd64",
        cache_root=str(tmp_path / "cache"),
        cache_directory="chromium_headless_shell-9876",
        expected_executable_relative_path=EXECUTABLE,
    )


def _zip_entry(
    archive: zipfile.ZipFile,
    name: str,
    data: bytes,
    *,
    mode: int = 0o644,
    file_type: int = stat.S_IFREG,
) -> None:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (file_type | mode) << 16
    archive.writestr(info, data)


def _write_zip(
    path: Path,
    *,
    executable: bool = True,
    include_executable: bool = True,
    extra_writer: object | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if include_executable:
            _zip_entry(
                archive,
                EXECUTABLE,
                b"#!/bin/sh\nexit 0\n",
                mode=0o755 if executable else 0o644,
            )
        _zip_entry(archive, "chrome-headless-shell-linux64/resources.pak", b"x")
        if callable(extra_writer):
            extra_writer(archive)


def _document(
    contract: BrowserContract,
    archive: Path,
    *,
    archive_format: str = "zip",
) -> dict[str, object]:
    return {
        "archive_filename": "browser-archive",
        "archive_format": archive_format,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_size": archive.stat().st_size,
        "browser_family": contract.browser_family,
        "browser_revision": contract.browser_revision,
        "cache_root": contract.cache_root,
        "expected_executable_relative_path": (
            contract.expected_executable_relative_path
        ),
        "manifest_version": 1,
        "platform": contract.platform,
        "playwright_package": contract.package,
        "playwright_package_version": contract.package_version,
        "source_kind": "operator-approved-offline-artifact",
        "source_reference": "approved-synthetic-fixture",
    }


def _write_manifest(path: Path, document: dict[str, object]) -> str:
    data = installer.canonical_json(document)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _fixture(
    tmp_path: Path,
    *,
    executable: bool = True,
    include_executable: bool = True,
    extra_writer: object | None = None,
) -> tuple[BrowserContract, Path, Path, dict[str, object], str]:
    contract = _contract(tmp_path)
    archive = tmp_path / "browser-archive"
    _write_zip(
        archive,
        executable=executable,
        include_executable=include_executable,
        extra_writer=extra_writer,
    )
    document = _document(contract, archive)
    manifest = tmp_path / "manifest.json"
    manifest_sha = _write_manifest(manifest, document)
    return contract, manifest, archive, document, manifest_sha


def _assert_failure(
    code: str,
    *,
    contract: BrowserContract,
    manifest: Path,
    archive: Path,
    manifest_sha: str,
) -> None:
    destination = Path(contract.cache_root) / contract.cache_directory
    with pytest.raises(installer.ArtifactContractError, match=f"^{code}$"):
        installer.install_artifact(
            manifest_path=manifest,
            archive_path=archive,
            expected_manifest_sha256=manifest_sha,
            contract=contract,
        )
    assert not destination.exists()
    assert not destination.is_symlink()


def test_contract_derives_revision_and_cache_layout_from_package_metadata() -> None:
    contract = contract_from_metadata(
        package_version="1.61.0",
        browsers_payload={
            "browsers": [
                {
                    "name": "chromium-headless-shell",
                    "revision": "9876",
                    "revisionOverrides": {"debian13-x64": "9877"},
                }
            ]
        },
        platform="linux/amd64",
    )

    assert contract.browser_revision == "9877"
    assert contract.cache_directory == (
        "chromium_headless_shell_debian13_x64_special-9877"
    )
    assert contract.expected_cache_layout.endswith(
        "/chrome-headless-shell-linux64/chrome-headless-shell"
    )


def test_valid_manifest_archive_extracts_and_publishes_atomically(
    tmp_path: Path,
) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(tmp_path)

    destination = installer.install_artifact(
        manifest_path=manifest,
        archive_path=archive,
        expected_manifest_sha256=manifest_sha,
        contract=contract,
    )

    executable = destination.joinpath(*EXECUTABLE.split("/"))
    assert destination == Path(contract.cache_root) / contract.cache_directory
    assert executable.is_file()
    assert stat.S_IMODE(executable.stat().st_mode) & 0o111
    assert (destination / "INSTALLATION_COMPLETE").is_file()
    assert not list(Path(contract.cache_root).glob(".*.????*"))


def test_missing_expected_manifest_sha_is_denied(tmp_path: Path) -> None:
    _, manifest, _, _, _ = _fixture(tmp_path)
    with pytest.raises(
        installer.ArtifactContractError,
        match="^EXPECTED_MANIFEST_SHA256_INVALID$",
    ):
        installer.load_verified_manifest(manifest, "")


def test_manifest_sha_mismatch_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, _, _ = _fixture(tmp_path)
    _assert_failure(
        "MANIFEST_SHA256_MISMATCH",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha="0" * 64,
    )


def test_noncanonical_manifest_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    data = json.dumps(document, indent=2, sort_keys=True).encode("ascii")
    manifest.write_bytes(data)
    _assert_failure(
        "MANIFEST_NOT_CANONICAL",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=hashlib.sha256(data).hexdigest(),
    )


def test_invalid_archive_sha_format_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    document["archive_sha256"] = "A" * 64
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        "ARCHIVE_SHA256_INVALID",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_unknown_manifest_field_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    document["unexpected"] = "denied"
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        "MANIFEST_FIELDS_INVALID",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_latest_source_reference_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    document["source_reference"] = "approved-latest-fixture"
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        "SOURCE_REFERENCE_INVALID",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_group_writable_archive_entry_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_writer=lambda item: _zip_entry(
            item,
            "group-writable-file",
            b"unsafe mode",
            mode=0o660,
        ),
    )
    _assert_failure(
        "ARCHIVE_MODE_UNSAFE",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("playwright_package_version", "1.60.0", "PACKAGE_VERSION_MISMATCH"),
        ("browser_family", "chromium", "BROWSER_FAMILY_MISMATCH"),
        ("browser_revision", "9999", "BROWSER_REVISION_MISMATCH"),
        ("platform", "linux/arm64", "PLATFORM_MISMATCH"),
    ],
)
def test_manifest_identity_mismatch_is_denied(
    tmp_path: Path, field: str, value: str, code: str
) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    document[field] = value
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        code,
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_archive_filename_traversal_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    document["archive_filename"] = "../browser-archive"
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        "ARCHIVE_FILENAME_INVALID",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_archive_size_mismatch_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    document["archive_size"] = archive.stat().st_size + 1
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        "ARCHIVE_SIZE_MISMATCH",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_archive_sha_mismatch_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, document, _ = _fixture(tmp_path)
    document["archive_sha256"] = "0" * 64
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        "ARCHIVE_SHA256_MISMATCH",
        contract=contract,
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
    ],
)
def test_unsafe_zip_path_is_denied(
    tmp_path: Path, name: str, code: str
) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_writer=lambda item: _zip_entry(item, name, b"unsafe"),
    )
    _assert_failure(
        code,
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_symlink_zip_entry_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(
        tmp_path,
        extra_writer=lambda item: _zip_entry(
            item,
            "link",
            b"../../escape",
            mode=0o777,
            file_type=stat.S_IFLNK,
        ),
    )
    _assert_failure(
        "ARCHIVE_SYMLINK_DENIED",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def _write_tar_with_special_entry(path: Path, kind: bytes) -> None:
    with tarfile.open(path, "w:gz") as archive:
        executable = tarfile.TarInfo(EXECUTABLE)
        payload = b"#!/bin/sh\nexit 0\n"
        executable.size = len(payload)
        executable.mode = 0o755
        archive.addfile(executable, io.BytesIO(payload))

        special = tarfile.TarInfo("special")
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
    tmp_path: Path, kind: bytes, code: str
) -> None:
    contract = _contract(tmp_path)
    archive = tmp_path / "browser-archive"
    _write_tar_with_special_entry(archive, kind)
    document = _document(contract, archive, archive_format="tar.gz")
    manifest = tmp_path / "manifest.json"
    manifest_sha = _write_manifest(manifest, document)
    _assert_failure(
        code,
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_duplicate_zip_path_is_denied(tmp_path: Path) -> None:
    def duplicate(archive: zipfile.ZipFile) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            _zip_entry(archive, EXECUTABLE, b"duplicate", mode=0o755)

    contract, manifest, archive, _, manifest_sha = _fixture(
        tmp_path, extra_writer=duplicate
    )
    _assert_failure(
        "ARCHIVE_DUPLICATE_PATH",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_missing_executable_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(
        tmp_path, include_executable=False
    )
    _assert_failure(
        "EXPECTED_EXECUTABLE_MISSING",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_non_executable_browser_file_is_denied(tmp_path: Path) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(
        tmp_path, executable=False
    )
    _assert_failure(
        "EXPECTED_EXECUTABLE_INVALID",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_preexisting_partial_cache_is_preserved_and_denied(tmp_path: Path) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(tmp_path)
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
            contract=contract,
        )

    assert sentinel.read_bytes() == b"unchanged"


def test_publish_failure_leaves_no_target_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(tmp_path)

    def fail_publish(_source: object, _target: object) -> None:
        raise OSError("synthetic publish failure")

    monkeypatch.setattr(installer.os, "replace", fail_publish)
    _assert_failure(
        "ATOMIC_PUBLISH_FAILED",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_cleanup_failure_does_not_mask_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract, manifest, archive, _, manifest_sha = _fixture(
        tmp_path, include_executable=False
    )

    def fail_cleanup(_path: object) -> None:
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(installer.shutil, "rmtree", fail_cleanup)
    _assert_failure(
        "EXPECTED_EXECUTABLE_MISSING",
        contract=contract,
        manifest=manifest,
        archive=archive,
        manifest_sha=manifest_sha,
    )


def test_cli_error_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    contract, _, archive, _, _ = _fixture(tmp_path)
    canary = "PRIVATE-MANIFEST-CANARY"
    manifest = tmp_path / canary
    manifest.write_bytes(b"not-json")
    expected_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(installer, "load_installed_contract", lambda _p: contract)

    result = installer.main(
        [
            "--manifest",
            str(manifest),
            "--archive",
            str(archive),
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


def test_installer_has_no_network_client_imports() -> None:
    source = Path(installer.__file__).read_text(encoding="utf-8")
    for forbidden in ("requests", "urllib", "httpx", "socket", "playwright.dev"):
        assert forbidden not in source
