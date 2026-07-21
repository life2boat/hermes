from __future__ import annotations

import hashlib
import json
import os
import stat
import unicodedata
from pathlib import Path
from typing import Mapping, Sequence


EXPECTED_IDENTITY_VERSION = 1
EXPECTED_IDENTITY_KIND = "PLAYWRIGHT_EXPECTED_CLOSURE"
EXPECTED_IDENTITY_SUFFIX = ".expected-closure.json"
INSTALLED_MARKER_VERSION = 2
INSTALLED_MARKER_KIND = "PLAYWRIGHT_INSTALLED_CLOSURE"
INSTALLATION_MARKER = "INSTALLATION_COMPLETE"
TREE_DIGEST_VERSION = 1
CLOSURE_DIGEST_VERSION = 1
MAX_IDENTITY_BYTES = 128 * 1024
IMMUTABLE_DIRECTORY_MODE = 0o555
IMMUTABLE_REGULAR_MODE = 0o444
IMMUTABLE_EXECUTABLE_MODE = 0o555

_EXPECTED_IDENTITY_FIELDS = frozenset(
    {
        "identity_version",
        "identity_kind",
        "closure_manifest_sha256",
        "playwright_package",
        "playwright_package_version",
        "platform",
        "artifact_count",
        "artifacts",
    }
)
_EXPECTED_ARTIFACT_FIELDS = frozenset(
    {
        "artifact_name",
        "revision",
        "archive_sha256",
        "layout_kind",
        "expected_executable_relative_path",
    }
)
_INSTALLED_MARKER_FIELDS = frozenset(
    {
        "marker_version",
        "marker_kind",
        "closure_manifest_sha256",
        "playwright_package",
        "playwright_package_version",
        "platform",
        "artifact_count",
        "artifacts",
        "complete_installed_closure_sha256",
    }
)
_INSTALLED_ARTIFACT_FIELDS = frozenset(
    {
        *_EXPECTED_ARTIFACT_FIELDS,
        "installed_tree_sha256",
        "installed_file_count",
        "installed_total_bytes",
        "executable_sha256",
    }
)


class InstalledClosureError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


def _fail(code: str) -> None:
    raise InstalledClosureError(code)


def canonical_json(document: Mapping[str, object]) -> bytes:
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


def _parse_canonical_document(data: bytes, code: str) -> dict[str, object]:
    if not data or len(data) > MAX_IDENTITY_BYTES:
        _fail(code)
    try:
        document = json.loads(
            data.decode("ascii"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (_DuplicateJsonKey, UnicodeError, json.JSONDecodeError):
        _fail(code)
    if not isinstance(document, dict) or canonical_json(document) != data:
        _fail(code)
    return document


def _required_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(code)
    return value


def _sha256(value: object, code: str) -> str:
    text = _required_string(value, code)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        _fail(code)
    return text


def _safe_relative_path(value: object, code: str) -> str:
    text = _required_string(value, code)
    if (
        text.startswith("/")
        or "\\" in text
        or "\x00" in text
        or any(part in {"", ".", ".."} for part in text.split("/"))
        or unicodedata.normalize("NFC", text) != text
    ):
        _fail(code)
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail(code)
    return text


def _validate_expected_artifact(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _EXPECTED_ARTIFACT_FIELDS:
        _fail("EXPECTED_ARTIFACT_FIELDS_INVALID")
    name = _required_string(value["artifact_name"], "EXPECTED_ARTIFACT_INVALID")
    if not name.isascii() or not all(
        character.islower() or character.isdigit() or character == "-"
        for character in name
    ):
        _fail("EXPECTED_ARTIFACT_INVALID")
    revision = _required_string(value["revision"], "EXPECTED_ARTIFACT_INVALID")
    if not revision.isascii() or not revision.isdigit():
        _fail("EXPECTED_ARTIFACT_INVALID")
    _sha256(value["archive_sha256"], "EXPECTED_ARTIFACT_INVALID")
    _required_string(value["layout_kind"], "EXPECTED_ARTIFACT_INVALID")
    _safe_relative_path(
        value["expected_executable_relative_path"],
        "EXPECTED_ARTIFACT_INVALID",
    )
    return value


def _validate_expected_identity(document: dict[str, object]) -> dict[str, object]:
    if set(document) != _EXPECTED_IDENTITY_FIELDS:
        _fail("EXPECTED_IDENTITY_FIELDS_INVALID")
    if (
        type(document["identity_version"]) is not int
        or document["identity_version"] != EXPECTED_IDENTITY_VERSION
        or document["identity_kind"] != EXPECTED_IDENTITY_KIND
    ):
        _fail("EXPECTED_IDENTITY_VERSION_INVALID")
    _sha256(document["closure_manifest_sha256"], "EXPECTED_IDENTITY_INVALID")
    for field in (
        "playwright_package",
        "playwright_package_version",
        "platform",
    ):
        _required_string(document[field], "EXPECTED_IDENTITY_INVALID")
    count = document["artifact_count"]
    artifacts = document["artifacts"]
    if (
        type(count) is not int
        or not isinstance(artifacts, list)
        or not 0 < count <= 16
        or len(artifacts) != count
    ):
        _fail("EXPECTED_IDENTITY_INVALID")
    validated = [_validate_expected_artifact(item) for item in artifacts]
    names = [str(item["artifact_name"]) for item in validated]
    if names != sorted(names) or len(set(names)) != len(names):
        _fail("EXPECTED_ARTIFACT_ORDER_INVALID")
    return document


def parse_expected_identity(data: bytes) -> dict[str, object]:
    return _validate_expected_identity(
        _parse_canonical_document(data, "EXPECTED_IDENTITY_INVALID")
    )


def build_expected_identity_document(
    *,
    closure_manifest_sha256: str,
    playwright_package: str,
    playwright_package_version: str,
    platform: str,
    artifacts: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    document: dict[str, object] = {
        "identity_version": EXPECTED_IDENTITY_VERSION,
        "identity_kind": EXPECTED_IDENTITY_KIND,
        "closure_manifest_sha256": closure_manifest_sha256,
        "playwright_package": playwright_package,
        "playwright_package_version": playwright_package_version,
        "platform": platform,
        "artifact_count": len(artifacts),
        "artifacts": [dict(item) for item in artifacts],
    }
    return parse_expected_identity(canonical_json(document))


def _validate_installed_artifact(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _INSTALLED_ARTIFACT_FIELDS:
        _fail("INSTALLED_ARTIFACT_FIELDS_INVALID")
    _validate_expected_artifact(
        {field: value[field] for field in _EXPECTED_ARTIFACT_FIELDS}
    )
    for field in (
        "installed_tree_sha256",
        "executable_sha256",
    ):
        _sha256(value[field], "INSTALLED_ARTIFACT_INVALID")
    if (
        type(value["installed_file_count"]) is not int
        or value["installed_file_count"] <= 0
        or type(value["installed_total_bytes"]) is not int
        or value["installed_total_bytes"] < 0
    ):
        _fail("INSTALLED_ARTIFACT_INVALID")
    return value


def _validate_installed_marker(document: dict[str, object]) -> dict[str, object]:
    if set(document) != _INSTALLED_MARKER_FIELDS:
        _fail("INSTALLED_MARKER_FIELDS_INVALID")
    if (
        type(document["marker_version"]) is not int
        or document["marker_version"] != INSTALLED_MARKER_VERSION
        or document["marker_kind"] != INSTALLED_MARKER_KIND
    ):
        _fail("INSTALLED_MARKER_VERSION_INVALID")
    _sha256(document["closure_manifest_sha256"], "INSTALLED_MARKER_INVALID")
    _sha256(
        document["complete_installed_closure_sha256"],
        "INSTALLED_MARKER_INVALID",
    )
    for field in (
        "playwright_package",
        "playwright_package_version",
        "platform",
    ):
        _required_string(document[field], "INSTALLED_MARKER_INVALID")
    count = document["artifact_count"]
    artifacts = document["artifacts"]
    if (
        type(count) is not int
        or not isinstance(artifacts, list)
        or not 0 < count <= 16
        or len(artifacts) != count
    ):
        _fail("INSTALLED_MARKER_INVALID")
    validated = [_validate_installed_artifact(item) for item in artifacts]
    names = [str(item["artifact_name"]) for item in validated]
    if names != sorted(names) or len(set(names)) != len(names):
        _fail("INSTALLED_ARTIFACT_ORDER_INVALID")
    return document


def parse_installed_marker(data: bytes) -> dict[str, object]:
    return _validate_installed_marker(
        _parse_canonical_document(data, "INSTALLED_MARKER_INVALID")
    )


def expected_identity_path(cache_root: Path) -> Path:
    if not cache_root.is_absolute() or not cache_root.name:
        _fail("CACHE_ROOT_INVALID")
    return cache_root.parent / f"{cache_root.name}{EXPECTED_IDENTITY_SUFFIX}"


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_owner_mode(
    metadata: os.stat_result,
    *,
    owner_uid: int,
    owner_gid: int,
    expected_mode: int,
    code: str,
) -> None:
    if (
        metadata.st_uid != owner_uid
        or metadata.st_gid != owner_gid
        or stat.S_IMODE(metadata.st_mode) != expected_mode
    ):
        _fail(code)


def _safe_component(name: str) -> bytes:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or "\x00" in name
        or unicodedata.normalize("NFC", name) != name
    ):
        _fail("INSTALLED_TREE_PATH_INVALID")
    try:
        return name.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("INSTALLED_TREE_PATH_INVALID")


def _hash_regular_file(
    path: Path,
    metadata: os.stat_result,
    *,
    owner_uid: int,
    owner_gid: int,
) -> str:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode)
        not in {IMMUTABLE_REGULAR_MODE, IMMUTABLE_EXECUTABLE_MODE}
    ):
        _fail("INSTALLED_TREE_ENTRY_INVALID")
    _validate_owner_mode(
        metadata,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        expected_mode=stat.S_IMODE(metadata.st_mode),
        code="INSTALLED_TREE_PERMISSION_INVALID",
    )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail("INSTALLED_TREE_READ_FAILED")
    try:
        before = os.fstat(descriptor)
        if _metadata_identity(before) != _metadata_identity(metadata):
            _fail("INSTALLED_TREE_CHANGED_DURING_HASH")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _metadata_identity(after) != _metadata_identity(before):
            _fail("INSTALLED_TREE_CHANGED_DURING_HASH")
        return digest.hexdigest()
    except InstalledClosureError:
        raise
    except OSError:
        _fail("INSTALLED_TREE_READ_FAILED")
    finally:
        os.close(descriptor)


def compute_installed_tree_identity(
    root: Path,
    *,
    expected_executable_relative_path: str,
    owner_uid: int,
    owner_gid: int,
) -> dict[str, object]:
    executable_path = _safe_relative_path(
        expected_executable_relative_path,
        "EXPECTED_EXECUTABLE_PATH_INVALID",
    )
    tree_digest = hashlib.sha256()
    tree_digest.update(f"PLAYWRIGHT_INSTALLED_TREE_V{TREE_DIGEST_VERSION}\n".encode())
    file_count = 0
    total_bytes = 0
    executable_sha256: str | None = None

    def visit(directory: Path, relative: str) -> None:
        nonlocal file_count, total_bytes, executable_sha256
        try:
            before = directory.lstat()
        except OSError:
            _fail("INSTALLED_TREE_READ_FAILED")
        if not stat.S_ISDIR(before.st_mode) or directory.is_symlink():
            _fail("INSTALLED_TREE_ENTRY_INVALID")
        _validate_owner_mode(
            before,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            expected_mode=IMMUTABLE_DIRECTORY_MODE,
            code="INSTALLED_TREE_PERMISSION_INVALID",
        )
        tree_digest.update(
            canonical_json(
                {
                    "entry_type": "directory",
                    "mode": IMMUTABLE_DIRECTORY_MODE,
                    "path": relative,
                }
            )
        )
        try:
            with os.scandir(directory) as scanner:
                entries = list(scanner)
        except OSError:
            _fail("INSTALLED_TREE_READ_FAILED")
        entries.sort(key=lambda entry: _safe_component(entry.name))
        initial_names = tuple(entry.name for entry in entries)
        for entry in entries:
            name = entry.name
            _safe_component(name)
            child = directory / name
            child_relative = name if relative == "." else f"{relative}/{name}"
            try:
                metadata = child.lstat()
            except OSError:
                _fail("INSTALLED_TREE_READ_FAILED")
            if stat.S_ISDIR(metadata.st_mode) and not child.is_symlink():
                visit(child, child_relative)
                continue
            if not stat.S_ISREG(metadata.st_mode) or child.is_symlink():
                _fail("INSTALLED_TREE_ENTRY_INVALID")
            file_sha256 = _hash_regular_file(
                child,
                metadata,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
            )
            mode = stat.S_IMODE(metadata.st_mode)
            tree_digest.update(
                canonical_json(
                    {
                        "entry_type": "regular_file",
                        "mode": mode,
                        "path": child_relative,
                        "sha256": file_sha256,
                        "size": metadata.st_size,
                    }
                )
            )
            file_count += 1
            total_bytes += metadata.st_size
            if child_relative == executable_path:
                if mode != IMMUTABLE_EXECUTABLE_MODE:
                    _fail("EXPECTED_EXECUTABLE_INVALID")
                executable_sha256 = file_sha256
        try:
            with os.scandir(directory) as scanner:
                final_names = tuple(
                    sorted((entry.name for entry in scanner), key=_safe_component)
                )
            after = directory.lstat()
        except OSError:
            _fail("INSTALLED_TREE_READ_FAILED")
        if final_names != initial_names or _metadata_identity(after) != _metadata_identity(before):
            _fail("INSTALLED_TREE_CHANGED_DURING_HASH")

    visit(root, ".")
    if executable_sha256 is None:
        _fail("EXPECTED_EXECUTABLE_MISSING")
    return {
        "installed_tree_sha256": tree_digest.hexdigest(),
        "installed_file_count": file_count,
        "installed_total_bytes": total_bytes,
        "executable_sha256": executable_sha256,
    }


def seal_installed_artifact_tree(
    root: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> None:
    def seal(path: Path) -> None:
        try:
            metadata = path.lstat()
        except OSError:
            _fail("INSTALLED_TREE_SEAL_FAILED")
        if metadata.st_uid != owner_uid or metadata.st_gid != owner_gid:
            _fail("INSTALLED_TREE_OWNER_INVALID")
        if stat.S_ISDIR(metadata.st_mode) and not path.is_symlink():
            try:
                with os.scandir(path) as scanner:
                    children = sorted(
                        (Path(entry.path) for entry in scanner),
                        key=lambda child: _safe_component(child.name),
                    )
            except OSError:
                _fail("INSTALLED_TREE_SEAL_FAILED")
            for child in children:
                seal(child)
            try:
                path.chmod(IMMUTABLE_DIRECTORY_MODE)
            except OSError:
                _fail("INSTALLED_TREE_SEAL_FAILED")
            return
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            _fail("INSTALLED_TREE_ENTRY_INVALID")
        mode = (
            IMMUTABLE_EXECUTABLE_MODE
            if stat.S_IMODE(metadata.st_mode) & 0o111
            else IMMUTABLE_REGULAR_MODE
        )
        try:
            path.chmod(mode)
        except OSError:
            _fail("INSTALLED_TREE_SEAL_FAILED")

    seal(root)


def _expected_artifacts(
    expected_identity: Mapping[str, object],
) -> list[dict[str, object]]:
    parsed = parse_expected_identity(canonical_json(expected_identity))
    artifacts = parsed["artifacts"]
    if not isinstance(artifacts, list):
        _fail("EXPECTED_IDENTITY_INVALID")
    return artifacts


def _complete_closure_digest(artifacts: Sequence[Mapping[str, object]]) -> str:
    document = {
        "closure_digest_version": CLOSURE_DIGEST_VERSION,
        "artifact_count": len(artifacts),
        "artifacts": [dict(item) for item in artifacts],
    }
    return hashlib.sha256(canonical_json(document)).hexdigest()


def build_installed_marker_document(
    *,
    expected_identity: Mapping[str, object],
    artifact_roots: Mapping[str, Path],
    owner_uid: int,
    owner_gid: int,
) -> dict[str, object]:
    expected = parse_expected_identity(canonical_json(expected_identity))
    expected_artifacts = _expected_artifacts(expected)
    expected_names = {str(item["artifact_name"]) for item in expected_artifacts}
    if set(artifact_roots) != expected_names:
        _fail("INSTALLED_ARTIFACT_SET_MISMATCH")
    installed_artifacts: list[dict[str, object]] = []
    for item in expected_artifacts:
        name = str(item["artifact_name"])
        tree = compute_installed_tree_identity(
            artifact_roots[name],
            expected_executable_relative_path=str(
                item["expected_executable_relative_path"]
            ),
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        installed_artifacts.append({**item, **tree})
    document: dict[str, object] = {
        "marker_version": INSTALLED_MARKER_VERSION,
        "marker_kind": INSTALLED_MARKER_KIND,
        "closure_manifest_sha256": expected["closure_manifest_sha256"],
        "playwright_package": expected["playwright_package"],
        "playwright_package_version": expected["playwright_package_version"],
        "platform": expected["platform"],
        "artifact_count": expected["artifact_count"],
        "artifacts": installed_artifacts,
        "complete_installed_closure_sha256": _complete_closure_digest(
            installed_artifacts
        ),
    }
    return parse_installed_marker(canonical_json(document))


def _read_immutable_identity(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
    code: str,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail(code)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= MAX_IDENTITY_BYTES
        ):
            _fail(code)
        _validate_owner_mode(
            before,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            expected_mode=IMMUTABLE_REGULAR_MODE,
            code=code,
        )
        data = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            data.extend(chunk)
            if len(data) > MAX_IDENTITY_BYTES:
                _fail(code)
        after = os.fstat(descriptor)
        if len(data) != before.st_size or _metadata_identity(after) != _metadata_identity(before):
            _fail(code)
        return bytes(data)
    except InstalledClosureError:
        raise
    except OSError:
        _fail(code)
    finally:
        os.close(descriptor)


def read_expected_identity(
    path: Path,
    *,
    owner_uid: int,
    owner_gid: int,
) -> dict[str, object]:
    return parse_expected_identity(
        _read_immutable_identity(
            path,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            code="EXPECTED_IDENTITY_FILE_INVALID",
        )
    )


def verify_installed_closure(
    *,
    cache_root: Path,
    expected_identity: Mapping[str, object],
    artifact_cache_directories: Mapping[str, str],
    owner_uid: int,
    owner_gid: int,
) -> dict[str, object]:
    expected = parse_expected_identity(canonical_json(expected_identity))
    expected_artifacts = _expected_artifacts(expected)
    expected_names = {str(item["artifact_name"]) for item in expected_artifacts}
    if set(artifact_cache_directories) != expected_names:
        _fail("INSTALLED_ARTIFACT_SET_MISMATCH")
    try:
        parent_metadata = cache_root.parent.lstat()
        root_metadata = cache_root.lstat()
    except OSError:
        _fail("PACKAGED_CACHE_INVALID")
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or cache_root.parent.is_symlink()
        or parent_metadata.st_uid != owner_uid
        or parent_metadata.st_gid != owner_gid
        or stat.S_IMODE(parent_metadata.st_mode) & 0o022
    ):
        _fail("PACKAGED_CACHE_PARENT_PERMISSION_INVALID")
    if not stat.S_ISDIR(root_metadata.st_mode) or cache_root.is_symlink():
        _fail("PACKAGED_CACHE_INVALID")
    _validate_owner_mode(
        root_metadata,
        owner_uid=owner_uid,
        owner_gid=owner_gid,
        expected_mode=IMMUTABLE_DIRECTORY_MODE,
        code="PACKAGED_CACHE_PERMISSION_INVALID",
    )
    try:
        children = {child.name for child in cache_root.iterdir()}
    except OSError:
        _fail("PACKAGED_CACHE_INVALID")
    expected_children = {INSTALLATION_MARKER} | set(
        artifact_cache_directories.values()
    )
    if children != expected_children:
        _fail("PACKAGED_CACHE_ENTRY_SET_MISMATCH")
    marker = parse_installed_marker(
        _read_immutable_identity(
            cache_root / INSTALLATION_MARKER,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            code="INSTALLED_MARKER_FILE_INVALID",
        )
    )
    expected_top_level = {
        "closure_manifest_sha256": expected["closure_manifest_sha256"],
        "playwright_package": expected["playwright_package"],
        "playwright_package_version": expected["playwright_package_version"],
        "platform": expected["platform"],
        "artifact_count": expected["artifact_count"],
    }
    if any(marker[field] != value for field, value in expected_top_level.items()):
        _fail("INSTALLED_MARKER_EXPECTED_IDENTITY_MISMATCH")
    marker_artifacts = marker["artifacts"]
    if not isinstance(marker_artifacts, list):
        _fail("INSTALLED_MARKER_INVALID")
    for expected_artifact, marker_artifact in zip(
        expected_artifacts, marker_artifacts, strict=True
    ):
        if any(
            marker_artifact[field] != expected_artifact[field]
            for field in _EXPECTED_ARTIFACT_FIELDS
        ):
            _fail("INSTALLED_MARKER_EXPECTED_IDENTITY_MISMATCH")
    recomputed = build_installed_marker_document(
        expected_identity=expected,
        artifact_roots={
            name: cache_root / directory
            for name, directory in artifact_cache_directories.items()
        },
        owner_uid=owner_uid,
        owner_gid=owner_gid,
    )
    if marker != recomputed:
        _fail("INSTALLED_CLOSURE_DIGEST_MISMATCH")
    return marker
