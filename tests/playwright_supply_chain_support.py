from __future__ import annotations

import hashlib
import json
import shutil
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
SOURCE_REFERENCE_SHA256 = hashlib.sha256(
    b"operator-approved-synthetic-playwright-closure"
).hexdigest()


def wheel_filename(version: str, platform: str = "linux/amd64") -> str:
    return f"playwright-{version}-py3-none-{WHEEL_TAGS[platform]}.whl"


def default_browser_entries(
    *,
    chromium_revision: str = "1228",
    ffmpeg_revision: str = "1011",
) -> list[dict[str, object]]:
    return [
        {
            "name": "chromium-headless-shell",
            "revision": chromium_revision,
            "browserVersion": "149.0.7827.55",
            "installByDefault": True,
            "revisionOverrides": {},
        },
        {
            "name": "ffmpeg",
            "revision": ffmpeg_revision,
            "installByDefault": True,
            "revisionOverrides": {},
        },
    ]


def write_wheel(
    path: Path,
    *,
    version: str = "1.61.0",
    metadata_version: str | None = None,
    browser_entries: list[dict[str, object]] | None = None,
    duplicate_browser_entry: bool = False,
) -> bytes:
    browsers = (
        default_browser_entries() if browser_entries is None else browser_entries
    )
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


def verified_closure(
    tmp_path: Path,
    *,
    platform: str = "linux/amd64",
    version: str = "1.61.0",
    browser_entries: list[dict[str, object]] | None = None,
    cache_root: Path | None = None,
) -> tuple[contract_module.VerifiedPlaywrightClosure, Path, Path]:
    wheel = tmp_path / "playwright-wheel"
    wheel_bytes = write_wheel(
        wheel,
        version=version,
        browser_entries=browser_entries,
    )
    lockfile = tmp_path / "uv.lock"
    write_lockfile(lockfile, wheel_bytes, version=version, platform=platform)
    verified = contract_module.load_verified_wheel_closure(
        lockfile_path=lockfile,
        wheel_path=wheel,
        platform=platform,
    )
    if cache_root is not None:
        closure = replace(
            verified.closure,
            cache_root=str(cache_root),
            artifacts=tuple(
                replace(artifact, cache_root=str(cache_root))
                for artifact in verified.closure.artifacts
            ),
        )
        verified = replace(verified, closure=closure)
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


def write_artifact_archive(
    path: Path,
    artifact: contract_module.ArtifactContract,
    *,
    executable: bool = True,
    include_executable: bool = True,
    force_layout: str | None = None,
    extra_entries: list[tuple[str, bytes, int, int]] | None = None,
) -> None:
    layout = force_layout or artifact.layout_kind
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if include_executable:
            _zip_entry(
                archive,
                artifact.expected_executable_relative_path,
                b"#!/bin/sh\nexit 0\n",
                mode=0o755 if executable else 0o644,
            )
        profile = installer._exact_root_file_set_profile(artifact)
        if layout == artifact.layout_kind and profile is not None:
            for member_name in sorted(
                profile.required_member_names - {profile.designated_executable}
            ):
                _zip_entry(
                    archive,
                    member_name,
                    b"synthetic license companion\n",
                    mode=0o644,
                )
        if layout == contract_module.LAYOUT_DIRECTORY_TREE:
            _zip_entry(
                archive,
                f"{artifact.archive_root}/resources.pak",
                b"synthetic-resource",
            )
        elif layout != contract_module.LAYOUT_SINGLE_EXECUTABLE_FILE:
            raise ValueError(f"unsupported synthetic layout: {layout}")
        for name, data, mode, file_type in extra_entries or []:
            _zip_entry(
                archive,
                name,
                data,
                mode=mode,
                file_type=file_type,
            )


def write_closure_archives(
    root: Path,
    verified: contract_module.VerifiedPlaywrightClosure,
) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for artifact in verified.closure.artifacts:
        path = root / f"{artifact.artifact_name}.zip"
        write_artifact_archive(path, artifact)
        result[artifact.artifact_name] = path
    return result


def closure_manifest_document(
    verified: contract_module.VerifiedPlaywrightClosure,
    archives: dict[str, Path],
) -> dict[str, object]:
    closure = verified.closure
    wheel = verified.wheel
    artifacts: list[dict[str, object]] = []
    for artifact in closure.artifacts:
        archive = archives[artifact.artifact_name]
        artifacts.append(
            {
                "artifact_name": artifact.artifact_name,
                "browser_family": artifact.browser_family,
                "revision": artifact.revision,
                "browser_version": artifact.browser_version,
                "platform": artifact.platform,
                "archive_filename": artifact.expected_archive_filename,
                "archive_size": archive.stat().st_size,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "archive_format": "zip",
                "layout_kind": artifact.layout_kind,
                "archive_root": artifact.archive_root,
                "expected_executable_relative_path": (
                    artifact.expected_executable_relative_path
                ),
                "executable_mode_required": True,
                "source_kind": "operator-approved-offline-artifact",
                "source_reference_sha256": SOURCE_REFERENCE_SHA256,
            }
        )
    return {
        "manifest_version": installer.MANIFEST_VERSION,
        "manifest_kind": installer.MANIFEST_KIND,
        "playwright_package": closure.package,
        "playwright_package_version": closure.package_version,
        "playwright_wheel_filename": wheel.filename,
        "playwright_wheel_size": wheel.size,
        "playwright_wheel_sha256": wheel.sha256,
        "platform": closure.platform,
        "cache_root": closure.cache_root,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }


def write_manifest(path: Path, document: dict[str, object]) -> str:
    data = installer.canonical_json(document)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def write_closure_context(
    root: Path,
    verified: contract_module.VerifiedPlaywrightClosure,
    wheel: Path,
    archives: dict[str, Path],
    *,
    document: dict[str, object] | None = None,
) -> str:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(wheel, root / "playwright-wheel")
    artifacts_root = root / "artifacts"
    artifacts_root.mkdir()
    for artifact in verified.closure.artifacts:
        directory = artifacts_root / artifact.artifact_name
        directory.mkdir()
        shutil.copyfile(archives[artifact.artifact_name], directory / "archive")
    return write_manifest(
        root / "closure.json",
        document or closure_manifest_document(verified, archives),
    )
