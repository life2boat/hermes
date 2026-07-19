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
    PlaywrightContractError,
    VerifiedBrowserContract,
    canonical_installation_identity,
    load_verified_wheel_contract,
)


MANIFEST_VERSION = 2
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100_000
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_ROOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}\.whl$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MANIFEST_FIELDS = frozenset(
    {
        "manifest_version",
        "playwright_package",
        "playwright_package_version",
        "playwright_wheel_filename",
        "playwright_wheel_size",
        "playwright_wheel_sha256",
        "browser_family",
        "browser_revision",
        "platform",
        "archive_filename",
        "archive_size",
        "archive_sha256",
        "archive_format",
        "archive_root",
        "cache_root",
        "expected_executable_relative_path",
        "source_kind",
        "source_reference",
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


def _validate_manifest_shape(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != _MANIFEST_FIELDS:
        _fail("MANIFEST_FIELDS_INVALID")
    expected_types: dict[str, type[object]] = {
        "manifest_version": int,
        "playwright_package": str,
        "playwright_package_version": str,
        "playwright_wheel_filename": str,
        "playwright_wheel_size": int,
        "playwright_wheel_sha256": str,
        "browser_family": str,
        "browser_revision": str,
        "platform": str,
        "archive_filename": str,
        "archive_size": int,
        "archive_sha256": str,
        "archive_format": str,
        "archive_root": str,
        "cache_root": str,
        "expected_executable_relative_path": str,
        "source_kind": str,
        "source_reference": str,
    }
    for name, expected_type in expected_types.items():
        if not _is_exact_type(document[name], expected_type):
            _fail("MANIFEST_FIELD_TYPE_INVALID")

    if document["manifest_version"] != MANIFEST_VERSION:
        _fail("MANIFEST_VERSION_INVALID")
    if document["archive_filename"] != "browser-archive":
        _fail("ARCHIVE_FILENAME_INVALID")
    archive_size = document["archive_size"]
    if not isinstance(archive_size, int) or not 0 < archive_size <= MAX_ARCHIVE_BYTES:
        _fail("ARCHIVE_SIZE_INVALID")
    archive_sha = document["archive_sha256"]
    if not isinstance(archive_sha, str) or _SHA256_RE.fullmatch(archive_sha) is None:
        _fail("ARCHIVE_SHA256_INVALID")
    wheel_filename = document["playwright_wheel_filename"]
    if (
        not isinstance(wheel_filename, str)
        or _SAFE_WHEEL_RE.fullmatch(wheel_filename) is None
    ):
        _fail("WHEEL_FILENAME_INVALID")
    wheel_size = document["playwright_wheel_size"]
    if not isinstance(wheel_size, int) or wheel_size <= 0:
        _fail("WHEEL_SIZE_INVALID")
    wheel_sha = document["playwright_wheel_sha256"]
    if not isinstance(wheel_sha, str) or _SHA256_RE.fullmatch(wheel_sha) is None:
        _fail("WHEEL_SHA256_INVALID")
    if document["archive_format"] not in {"zip", "tar.gz"}:
        _fail("ARCHIVE_FORMAT_INVALID")
    archive_root = document["archive_root"]
    if (
        not isinstance(archive_root, str)
        or _SAFE_ROOT_RE.fullmatch(archive_root) is None
        or archive_root.endswith(".")
    ):
        _fail("ARCHIVE_ROOT_INVALID")
    if document["source_kind"] != "operator-approved-offline-artifact":
        _fail("SOURCE_KIND_INVALID")
    source_reference = document["source_reference"]
    if (
        not isinstance(source_reference, str)
        or _SAFE_REFERENCE_RE.fullmatch(source_reference) is None
        or "latest" in source_reference.lower()
    ):
        _fail("SOURCE_REFERENCE_INVALID")
    return document


def parse_canonical_manifest(data: bytes) -> dict[str, object]:
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


def load_verified_manifest(path: Path, expected_sha256: str) -> dict[str, object]:
    expected = _validate_lower_sha256(
        expected_sha256, "EXPECTED_MANIFEST_SHA256_INVALID"
    )
    file_metadata = _regular_file_metadata(path, "MANIFEST_FILE_INVALID")
    if not 0 < file_metadata.st_size <= MAX_MANIFEST_BYTES:
        _fail("MANIFEST_SIZE_INVALID")
    try:
        data = path.read_bytes()
    except OSError:
        _fail("MANIFEST_READ_FAILED")
    if hashlib.sha256(data).hexdigest() != expected:
        _fail("MANIFEST_SHA256_MISMATCH")
    return parse_canonical_manifest(data)


def validate_manifest_contract(
    document: dict[str, object], verified: VerifiedBrowserContract
) -> None:
    contract = verified.browser
    wheel = verified.wheel
    exact_matches = {
        "playwright_package": contract.package,
        "playwright_package_version": contract.package_version,
        "playwright_wheel_filename": wheel.filename,
        "playwright_wheel_size": wheel.size,
        "playwright_wheel_sha256": wheel.sha256,
        "browser_family": contract.browser_family,
        "browser_revision": contract.browser_revision,
        "platform": contract.platform,
        "archive_root": contract.archive_root,
        "cache_root": contract.cache_root,
        "expected_executable_relative_path": (
            contract.expected_executable_relative_path
        ),
    }
    error_codes = {
        "playwright_package": "PACKAGE_NAME_MISMATCH",
        "playwright_package_version": "PACKAGE_VERSION_MISMATCH",
        "playwright_wheel_filename": "WHEEL_FILENAME_MISMATCH",
        "playwright_wheel_size": "WHEEL_SIZE_MISMATCH",
        "playwright_wheel_sha256": "WHEEL_SHA256_MISMATCH",
        "browser_family": "BROWSER_FAMILY_MISMATCH",
        "browser_revision": "BROWSER_REVISION_MISMATCH",
        "platform": "PLATFORM_MISMATCH",
        "archive_root": "ARCHIVE_ROOT_MISMATCH",
        "cache_root": "CACHE_ROOT_MISMATCH",
        "expected_executable_relative_path": "EXECUTABLE_PATH_MISMATCH",
    }
    for name, expected in exact_matches.items():
        if document[name] != expected:
            _fail(error_codes[name])
    executable = str(document["expected_executable_relative_path"])
    archive_root = str(document["archive_root"])
    if not executable.startswith(f"{archive_root}/"):
        _fail("EXPECTED_EXECUTABLE_OUTSIDE_ARCHIVE_ROOT")


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
        permissions = 0o755 if is_directory else 0o644
    return permissions


def _validate_entry_set(entries: list[ArchiveEntry], archive_root: str) -> None:
    if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
        _fail("ARCHIVE_ENTRY_COUNT_INVALID")
    names: set[str] = set()
    casefold_names: set[str] = set()
    files: set[str] = set()
    top_level: set[str] = set()
    expanded_size = 0
    for entry in entries:
        if entry.name in names:
            _fail("ARCHIVE_DUPLICATE_PATH")
        names.add(entry.name)
        folded = entry.name.casefold()
        if folded in casefold_names:
            _fail("ARCHIVE_CASE_COLLISION")
        casefold_names.add(folded)
        top_level.add(entry.name.split("/", 1)[0])
        if not (
            entry.name == archive_root
            or entry.name.startswith(f"{archive_root}/")
        ):
            _fail("ARCHIVE_UNEXPECTED_TOP_LEVEL")
        if entry.name == archive_root and not entry.is_directory:
            _fail("ARCHIVE_ROOT_NOT_DIRECTORY")
        if not entry.is_directory:
            files.add(entry.name)
            expanded_size += entry.size
            if expanded_size > MAX_EXPANDED_BYTES:
                _fail("ARCHIVE_EXPANDED_SIZE_INVALID")
    if top_level != {archive_root}:
        _fail("ARCHIVE_UNEXPECTED_TOP_LEVEL")
    for name in names:
        parts = name.split("/")
        for index in range(1, len(parts)):
            if "/".join(parts[:index]) in files:
                _fail("ARCHIVE_PATH_CONFLICT")


def _zip_entries(
    archive: zipfile.ZipFile, archive_root: str
) -> list[ArchiveEntry]:
    entries: list[ArchiveEntry] = []
    try:
        infos = archive.infolist()
    except (OSError, zipfile.BadZipFile):
        _fail("ARCHIVE_INVALID")
    for info in infos:
        if info.flag_bits & 0x1:
            _fail("ARCHIVE_ENCRYPTED")
        raw_mode = info.external_attr >> 16
        file_type = stat.S_IFMT(raw_mode)
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
    _validate_entry_set(entries, archive_root)
    return entries


def _tar_entries(
    archive: tarfile.TarFile, archive_root: str
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
    _validate_entry_set(entries, archive_root)
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


def _extract_archive(
    archive_path: Path,
    archive_format: str,
    archive_root: str,
    destination: Path,
) -> None:
    if archive_format == "zip":
        try:
            with zipfile.ZipFile(archive_path) as archive:
                entries = _zip_entries(archive, archive_root)
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
                entries = _tar_entries(archive, archive_root)
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


def _prepare_cache_root(cache_root: Path) -> bool:
    if not cache_root.is_absolute():
        _fail("CACHE_ROOT_NOT_ABSOLUTE")
    _assert_no_symlink_components(cache_root)
    try:
        root_metadata = cache_root.lstat()
    except FileNotFoundError:
        try:
            parent_metadata = cache_root.parent.lstat()
            if not stat.S_ISDIR(parent_metadata.st_mode) or stat.S_ISLNK(
                parent_metadata.st_mode
            ):
                _fail("CACHE_ROOT_PARENT_INVALID")
            cache_root.mkdir(mode=0o755)
            _fsync_directory(cache_root.parent)
            return True
        except ArtifactContractError:
            raise
        except OSError:
            _fail("CACHE_ROOT_CREATE_FAILED")
    except OSError:
        _fail("CACHE_ROOT_METADATA_FAILED")
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        _fail("CACHE_ROOT_INVALID")
    if stat.S_IMODE(root_metadata.st_mode) & 0o022:
        _fail("CACHE_ROOT_WRITABLE_BY_OTHERS")
    return False


def _remove_cache_root_if_empty(cache_root: Path, created: bool) -> None:
    if not created:
        return
    try:
        cache_root.rmdir()
        _fsync_directory(cache_root.parent)
    except FileNotFoundError:
        return
    except ArtifactContractError:
        raise
    except OSError:
        _fail("CACHE_ROOT_CLEANUP_FAILED")


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


def _read_identity_marker(path: Path) -> bytes:
    file_metadata = _regular_file_metadata(path, "PUBLISHED_CACHE_IDENTITY_MISMATCH")
    if not 0 < file_metadata.st_size <= 4096:
        _fail("PUBLISHED_CACHE_IDENTITY_MISMATCH")
    try:
        return path.read_bytes()
    except OSError:
        _fail("PUBLISHED_CACHE_IDENTITY_MISMATCH")


def _revalidate_published_cache(
    destination: Path,
    identity: bytes,
    executable_relative_path: str,
) -> None:
    try:
        destination_metadata = destination.lstat()
    except OSError:
        _fail("PUBLISHED_CACHE_IDENTITY_MISMATCH")
    if not stat.S_ISDIR(destination_metadata.st_mode) or destination.is_symlink():
        _fail("PUBLISHED_CACHE_IDENTITY_MISMATCH")
    marker = destination / "INSTALLATION_COMPLETE"
    if _read_identity_marker(marker) != identity:
        _fail("PUBLISHED_CACHE_IDENTITY_MISMATCH")
    _validate_executable(destination, executable_relative_path)


def _discard_published_destination(
    destination: Path, cache_root: Path
) -> None:
    if not destination.exists() and not destination.is_symlink():
        return
    holder: Path | None = None
    quarantine: Path | None = None
    try:
        holder = Path(tempfile.mkdtemp(prefix=".failed-publish.", dir=cache_root))
        quarantine = holder / "cache"
        os.replace(destination, quarantine)
        _fsync_directory(cache_root)
    except (OSError, ArtifactContractError):
        try:
            shutil.rmtree(destination)
            _fsync_directory(cache_root)
        except (OSError, ArtifactContractError):
            return
    if holder is not None:
        try:
            shutil.rmtree(holder)
            _fsync_directory(cache_root)
        except (OSError, ArtifactContractError):
            if quarantine is not None:
                try:
                    (quarantine / "INSTALLATION_COMPLETE").unlink(missing_ok=True)
                except OSError:
                    pass


def install_artifact(
    *,
    manifest_path: Path,
    archive_path: Path,
    expected_manifest_sha256: str,
    verified_contract: VerifiedBrowserContract,
) -> Path:
    document = load_verified_manifest(manifest_path, expected_manifest_sha256)
    validate_manifest_contract(document, verified_contract)

    archive_metadata = _regular_file_metadata(
        archive_path, "ARCHIVE_FILE_INVALID"
    )
    if archive_path.name != document["archive_filename"]:
        _fail("ARCHIVE_FILENAME_MISMATCH")
    if archive_metadata.st_size != document["archive_size"]:
        _fail("ARCHIVE_SIZE_MISMATCH")
    if _sha256_file(archive_path) != document["archive_sha256"]:
        _fail("ARCHIVE_SHA256_MISMATCH")

    contract = verified_contract.browser
    cache_root = Path(contract.cache_root)
    destination = cache_root / contract.cache_directory
    if destination.exists() or destination.is_symlink():
        _fail("TARGET_CACHE_ALREADY_EXISTS")

    cache_root_created = _prepare_cache_root(cache_root)
    temporary: Path | None = None
    published = False
    primary_failure = False
    identity = canonical_installation_identity(verified_contract)
    try:
        try:
            temporary = Path(
                tempfile.mkdtemp(
                    prefix=f".{contract.cache_directory}.",
                    dir=cache_root,
                )
            )
            temporary.chmod(0o700)
            if temporary.stat().st_dev != cache_root.stat().st_dev:
                _fail("CACHE_FILESYSTEM_MISMATCH")
        except ArtifactContractError:
            raise
        except OSError:
            _fail("TEMP_DIRECTORY_CREATE_FAILED")

        _extract_archive(
            archive_path,
            str(document["archive_format"]),
            str(document["archive_root"]),
            temporary,
        )
        _validate_executable(
            temporary,
            contract.expected_executable_relative_path,
        )
        marker = temporary / "INSTALLATION_COMPLETE"
        if marker.exists() or marker.is_symlink():
            _fail("INSTALLATION_MARKER_COLLISION")
        _write_identity_marker(marker, identity)
        _fsync_tree_directories(temporary)
        try:
            os.replace(temporary, destination)
        except OSError:
            _fail("ATOMIC_PUBLISH_FAILED")
        temporary = None
        published = True
        _fsync_directory(cache_root, "FINAL_PARENT_FSYNC_FAILED")
        _revalidate_published_cache(
            destination,
            identity,
            contract.expected_executable_relative_path,
        )
        return destination
    except BaseException:
        primary_failure = True
        if published:
            _discard_published_destination(destination, cache_root)
        raise
    finally:
        cleanup_error = False
        if temporary is not None:
            try:
                shutil.rmtree(temporary)
                _fsync_directory(cache_root)
            except (OSError, ArtifactContractError):
                cleanup_error = True
        if primary_failure:
            try:
                _remove_cache_root_if_empty(cache_root, cache_root_created)
            except ArtifactContractError:
                cleanup_error = True
        if cleanup_error and not primary_failure:
            _fail("TEMP_CLEANUP_FAILED")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install a verified offline Playwright browser artifact."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--lockfile", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument(
        "--platform", required=True, choices=("linux/amd64", "linux/arm64")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verified = load_verified_wheel_contract(
            lockfile_path=args.lockfile,
            wheel_path=args.wheel,
            platform=args.platform,
        )
        install_artifact(
            manifest_path=args.manifest,
            archive_path=args.archive,
            expected_manifest_sha256=args.expected_manifest_sha256,
            verified_contract=verified,
        )
    except (ArtifactContractError, PlaywrightContractError) as exc:
        print("PLAYWRIGHT_ARTIFACT_INSTALL=FAIL", file=sys.stderr)
        print(f"ERROR_CLASS={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("PLAYWRIGHT_ARTIFACT_INSTALL=FAIL", file=sys.stderr)
        print("ERROR_CLASS=INTERNAL_ERROR", file=sys.stderr)
        return 2

    contract = verified.browser
    print("PLAYWRIGHT_ARTIFACT_INSTALL=PASS")
    print(f"PLAYWRIGHT_PACKAGE={contract.package}")
    print(f"PLAYWRIGHT_PACKAGE_VERSION={contract.package_version}")
    print(f"BROWSER_FAMILY={contract.browser_family}")
    print(f"BROWSER_REVISION={contract.browser_revision}")
    print(f"PLATFORM={contract.platform}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
