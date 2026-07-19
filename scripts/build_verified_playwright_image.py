from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from scripts.install_pinned_playwright_artifact import (
    ArtifactContractError,
    load_verified_manifest,
    validate_manifest_contract,
)
from scripts.playwright_artifact_contract import (
    PlaywrightContractError,
    load_verified_wheel_contract,
)
from scripts.secret_scanner import SecretScanError, scan_secret_bytes


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_TAG_RE = re.compile(
    r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$"
)
_FIXED_ARTIFACT_FILES = frozenset(
    {"manifest.json", "browser-archive", "playwright-wheel"}
)
_FORBIDDEN_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".pytest-cache",
        ".ruff_cache",
        ".codex-remote-edit",
        "evidence",
        "review-mirrors",
    }
)
_FORBIDDEN_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".patch",
    ".diff",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".sqlite-wal",
    ".sqlite-shm",
    ".p12",
    ".pfx",
)
_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1\n"


class BuildContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GitTreeEntry:
    path: str
    mode: str
    oid: str


@dataclass(frozen=True)
class BuildRequest:
    repository_root: Path
    source_sha: str
    source_tree_sha: str
    artifact_context: Path
    manifest_sha256: str
    image_tag: str
    platform: str


@dataclass(frozen=True)
class BuildInputs:
    repository_root: Path
    source_sha: str
    source_tree_sha: str
    build_context: Path
    context_manifest: Path
    context_manifest_sha256: str
    context_file_count: int
    artifact_context: Path
    manifest_sha256: str
    image_tag: str
    platform: str


def _fail(code: str) -> None:
    raise BuildContractError(code)


def _run_git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError:
        _fail("GIT_EXECUTION_FAILED")
    if completed.returncode != 0:
        _fail("GIT_CONTRACT_FAILED")
    return completed.stdout.strip()


def _run_git_bytes(root: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
        )
    except OSError:
        _fail("GIT_EXECUTION_FAILED")
    if completed.returncode != 0:
        _fail("GIT_CONTRACT_FAILED")
    return completed.stdout


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            path_metadata = current.lstat()
        except FileNotFoundError:
            _fail("ARTIFACT_CONTEXT_MISSING")
        except OSError:
            _fail("ARTIFACT_CONTEXT_METADATA_FAILED")
        if stat.S_ISLNK(path_metadata.st_mode):
            _fail("ARTIFACT_CONTEXT_SYMLINK_DENIED")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_repository(root: Path, expected_sha: str) -> str:
    if _SHA_RE.fullmatch(expected_sha) is None:
        _fail("SOURCE_SHA_INVALID")
    if _run_git(root, "rev-parse", "HEAD") != expected_sha:
        _fail("SOURCE_SHA_MISMATCH")
    _run_git(root, "cat-file", "-e", f"{expected_sha}^{{commit}}")
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=no"):
        _fail("SOURCE_TRACKED_WORKTREE_DIRTY")
    if _run_git(root, "diff", "--check"):
        _fail("SOURCE_DIFF_CHECK_FAILED")
    tree_sha = _run_git(root, "rev-parse", f"{expected_sha}^{{tree}}")
    if _SHA_RE.fullmatch(tree_sha) is None:
        _fail("SOURCE_TREE_SHA_INVALID")
    return tree_sha


def _validate_artifact_context(
    root: Path,
    repository_root: Path,
    expected_manifest_sha256: str,
    platform: str,
) -> None:
    if not root.is_absolute():
        _fail("ARTIFACT_CONTEXT_NOT_ABSOLUTE")
    _assert_no_symlink_components(root)
    resolved_root = root.resolve(strict=True)
    resolved_repository = repository_root.resolve(strict=True)
    if _is_within(resolved_root, resolved_repository):
        _fail("ARTIFACT_CONTEXT_INSIDE_REPOSITORY")
    try:
        root_metadata = resolved_root.lstat()
    except OSError:
        _fail("ARTIFACT_CONTEXT_METADATA_FAILED")
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or root_metadata.st_nlink < 2
        or stat.S_IMODE(root_metadata.st_mode) & 0o022
    ):
        _fail("ARTIFACT_CONTEXT_METADATA_INVALID")
    try:
        children = {child.name for child in resolved_root.iterdir()}
    except OSError:
        _fail("ARTIFACT_CONTEXT_READ_FAILED")
    if children != _FIXED_ARTIFACT_FILES:
        _fail("ARTIFACT_CONTEXT_CONTENTS_INVALID")

    for name in sorted(_FIXED_ARTIFACT_FILES):
        path = resolved_root / name
        try:
            file_metadata = path.lstat()
        except OSError:
            _fail("ARTIFACT_FILE_METADATA_FAILED")
        if (
            stat.S_ISLNK(file_metadata.st_mode)
            or not stat.S_ISREG(file_metadata.st_mode)
            or file_metadata.st_nlink != 1
            or stat.S_IMODE(file_metadata.st_mode) & 0o022
        ):
            _fail("ARTIFACT_FILE_METADATA_INVALID")

    try:
        manifest = load_verified_manifest(
            resolved_root / "manifest.json", expected_manifest_sha256
        )
        verified = load_verified_wheel_contract(
            lockfile_path=resolved_repository / "uv.lock",
            wheel_path=resolved_root / "playwright-wheel",
            platform=platform,
        )
        validate_manifest_contract(manifest, verified)
    except (ArtifactContractError, PlaywrightContractError) as exc:
        raise BuildContractError(exc.code) from None
    archive = resolved_root / "browser-archive"
    try:
        archive_size = archive.stat().st_size
    except OSError:
        _fail("ARTIFACT_ARCHIVE_READ_FAILED")
    if archive_size != manifest["archive_size"]:
        _fail("ARTIFACT_ARCHIVE_SIZE_MISMATCH")
    digest = hashlib.sha256()
    try:
        with archive.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        _fail("ARTIFACT_ARCHIVE_READ_FAILED")
    if digest.hexdigest() != manifest["archive_sha256"]:
        _fail("ARTIFACT_ARCHIVE_SHA256_MISMATCH")


def _validate_image_tag(image_tag: str, source_sha: str) -> None:
    if (
        _IMAGE_TAG_RE.fullmatch(image_tag) is None
        or image_tag.rsplit(":", 1)[-1].lower() in {"latest", "main", "test"}
        or source_sha[:12] not in image_tag
    ):
        _fail("IMAGE_TAG_NOT_IMMUTABLE")


def validate_build_inputs(
    *,
    repository_root: Path,
    expected_source_sha: str,
    artifact_context: Path,
    expected_manifest_sha256: str,
    image_tag: str,
    platform: str,
) -> BuildRequest:
    if _SHA256_RE.fullmatch(expected_manifest_sha256) is None:
        _fail("EXPECTED_MANIFEST_SHA256_INVALID")
    if platform not in {"linux/amd64", "linux/arm64"}:
        _fail("PLATFORM_UNSUPPORTED")
    resolved_repository = repository_root.resolve(strict=True)
    source_tree_sha = _validate_repository(
        resolved_repository, expected_source_sha
    )
    _validate_artifact_context(
        artifact_context,
        resolved_repository,
        expected_manifest_sha256,
        platform,
    )
    _validate_image_tag(image_tag, expected_source_sha)
    return BuildRequest(
        repository_root=resolved_repository,
        source_sha=expected_source_sha,
        source_tree_sha=source_tree_sha,
        artifact_context=artifact_context.resolve(strict=True),
        manifest_sha256=expected_manifest_sha256,
        image_tag=image_tag,
        platform=platform,
    )


def _safe_git_path(raw_path: str) -> str:
    if (
        not raw_path
        or raw_path.startswith("/")
        or "\\" in raw_path
        or "\x00" in raw_path
        or any(ord(character) < 32 for character in raw_path)
    ):
        _fail("GIT_TREE_PATH_INVALID")
    parts = raw_path.split("/")
    if any(
        part in {"", ".", ".."}
        or part.endswith((".", " "))
        or ":" in part
        for part in parts
    ):
        _fail("GIT_TREE_PATH_INVALID")
    return "/".join(parts)


def _assert_context_path_allowed(path: str) -> None:
    parts = path.split("/")
    lowered = [part.lower() for part in parts]
    if any(part in _FORBIDDEN_DIRECTORY_NAMES for part in lowered):
        _fail("CONTEXT_LOCAL_ARTIFACT_DENIED")
    if any(part.endswith(".egg-info") for part in lowered):
        _fail("CONTEXT_LOCAL_ARTIFACT_DENIED")
    lower_path = path.lower()
    if lower_path.endswith(_FORBIDDEN_SUFFIXES):
        _fail("CONTEXT_LOCAL_ARTIFACT_DENIED")
    basename = lowered[-1]
    if basename == ".env" or (
        basename.startswith(".env.") and basename != ".env.example"
    ):
        _fail("CONTEXT_SECRET_FILE_DENIED")
    if basename in {"id_rsa", "id_ed25519"} or basename.endswith(".key"):
        _fail("CONTEXT_SECRET_FILE_DENIED")
    if "reviews" in lowered and "deploy" in lowered:
        _fail("CONTEXT_LOCAL_ARTIFACT_DENIED")


def _git_tree_entries(root: Path, source_sha: str) -> list[GitTreeEntry]:
    output = _run_git_bytes(
        root,
        "ls-tree",
        "-rz",
        "--full-tree",
        source_sha,
    )
    entries: list[GitTreeEntry] = []
    paths: set[str] = set()
    casefold_paths: set[str] = set()
    for raw_record in output.split(b"\x00"):
        if not raw_record:
            continue
        try:
            metadata_bytes, path_bytes = raw_record.split(b"\t", 1)
            mode_bytes, type_bytes, oid_bytes = metadata_bytes.split(b" ", 2)
            mode = mode_bytes.decode("ascii")
            object_type = type_bytes.decode("ascii")
            oid = oid_bytes.decode("ascii")
            path = _safe_git_path(path_bytes.decode("utf-8"))
        except (UnicodeError, ValueError):
            _fail("GIT_TREE_RECORD_INVALID")
        if mode == "160000" or object_type == "commit":
            _fail("GIT_SUBMODULE_UNSUPPORTED")
        if mode == "120000":
            _fail("GIT_SYMLINK_UNSUPPORTED")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            _fail("GIT_TREE_ENTRY_UNSUPPORTED")
        if _SHA_RE.fullmatch(oid) is None:
            _fail("GIT_TREE_RECORD_INVALID")
        if path in paths or path.casefold() in casefold_paths:
            _fail("GIT_TREE_PATH_COLLISION")
        _assert_context_path_allowed(path)
        paths.add(path)
        casefold_paths.add(path.casefold())
        entries.append(GitTreeEntry(path=path, mode=mode, oid=oid))
    if not entries:
        _fail("GIT_TREE_EMPTY")
    return sorted(entries, key=lambda entry: entry.path)


def _git_blob_digest(data: bytes, object_format: str) -> str:
    if object_format not in {"sha1", "sha256"}:
        _fail("GIT_OBJECT_FORMAT_UNSUPPORTED")
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


def _assert_content_allowed(data: bytes) -> None:
    if data.startswith(_LFS_POINTER):
        _fail("GIT_LFS_POINTER_UNSUPPORTED")
    try:
        findings = scan_secret_bytes(data)
    except SecretScanError:
        _fail("CONTEXT_SECRET_SCAN_FAILED")
    if findings:
        _fail("CONTEXT_SECRET_CONTENT_DENIED")


def _canonical_context_manifest(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _extract_git_archive(
    *,
    archive_path: Path,
    context_root: Path,
    entries: list[GitTreeEntry],
    object_format: str,
) -> list[dict[str, object]]:
    expected = {entry.path: entry for entry in entries}
    seen: set[str] = set()
    records: list[dict[str, object]] = []
    try:
        archive = tarfile.open(archive_path, mode="r:")
    except (OSError, tarfile.TarError):
        _fail("GIT_ARCHIVE_INVALID")
    with archive:
        try:
            members = archive.getmembers()
        except (OSError, tarfile.TarError):
            _fail("GIT_ARCHIVE_INVALID")
        for member in members:
            name = member.name[:-1] if member.isdir() and member.name.endswith("/") else member.name
            safe_name = _safe_git_path(name)
            if member.isdir():
                try:
                    (context_root / safe_name).mkdir(
                        mode=0o755, parents=True, exist_ok=True
                    )
                except OSError:
                    _fail("GIT_CONTEXT_EXPORT_FAILED")
                continue
            if not member.isreg():
                _fail("GIT_ARCHIVE_ENTRY_UNSUPPORTED")
            entry = expected.get(safe_name)
            if entry is None or safe_name in seen:
                _fail("GIT_ARCHIVE_TREE_MISMATCH")
            source = archive.extractfile(member)
            if source is None:
                _fail("GIT_ARCHIVE_READ_FAILED")
            try:
                data = source.read()
            except (OSError, tarfile.TarError):
                _fail("GIT_ARCHIVE_READ_FAILED")
            finally:
                source.close()
            _assert_content_allowed(data)
            if _git_blob_digest(data, object_format) != entry.oid:
                _fail("GIT_ARCHIVE_BLOB_MISMATCH")
            expected_mode = 0o755 if entry.mode == "100755" else 0o644
            if stat.S_IMODE(member.mode) != expected_mode:
                _fail("GIT_ARCHIVE_MODE_MISMATCH")
            target = context_root.joinpath(*safe_name.split("/"))
            try:
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(expected_mode)
            except OSError:
                _fail("GIT_CONTEXT_EXPORT_FAILED")
            records.append(
                {
                    "mode": entry.mode,
                    "oid": entry.oid,
                    "path": safe_name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
            seen.add(safe_name)
    if seen != set(expected):
        _fail("GIT_ARCHIVE_TREE_MISMATCH")
    return sorted(records, key=lambda record: str(record["path"]))


def _write_context_manifest(
    *,
    manifest_path: Path,
    source_sha: str,
    tree_sha: str,
    object_format: str,
    records: list[dict[str, object]],
) -> str:
    document: dict[str, object] = {
        "files": records,
        "format_version": 1,
        "git_object_format": object_format,
        "source_sha": source_sha,
        "tree_sha": tree_sha,
    }
    data = _canonical_context_manifest(document)
    try:
        manifest_path.write_bytes(data)
        manifest_path.chmod(0o600)
    except OSError:
        _fail("CONTEXT_MANIFEST_WRITE_FAILED")
    return hashlib.sha256(data).hexdigest()


def inspect_exported_context(
    *,
    context_root: Path,
    manifest_path: Path,
    expected_source_sha: str,
    expected_tree_sha: str,
) -> int:
    try:
        data = manifest_path.read_bytes()
        document = json.loads(data.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("CONTEXT_MANIFEST_INVALID")
    if not isinstance(document, dict) or _canonical_context_manifest(document) != data:
        _fail("CONTEXT_MANIFEST_INVALID")
    if (
        document.get("format_version") != 1
        or document.get("source_sha") != expected_source_sha
        or document.get("tree_sha") != expected_tree_sha
    ):
        _fail("CONTEXT_MANIFEST_IDENTITY_MISMATCH")
    object_format = document.get("git_object_format")
    records = document.get("files")
    if object_format not in {"sha1", "sha256"} or not isinstance(records, list):
        _fail("CONTEXT_MANIFEST_INVALID")
    expected: dict[str, dict[str, object]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "mode",
            "oid",
            "path",
            "sha256",
            "size",
        }:
            _fail("CONTEXT_MANIFEST_INVALID")
        path = record.get("path")
        if not isinstance(path, str) or path in expected:
            _fail("CONTEXT_MANIFEST_INVALID")
        expected[path] = record

    seen: set[str] = set()
    try:
        for path in context_root.rglob("*"):
            relative = path.relative_to(context_root).as_posix()
            if path.is_symlink():
                _fail("CONTEXT_SYMLINK_DENIED")
            if path.is_dir():
                _assert_context_path_allowed(relative)
                continue
            file_metadata = path.lstat()
            if not stat.S_ISREG(file_metadata.st_mode):
                _fail("CONTEXT_FILE_TYPE_DENIED")
            _assert_context_path_allowed(relative)
            record = expected.get(relative)
            if record is None or relative in seen:
                _fail("CONTEXT_TREE_MISMATCH")
            file_data = path.read_bytes()
            _assert_content_allowed(file_data)
            expected_mode = 0o755 if record["mode"] == "100755" else 0o644
            if (
                file_metadata.st_size != record["size"]
                or stat.S_IMODE(file_metadata.st_mode) != expected_mode
                or hashlib.sha256(file_data).hexdigest() != record["sha256"]
                or _git_blob_digest(file_data, str(object_format)) != record["oid"]
            ):
                _fail("CONTEXT_TREE_MISMATCH")
            seen.add(relative)
    except BuildContractError:
        raise
    except OSError:
        _fail("CONTEXT_INSPECTION_FAILED")
    if seen != set(expected):
        _fail("CONTEXT_TREE_MISMATCH")
    return len(seen)


def export_exact_git_context(
    *,
    repository_root: Path,
    source_sha: str,
    source_tree_sha: str,
    operation_root: Path,
) -> tuple[Path, Path, str, int]:
    context_root = operation_root / "git-tree-context"
    manifest_path = operation_root / "context-manifest.json"
    archive_path = operation_root / "git-tree.tar"
    try:
        context_root.mkdir(mode=0o700)
    except OSError:
        _fail("GIT_CONTEXT_CREATE_FAILED")
    entries = _git_tree_entries(repository_root, source_sha)
    object_format = _run_git(repository_root, "rev-parse", "--show-object-format")
    if object_format not in {"sha1", "sha256"}:
        _fail("GIT_OBJECT_FORMAT_UNSUPPORTED")
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repository_root),
                "-c",
                "tar.umask=0022",
                "archive",
                "--format=tar",
                f"--output={archive_path}",
                source_sha,
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        _fail("GIT_EXECUTION_FAILED")
    if completed.returncode != 0:
        _fail("GIT_ARCHIVE_FAILED")
    records = _extract_git_archive(
        archive_path=archive_path,
        context_root=context_root,
        entries=entries,
        object_format=object_format,
    )
    try:
        archive_path.unlink()
    except OSError:
        _fail("GIT_ARCHIVE_CLEANUP_FAILED")
    manifest_sha = _write_context_manifest(
        manifest_path=manifest_path,
        source_sha=source_sha,
        tree_sha=source_tree_sha,
        object_format=object_format,
        records=records,
    )
    file_count = inspect_exported_context(
        context_root=context_root,
        manifest_path=manifest_path,
        expected_source_sha=source_sha,
        expected_tree_sha=source_tree_sha,
    )
    return context_root, manifest_path, manifest_sha, file_count


@contextlib.contextmanager
def prepared_build_inputs(
    *,
    repository_root: Path,
    expected_source_sha: str,
    artifact_context: Path,
    expected_manifest_sha256: str,
    image_tag: str,
    platform: str,
) -> Iterator[BuildInputs]:
    request = validate_build_inputs(
        repository_root=repository_root,
        expected_source_sha=expected_source_sha,
        artifact_context=artifact_context,
        expected_manifest_sha256=expected_manifest_sha256,
        image_tag=image_tag,
        platform=platform,
    )
    with tempfile.TemporaryDirectory(prefix="hermes-exact-git-context-") as raw:
        operation_root = Path(raw)
        operation_root.chmod(0o700)
        context_root, context_manifest, context_sha, file_count = (
            export_exact_git_context(
                repository_root=request.repository_root,
                source_sha=request.source_sha,
                source_tree_sha=request.source_tree_sha,
                operation_root=operation_root,
            )
        )
        yield BuildInputs(
            repository_root=request.repository_root,
            source_sha=request.source_sha,
            source_tree_sha=request.source_tree_sha,
            build_context=context_root,
            context_manifest=context_manifest,
            context_manifest_sha256=context_sha,
            context_file_count=file_count,
            artifact_context=request.artifact_context,
            manifest_sha256=request.manifest_sha256,
            image_tag=request.image_tag,
            platform=request.platform,
        )


def docker_build_command(inputs: BuildInputs) -> list[str]:
    return [
        "docker",
        "build",
        "--platform",
        inputs.platform,
        "--build-context",
        f"playwright_artifact={inputs.artifact_context}",
        "--build-arg",
        f"HERMES_GIT_SHA={inputs.source_sha}",
        "--build-arg",
        (
            "PLAYWRIGHT_ARTIFACT_MANIFEST_SHA256="
            f"{inputs.manifest_sha256}"
        ),
        "--label",
        f"org.opencontainers.image.revision={inputs.source_sha}",
        "--tag",
        inputs.image_tag,
        str(inputs.build_context),
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or execute the canonical verified Playwright build."
    )
    parser.add_argument("mode", choices=("check", "build"))
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--artifact-context", required=True, type=Path)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--image-tag", required=True)
    parser.add_argument(
        "--platform", required=True, choices=("linux/amd64", "linux/arm64")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    try:
        with prepared_build_inputs(
            repository_root=repository_root,
            expected_source_sha=args.expected_source_sha,
            artifact_context=args.artifact_context,
            expected_manifest_sha256=args.expected_manifest_sha256,
            image_tag=args.image_tag,
            platform=args.platform,
        ) as inputs:
            if args.mode == "build":
                completed = subprocess.run(
                    docker_build_command(inputs),
                    check=False,
                    env={**os.environ, "DOCKER_BUILDKIT": "1"},
                )
                if completed.returncode != 0:
                    _fail("DOCKER_BUILD_FAILED")
            print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=PASS")
            print(f"MODE={args.mode}")
            print(f"SOURCE_SHA={inputs.source_sha}")
            print(f"SOURCE_TREE_SHA={inputs.source_tree_sha}")
            print(f"CONTEXT_MANIFEST_SHA256={inputs.context_manifest_sha256}")
            print(f"CONTEXT_FILE_COUNT={inputs.context_file_count}")
            print(f"PLATFORM={inputs.platform}")
            print("BUILD_CONTEXT_SOURCE=EXACT_GIT_TREE_EXPORT")
            print("ARTIFACT_CONTEXT_VERIFIED=true")
            print("MANIFEST_SHA256_VERIFIED=true")
            print(f"IMAGE_BUILD_PERFORMED={str(args.mode == 'build').lower()}")
    except (BuildContractError, ArtifactContractError) as exc:
        print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=FAIL", file=sys.stderr)
        print(f"ERROR_CLASS={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=FAIL", file=sys.stderr)
        print("ERROR_CLASS=INTERNAL_ERROR", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
