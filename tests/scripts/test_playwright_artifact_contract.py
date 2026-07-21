from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

from scripts import playwright_artifact_contract as contract_module
from scripts import playwright_installed_closure as installed
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
    monkeypatch.setattr(contract_module, "_IMMUTABLE_OWNER_UID", os.geteuid())
    monkeypatch.setattr(contract_module, "_IMMUTABLE_OWNER_GID", os.getegid())
    closure = contract_module.contract_from_metadata(
        package_version="1.61.0",
        browsers_payload=payload,
        platform="linux/amd64",
    )
    cache_root.mkdir(mode=0o700)
    artifact_roots: dict[str, Path] = {}
    for artifact in closure.artifacts:
        destination = cache_root / artifact.cache_directory
        executable = destination.joinpath(
            *artifact.expected_executable_relative_path.split("/")
        )
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"synthetic executable")
        executable.chmod(0o755)
        if artifact.artifact_name == "chromium-headless-shell":
            resource = destination / artifact.archive_root / "resources.pak"
            resource.write_bytes(b"synthetic resource")
            resource.chmod(0o644)
        installed.seal_installed_artifact_tree(
            destination,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )
        artifact_roots[artifact.artifact_name] = destination
    expected = installed.build_expected_identity_document(
        closure_manifest_sha256=hashlib.sha256(
            b"synthetic closure manifest"
        ).hexdigest(),
        playwright_package=closure.package,
        playwright_package_version=closure.package_version,
        platform=closure.platform,
        artifacts=[
            {
                "artifact_name": artifact.artifact_name,
                "revision": artifact.revision,
                "archive_sha256": hashlib.sha256(
                    f"archive:{artifact.artifact_name}".encode()
                ).hexdigest(),
                "layout_kind": artifact.layout_kind,
                "expected_executable_relative_path": (
                    artifact.expected_executable_relative_path
                ),
            }
            for artifact in closure.artifacts
        ],
    )
    expected_path = installed.expected_identity_path(cache_root)
    expected_path.write_bytes(installed.canonical_json(expected))
    expected_path.chmod(0o444)
    marker = installed.build_installed_marker_document(
        expected_identity=expected,
        artifact_roots=artifact_roots,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )
    marker_path = cache_root / contract_module.INSTALLATION_MARKER
    marker_path.write_bytes(installed.canonical_json(marker))
    marker_path.chmod(0o444)
    cache_root.chmod(0o555)

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


def _make_test_tree_removable(path: Path) -> None:
    for current, _directories, _files in os.walk(path, topdown=True):
        Path(current).chmod(0o700)
    shutil.rmtree(path)


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
    cache_root.chmod(0o755)
    _make_test_tree_removable(target)
    cache_root.chmod(0o555)
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="PACKAGED_CACHE_ENTRY_SET_MISMATCH",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_packaged_readiness_denies_unexpected_cache_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    cache_root.chmod(0o755)
    (cache_root / "unapproved-9999").mkdir()
    cache_root.chmod(0o555)
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
        match="INSTALLED_TREE_ENTRY_INVALID",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_packaged_readiness_denies_altered_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    marker = cache_root / contract_module.INSTALLATION_MARKER
    document = json.loads(marker.read_text(encoding="ascii"))
    document["artifacts"][1]["revision"] = "9999"
    marker.chmod(0o644)
    marker.write_bytes(installed.canonical_json(document))
    marker.chmod(0o444)
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="INSTALLED_MARKER_EXPECTED_IDENTITY_MISMATCH",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")



def _rewrite_readonly_json(path: Path, document: dict[str, object]) -> None:
    path.chmod(0o644)
    path.write_bytes(installed.canonical_json(document))
    path.chmod(0o444)


def _artifact_file(
    closure: contract_module.PlaywrightClosureContract,
    cache_root: Path,
    artifact_name: str,
    relative_path: str,
) -> Path:
    artifact = closure.artifact(artifact_name)
    return cache_root / artifact.cache_directory / relative_path


@pytest.mark.parametrize(
    "case",
    [
        "chromium_executable_bytes",
        "ffmpeg_executable_bytes",
        "chromium_non_executable_bytes",
        "extra_chromium_file",
        "missing_chromium_file",
        "executable_mode",
        "directory_mode",
        "marker_closure_sha",
        "marker_chromium_archive_sha",
        "marker_ffmpeg_archive_sha",
        "marker_tree_sha",
        "canonical_different_marker",
        "marker_runtime_writable",
        "artifact_directory_runtime_writable",
        "alter_after_successful_readiness",
    ],
)
def test_packaged_readiness_rejects_complete_byte_tamper_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    chromium = closure.artifact("chromium-headless-shell")
    ffmpeg = closure.artifact("ffmpeg")
    chromium_executable = _artifact_file(
        closure,
        cache_root,
        chromium.artifact_name,
        chromium.expected_executable_relative_path,
    )
    ffmpeg_executable = _artifact_file(
        closure,
        cache_root,
        ffmpeg.artifact_name,
        ffmpeg.expected_executable_relative_path,
    )
    chromium_resource = _artifact_file(
        closure,
        cache_root,
        chromium.artifact_name,
        f"{chromium.archive_root}/resources.pak",
    )
    marker_path = cache_root / installed.INSTALLATION_MARKER
    if case == "alter_after_successful_readiness":
        assert contract_module.verify_packaged_browser_readiness(
            "linux/amd64"
        ) == closure
        case = "chromium_executable_bytes"
    if case == "chromium_executable_bytes":
        chromium_executable.chmod(0o755)
        chromium_executable.write_bytes(b"altered chromium")
        chromium_executable.chmod(0o555)
    elif case == "ffmpeg_executable_bytes":
        ffmpeg_executable.chmod(0o755)
        ffmpeg_executable.write_bytes(b"altered ffmpeg")
        ffmpeg_executable.chmod(0o555)
    elif case == "chromium_non_executable_bytes":
        chromium_resource.chmod(0o644)
        chromium_resource.write_bytes(b"altered resource")
        chromium_resource.chmod(0o444)
    elif case == "extra_chromium_file":
        chromium_root = cache_root / chromium.cache_directory
        chromium_root.chmod(0o755)
        extra = chromium_root / "extra.bin"
        extra.write_bytes(b"extra")
        extra.chmod(0o444)
        chromium_root.chmod(0o555)
    elif case == "missing_chromium_file":
        chromium_resource.parent.chmod(0o755)
        chromium_resource.unlink()
        chromium_resource.parent.chmod(0o555)
    elif case == "executable_mode":
        chromium_executable.chmod(0o444)
    elif case == "directory_mode":
        chromium_resource.parent.chmod(0o500)
    elif case in {
        "marker_closure_sha",
        "marker_chromium_archive_sha",
        "marker_ffmpeg_archive_sha",
        "marker_tree_sha",
        "canonical_different_marker",
    }:
        marker = json.loads(marker_path.read_text(encoding="ascii"))
        if case == "marker_closure_sha":
            marker["closure_manifest_sha256"] = "a" * 64
        elif case == "marker_chromium_archive_sha":
            marker["artifacts"][0]["archive_sha256"] = "a" * 64
        elif case == "marker_ffmpeg_archive_sha":
            marker["artifacts"][1]["archive_sha256"] = "b" * 64
        elif case == "marker_tree_sha":
            marker["artifacts"][0]["installed_tree_sha256"] = "c" * 64
        else:
            marker["complete_installed_closure_sha256"] = "d" * 64
        _rewrite_readonly_json(marker_path, marker)
    elif case == "marker_runtime_writable":
        marker_path.chmod(0o644)
    elif case == "artifact_directory_runtime_writable":
        (cache_root / chromium.cache_directory).chmod(0o755)
    else:
        raise AssertionError(case)
    with pytest.raises(contract_module.PlaywrightContractError):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_distinct_manifest_identities_are_not_cross_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    expected_path = installed.expected_identity_path(cache_root)
    marker_path = cache_root / installed.INSTALLATION_MARKER
    expected_a = json.loads(expected_path.read_text(encoding="ascii"))
    marker_a = marker_path.read_bytes()
    assert contract_module.verify_packaged_browser_readiness("linux/amd64") == closure

    expected_b = json.loads(installed.canonical_json(expected_a))
    expected_b["closure_manifest_sha256"] = "e" * 64
    expected_b["artifacts"][1]["archive_sha256"] = "f" * 64
    marker_b_document = installed.build_installed_marker_document(
        expected_identity=expected_b,
        artifact_roots={
            artifact.artifact_name: cache_root / artifact.cache_directory
            for artifact in closure.artifacts
        },
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )
    marker_b = installed.canonical_json(marker_b_document)
    assert marker_a != marker_b

    _rewrite_readonly_json(expected_path, expected_b)
    with pytest.raises(contract_module.PlaywrightContractError):
        contract_module.verify_packaged_browser_readiness("linux/amd64")
    marker_path.chmod(0o644)
    marker_path.write_bytes(marker_b)
    marker_path.chmod(0o444)
    assert contract_module.verify_packaged_browser_readiness("linux/amd64") == closure
    _rewrite_readonly_json(expected_path, expected_a)
    with pytest.raises(contract_module.PlaywrightContractError):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


@pytest.mark.parametrize(
    "target", ["expected_identity", "cache_root", "cache_parent"]
)
def test_runtime_writable_trust_boundaries_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    path = {
        "expected_identity": installed.expected_identity_path(cache_root),
        "cache_root": cache_root,
        "cache_parent": cache_root.parent,
    }[target]
    path.chmod(0o777 if target == "cache_parent" else (0o755 if path.is_dir() else 0o644))
    with pytest.raises(contract_module.PlaywrightContractError):
        contract_module.verify_packaged_browser_readiness("linux/amd64")



def test_installed_marker_v2_binds_complete_tree_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    marker = installed.parse_installed_marker(
        (cache_root / installed.INSTALLATION_MARKER).read_bytes()
    )
    assert marker["marker_version"] == 2
    assert marker["marker_kind"] == "PLAYWRIGHT_INSTALLED_CLOSURE"
    assert marker["artifact_count"] == 2
    artifacts = marker["artifacts"]
    assert isinstance(artifacts, list)
    counts = {
        item["artifact_name"]: item["installed_file_count"]
        for item in artifacts
    }
    assert counts == {"chromium-headless-shell": 2, "ffmpeg": 1}
    assert all(item["installed_total_bytes"] > 0 for item in artifacts)
    assert all(len(item["installed_tree_sha256"]) == 64 for item in artifacts)
    assert len(marker["complete_installed_closure_sha256"]) == 64
    assert installed.expected_identity_path(cache_root).parent == cache_root.parent
    assert not installed.expected_identity_path(cache_root).is_relative_to(cache_root)
    assert contract_module.verify_packaged_browser_readiness("linux/amd64") == closure


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("marker_version", 1),
        ("marker_kind", "LEGACY"),
        ("artifact_count", True),
        ("complete_installed_closure_sha256", "not-a-sha"),
    ],
)
def test_installed_marker_v2_schema_is_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    marker_path = cache_root / installed.INSTALLATION_MARKER
    marker = json.loads(marker_path.read_text(encoding="ascii"))
    marker[field] = value
    _rewrite_readonly_json(marker_path, marker)
    with pytest.raises(contract_module.PlaywrightContractError):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_installed_marker_v2_unknown_fields_are_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    marker_path = cache_root / installed.INSTALLATION_MARKER
    marker = json.loads(marker_path.read_text(encoding="ascii"))
    marker["unexpected"] = False
    _rewrite_readonly_json(marker_path, marker)
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="INSTALLED_MARKER_FIELDS_INVALID",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


@pytest.mark.parametrize("entry_kind", ["symlink", "hardlink"])
def test_runtime_tree_forbidden_entries_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    chromium = closure.artifact("chromium-headless-shell")
    root = cache_root / chromium.cache_directory
    root.chmod(0o755)
    candidate = root / "forbidden-entry"
    if entry_kind == "symlink":
        candidate.symlink_to("chrome-headless-shell-linux64/resources.pak")
    else:
        candidate.hardlink_to(
            root / chromium.archive_root / "resources.pak"
        )
    root.chmod(0o555)
    with pytest.raises(contract_module.PlaywrightContractError):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_runtime_readiness_requires_build_owner_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure, _cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    monkeypatch.setattr(
        contract_module, "_IMMUTABLE_OWNER_UID", os.geteuid() + 1
    )
    with pytest.raises(contract_module.PlaywrightContractError):
        contract_module.verify_packaged_browser_readiness("linux/amd64")



def test_legacy_readiness_marker_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _closure, cache_root = _prepare_packaged_closure(tmp_path, monkeypatch)
    marker = cache_root / installed.INSTALLATION_MARKER
    _rewrite_readonly_json(marker, {"identity_version": 1})
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="INSTALLED_MARKER_FIELDS_INVALID",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")



def test_contract_reporter_has_no_network_or_install_path() -> None:
    source = Path(contract_module.__file__).read_text(encoding="utf-8")
    assert "requests" not in source
    assert "urllib" not in source
    assert "subprocess" not in source
    assert "playwright install" not in source
    assert "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT" not in source
