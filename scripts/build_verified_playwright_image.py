from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.install_pinned_playwright_artifact import (
    ArtifactContractError,
    load_verified_manifest,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_TAG_RE = re.compile(
    r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*(?::[A-Za-z0-9][A-Za-z0-9_.-]{0,127})$"
)
_FIXED_ARTIFACT_FILES = frozenset({"manifest.json", "browser-archive"})


class BuildContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BuildInputs:
    repository_root: Path
    source_sha: str
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


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            _fail("ARTIFACT_CONTEXT_MISSING")
        except OSError:
            _fail("ARTIFACT_CONTEXT_METADATA_FAILED")
        if stat.S_ISLNK(metadata.st_mode):
            _fail("ARTIFACT_CONTEXT_SYMLINK_DENIED")


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_repository(root: Path, expected_sha: str) -> None:
    if _SHA_RE.fullmatch(expected_sha) is None:
        _fail("SOURCE_SHA_INVALID")
    if _run_git(root, "rev-parse", "HEAD") != expected_sha:
        _fail("SOURCE_SHA_MISMATCH")
    if _run_git(root, "status", "--porcelain=v1", "--untracked-files=normal"):
        _fail("SOURCE_WORKTREE_DIRTY")
    if _run_git(root, "diff", "--check"):
        _fail("SOURCE_DIFF_CHECK_FAILED")


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

    try:
        manifest = load_verified_manifest(
            resolved_root / "manifest.json", expected_manifest_sha256
        )
    except ArtifactContractError as exc:
        raise BuildContractError(exc.code) from None
    if manifest["platform"] != platform:
        _fail("ARTIFACT_PLATFORM_MISMATCH")
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
) -> BuildInputs:
    if _SHA256_RE.fullmatch(expected_manifest_sha256) is None:
        _fail("EXPECTED_MANIFEST_SHA256_INVALID")
    if platform not in {"linux/amd64", "linux/arm64"}:
        _fail("PLATFORM_UNSUPPORTED")
    _validate_repository(repository_root, expected_source_sha)
    _validate_artifact_context(
        artifact_context,
        repository_root,
        expected_manifest_sha256,
        platform,
    )
    _validate_image_tag(image_tag, expected_source_sha)
    return BuildInputs(
        repository_root=repository_root,
        source_sha=expected_source_sha,
        artifact_context=artifact_context.resolve(strict=True),
        manifest_sha256=expected_manifest_sha256,
        image_tag=image_tag,
        platform=platform,
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
        str(inputs.repository_root),
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
        inputs = validate_build_inputs(
            repository_root=repository_root,
            expected_source_sha=args.expected_source_sha,
            artifact_context=args.artifact_context,
            expected_manifest_sha256=args.expected_manifest_sha256,
            image_tag=args.image_tag,
            platform=args.platform,
        )
        if args.mode == "build":
            completed = subprocess.run(
                docker_build_command(inputs),
                check=False,
                env={**os.environ, "DOCKER_BUILDKIT": "1"},
            )
            if completed.returncode != 0:
                _fail("DOCKER_BUILD_FAILED")
    except (BuildContractError, ArtifactContractError) as exc:
        print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=FAIL", file=sys.stderr)
        print(f"ERROR_CLASS={exc.code}", file=sys.stderr)
        return 2
    except Exception:
        print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=FAIL", file=sys.stderr)
        print("ERROR_CLASS=INTERNAL_ERROR", file=sys.stderr)
        return 2

    print("PLAYWRIGHT_IMAGE_BUILD_CONTRACT=PASS")
    print(f"MODE={args.mode}")
    print(f"SOURCE_SHA={inputs.source_sha}")
    print(f"PLATFORM={inputs.platform}")
    print("ARTIFACT_CONTEXT_VERIFIED=true")
    print("MANIFEST_SHA256_VERIFIED=true")
    print(f"IMAGE_BUILD_PERFORMED={str(args.mode == 'build').lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
