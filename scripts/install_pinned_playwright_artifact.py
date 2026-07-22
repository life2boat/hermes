from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Iterable

from scripts.playwright_artifact_contract import (
    ARTIFACT_PLATFORM_MAPPINGS,
    LAYOUT_DIRECTORY_TREE,
    LAYOUT_SINGLE_EXECUTABLE_FILE,
    ArtifactContract,
    PlaywrightClosureContract,
    PlaywrightContractError,
    VerifiedPlaywrightClosure,
    load_verified_wheel_closure,
)
from scripts.playwright_installed_closure import (
    IMMUTABLE_DIRECTORY_MODE,
    InstalledClosureError,
    build_expected_identity_document,
    build_installed_marker_document,
    canonical_json as canonical_installed_json,
    expected_identity_path,
    read_expected_identity,
    seal_installed_artifact_tree,
    verify_installed_closure,
)



MANIFEST_VERSION = 2
MANIFEST_KIND = "PLAYWRIGHT_ARTIFACT_CLOSURE"
MAX_MANIFEST_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ROOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.whl$")
_SAFE_ARTIFACT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SAFE_ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")
_SAFE_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9._+/-]{1,512}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "manifest_kind",
        "playwright_package",
        "playwright_package_version",
        "playwright_wheel_filename",
        "playwright_wheel_size",
        "playwright_wheel_sha256",
        "platform",
        "cache_root",
        "artifact_count",
        "artifacts",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_name",
        "browser_family",
        "revision",
        "browser_version",
        "platform",
        "archive_filename",
        "archive_size",
        "archive_sha256",
        "archive_format",
        "layout_kind",
        "archive_root",
        "expected_executable_relative_path",
        "executable_mode_required",
        "source_kind",
        "source_reference_sha256",
    }
)


class ArtifactContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class ArchiveEntry:
    name: str
    is_directory: bool
    mode: int
    size: int
    source: object


LAYOUT_EXACT_ROOT_FILE_SET = "EXACT_ROOT_FILE_SET"


@dataclass(frozen=True)
class ExactRootFileSetProfile:
    layout_kind: str
    revision: str
    required_member_names: frozenset[str]
    designated_executable: str
    required_member_modes: tuple[tuple[str, int], ...]


# Revision remains authoritative from the SHA-verified wheel metadata.
_EXACT_ROOT_FILE_SET_MEMBER_POLICIES = {
    (
        "playwright",
        "1.61.0",
        "debian13-x64",
        "ffmpeg",
        "ffmpeg-linux.zip",
    ): (
        ("COPYING.LGPLv2.1", 0o644),
        ("ffmpeg-linux", 0o755),
    ),
}


@dataclass(frozen=True)
class ValidatedArchive:
    artifact: ArtifactContract
    manifest: dict[str, object]
    path: Path


def _fail(code: str) -> None:
    raise ArtifactContractError(code)


def canonical_json(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _is_exact_type(value: object, expected: type[object]) -> bool:
    return type(value) is expected



def _validate_artifact_shape(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != _ARTIFACT_FIELDS:
        _fail("ARTIFACT_MANIFEST_FIELDS_INVALID")
    exact_types: dict[str, type[object]] = {
        "artifact_name": str,
        "browser_family": str,
        "revision": str,
        "platform": str,
        "archive_filename": str,
        "archive_size": int,
        "archive_sha256": str,
        "archive_format": str,
        "layout_kind": str,
        "archive_root": str,
        "expected_executable_relative_path": str,
        "executable_mode_required": bool,
        "source_kind": str,
        "source_reference_sha256": str,
    }
    for name, expected_type in exact_types.items():
        if not _is_exact_type(document[name], expected_type):
            _fail("ARTIFACT_MANIFEST_FIELD_TYPE_INVALID")
    browser_version = document["browser_version"]
    if browser_version is not None and (
        not isinstance(browser_version, str) or not browser_version
    ):
        _fail("ARTIFACT_BROWSER_VERSION_INVALID")
    artifact_name = str(document["artifact_name"])
    browser_family = str(document["browser_family"])
    if (
        _SAFE_ARTIFACT_RE.fullmatch(artifact_name) is None
        or browser_family != artifact_name
    ):
        _fail("ARTIFACT_NAME_INVALID")
    revision = str(document["revision"])
    if not revision.isascii() or not revision.isdigit():
        _fail("ARTIFACT_REVISION_INVALID")
    archive_filename = str(document["archive_filename"])
    if (
        _SAFE_ARCHIVE_RE.fullmatch(archive_filename) is None
        or "latest" in archive_filename.lower()
    ):
        _fail("ARCHIVE_FILENAME_INVALID")
    archive_size = document["archive_size"]
    if not isinstance(archive_size, int) or not 0 < archive_size <= MAX_ARCHIVE_BYTES:
        _fail("ARCHIVE_SIZE_INVALID")
    _validate_lower_sha256(document["archive_sha256"], "ARCHIVE_SHA256_INVALID")
    if document["archive_format"] not in {"zip", "tar.gz"}:
        _fail("ARCHIVE_FORMAT_INVALID")
    if document["layout_kind"] not in {
        LAYOUT_DIRECTORY_TREE,
        LAYOUT_SINGLE_EXECUTABLE_FILE,
    }:
        _fail("LAYOUT_KIND_INVALID")
    archive_root = str(document["archive_root"])
    if (
        _SAFE_ROOT_RE.fullmatch(archive_root) is None
        or archive_root.endswith(".")
    ):
        _fail("ARCHIVE_ROOT_INVALID")
    executable = str(document["expected_executable_relative_path"])
    if (
        _SAFE_EXECUTABLE_RE.fullmatch(executable) is None
        or executable.startswith("/")
        or ".." in executable.split("/")
    ):
        _fail("EXPECTED_EXECUTABLE_PATH_INVALID")
    if document["executable_mode_required"] is not True:
        _fail("EXECUTABLE_MODE_POLICY_INVALID")
    if document["source_kind"] != "operator-approved-offline-artifact":
        _fail("SOURCE_KIND_INVALID")
    _validate_lower_sha256(
        document["source_reference_sha256"],
        "SOURCE_REFERENCE_SHA256_INVALID",
    )
    return document


def _validate_manifest_shape(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != _MANIFEST_FIELDS:
        _fail("MANIFEST_FIELDS_INVALID")
    exact_types: dict[str, type[object]] = {
        "manifest_version": int,
        "manifest_kind": str,
        "playwright_package": str,
        "playwright_package_version": str,
        "playwright_wheel_filename": str,
        "playwright_wheel_size": int,
        "playwright_wheel_sha256": str,
        "platform": str,
        "cache_root": str,
        "artifact_count": int,
        "artifacts": list,
    }
    for name, expected_type in exact_types.items():
        if not _is_exact_type(document[name], expected_type):
            _fail("MANIFEST_FIELD_TYPE_INVALID")
    if document["manifest_version"] != MANIFEST_VERSION:
        _fail("MANIFEST_VERSION_INVALID")
    if document["manifest_kind"] != MANIFEST_KIND:
        _fail("MANIFEST_KIND_INVALID")
    wheel_filename = str(document["playwright_wheel_filename"])
    if _SAFE_WHEEL_RE.fullmatch(wheel_filename) is None:
        _fail("WHEEL_FILENAME_INVALID")
    wheel_size = document["playwright_wheel_size"]
    if not isinstance(wheel_size, int) or wheel_size <= 0:
        _fail("WHEEL_SIZE_INVALID")
    _validate_lower_sha256(
        document["playwright_wheel_sha256"], "WHEEL_SHA256_INVALID"
    )
    artifact_count = document["artifact_count"]
    artifacts = document["artifacts"]
    if (
        not isinstance(artifact_count, int)
        or not isinstance(artifacts, list)
        or not 0 < artifact_count <= 16
        or len(artifacts) != artifact_count
    ):
        _fail("ARTIFACT_COUNT_INVALID")
    validated_artifacts = [_validate_artifact_shape(item) for item in artifacts]
    names = [str(item["artifact_name"]) for item in validated_artifacts]
    revisions = [str(item["revision"]) for item in validated_artifacts]
    if names != sorted(names):
        _fail("ARTIFACT_ORDER_NONCANONICAL")
    if len(set(names)) != len(names):
        _fail("DUPLICATE_ARTIFACT_NAME")
    if len(set(revisions)) != len(revisions):
        _fail("DUPLICATE_ARTIFACT_REVISION")
    return document


def parse_canonical_closure_manifest(data: bytes) -> dict[str, object]:
    if not data or len(data) > MAX_MANIFEST_BYTES:
        _fail("MANIFEST_SIZE_INVALID")
    try:
        text = data.decode("ascii")
        document = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (_DuplicateJsonKey, UnicodeError, json.JSONDecodeError):
        _fail("MANIFEST_JSON_INVALID")
    validated = _validate_manifest_shape(document)
    if canonical_json(validated) != data:
        _fail("MANIFEST_NOT_CANONICAL")
    return validated


def _validate_lower_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _fail(code)
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        _fail("FILE_HASH_FAILED")
    return digest.hexdigest()


def _regular_file_metadata(path: Path, code: str) -> os.stat_result:
    try:
        file_metadata = path.lstat()
    except OSError:
        _fail(code)
    if (
        path.is_symlink()
        or not stat.S_ISREG(file_metadata.st_mode)
        or file_metadata.st_nlink != 1
    ):
        _fail(code)
    return file_metadata



def load_verified_closure_manifest(
    path: Path, expected_sha256: str
) -> dict[str, object]:
    expected = _validate_lower_sha256(
        expected_sha256, "EXPECTED_CLOSURE_MANIFEST_SHA256_INVALID"
    )
    file_metadata = _regular_file_metadata(path, "CLOSURE_MANIFEST_FILE_INVALID")
    if not 0 < file_metadata.st_size <= MAX_MANIFEST_BYTES:
        _fail("MANIFEST_SIZE_INVALID")
    try:
        data = path.read_bytes()
    except OSError:
        _fail("MANIFEST_READ_FAILED")
    if hashlib.sha256(data).hexdigest() != expected:
        _fail("CLOSURE_MANIFEST_SHA256_MISMATCH")
    return parse_canonical_closure_manifest(data)


def _artifact_documents(
    document: dict[str, object],
) -> dict[str, dict[str, object]]:
    artifacts = document["artifacts"]
    if not isinstance(artifacts, list):
        _fail("ARTIFACT_COUNT_INVALID")
    result: dict[str, dict[str, object]] = {}
    for item in artifacts:
        if not isinstance(item, dict):
            _fail("ARTIFACT_MANIFEST_FIELDS_INVALID")
        name = str(item["artifact_name"])
        if name in result:
            _fail("DUPLICATE_ARTIFACT_NAME")
        result[name] = item
    return result


def validate_closure_manifest_contract(
    document: dict[str, object], verified: VerifiedPlaywrightClosure
) -> None:
    closure = verified.closure
    wheel = verified.wheel
    exact_top_level = {
        "playwright_package": closure.package,
        "playwright_package_version": closure.package_version,
        "playwright_wheel_filename": wheel.filename,
        "playwright_wheel_size": wheel.size,
        "playwright_wheel_sha256": wheel.sha256,
        "platform": closure.platform,
        "cache_root": closure.cache_root,
    }
    for field, expected in exact_top_level.items():
        if document[field] != expected:
            _fail("CLOSURE_IDENTITY_MISMATCH")
    manifest_artifacts = _artifact_documents(document)
    if set(manifest_artifacts) != set(closure.artifact_names):
        _fail("ARTIFACT_SET_MISMATCH")
    for artifact in closure.artifacts:
        item = manifest_artifacts[artifact.artifact_name]
        exact_artifact = {
            "artifact_name": artifact.artifact_name,
            "browser_family": artifact.browser_family,
            "revision": artifact.revision,
            "browser_version": artifact.browser_version,
            "platform": artifact.platform,
            "archive_filename": artifact.expected_archive_filename,
            "layout_kind": artifact.layout_kind,
            "archive_root": artifact.archive_root,
            "expected_executable_relative_path": (
                artifact.expected_executable_relative_path
            ),
            "executable_mode_required": True,
        }
        for field, expected in exact_artifact.items():
            if item[field] != expected:
                _fail("ARTIFACT_IDENTITY_MISMATCH")


def _safe_member_name(raw_name: str, *, is_directory: bool) -> str:
    if not raw_name or "\x00" in raw_name or "\\" in raw_name:
        _fail("ARCHIVE_PATH_INVALID")
    name = raw_name[:-1] if is_directory and raw_name.endswith("/") else raw_name
    if (
        not name
        or len(name) > 4096
        or name.startswith("/")
        or _WINDOWS_DRIVE_RE.match(name)
        or any(ord(character) < 32 for character in name)
    ):
        _fail("ARCHIVE_ABSOLUTE_OR_INVALID_PATH")
    if unicodedata.normalize("NFC", name) != name:
        _fail("ARCHIVE_PATH_NORMALIZATION_AMBIGUOUS")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        _fail("ARCHIVE_PATH_TRAVERSAL")
    if any(part.endswith((".", " ")) for part in parts):
        _fail("ARCHIVE_PATH_NORMALIZATION_AMBIGUOUS")
    if len(parts) > 128:
        _fail("ARCHIVE_PATH_INVALID")
    return "/".join(parts)


def _validate_mode(mode: int, *, is_directory: bool) -> int:
    permissions = stat.S_IMODE(mode)
    if permissions & 0o7000 or permissions & 0o022:
        _fail("ARCHIVE_MODE_UNSAFE")
    if permissions == 0:
        _fail("ARCHIVE_MODE_METADATA_INVALID")
    return permissions


def _exact_root_file_set_profile(
    artifact: ArtifactContract,
) -> ExactRootFileSetProfile | None:
    try:
        host_platform = ARTIFACT_PLATFORM_MAPPINGS[artifact.artifact_name][
            artifact.platform
        ].playwright_host_platform
    except KeyError:
        return None
    required_member_modes = _EXACT_ROOT_FILE_SET_MEMBER_POLICIES.get(
        (
            artifact.package,
            artifact.package_version,
            host_platform,
            artifact.artifact_name,
            artifact.expected_archive_filename,
        )
    )
    if required_member_modes is None:
        return None
    return ExactRootFileSetProfile(
        layout_kind=LAYOUT_EXACT_ROOT_FILE_SET,
        revision=artifact.revision,
        required_member_names=frozenset(
            name for name, _mode in required_member_modes
        ),
        designated_executable=artifact.expected_executable_relative_path,
        required_member_modes=required_member_modes,
    )


def _validate_exact_root_file_set(
    entries: list[ArchiveEntry],
    artifact: ArtifactContract,
    profile: ExactRootFileSetProfile,
) -> None:
    if (
        profile.layout_kind != LAYOUT_EXACT_ROOT_FILE_SET
        or profile.revision != artifact.revision
    ):
        _fail("EXACT_ROOT_FILE_SET_INVALID")
    expected_modes = dict(profile.required_member_modes)
    if frozenset(expected_modes) != profile.required_member_names:
        _fail("EXACT_ROOT_FILE_SET_INVALID")
    names = frozenset(entry.name for entry in entries)
    if (
        len(entries) != len(profile.required_member_names)
        or names != profile.required_member_names
    ):
        _fail("EXACT_ROOT_FILE_SET_INVALID")
    if any(entry.is_directory or "/" in entry.name for entry in entries):
        _fail("EXACT_ROOT_FILE_SET_INVALID")
    companions = [
        entry
        for entry in entries
        if entry.name != profile.designated_executable
    ]
    if any(entry.mode & 0o111 for entry in companions):
        _fail("EXACT_ROOT_FILE_SET_COMPANION_EXECUTABLE")
    if any(entry.mode != expected_modes[entry.name] for entry in entries):
        _fail("EXACT_ROOT_FILE_SET_MODE_INVALID")



def _validate_entry_set(
    entries: list[ArchiveEntry], artifact: ArtifactContract
) -> None:
    if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
        _fail("ARCHIVE_ENTRY_COUNT_INVALID")
    names: set[str] = set()
    casefold_names: set[str] = set()
    files: set[str] = set()
    expanded_size = 0
    for entry in entries:
        if entry.name in names:
            _fail("ARCHIVE_DUPLICATE_PATH")
        names.add(entry.name)
        folded = entry.name.casefold()
        if folded in casefold_names:
            _fail("ARCHIVE_CASE_COLLISION")
        casefold_names.add(folded)
        if not entry.is_directory:
            files.add(entry.name)
            expanded_size += entry.size
            if expanded_size > MAX_EXPANDED_BYTES:
                _fail("ARCHIVE_EXPANDED_SIZE_INVALID")
    for name in names:
        parts = name.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                _fail("ARCHIVE_PATH_CONFLICT")

    expected_executable = artifact.expected_executable_relative_path
    executable_entries = [
        entry for entry in entries if entry.name == expected_executable
    ]
    if len(executable_entries) != 1 or executable_entries[0].is_directory:
        _fail("EXPECTED_EXECUTABLE_MISSING")
    if executable_entries[0].mode & 0o111 == 0:
        _fail("EXPECTED_EXECUTABLE_INVALID")

    if artifact.layout_kind == LAYOUT_DIRECTORY_TREE:
        top_level = {entry.name.split("/", 1)[0] for entry in entries}
        if top_level != {artifact.archive_root}:
            _fail("ARCHIVE_UNEXPECTED_TOP_LEVEL")
        root_entries = [entry for entry in entries if entry.name == artifact.archive_root]
        if root_entries and any(not entry.is_directory for entry in root_entries):
            _fail("ARCHIVE_ROOT_NOT_DIRECTORY")
        if not expected_executable.startswith(f"{artifact.archive_root}/"):
            _fail("EXPECTED_EXECUTABLE_OUTSIDE_ARCHIVE_ROOT")
        return
    if artifact.layout_kind == LAYOUT_SINGLE_EXECUTABLE_FILE:
        profile = _exact_root_file_set_profile(artifact)
        if profile is not None:
            if (
                artifact.archive_root != expected_executable
                or expected_executable != profile.designated_executable
                or "/" in expected_executable
            ):
                _fail("EXACT_ROOT_FILE_SET_INVALID")
            _validate_exact_root_file_set(entries, artifact, profile)
            return
        if (
            len(entries) != 1
            or entries[0].is_directory
            or entries[0].name != artifact.archive_root
            or artifact.archive_root != expected_executable
            or "/" in expected_executable
        ):
            _fail("SINGLE_EXECUTABLE_LAYOUT_INVALID")
        return
    _fail("LAYOUT_KIND_INVALID")


def _zip_entries(
    archive: zipfile.ZipFile, artifact: ArtifactContract
) -> list[ArchiveEntry]:
    entries: list[ArchiveEntry] = []
    try:
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile):
        _fail("ARCHIVE_INVALID")
    for info in infos:
        if info.flag_bits & 0x1:
            _fail("ARCHIVE_ENCRYPTED")
        if info.create_system != 3:
            _fail("ARCHIVE_UNIX_MODE_METADATA_INVALID")
        raw_mode = info.external_attr >> 16
        file_type = stat.S_IFMT(raw_mode)
        if file_type == 0 or stat.S_IMODE(raw_mode) == 0:
            _fail("ARCHIVE_UNIX_MODE_METADATA_INVALID")
        is_directory = info.is_dir()
        if file_type == stat.S_IFLNK:
            _fail("ARCHIVE_SYMLINK_DENIED")
        if file_type == stat.S_IFIFO:
            _fail("ARCHIVE_FIFO_DENIED")
        if file_type in {stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK}:
            _fail("ARCHIVE_DEVICE_DENIED")
        allowed_type = stat.S_IFDIR if is_directory else stat.S_IFREG
        if file_type not in {0, allowed_type}:
            _fail("ARCHIVE_ENTRY_TYPE_DENIED")
        name = _safe_member_name(info.filename, is_directory=is_directory)
        entries.append(
            ArchiveEntry(
                name=name,
                is_directory=is_directory,
                mode=_validate_mode(raw_mode, is_directory=is_directory),
                size=0 if is_directory else info.file_size,
                source=info,
            )
        )
    _validate_entry_set(entries, artifact)
    return entries


def _tar_entries(
    archive: tarfile.TarFile, artifact: ArtifactContract
) -> list[ArchiveEntry]:
    entries: list[ArchiveEntry] = []
    try:
        members = archive.getmembers()
    except (OSError, tarfile.TarError):
        _fail("ARCHIVE_INVALID")
    for member in members:
        if member.issym():
            _fail("ARCHIVE_SYMLINK_DENIED")
        if member.islnk():
            _fail("ARCHIVE_HARDLINK_DENIED")
        if member.isfifo():
            _fail("ARCHIVE_FIFO_DENIED")
        if member.isdev():
            _fail("ARCHIVE_DEVICE_DENIED")
        if getattr(member, "sparse", None):
            _fail("ARCHIVE_SPARSE_ENTRY_DENIED")
        if not (member.isdir() or member.isreg()):
            _fail("ARCHIVE_ENTRY_TYPE_DENIED")
        name = _safe_member_name(member.name, is_directory=member.isdir())
        entries.append(
            ArchiveEntry(
                name=name,
                is_directory=member.isdir(),
                mode=_validate_mode(member.mode, is_directory=member.isdir()),
                size=0 if member.isdir() else member.size,
                source=member,
            )
        )
    _validate_entry_set(entries, artifact)
    return entries


def _fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            file_metadata = os.fstat(descriptor)
            if not stat.S_ISREG(file_metadata.st_mode):
                _fail("FILE_FSYNC_FAILED")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except ArtifactContractError:
        raise
    except OSError:
        _fail("FILE_FSYNC_FAILED")


def _fsync_directory(path: Path, code: str = "DIRECTORY_FSYNC_FAILED") -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            directory_metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(directory_metadata.st_mode):
                _fail(code)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except ArtifactContractError:
        raise
    except OSError:
        _fail(code)


def _copy_stream(source: BinaryIO, target: Path, expected_size: int) -> None:
    written = 0
    try:
        with target.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
                written += len(chunk)
                if written > expected_size:
                    _fail("ARCHIVE_MEMBER_SIZE_MISMATCH")
            output.flush()
    except ArtifactContractError:
        raise
    except OSError:
        _fail("ARCHIVE_EXTRACTION_FAILED")
    if written != expected_size:
        _fail("ARCHIVE_MEMBER_SIZE_MISMATCH")


def _extract_entries(
    entries: Iterable[ArchiveEntry],
    destination: Path,
    opener: Callable[[ArchiveEntry], BinaryIO | None],
) -> None:
    ordered = sorted(entries, key=lambda entry: (entry.name.count("/"), entry.name))
    for entry in ordered:
        target = destination.joinpath(*entry.name.split("/"))
        if entry.is_directory:
            try:
                target.mkdir(mode=entry.mode, parents=True, exist_ok=True)
                target.chmod(entry.mode)
            except OSError:
                _fail("ARCHIVE_EXTRACTION_FAILED")
            continue
        try:
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        except OSError:
            _fail("ARCHIVE_EXTRACTION_FAILED")
        source = opener(entry)
        if source is None:
            _fail("ARCHIVE_MEMBER_READ_FAILED")
        try:
            with source:
                _copy_stream(source, target, entry.size)
            target.chmod(entry.mode)
            _fsync_regular_file(target)
        except ArtifactContractError:
            raise
        except (OSError, zipfile.BadZipFile, tarfile.TarError):
            _fail("ARCHIVE_EXTRACTION_FAILED")



def _validate_archive_layout(
    archive_path: Path,
    archive_format: str,
    artifact: ArtifactContract,
) -> None:
    if archive_format == "zip":
        try:
            with zipfile.ZipFile(archive_path) as archive:
                _zip_entries(archive, artifact)
        except ArtifactContractError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError):
            _fail("ARCHIVE_INVALID")
        return
    if archive_format == "tar.gz":
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                _tar_entries(archive, artifact)
        except ArtifactContractError:
            raise
        except (OSError, tarfile.TarError):
            _fail("ARCHIVE_INVALID")
        return
    _fail("ARCHIVE_FORMAT_INVALID")


def _extract_archive(
    archive_path: Path,
    archive_format: str,
    artifact: ArtifactContract,
    destination: Path,
) -> None:
    if archive_format == "zip":
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = _zip_entries(archive, artifact)
                _extract_entries(
                    entries,
                    destination,
                    lambda entry: archive.open(entry.source),
                )
        except ArtifactContractError:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError):
            _fail("ARCHIVE_INVALID")
        return
    if archive_format == "tar.gz":
        try:
            with tarfile.open(archive_path, mode="r:gz") as archive:
                entries = _tar_entries(archive, artifact)
                _extract_entries(
                    entries,
                    destination,
                    lambda entry: archive.extractfile(entry.source),
                )
        except ArtifactContractError:
            raise
        except (OSError, tarfile.TarError):
            _fail("ARCHIVE_INVALID")
        return
    _fail("ARCHIVE_FORMAT_INVALID")


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            path_metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("CACHE_PATH_METADATA_FAILED")
        if stat.S_ISLNK(path_metadata.st_mode):
            _fail("CACHE_PATH_SYMLINK_DENIED")



def _validate_cache_parent(cache_root: Path) -> Path:
    if not cache_root.is_absolute():
        _fail("CACHE_ROOT_NOT_ABSOLUTE")
    _assert_no_symlink_components(cache_root)
    parent = cache_root.parent
    try:
        parent_metadata = parent.lstat()
    except OSError:
        _fail("CACHE_ROOT_PARENT_INVALID")
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        _fail("CACHE_ROOT_PARENT_INVALID")
    return parent


def _validate_executable(root: Path, relative_path: str) -> None:
    executable = root.joinpath(*relative_path.split("/"))
    try:
        executable_metadata = executable.lstat()
    except OSError:
        _fail("EXPECTED_EXECUTABLE_MISSING")
    if (
        executable.is_symlink()
        or not stat.S_ISREG(executable_metadata.st_mode)
        or stat.S_IMODE(executable_metadata.st_mode) & 0o111 == 0
    ):
        _fail("EXPECTED_EXECUTABLE_INVALID")


def _write_identity_marker(path: Path, identity: bytes) -> None:
    try:
        with path.open("xb") as marker:
            marker.write(identity)
            marker.flush()
        path.chmod(0o444)
        _fsync_regular_file(path)
    except ArtifactContractError:
        raise
    except OSError:
        _fail("INSTALLATION_MARKER_WRITE_FAILED")


def _fsync_tree_directories(root: Path) -> None:
    directories = [root]
    try:
        for current, names, _files in os.walk(root, topdown=False):
            directories.extend(Path(current) / name for name in names)
    except OSError:
        _fail("DIRECTORY_FSYNC_FAILED")
    unique = sorted(set(directories), key=lambda item: len(item.parts), reverse=True)
    for directory in unique:
        _fsync_directory(directory)


def _artifact_cache_directories(
    closure: PlaywrightClosureContract,
) -> dict[str, str]:
    return {
        artifact.artifact_name: artifact.cache_directory
        for artifact in closure.artifacts
    }



def _current_owner_identity() -> tuple[int, int]:
    uid_provider = getattr(os, "geteuid", None)
    gid_provider = getattr(os, "getegid", None)
    if not callable(uid_provider) or not callable(gid_provider):
        _fail("OWNER_IDENTITY_UNSUPPORTED")
    return uid_provider(), gid_provider()


def _build_expected_identity(
    document: dict[str, object],
    expected_manifest_sha256: str,
    closure: PlaywrightClosureContract,
) -> dict[str, object]:
    manifest_artifacts = _artifact_documents(document)
    return build_expected_identity_document(
        closure_manifest_sha256=expected_manifest_sha256,
        playwright_package=closure.package,
        playwright_package_version=closure.package_version,
        platform=closure.platform,
        artifacts=[
            {
                "artifact_name": artifact.artifact_name,
                "revision": artifact.revision,
                "archive_sha256": manifest_artifacts[
                    artifact.artifact_name
                ]["archive_sha256"],
                "layout_kind": artifact.layout_kind,
                "expected_executable_relative_path": (
                    artifact.expected_executable_relative_path
                ),
            }
            for artifact in closure.artifacts
        ],
    )


def _revalidate_complete_cache(
    cache_root: Path,
    independent_identity: Path,
    closure: PlaywrightClosureContract,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    try:
        expected = read_expected_identity(
            independent_identity,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        verify_installed_closure(
            cache_root=cache_root,
            expected_identity=expected,
            artifact_cache_directories=_artifact_cache_directories(closure),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
    except InstalledClosureError as exc:
        _fail(exc.code)


def _make_tree_removable(root: Path) -> None:
    try:
        for current, directory_names, _file_names in os.walk(
            root, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            current_path.chmod(0o700)
            directory_names[:] = [
                name
                for name in directory_names
                if not (current_path / name).is_symlink()
            ]
    except OSError:
        _fail("FAILED_PUBLICATION_CLEANUP_FAILED")


def _discard_published_destination(destination: Path, parent: Path) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    holder: Path | None = None
    quarantine: Path | None = None
    try:
        holder = Path(tempfile.mkdtemp(prefix=".failed-publish.", dir=parent))
        quarantine = holder / "cache"
        destination.chmod(0o700)
        os.replace(destination, quarantine)
        _fsync_directory(parent)
        _make_tree_removable(quarantine)
        shutil.rmtree(quarantine)
        holder.rmdir()
        _fsync_directory(parent)
    except (OSError, ArtifactContractError):
        failed_root = (
            quarantine
            if quarantine is not None and quarantine.exists()
            else destination
        )
        try:
            if failed_root.exists() or failed_root.is_symlink():
                failed_root.chmod(0o700)
                marker = failed_root / "INSTALLATION_COMPLETE"
                if marker.exists() or marker.is_symlink():
                    marker.chmod(0o600)
                    marker.unlink()
                _make_tree_removable(failed_root)
                shutil.rmtree(failed_root)
            if holder is not None and holder.exists():
                holder.rmdir()
            _fsync_directory(parent)
        except (OSError, ArtifactContractError):
            try:
                marker = failed_root / "INSTALLATION_COMPLETE"
                marker.chmod(0o600)
                marker.unlink(missing_ok=True)
                _fsync_directory(parent)
            except OSError:
                pass


def _discard_expected_identity(path: Path, parent: Path) -> None:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            _fail("EXPECTED_IDENTITY_CLEANUP_FAILED")
        path.chmod(0o600)
        path.unlink()
        _fsync_directory(parent)
    except FileNotFoundError:
        return
    except ArtifactContractError:
        raise
    except OSError:
        _fail("EXPECTED_IDENTITY_CLEANUP_FAILED")


def _validate_artifact_context_layout(
    artifacts_root: Path,
    closure: PlaywrightClosureContract,
) -> None:
    if not artifacts_root.is_absolute():
        _fail("ARTIFACTS_ROOT_NOT_ABSOLUTE")
    _assert_no_symlink_components(artifacts_root)
    try:
        root_metadata = artifacts_root.lstat()
        children = {child.name for child in artifacts_root.iterdir()}
    except OSError:
        _fail("ARTIFACTS_ROOT_INVALID")
    if not stat.S_ISDIR(root_metadata.st_mode) or artifacts_root.is_symlink():
        _fail("ARTIFACTS_ROOT_INVALID")
    if children != set(closure.artifact_names):
        _fail("ARTIFACT_CONTEXT_SET_MISMATCH")
    for artifact in closure.artifacts:
        directory = artifacts_root / artifact.artifact_name
        try:
            directory_metadata = directory.lstat()
            artifact_children = {child.name for child in directory.iterdir()}
        except OSError:
            _fail("ARTIFACT_CONTEXT_ENTRY_INVALID")
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory.is_symlink()
            or artifact_children != {"archive"}
        ):
            _fail("ARTIFACT_CONTEXT_ENTRY_INVALID")


def _validate_all_archives(
    document: dict[str, object],
    artifacts_root: Path,
    verified: VerifiedPlaywrightClosure,
) -> tuple[ValidatedArchive, ...]:
    closure = verified.closure
    _validate_artifact_context_layout(artifacts_root, closure)
    manifest_artifacts = _artifact_documents(document)
    validated: list[ValidatedArchive] = []
    for artifact in closure.artifacts:
        item = manifest_artifacts[artifact.artifact_name]
        archive_path = artifacts_root / artifact.artifact_name / "archive"
        archive_metadata = _regular_file_metadata(
            archive_path, "ARCHIVE_FILE_INVALID"
        )
        if archive_metadata.st_size != item["archive_size"]:
            _fail("ARCHIVE_SIZE_MISMATCH")
        if _sha256_file(archive_path) != item["archive_sha256"]:
            _fail("ARCHIVE_SHA256_MISMATCH")
        _validate_archive_layout(
            archive_path,
            str(item["archive_format"]),
            artifact,
        )
        validated.append(
            ValidatedArchive(
                artifact=artifact,
                manifest=item,
                path=archive_path,
            )
        )
    return tuple(validated)



def install_closure(
    *,
    manifest_path: Path,
    artifacts_root: Path,
    expected_manifest_sha256: str,
    verified_closure: VerifiedPlaywrightClosure,
) -> Path:
    document = load_verified_closure_manifest(
        manifest_path, expected_manifest_sha256
    )
    validate_closure_manifest_contract(document, verified_closure)
    archives = _validate_all_archives(
        document,
        artifacts_root,
        verified_closure,
    )

    closure = verified_closure.closure
    cache_root = Path(closure.cache_root)
    parent = _validate_cache_parent(cache_root)
    owner_uid, owner_gid = _current_owner_identity()
    try:
        expected_document = _build_expected_identity(
            document,
            expected_manifest_sha256,
            closure,
        )
    except InstalledClosureError as exc:
        _fail(exc.code)
    expected_bytes = canonical_installed_json(expected_document)
    independent_identity = expected_identity_path(cache_root)
    if cache_root.exists() or cache_root.is_symlink():
        try:
            _revalidate_complete_cache(
                cache_root,
                independent_identity,
                closure,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        except ArtifactContractError:
            _fail("EXISTING_CACHE_INCOMPLETE_OR_MISMATCH")
        return cache_root
    if independent_identity.exists() or independent_identity.is_symlink():
        _fail("EXPECTED_IDENTITY_COLLISION")

    temporary: Path | None = None
    published = False
    expected_identity_published = False
    primary_failure = False
    try:
        try:
            temporary = Path(
                tempfile.mkdtemp(prefix=".playwright-closure.", dir=parent)
            )
            temporary.chmod(0o700)
            if temporary.stat().st_dev != parent.stat().st_dev:
                _fail("CACHE_FILESYSTEM_MISMATCH")
        except ArtifactContractError:
            raise
        except OSError:
            _fail("TEMP_DIRECTORY_CREATE_FAILED")

        artifact_roots: dict[str, Path] = {}
        for validated in archives:
            destination = temporary / validated.artifact.cache_directory
            try:
                destination.mkdir(mode=0o700)
            except OSError:
                _fail("ARTIFACT_STAGING_CREATE_FAILED")
            _extract_archive(
                validated.path,
                str(validated.manifest["archive_format"]),
                validated.artifact,
                destination,
            )
            _validate_executable(
                destination,
                validated.artifact.expected_executable_relative_path,
            )
            try:
                seal_installed_artifact_tree(
                    destination,
                    owner_uid=owner_uid,
                    owner_gid=owner_gid,
                )
            except InstalledClosureError as exc:
                _fail(exc.code)
            artifact_roots[validated.artifact.artifact_name] = destination

        try:
            marker_document = build_installed_marker_document(
                expected_identity=expected_document,
                artifact_roots=artifact_roots,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
        except InstalledClosureError as exc:
            _fail(exc.code)
        marker = temporary / "INSTALLATION_COMPLETE"
        if marker.exists() or marker.is_symlink():
            _fail("INSTALLATION_MARKER_COLLISION")
        _write_identity_marker(marker, canonical_installed_json(marker_document))
        staged_expected_identity = temporary / "EXPECTED_CLOSURE_IDENTITY"
        _write_identity_marker(staged_expected_identity, expected_bytes)
        _fsync_tree_directories(temporary)
        try:
            os.replace(staged_expected_identity, independent_identity)
        except OSError:
            _fail("EXPECTED_IDENTITY_PUBLISH_FAILED")
        expected_identity_published = True
        try:
            temporary.chmod(IMMUTABLE_DIRECTORY_MODE)
        except OSError:
            _fail("CACHE_PERMISSION_SEAL_FAILED")
        _fsync_directory(temporary)
        _fsync_directory(parent, "EXPECTED_IDENTITY_PARENT_FSYNC_FAILED")
        try:
            os.replace(temporary, cache_root)
        except OSError:
            _fail("ATOMIC_PUBLISH_FAILED")
        temporary = None
        published = True
        _fsync_directory(parent, "FINAL_PARENT_FSYNC_FAILED")
        _revalidate_complete_cache(
            cache_root,
            independent_identity,
            closure,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        return cache_root
    except BaseException:
        primary_failure = True
        if published:
            _discard_published_destination(cache_root, parent)
        if expected_identity_published:
            try:
                _discard_expected_identity(independent_identity, parent)
            except ArtifactContractError:
                pass
        raise
    finally:
        cleanup_error = False
        if temporary is not None:
            try:
                _make_tree_removable(temporary)
                shutil.rmtree(temporary)
                _fsync_directory(parent)
            except (OSError, ArtifactContractError):
                cleanup_error = True
        if cleanup_error and not primary_failure:
            _fail("TEMP_CLEANUP_FAILED")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install a verified offline Playwright artifact closure."
    )
    parser.add_argument("--closure-manifest", required=True, type=Path)
    parser.add_argument("--artifacts-root", required=True, type=Path)
    parser.add_argument("--lockfile", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--expected-closure-manifest-sha256", required=True)
    parser.add_argument(
        "--platform", required=True, choices=("linux/amd64", "linux/arm64")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verified = load_verified_wheel_closure(
            lockfile_path=args.lockfile,
            wheel_path=args.wheel,
            platform=args.platform,
        )
        install_closure(
            manifest_path=args.closure_manifest,
            artifacts_root=args.artifacts_root,
            expected_manifest_sha256=(
                args.expected_closure_manifest_sha256
            ),
            verified_closure=verified,
        )
    except (ArtifactContractError, PlaywrightContractError) as exc:
        print("PLAYWRIGHT_ARTIFACT_INSTALL=FAIL", file=sys.stderr)
        print(f"ERROR_CLASS={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("PLAYWRIGHT_ARTIFACT_INSTALL=FAIL", file=sys.stderr)
        print("ERROR_CLASS=INTERNAL_ERROR", file=sys.stderr)
        return 2

    closure = verified.closure
    print("PLAYWRIGHT_ARTIFACT_INSTALL=PASS")
    print(f"PLAYWRIGHT_PACKAGE={closure.package}")
    print(f"PLAYWRIGHT_PACKAGE_VERSION={closure.package_version}")
    print(f"ARTIFACT_COUNT={len(closure.artifacts)}")
    print(f"ARTIFACT_NAMES={','.join(closure.artifact_names)}")
    print(f"PLATFORM={closure.platform}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
