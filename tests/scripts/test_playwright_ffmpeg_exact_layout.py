from __future__ import annotations

import stat
import unicodedata
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import install_pinned_playwright_artifact as installer
from scripts.playwright_artifact_contract import ArtifactContract
from tests.playwright_supply_chain_support import (
    closure_manifest_document,
    verified_closure,
    write_closure_archives,
)


EXECUTABLE = "ffmpeg-linux"
COMPANION = "COPYING.LGPLv2.1"


def _artifact(tmp_path: Path) -> ArtifactContract:
    verified, _lockfile, _wheel = verified_closure(tmp_path)
    return verified.closure.artifact("ffmpeg")


def _entry(
    name: str,
    *,
    mode: int = 0o644,
    file_type: int = stat.S_IFREG,
    data: bytes = b"synthetic",
) -> tuple[str, bytes, int, int]:
    return name, data, mode, file_type


def _write_archive(
    path: Path,
    entries: list[tuple[str, bytes, int, int]],
    *,
    create_system_overrides: dict[str, int] | None = None,
    external_attr_overrides: dict[str, int] | None = None,
) -> None:
    create_system_overrides = create_system_overrides or {}
    external_attr_overrides = external_attr_overrides or {}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode, file_type in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = create_system_overrides.get(name, 3)
            info.external_attr = external_attr_overrides.get(
                name,
                (file_type | mode) << 16,
            )
            archive.writestr(info, data)


def _executable(
    *,
    name: str = EXECUTABLE,
    mode: int = 0o755,
    file_type: int = stat.S_IFREG,
) -> tuple[str, bytes, int, int]:
    return _entry(
        name,
        mode=mode,
        file_type=file_type,
        data=b"synthetic executable",
    )


def _companion(
    *,
    name: str = COMPANION,
    mode: int = 0o644,
    file_type: int = stat.S_IFREG,
) -> tuple[str, bytes, int, int]:
    return _entry(
        name,
        mode=mode,
        file_type=file_type,
        data=b"synthetic license",
    )


def test_exact_ffmpeg_root_file_set_is_accepted(tmp_path: Path) -> None:
    archive = tmp_path / "ffmpeg.zip"
    artifact = _artifact(tmp_path)
    _write_archive(archive, [_executable(), _companion()])

    installer._validate_archive_layout(archive, "zip", artifact)


@pytest.mark.parametrize(
    ("case", "entries", "error_code"),
    [
        (
            "executable_only",
            [_executable()],
            "EXACT_ROOT_FILE_SET_INVALID",
        ),
        (
            "companion_only",
            [_companion()],
            "EXPECTED_EXECUTABLE_MISSING",
        ),
        (
            "unknown_second_filename",
            [_executable(), _companion(name="unexpected-license")],
            "EXACT_ROOT_FILE_SET_INVALID",
        ),
        (
            "third_regular_member",
            [_executable(), _companion(), _entry("unexpected-third")],
            "EXACT_ROOT_FILE_SET_INVALID",
        ),
        (
            "nested_companion",
            [_executable(), _companion(name=f"nested/{COMPANION}")],
            "EXACT_ROOT_FILE_SET_INVALID",
        ),
        (
            "nested_executable",
            [_executable(name=f"nested/{EXECUTABLE}"), _companion()],
            "EXPECTED_EXECUTABLE_MISSING",
        ),
        (
            "executable_directory",
            [
                _executable(
                    name=f"{EXECUTABLE}/",
                    mode=0o755,
                    file_type=stat.S_IFDIR,
                ),
                _companion(),
            ],
            "EXPECTED_EXECUTABLE_MISSING",
        ),
        (
            "companion_directory",
            [
                _executable(),
                _companion(
                    name=f"{COMPANION}/",
                    mode=0o755,
                    file_type=stat.S_IFDIR,
                ),
            ],
            "EXACT_ROOT_FILE_SET_INVALID",
        ),
        (
            "companion_symlink",
            [
                _executable(),
                _companion(file_type=stat.S_IFLNK, mode=0o777),
            ],
            "ARCHIVE_SYMLINK_DENIED",
        ),
        (
            "executable_symlink",
            [
                _executable(file_type=stat.S_IFLNK, mode=0o777),
                _companion(),
            ],
            "ARCHIVE_SYMLINK_DENIED",
        ),
        (
            "normalized_duplicate",
            [
                _executable(),
                _companion(),
                _entry(unicodedata.normalize("NFC", "cafe\u0301")),
                _entry("cafe\u0301"),
            ],
            "ARCHIVE_PATH_NORMALIZATION_AMBIGUOUS",
        ),
        (
            "case_collision",
            [_executable(), _companion(), _entry(COMPANION.lower())],
            "ARCHIVE_CASE_COLLISION",
        ),
        (
            "second_executable_designation",
            [
                _executable(),
                _companion(),
                _entry("unexpected-executable", mode=0o755),
            ],
            "EXACT_ROOT_FILE_SET_INVALID",
        ),
        (
            "executable_companion",
            [_executable(), _companion(mode=0o755)],
            "EXACT_ROOT_FILE_SET_COMPANION_EXECUTABLE",
        ),
        (
            "unexpected_companion_mode",
            [_executable(), _companion(mode=0o600)],
            "EXACT_ROOT_FILE_SET_MODE_INVALID",
        ),
        (
            "unexpected_executable_mode",
            [_executable(mode=0o744), _companion()],
            "EXACT_ROOT_FILE_SET_MODE_INVALID",
        ),
        (
            "path_traversal",
            [_executable(), _companion(name=f"../{COMPANION}")],
            "ARCHIVE_PATH_TRAVERSAL",
        ),
        (
            "absolute_path",
            [_executable(), _companion(name=f"/{COMPANION}")],
            "ARCHIVE_ABSOLUTE_OR_INVALID_PATH",
        ),
    ],
)
def test_ffmpeg_exact_root_file_set_denies_noncanonical_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    entries: list[tuple[str, bytes, int, int]],
    error_code: str,
) -> None:
    del case
    archive = tmp_path / "ffmpeg.zip"
    destination = tmp_path / "installed"
    _write_archive(archive, entries)
    extraction_started = False

    def extraction_must_not_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal extraction_started
        extraction_started = True
        raise AssertionError("extraction started before complete validation")

    monkeypatch.setattr(installer, "_extract_entries", extraction_must_not_start)

    with pytest.raises(installer.ArtifactContractError, match=error_code):
        installer._extract_archive(
            archive,
            "zip",
            _artifact(tmp_path),
            destination,
        )
    assert extraction_started is False
    assert not destination.exists()


@pytest.mark.parametrize(
    (
        "case",
        "create_system_overrides",
        "external_attr_overrides",
    ),
    [
        (
            "companion_missing_unix_mode_metadata",
            {},
            {COMPANION: 1},
        ),
        (
            "companion_dos_only_mode_metadata",
            {COMPANION: 0},
            {COMPANION: (stat.S_IFREG | 0o644) << 16},
        ),
        (
            "companion_zero_permission_bits",
            {},
            {COMPANION: stat.S_IFREG << 16},
        ),
        (
            "companion_missing_file_type_bits",
            {},
            {COMPANION: 0o644 << 16},
        ),
        (
            "executable_missing_unix_mode_metadata",
            {},
            {EXECUTABLE: 1},
        ),
        (
            "executable_dos_only_mode_metadata",
            {EXECUTABLE: 0},
            {EXECUTABLE: (stat.S_IFREG | 0o755) << 16},
        ),
    ],
)
def test_ffmpeg_exact_root_file_set_requires_explicit_unix_mode_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    create_system_overrides: dict[str, int],
    external_attr_overrides: dict[str, int],
) -> None:
    del case
    archive = tmp_path / "ffmpeg.zip"
    destination = tmp_path / "installed"
    _write_archive(
        archive,
        [_executable(), _companion()],
        create_system_overrides=create_system_overrides,
        external_attr_overrides=external_attr_overrides,
    )
    extraction_started = False

    def extraction_must_not_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal extraction_started
        extraction_started = True
        raise AssertionError("extraction started before Unix mode validation")

    monkeypatch.setattr(installer, "_extract_entries", extraction_must_not_start)

    with pytest.raises(
        installer.ArtifactContractError,
        match="ARCHIVE_UNIX_MODE_METADATA_INVALID",
    ):
        installer._extract_archive(
            archive,
            "zip",
            _artifact(tmp_path),
            destination,
        )
    assert extraction_started is False
    assert not destination.exists()


def test_ffmpeg_exact_profile_is_version_revision_and_platform_bound(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    profile = installer._exact_root_file_set_profile(artifact)

    assert profile is not None
    assert installer.LAYOUT_EXACT_ROOT_FILE_SET == "EXACT_ROOT_FILE_SET"
    assert profile.layout_kind == installer.LAYOUT_EXACT_ROOT_FILE_SET
    assert artifact.revision == "1011"
    assert profile.revision == artifact.revision
    assert profile.required_member_names == {EXECUTABLE, COMPANION}
    assert profile.designated_executable == EXECUTABLE
    assert dict(profile.required_member_modes) == {
        EXECUTABLE: 0o755,
        COMPANION: 0o644,
    }
    assert (
        installer._exact_root_file_set_profile(
            replace(artifact, package_version="1.61.1")
        )
        is None
    )
    assert (
        installer._exact_root_file_set_profile(
            replace(artifact, platform="linux/arm64")
        )
        is None
    )


@pytest.mark.parametrize(
    ("case", "field_name", "field_value"),
    [
        (
            "manifest_adds_third_member",
            "required_member_names",
            [EXECUTABLE, COMPANION, "unexpected-third"],
        ),
        (
            "manifest_renames_companion",
            "companion_member_name",
            "renamed-companion",
        ),
        (
            "manifest_removes_companion",
            "companion_required",
            False,
        ),
    ],
)
def test_manifest_cannot_change_trusted_ffmpeg_member_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    field_name: str,
    field_value: object,
) -> None:
    del case
    cache_root = tmp_path / "cache-parent" / "ms-playwright"
    verified, _lockfile, _wheel = verified_closure(
        tmp_path,
        cache_root=cache_root,
    )
    archives = write_closure_archives(tmp_path / "archives", verified)
    document = closure_manifest_document(verified, archives)
    artifacts = document["artifacts"]
    assert isinstance(artifacts, list)
    ffmpeg = next(
        item for item in artifacts if item["artifact_name"] == "ffmpeg"
    )
    ffmpeg[field_name] = field_value
    extraction_started = False

    def extraction_must_not_start(*args: object, **kwargs: object) -> None:
        del args, kwargs
        nonlocal extraction_started
        extraction_started = True
        raise AssertionError("extraction started for an invalid manifest")

    monkeypatch.setattr(installer, "_extract_archive", extraction_must_not_start)

    with pytest.raises(
        installer.ArtifactContractError,
        match="ARTIFACT_MANIFEST_FIELDS_INVALID",
    ):
        installer.parse_canonical_closure_manifest(
            installer.canonical_json(document)
        )
    assert extraction_started is False
    assert not cache_root.exists()
