from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from scripts import playwright_artifact_contract as contract_module
from tests.playwright_supply_chain_support import (
    default_browser_entries,
    verified_closure,
    wheel_filename,
    write_lockfile,
    write_wheel,
)


def test_contract_derives_complete_package_closure_from_verified_wheel(
    tmp_path: Path,
) -> None:
    verified, _lockfile, _wheel = verified_closure(tmp_path)
    closure = verified.closure

    assert closure.package == "playwright"
    assert closure.package_version == "1.61.0"
    assert closure.platform == "linux/amd64"
    assert closure.artifact_names == ("chromium-headless-shell", "ffmpeg")
    assert [artifact.revision for artifact in closure.artifacts] == ["1228", "1011"]
    chromium = closure.artifact("chromium-headless-shell")
    ffmpeg = closure.artifact("ffmpeg")
    assert chromium.browser_version == "149.0.7827.55"
    assert chromium.layout_kind == contract_module.LAYOUT_DIRECTORY_TREE
    assert chromium.expected_archive_filename == "chrome-headless-shell-linux64.zip"
    assert chromium.expected_executable_relative_path.endswith("chrome-headless-shell")
    assert ffmpeg.browser_version is None
    assert ffmpeg.layout_kind == contract_module.LAYOUT_SINGLE_EXECUTABLE_FILE
    assert ffmpeg.expected_archive_filename == "ffmpeg-linux.zip"
    assert ffmpeg.expected_executable_relative_path == "ffmpeg-linux"


@pytest.mark.parametrize("platform", ["linux/amd64", "linux/arm64"])
def test_platform_mapping_is_explicit_for_every_required_artifact(
    tmp_path: Path, platform: str
) -> None:
    verified, _lockfile, _wheel = verified_closure(tmp_path, platform=platform)
    assert verified.closure.artifact_names == contract_module.REQUIRED_ARTIFACT_NAMES
    for artifact in verified.closure.artifacts:
        mapping = contract_module.ARTIFACT_PLATFORM_MAPPINGS[
            artifact.artifact_name
        ][platform]
        assert artifact.layout_kind == mapping.layout_kind
        assert artifact.expected_archive_filename == mapping.archive_filename
        assert artifact.expected_executable_relative_path == (
            mapping.executable_relative_path
        )


def test_revision_overrides_are_derived_per_artifact() -> None:
    entries = default_browser_entries()
    entries[0]["revisionOverrides"] = {"debian13-x64": "2222"}
    entries[1]["revisionOverrides"] = {"debian13-x64": "3333"}
    closure = contract_module.contract_from_metadata(
        package_version="1.61.0",
        browsers_payload={"browsers": entries},
        platform="linux/amd64",
    )
    assert [artifact.revision for artifact in closure.artifacts] == ["2222", "3333"]
    assert closure.artifact("chromium-headless-shell").cache_directory.endswith(
        "_debian13_x64_special-2222"
    )
    assert closure.artifact("ffmpeg").cache_directory.endswith(
        "_debian13_x64_special-3333"
    )


@pytest.mark.parametrize("missing_name", ["chromium-headless-shell", "ffmpeg"])
def test_missing_required_wheel_artifact_is_denied(missing_name: str) -> None:
    entries = [
        item for item in default_browser_entries() if item["name"] != missing_name
    ]
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="ARTIFACT_METADATA_AMBIGUOUS",
    ):
        contract_module.contract_from_metadata(
            package_version="1.61.0",
            browsers_payload={"browsers": entries},
            platform="linux/amd64",
        )


@pytest.mark.parametrize("duplicate_name", ["chromium-headless-shell", "ffmpeg"])
def test_duplicate_required_wheel_artifact_is_denied(duplicate_name: str) -> None:
    entries = default_browser_entries()
    entries.append(dict(next(item for item in entries if item["name"] == duplicate_name)))
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="ARTIFACT_METADATA_AMBIGUOUS",
    ):
        contract_module.contract_from_metadata(
            package_version="1.61.0",
            browsers_payload={"browsers": entries},
            platform="linux/amd64",
        )


@pytest.mark.parametrize("artifact_index", [0, 1])
def test_required_artifact_not_install_by_default_is_denied(
    artifact_index: int,
) -> None:
    entries = default_browser_entries()
    entries[artifact_index]["installByDefault"] = False
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="REQUIRED_ARTIFACT_NOT_INSTALL_BY_DEFAULT",
    ):
        contract_module.contract_from_metadata(
            package_version="1.61.0",
            browsers_payload={"browsers": entries},
            platform="linux/amd64",
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("revision", "latest", "ARTIFACT_REVISION_INVALID"),
        ("revisionOverrides", [], "ARTIFACT_REVISION_OVERRIDES_INVALID"),
        ("browserVersion", 149, "ARTIFACT_BROWSER_VERSION_INVALID"),
    ],
)
def test_invalid_wheel_artifact_metadata_is_denied(
    field: str, value: object, error: str
) -> None:
    entries = default_browser_entries()
    entries[0][field] = value
    with pytest.raises(contract_module.PlaywrightContractError, match=error):
        contract_module.contract_from_metadata(
            package_version="1.61.0",
            browsers_payload={"browsers": entries},
            platform="linux/amd64",
        )


def test_wheel_sha_not_authorized_by_lock_is_denied(tmp_path: Path) -> None:
    wheel = tmp_path / "playwright-wheel"
    wheel_bytes = write_wheel(wheel)
    lockfile = tmp_path / "uv.lock"
    write_lockfile(lockfile, wheel_bytes, sha256="a" * 64)
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="WHEEL_SHA256_MISMATCH",
    ):
        contract_module.load_verified_wheel_closure(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_wheel_package_version_mismatch_is_denied(tmp_path: Path) -> None:
    wheel = tmp_path / "playwright-wheel"
    wheel_bytes = write_wheel(wheel, metadata_version="1.60.0")
    lockfile = tmp_path / "uv.lock"
    write_lockfile(lockfile, wheel_bytes)
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="PACKAGE_VERSION_MISMATCH",
    ):
        contract_module.load_verified_wheel_closure(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_duplicate_browser_metadata_wheel_entry_is_denied(tmp_path: Path) -> None:
    wheel = tmp_path / "playwright-wheel"
    wheel_bytes = write_wheel(wheel, duplicate_browser_entry=True)
    lockfile = tmp_path / "uv.lock"
    write_lockfile(lockfile, wheel_bytes)
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="BROWSER_METADATA_ENTRY_AMBIGUOUS",
    ):
        contract_module.load_verified_wheel_closure(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_installed_metadata_cannot_replace_verified_wheel_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    verified, lockfile, wheel = verified_closure(tmp_path)
    monkeypatch.setattr(
        contract_module,
        "_load_installed_browsers_payload",
        lambda: ("9.9.9", {"browsers": []}),
    )
    reloaded = contract_module.load_verified_wheel_closure(
        lockfile_path=lockfile,
        wheel_path=wheel,
        platform="linux/amd64",
    )
    assert reloaded == verified


def _prepare_packaged_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[contract_module.PlaywrightClosureContract, Path]:
    payload = {"browsers": default_browser_entries()}
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(contract_module, "CACHE_ROOT", str(cache_root))
    closure = contract_module.contract_from_metadata(
        package_version="1.61.0",
        browsers_payload=payload,
        platform="linux/amd64",
    )
    wheel = contract_module.LockedWheel(
        package="playwright",
        package_version="1.61.0",
        filename=wheel_filename("1.61.0"),
        size=123,
        sha256=hashlib.sha256(b"wheel").hexdigest(),
        platform="linux/amd64",
    )
    verified = contract_module.VerifiedPlaywrightClosure(closure=closure, wheel=wheel)
    cache_root.mkdir()
    for artifact in closure.artifacts:
        executable = (
            cache_root
            / artifact.cache_directory
            / artifact.expected_executable_relative_path
        )
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"synthetic")
        executable.chmod(0o755)
    (cache_root / contract_module.INSTALLATION_MARKER).write_bytes(
        contract_module.canonical_installation_identity(verified)
    )

    metadata_path = tmp_path / "installed-browsers.json"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    class Distribution:
        version = "1.61.0"
        files = [PurePosixPath(contract_module._BROWSER_METADATA_PATH)]

        @staticmethod
        def locate_file(_path: object) -> Path:
            return metadata_path

    monkeypatch.setattr(
        contract_module.metadata,
        "distribution",
        lambda _name: Distribution(),
    )
    return closure, cache_root


def test_packaged_readiness_accepts_only_complete_matching_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closure, _cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    assert contract_module.verify_packaged_browser_readiness("linux/amd64") == closure


@pytest.mark.parametrize("missing_name", ["chromium-headless-shell", "ffmpeg"])
def test_packaged_readiness_denies_single_artifact_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_name: str,
) -> None:
    closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    missing = closure.artifact(missing_name)
    target = cache_root / missing.cache_directory
    for path in sorted(target.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    target.rmdir()
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="PACKAGED_CACHE_ENTRY_SET_MISMATCH",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_packaged_readiness_denies_unexpected_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    (cache_root / "unapproved-9999").mkdir()
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="PACKAGED_CACHE_ENTRY_SET_MISMATCH",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


@pytest.mark.parametrize("name", ["chromium-headless-shell", "ffmpeg"])
def test_packaged_readiness_denies_non_executable_required_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    artifact = closure.artifact(name)
    executable = (
        cache_root / artifact.cache_directory / artifact.expected_executable_relative_path
    )
    executable.chmod(0o644)
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="PACKAGED_ARTIFACT_INVALID",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_packaged_readiness_denies_altered_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    marker = cache_root / contract_module.INSTALLATION_MARKER
    document = json.loads(marker.read_text(encoding="ascii"))
    document["artifacts"][1]["revision"] = "9999"
    marker.write_bytes(contract_module._canonical_json(document))
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="CLOSURE_IDENTITY_MISMATCH",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_contract_reporter_has_no_network_or_install_path() -> None:
    source = Path(contract_module.__file__).read_text(encoding="utf-8")
    assert "requests" not in source
    assert "urllib" not in source
    assert "subprocess" not in source
    assert "playwright install" not in source
    assert "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT" not in source
