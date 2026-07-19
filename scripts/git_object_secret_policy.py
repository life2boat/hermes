from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from scripts.secret_scanner import SecretScanError, scan_secret_bytes


SUPPORTED_REGULAR_MODES = frozenset({"100644", "100755"})
MAX_GIT_BLOB_BYTES = 64 * 1024 * 1024
_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\n"


class GitObjectResult(str, Enum):
    CLEAN = "CLEAN"
    SECRET_FOUND = "SECRET_FOUND"
    UNSUPPORTED_OBJECT = "UNSUPPORTED_OBJECT"
    BINARY_DENIED = "BINARY_DENIED"
    DECODE_DENIED = "DECODE_DENIED"
    READ_ERROR = "READ_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass(frozen=True)
class GitObjectDescriptor:
    path: str
    mode: str
    object_type: str
    oid: str


@dataclass(frozen=True)
class GitObjectScanOutcome:
    descriptor: GitObjectDescriptor
    result: GitObjectResult
    error_code: str | None
    size: int | None

    @property
    def clean(self) -> bool:
        return self.result is GitObjectResult.CLEAN

    @property
    def exit_class(self) -> str:
        if self.result is GitObjectResult.CLEAN:
            return "CLEAN"
        if self.result in {
            GitObjectResult.READ_ERROR,
            GitObjectResult.INTERNAL_ERROR,
        }:
            return "INTERNAL_ERROR"
        return "SECURITY_DENIED"

    @property
    def exit_code(self) -> int:
        return {
            "CLEAN": 0,
            "SECURITY_DENIED": 1,
            "INTERNAL_ERROR": 2,
        }[self.exit_class]


class GitObjectAcquisitionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GitObjectReadError(RuntimeError):
    pass


BlobReader = Callable[[str], bytes]
ObjectScanner = Callable[[GitObjectDescriptor, BlobReader], GitObjectScanOutcome]


def _outcome(
    descriptor: GitObjectDescriptor,
    result: GitObjectResult,
    error_code: str | None,
    *,
    size: int | None = None,
) -> GitObjectScanOutcome:
    return GitObjectScanOutcome(
        descriptor=descriptor,
        result=result,
        error_code=error_code,
        size=size,
    )


def scan_git_object(
    descriptor: GitObjectDescriptor,
    read_blob: BlobReader,
) -> GitObjectScanOutcome:
    if descriptor.mode == "120000":
        return _outcome(
            descriptor,
            GitObjectResult.UNSUPPORTED_OBJECT,
            "GIT_SYMLINK_UNSUPPORTED",
        )
    if descriptor.mode == "160000" or descriptor.object_type == "commit":
        return _outcome(
            descriptor,
            GitObjectResult.UNSUPPORTED_OBJECT,
            "GIT_SUBMODULE_UNSUPPORTED",
        )
    if (
        descriptor.mode not in SUPPORTED_REGULAR_MODES
        or descriptor.object_type != "blob"
    ):
        return _outcome(
            descriptor,
            GitObjectResult.UNSUPPORTED_OBJECT,
            "GIT_TREE_ENTRY_UNSUPPORTED",
        )
    if _OBJECT_ID_RE.fullmatch(descriptor.oid) is None:
        return _outcome(
            descriptor,
            GitObjectResult.READ_ERROR,
            "GIT_OBJECT_READ_FAILED",
        )

    try:
        data = read_blob(descriptor.oid)
    except GitObjectReadError:
        return _outcome(
            descriptor,
            GitObjectResult.READ_ERROR,
            "GIT_OBJECT_READ_FAILED",
        )
    except Exception:
        return _outcome(
            descriptor,
            GitObjectResult.INTERNAL_ERROR,
            "GIT_OBJECT_SCAN_INTERNAL_ERROR",
        )

    size = len(data)
    if size > MAX_GIT_BLOB_BYTES:
        return _outcome(
            descriptor,
            GitObjectResult.UNSUPPORTED_OBJECT,
            "GIT_BLOB_SIZE_UNSUPPORTED",
            size=size,
        )
    if data.startswith(_LFS_POINTER):
        return _outcome(
            descriptor,
            GitObjectResult.UNSUPPORTED_OBJECT,
            "GIT_LFS_POINTER_UNSUPPORTED",
            size=size,
        )
    if b"\x00" in data:
        return _outcome(
            descriptor,
            GitObjectResult.BINARY_DENIED,
            "GIT_BINARY_BLOB_DENIED",
            size=size,
        )
    try:
        findings = scan_secret_bytes(data)
    except SecretScanError as exc:
        if exc.code == "SECRET_SCAN_BINARY_DENIED":
            result = GitObjectResult.BINARY_DENIED
            code = "GIT_BINARY_BLOB_DENIED"
        elif exc.code == "SECRET_SCAN_DECODING_FAILED":
            result = GitObjectResult.DECODE_DENIED
            code = "GIT_BLOB_UTF8_DECODE_DENIED"
        else:
            result = GitObjectResult.INTERNAL_ERROR
            code = "GIT_OBJECT_SCAN_INTERNAL_ERROR"
        return _outcome(descriptor, result, code, size=size)
    except Exception:
        return _outcome(
            descriptor,
            GitObjectResult.INTERNAL_ERROR,
            "GIT_OBJECT_SCAN_INTERNAL_ERROR",
            size=size,
        )
    if findings:
        return _outcome(
            descriptor,
            GitObjectResult.SECRET_FOUND,
            "GIT_SECRET_CONTENT_DENIED",
            size=size,
        )
    return _outcome(descriptor, GitObjectResult.CLEAN, None, size=size)


def read_git_blob(repository_root: Path, oid: str) -> bytes:
    if _OBJECT_ID_RE.fullmatch(oid) is None:
        raise GitObjectReadError
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), "cat-file", "blob", oid],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise GitObjectReadError from exc
    if completed.returncode != 0:
        raise GitObjectReadError
    return completed.stdout


def _run_git_bytes(repository_root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *arguments],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise GitObjectAcquisitionError("GIT_EXECUTION_FAILED") from exc
    if completed.returncode != 0:
        raise GitObjectAcquisitionError("GIT_OBJECT_ENUMERATION_FAILED")
    return completed.stdout


def _safe_git_path(path_bytes: bytes) -> str:
    try:
        path = path_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GitObjectAcquisitionError("GIT_TREE_PATH_INVALID") from exc
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(ord(character) < 32 for character in path)
    ):
        raise GitObjectAcquisitionError("GIT_TREE_PATH_INVALID")
    parts = path.split("/")
    if any(
        part in {"", ".", ".."} or part.endswith((".", " ")) or ":" in part
        for part in parts
    ):
        raise GitObjectAcquisitionError("GIT_TREE_PATH_INVALID")
    return "/".join(parts)


def _object_type_for_mode(mode: str) -> str:
    if mode == "160000":
        return "commit"
    if mode in SUPPORTED_REGULAR_MODES or mode == "120000":
        return "blob"
    return "unknown"


def list_index_candidate_entries(
    repository_root: Path,
) -> tuple[GitObjectDescriptor, ...]:
    changed_output = _run_git_bytes(
        repository_root,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--diff-filter=ACMR",
        "--no-renames",
        "HEAD",
    )
    changed_paths = {
        _safe_git_path(raw_path)
        for raw_path in changed_output.split(b"\x00")
        if raw_path
    }
    if not changed_paths:
        return ()

    index_output = _run_git_bytes(repository_root, "ls-files", "--stage", "-z")
    entries: list[GitObjectDescriptor] = []
    seen: set[str] = set()
    for record in index_output.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_bytes, oid_bytes, stage_bytes = metadata.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            oid = oid_bytes.decode("ascii")
            stage = stage_bytes.decode("ascii")
            path = _safe_git_path(path_bytes)
        except (UnicodeError, ValueError) as exc:
            raise GitObjectAcquisitionError("GIT_INDEX_RECORD_INVALID") from exc
        if path not in changed_paths:
            continue
        if path in seen or stage != "0":
            raise GitObjectAcquisitionError("GIT_INDEX_RECORD_UNSUPPORTED")
        seen.add(path)
        entries.append(
            GitObjectDescriptor(
                path=path,
                mode=mode,
                object_type=_object_type_for_mode(mode),
                oid=oid,
            )
        )
    if seen != changed_paths:
        raise GitObjectAcquisitionError("GIT_INDEX_OBJECT_MISSING")
    return tuple(sorted(entries, key=lambda entry: entry.path))


def list_commit_tree_entries(
    repository_root: Path,
    revision: str,
) -> tuple[GitObjectDescriptor, ...]:
    output = _run_git_bytes(
        repository_root,
        "ls-tree",
        "-rz",
        "--full-tree",
        revision,
    )
    entries: list[GitObjectDescriptor] = []
    paths: set[str] = set()
    casefold_paths: set[str] = set()
    for record in output.split(b"\x00"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_bytes, type_bytes, oid_bytes = metadata.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            object_type = type_bytes.decode("ascii")
            oid = oid_bytes.decode("ascii")
            path = _safe_git_path(path_bytes)
        except (UnicodeError, ValueError) as exc:
            raise GitObjectAcquisitionError("GIT_TREE_RECORD_INVALID") from exc
        if path in paths or path.casefold() in casefold_paths:
            raise GitObjectAcquisitionError("GIT_TREE_PATH_COLLISION")
        paths.add(path)
        casefold_paths.add(path.casefold())
        entries.append(
            GitObjectDescriptor(
                path=path,
                mode=mode,
                object_type=object_type,
                oid=oid,
            )
        )
    if not entries:
        raise GitObjectAcquisitionError("GIT_TREE_EMPTY")
    return tuple(sorted(entries, key=lambda entry: entry.path))


def candidate_tree_entries(
    *,
    repository_root: Path,
    approved_base_sha: str,
    source_sha: str,
) -> tuple[tuple[GitObjectDescriptor, ...], tuple[GitObjectDescriptor, ...]]:
    base_entries = list_commit_tree_entries(repository_root, approved_base_sha)
    source_entries = list_commit_tree_entries(repository_root, source_sha)
    trusted_regular_oids = {
        entry.oid
        for entry in base_entries
        if (
            entry.mode in SUPPORTED_REGULAR_MODES
            and entry.object_type == "blob"
            and _OBJECT_ID_RE.fullmatch(entry.oid) is not None
        )
    }
    candidates = tuple(
        entry
        for entry in source_entries
        if (
            entry.mode not in SUPPORTED_REGULAR_MODES
            or entry.object_type != "blob"
            or entry.oid not in trusted_regular_oids
        )
    )
    return source_entries, candidates


def scan_descriptors(
    *,
    repository_root: Path,
    descriptors: Iterable[GitObjectDescriptor],
    scanner: ObjectScanner = scan_git_object,
    reader: BlobReader | None = None,
) -> tuple[GitObjectScanOutcome, ...]:
    blob_reader = reader or (lambda oid: read_git_blob(repository_root, oid))
    return tuple(scanner(descriptor, blob_reader) for descriptor in descriptors)


def aggregate_exit_code(outcomes: Iterable[GitObjectScanOutcome]) -> int:
    exit_code = 0
    for outcome in outcomes:
        if outcome.exit_code == 2:
            return 2
        if outcome.exit_code == 1:
            exit_code = 1
    return exit_code
