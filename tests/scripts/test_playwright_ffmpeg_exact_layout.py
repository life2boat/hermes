from __future__ import annotations

import stat
import unicodedata
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from scripts import install_pinned_playwright_artifact as installer
from scripts.playwright_artifact_contract import ArtifactContract
from tests.playwright_supply_chain_support import verified_closure


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
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode, file_type in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (file_type | mode) << 16
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
    case: str,
    entries: list[tuple[str, bytes, int, int]],
    error_code: str,
) -> None:
    del case
    archive = tmp_path / "ffmpeg.zip"
    _write_archive(archive, entries)

    with pytest.raises(installer.ArtifactContractError, match=error_code):
        installer._validate_archive_layout(
            archive,
            "zip",
            _artifact(tmp_path),
        )


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
