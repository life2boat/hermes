from __future__ import annotations

import hashlib
import json
import stat
import warnings
import zipfile
from dataclasses import replace
from pathlib import Path

from scripts import install_pinned_playwright_artifact as installer
from scripts import playwright_artifact_contract as contract_module


WHEEL_TAGS = {
    "linux/amd64": "manylinux1_x86_64",
    "linux/arm64": "manylinux_2_17_aarch64.manylinux2014_aarch64",
}


def wheel_filename(version: str, platform: str = "linux/amd64") -> str:
    return f"playwright-{version}-py3-none-{WHEEL_TAGS[platform]}.whl"


def write_wheel(
    path: Path,
    *,
    version: str = "1.61.0",
    metadata_version: str | None = None,
    revision: str = "9876",
    browser_matches: int = 1,
    duplicate_browser_entry: bool = False,
) -> bytes:
    browsers = [
        {
            "name": "chromium-headless-shell",
            "revision": revision,
            "revisionOverrides": {},
        }
        for _ in range(browser_matches)
    ]
    browser_data = json.dumps({"browsers": browsers}).encode("utf-8")
    package_data = (
        "Metadata-Version: 2.4\n"
        "Name: playwright\n"
        f"Version: {metadata_version or version}\n\n"
    ).encode("ascii")
    browser_path = "playwright/driver/package/browsers.json"
    package_path = f"playwright-{version}.dist-info/METADATA"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(browser_path, browser_data)
        archive.writestr(package_path, package_data)
        if duplicate_browser_entry:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(browser_path, browser_data)
    return path.read_bytes()


def write_lockfile(
    path: Path,
    wheel_bytes: bytes,
    *,
    version: str = "1.61.0",
    platform: str = "linux/amd64",
    sha256: str | None = None,
    size: int | None = None,
) -> None:
    filename = wheel_filename(version, platform)
    digest = sha256 or hashlib.sha256(wheel_bytes).hexdigest()
    wheel_size = len(wheel_bytes) if size is None else size
    path.write_text(
        "version = 1\n"
        "revision = 3\n"
        'requires-python = ">=3.11"\n\n'
        "[[package]]\n"
        'name = "playwright"\n'
        f'version = "{version}"\n'
        'source = { registry = "https://packages.invalid/simple" }\n'
        "wheels = [\n"
        "    { url = "
        f'"https://packages.invalid/{filename}", '
        f'hash = "sha256:{digest}", size = {wheel_size} }},\n'
        "]\n",
        encoding="utf-8",
    )


def verified_contract(
    tmp_path: Path,
    *,
    platform: str = "linux/amd64",
    version: str = "1.61.0",
    revision: str = "9876",
    cache_root: Path | None = None,
) -> tuple[contract_module.VerifiedBrowserContract, Path, Path]:
    wheel = tmp_path / "playwright-wheel"
    wheel_bytes = write_wheel(wheel, version=version, revision=revision)
    lockfile = tmp_path / "uv.lock"
    write_lockfile(lockfile, wheel_bytes, version=version, platform=platform)
    verified = contract_module.load_verified_wheel_contract(
        lockfile_path=lockfile,
        wheel_path=wheel,
        platform=platform,
    )
    if cache_root is not None:
        verified = replace(
            verified,
            browser=replace(verified.browser, cache_root=str(cache_root)),
        )
    return verified, lockfile, wheel


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


def write_browser_archive(
    path: Path,
    verified: contract_module.VerifiedBrowserContract,
    *,
    executable: bool = True,
    include_executable: bool = True,
    extra_entries: list[tuple[str, bytes, int, int]] | None = None,
) -> None:
    contract = verified.browser
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if include_executable:
            _zip_entry(
                archive,
                contract.expected_executable_relative_path,
                b"#!/bin/sh\nexit 0\n",
                mode=0o755 if executable else 0o644,
            )
        _zip_entry(
            archive,
            f"{contract.archive_root}/resources.pak",
            b"synthetic-resource",
        )
        for name, data, mode, file_type in extra_entries or []:
            _zip_entry(
                archive,
                name,
                data,
                mode=mode,
                file_type=file_type,
            )


def manifest_document(
    verified: contract_module.VerifiedBrowserContract,
    archive: Path,
    *,
    archive_format: str = "zip",
) -> dict[str, object]:
    contract = verified.browser
    wheel = verified.wheel
    return {
        "archive_filename": "browser-archive",
        "archive_format": archive_format,
        "archive_root": contract.archive_root,
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "archive_size": archive.stat().st_size,
        "browser_family": contract.browser_family,
        "browser_revision": contract.browser_revision,
        "cache_root": contract.cache_root,
        "expected_executable_relative_path": (
            contract.expected_executable_relative_path
        ),
        "manifest_version": installer.MANIFEST_VERSION,
        "platform": contract.platform,
        "playwright_package": contract.package,
        "playwright_package_version": contract.package_version,
        "playwright_wheel_filename": wheel.filename,
        "playwright_wheel_sha256": wheel.sha256,
        "playwright_wheel_size": wheel.size,
        "source_kind": "operator-approved-offline-artifact",
        "source_reference": "approved-synthetic-fixture",
    }


def write_manifest(path: Path, document: dict[str, object]) -> str:
    data = installer.canonical_json(document)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
