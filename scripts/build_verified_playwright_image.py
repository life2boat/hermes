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

from scripts.git_object_secret_policy import (
    GitObjectAcquisitionError,
    GitObjectDescriptor,
    candidate_tree_entries,
    scan_descriptors,
)
from scripts.install_pinned_playwright_artifact import (
    ArtifactContractError,
    load_verified_closure_manifest,
    validate_closure_manifest_contract,
)
from scripts.playwright_artifact_contract import (
    PlaywrightContractError,
    load_verified_wheel_closure,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_TAG_RE = re.compile(
    r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$"
)
_FIXED_ARTIFACT_CONTEXT_ENTRIES = frozenset({
    "artifacts",
    "closure.json",
    "playwright-wheel",
})
_FORBIDDEN_DIRECTORY_NAMES = frozenset({
    "__pycache__",
    ".pytest_cache",
    ".pytest-cache",
    ".ruff_cache",
    ".codex-remote-edit",
    "evidence",
    "review-mirrors",
})
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


class BuildContractError(RuntimeError):
    def __init__(self, code: str, *, exit_class: str = "INTERNAL_ERROR") -> None:
        super().__init__(code)
        self.code = code
        self.exit_class = exit_class
        self.exit_code = {
            "SECURITY_DENIED": 1,
            "INTERNAL_ERROR": 2,
        }[exit_class]


@dataclass(frozen=True)
class BuildRequest:
    repository_root: Path
    source_sha: str
    source_tree_sha: str
    approved_base_sha: str
    approved_base_tree_sha: str
    artifact_context: Path
    closure_manifest_sha256: str
    image_tag: str
    platform: str


@dataclass(frozen=True)
class BuildInputs:
    repository_root: Path
    source_sha: str
    source_tree_sha: str
    approved_base_sha: str
    approved_base_tree_sha: str
    build_context: Path
    context_manifest: Path
    context_manifest_sha256: str
    context_file_count: int
    artifact_context: Path
    closure_manifest_sha256: str
    image_tag: str
    platform: str


def _fail(code: str, *, exit_class: str = "INTERNAL_ERROR") -> None:
    raise BuildContractError(code, exit_class=exit_class)


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


def _validate_repository(
    root: Path,
    expected_sha: str,
    approved_base_sha: str,
) -> tuple[str, str]:
    if _SHA_RE.fullmatch(expected_sha) is None:
        _fail("SOURCE_SHA_INVALID")
    if _SHA_RE.fullmatch(approved_base_sha) is None:
        _fail("APPROVED_BASE_SHA_INVALID")
    if _run_git(root, "rev-parse", "HEAD") != expected_sha:
        _fail("SOURCE_SHA_MISMATCH")
    _run_git(root, "cat-file", "-e", f"{expected_sha}^{{commit}}")
    _run_git(root, "cat-file", "-e", f"{approved_base_sha}^{{commit}}")
    _run_git(root, "merge-base", "--is-ancestor", approved_base_sha, expected_sha)
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=no"):
        _fail("SOURCE_TRACKED_WORKTREE_DIRTY")
    if _run_git(root, "diff", "--check"):
        _fail("SOURCE_DIFF_CHECK_FAILED")
    tree_sha = _run_git(root, "rev-parse", f"{expected_sha}^{{tree}}")
    base_tree_sha = _run_git(root, "rev-parse", f"{approved_base_sha}^{{tree}}")
    if _SHA_RE.fullmatch(tree_sha) is None or _SHA_RE.fullmatch(base_tree_sha) is None:
        _fail("SOURCE_TREE_SHA_INVALID")
    return tree_sha, base_tree_sha


def _validate_regular_context_file(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        _fail("ARTIFACT_FILE_METADATA_FAILED")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _fail("ARTIFACT_FILE_METADATA_INVALID")
    return metadata


def _validate_context_directory(path: Path) -> set[str]:
    try:
        metadata = path.lstat()
        children = {child.name for child in path.iterdir()}
    except OSError:
        _fail("ARTIFACT_CONTEXT_READ_FAILED")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_nlink < 2
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _fail("ARTIFACT_CONTEXT_METADATA_INVALID")
    return children


def _manifest_artifacts(document: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, list):
        _fail("ARTIFACT_CONTEXT_CONTENTS_INVALID")
    result: dict[str, dict[str, object]] = {}
    for item in raw_artifacts:
        if not isinstance(item, dict):
            _fail("ARTIFACT_CONTEXT_CONTENTS_INVALID")
        name = item.get("artifact_name")
        if not isinstance(name, str) or name in result:
            _fail("ARTIFACT_CONTEXT_CONTENTS_INVALID")
        result[name] = item
    return result


def _validate_artifact_context(
    root: Path,
    repository_root: Path,
    expected_closure_manifest_sha256: str,
    platform: str,
) -> None:
    if not root.is_absolute():
        _fail("ARTIFACT_CONTEXT_NOT_ABSOLUTE")
    _assert_no_symlink_components(root)
    resolved_root = root.resolve(strict=True)
    resolved_repository = repository_root.resolve(strict=True)
    if _is_within(resolved_root, resolved_repository):
        _fail("ARTIFACT_CONTEXT_INSIDE_REPOSITORY")
    if _validate_context_directory(resolved_root) != (
        _FIXED_ARTIFACT_CONTEXT_ENTRIES
    ):
        _fail("ARTIFACT_CONTEXT_CONTENTS_INVALID")

    closure_path = resolved_root / "closure.json"
    wheel_path = resolved_root / "playwright-wheel"
    _validate_regular_context_file(closure_path)
    _validate_regular_context_file(wheel_path)

    try:
        manifest = load_verified_closure_manifest(
            closure_path, expected_closure_manifest_sha256
        )
        verified = load_verified_wheel_closure(
            lockfile_path=resolved_repository / "uv.lock",
            wheel_path=wheel_path,
            platform=platform,
        )
        validate_closure_manifest_contract(manifest, verified)
    except (ArtifactContractError, PlaywrightContractError) as exc:
        raise BuildContractError(exc.code) from None

    artifacts_root = resolved_root / "artifacts"
    if _validate_context_directory(artifacts_root) != set(
        verified.closure.artifact_names
    ):
        _fail("ARTIFACT_CONTEXT_CONTENTS_INVALID")
    artifacts = _manifest_artifacts(manifest)
    for artifact in verified.closure.artifacts:
        artifact_root = artifacts_root / artifact.artifact_name
        if _validate_context_directory(artifact_root) != {"archive"}:
            _fail("ARTIFACT_CONTEXT_CONTENTS_INVALID")
        archive = artifact_root / "archive"
        archive_metadata = _validate_regular_context_file(archive)
        artifact_manifest = artifacts[artifact.artifact_name]
        if archive_metadata.st_size != artifact_manifest["archive_size"]:
            _fail("ARTIFACT_ARCHIVE_SIZE_MISMATCH")
        digest = hashlib.sha256()
        try:
            with archive.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            _fail("ARTIFACT_ARCHIVE_READ_FAILED")
        if digest.hexdigest() != artifact_manifest["archive_sha256"]:
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
    approved_base_sha: str,
    artifact_context: Path,
    expected_closure_manifest_sha256: str,
    image_tag: str,
    platform: str,
) -> BuildRequest:
    if _SHA256_RE.fullmatch(expected_closure_manifest_sha256) is None:
        _fail("EXPECTED_CLOSURE_MANIFEST_SHA256_INVALID")
    if platform not in {"linux/amd64", "linux/arm64"}:
        _fail("PLATFORM_UNSUPPORTED")
    resolved_repository = repository_root.resolve(strict=True)
    source_tree_sha, approved_base_tree_sha = _validate_repository(
        resolved_repository,
        expected_source_sha,
        approved_base_sha,
    )
    _validate_artifact_context(
        artifact_context,
        resolved_repository,
        expected_closure_manifest_sha256,
        platform,
    )
    _validate_image_tag(image_tag, expected_source_sha)
    return BuildRequest(
        repository_root=resolved_repository,
        source_sha=expected_source_sha,
        source_tree_sha=source_tree_sha,
        approved_base_sha=approved_base_sha,
        approved_base_tree_sha=approved_base_tree_sha,
        artifact_context=artifact_context.resolve(strict=True),
        closure_manifest_sha256=expected_closure_manifest_sha256,
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
        part in {"", ".", ".."} or part.endswith((".", " ")) or ":" in part
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


def _validated_git_tree_entries(
    *,
    repository_root: Path,
    approved_base_sha: str,
    source_sha: str,
) -> list[GitObjectDescriptor]:
    try:
        source_entries, candidate_entries = candidate_tree_entries(
            repository_root=repository_root,
            approved_base_sha=approved_base_sha,
            source_sha=source_sha,
        )
        outcomes = scan_descriptors(
            repository_root=repository_root,
            descriptors=candidate_entries,
        )
    except GitObjectAcquisitionError as exc:
        _fail(exc.code)
    except Exception:
        _fail("GIT_OBJECT_SCAN_INTERNAL_ERROR")
    for entry in source_entries:
        _assert_context_path_allowed(entry.path)
    for outcome in outcomes:
        if not outcome.clean:
            _fail(
                outcome.error_code or "GIT_OBJECT_SCAN_INTERNAL_ERROR",
                exit_class=outcome.exit_class,
            )
    return list(source_entries)


def _git_blob_digest(data: bytes, object_format: str) -> str:
    if object_format not in {"sha1", "sha256"}:
        _fail("GIT_OBJECT_FORMAT_UNSUPPORTED")
    digest = hashlib.new(object_format)
    digest.update(f"blob {len(data)}\0".encode("ascii"))
    digest.update(data)
    return digest.hexdigest()


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
    entries: list[GitObjectDescriptor],
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
            name = (
                member.name[:-1]
                if member.isdir() and member.name.endswith("/")
                else member.name
            )
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
            records.append({
                "mode": entry.mode,
                "oid": entry.oid,
                "path": safe_name,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            })
            seen.add(safe_name)
    if seen != set(expected):
        _fail("GIT_ARCHIVE_TREE_MISMATCH")
    return sorted(records, key=lambda record: str(record["path"]))


def _write_context_manifest(
    *,
    manifest_path: Path,
    source_sha: str,
    tree_sha: str,
    approved_base_sha: str,
    approved_base_tree_sha: str,
    object_format: str,
    records: list[dict[str, object]],
) -> str:
    document: dict[str, object] = {
        "approved_base_sha": approved_base_sha,
        "approved_base_tree_sha": approved_base_tree_sha,
        "files": records,
        "format_version": 2,
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
    expected_base_sha: str,
    expected_base_tree_sha: str,
) -> int:
    try:
        data = manifest_path.read_bytes()
        document = json.loads(data.decode("ascii"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("CONTEXT_MANIFEST_INVALID")
    if not isinstance(document, dict) or _canonical_context_manifest(document) != data:
        _fail("CONTEXT_MANIFEST_INVALID")
    if (
        document.get("format_version") != 2
        or document.get("source_sha") != expected_source_sha
        or document.get("tree_sha") != expected_tree_sha
        or document.get("approved_base_sha") != expected_base_sha
        or document.get("approved_base_tree_sha") != expected_base_tree_sha
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
    approved_base_sha: str,
    approved_base_tree_sha: str,
    operation_root: Path,
) -> tuple[Path, Path, str, int]:
    identities = (
        source_sha,
        source_tree_sha,
        approved_base_sha,
        approved_base_tree_sha,
    )
    if any(_SHA_RE.fullmatch(identity) is None for identity in identities):
        _fail("GIT_EXPORT_IDENTITY_INVALID")
    if (
        _run_git(repository_root, "rev-parse", f"{source_sha}^{{tree}}")
        != source_tree_sha
        or _run_git(
            repository_root,
            "rev-parse",
            f"{approved_base_sha}^{{tree}}",
        )
        != approved_base_tree_sha
    ):
        _fail("GIT_EXPORT_IDENTITY_MISMATCH")
    _run_git(
        repository_root, "merge-base", "--is-ancestor", approved_base_sha, source_sha
    )
    context_root = operation_root / "git-tree-context"
    manifest_path = operation_root / "context-manifest.json"
    archive_path = operation_root / "git-tree.tar"
    try:
        context_root.mkdir(mode=0o700)
    except OSError:
        _fail("GIT_CONTEXT_CREATE_FAILED")
    entries = _validated_git_tree_entries(
        repository_root=repository_root,
        approved_base_sha=approved_base_sha,
        source_sha=source_sha,
    )
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
        approved_base_sha=approved_base_sha,
        approved_base_tree_sha=approved_base_tree_sha,
        object_format=object_format,
        records=records,
    )
    file_count = inspect_exported_context(
        context_root=context_root,
        manifest_path=manifest_path,
        expected_source_sha=source_sha,
        expected_tree_sha=source_tree_sha,
        expected_base_sha=approved_base_sha,
        expected_base_tree_sha=approved_base_tree_sha,
    )
    return context_root, manifest_path, manifest_sha, file_count


@contextlib.contextmanager
def prepared_build_inputs(
    *,
    repository_root: Path,
    expected_source_sha: str,
    approved_base_sha: str,
    artifact_context: Path,
    expected_closure_manifest_sha256: str,
    image_tag: str,
    platform: str,
) -> Iterator[BuildInputs]:
    request = validate_build_inputs(
        repository_root=repository_root,
        expected_source_sha=expected_source_sha,
        approved_base_sha=approved_base_sha,
        artifact_context=artifact_context,
        expected_closure_manifest_sha256=expected_closure_manifest_sha256,
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
                approved_base_sha=request.approved_base_sha,
                approved_base_tree_sha=request.approved_base_tree_sha,
                operation_root=operation_root,
            )
        )
        yield BuildInputs(
            repository_root=request.repository_root,
            source_sha=request.source_sha,
            source_tree_sha=request.source_tree_sha,
            approved_base_sha=request.approved_base_sha,
            approved_base_tree_sha=request.approved_base_tree_sha,
            build_context=context_root,
            context_manifest=context_manifest,
            context_manifest_sha256=context_sha,
            context_file_count=file_count,
            artifact_context=request.artifact_context,
            closure_manifest_sha256=request.closure_manifest_sha256,
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
        f"playwright_artifacts={inputs.artifact_context}",
        "--build-arg",
        f"HERMES_GIT_SHA={inputs.source_sha}",
        "--build-arg",
        (f"PLAYWRIGHT_ARTIFACT_CLOSURE_SHA256={inputs.closure_manifest_sha256}"),
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
    parser.add_argument("--approved-base-sha", required=True)
    parser.add_argument("--artifact-context", required=True, type=Path)
    parser.add_argument("--expected-closure-manifest-sha256", required=True)
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
            approved_base_sha=args.approved_base_sha,
            artifact_context=args.artifact_context,
            expected_closure_manifest_sha256=args.expected_closure_manifest_sha256,
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
            print(f"APPROVED_BASE_SHA={inputs.approved_base_sha}")
            print(f"APPROVED_BASE_TREE_SHA={inputs.approved_base_tree_sha}")
            print(f"CONTEXT_MANIFEST_SHA256={inputs.context_manifest_sha256}")
            print(f"CONTEXT_FILE_COUNT={inputs.context_file_count}")
            print(f"PLATFORM={inputs.platform}")
            print("BUILD_CONTEXT_SOURCE=EXACT_GIT_TREE_EXPORT")
            print("ARTIFACT_CLOSURE_CONTEXT_VERIFIED=true")
            print("CLOSURE_MANIFEST_SHA256_VERIFIED=true")
            print(f"IMAGE_BUILD_PERFORMED={str(args.mode == 'build').lower()}")
    except (BuildContractError, ArtifactContractError) as exc:
        print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=FAIL", file=sys.stderr)
        print(f"ERROR_CLASS={exc.code}", file=sys.stderr)
        exit_class = getattr(exc, "exit_class", "INTERNAL_ERROR")
        print(f"ERROR_EXIT_CLASS={exit_class}", file=sys.stderr)
        return getattr(exc, "exit_code", 2)
    except Exception:
        print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=FAIL", file=sys.stderr)
        print("ERROR_CLASS=INTERNAL_ERROR", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
