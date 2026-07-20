from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform as host_platform
import re
import stat
import tomllib
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any



PLAYWRIGHT_PACKAGE = "playwright"
PRIMARY_BROWSER_FAMILY = "chromium-headless-shell"
FFMPEG_FAMILY = "ffmpeg"
CACHE_ROOT = "/opt/hermes/.playwright"
INSTALLATION_MARKER = "INSTALLATION_COMPLETE"
LAYOUT_DIRECTORY_TREE = "DIRECTORY_TREE"
LAYOUT_SINGLE_EXECUTABLE_FILE = "SINGLE_EXECUTABLE_FILE"
_BROWSER_METADATA_PATH = "playwright/driver/package/browsers.json"
_MAX_LOCKFILE_BYTES = 32 * 1024 * 1024
_MAX_WHEEL_BYTES = 128 * 1024 * 1024
_MAX_METADATA_BYTES = 128 * 1024
_MAX_PACKAGE_METADATA_BYTES = 64 * 1024
_MAX_IDENTITY_BYTES = 16 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.-]+)?$")
_REVISION_RE = re.compile(r"^[0-9]+$")
_WHEEL_TAGS = {
    "linux/amd64": "manylinux1_x86_64",
    "linux/arm64": "manylinux_2_17_aarch64.manylinux2014_aarch64",
}
_IDENTITY_FIELDS = frozenset(
    {
        "identity_version",
        "playwright_package",
        "playwright_package_version",
        "playwright_wheel_filename",
        "playwright_wheel_size",
        "playwright_wheel_sha256",
        "platform",
        "cache_root",
        "artifacts",
    }
)
_ARTIFACT_IDENTITY_FIELDS = frozenset(
    {
        "artifact_name",
        "browser_family",
        "revision",
        "browser_version",
        "cache_directory",
        "layout_kind",
        "expected_executable_relative_path",
    }
)


class PlaywrightContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactPlatformMapping:
    playwright_host_platform: str
    executable_relative_path: str
    archive_filename: str
    layout_kind: str


ARTIFACT_PLATFORM_MAPPINGS = {
    PRIMARY_BROWSER_FAMILY: {
        "linux/amd64": ArtifactPlatformMapping(
            playwright_host_platform="debian13-x64",
            executable_relative_path=(
                "chrome-headless-shell-linux64/chrome-headless-shell"
            ),
            archive_filename="chrome-headless-shell-linux64.zip",
            layout_kind=LAYOUT_DIRECTORY_TREE,
        ),
        "linux/arm64": ArtifactPlatformMapping(
            playwright_host_platform="debian13-arm64",
            executable_relative_path="chrome-linux/headless_shell",
            archive_filename="chromium-headless-shell-linux-arm64.zip",
            layout_kind=LAYOUT_DIRECTORY_TREE,
        ),
    },
    FFMPEG_FAMILY: {
        "linux/amd64": ArtifactPlatformMapping(
            playwright_host_platform="debian13-x64",
            executable_relative_path="ffmpeg-linux",
            archive_filename="ffmpeg-linux.zip",
            layout_kind=LAYOUT_SINGLE_EXECUTABLE_FILE,
        ),
        "linux/arm64": ArtifactPlatformMapping(
            playwright_host_platform="debian13-arm64",
            executable_relative_path="ffmpeg-linux",
            archive_filename="ffmpeg-linux-arm64.zip",
            layout_kind=LAYOUT_SINGLE_EXECUTABLE_FILE,
        ),
    },
}
REQUIRED_ARTIFACT_NAMES = tuple(sorted(ARTIFACT_PLATFORM_MAPPINGS))
SUPPORTED_PLATFORMS = tuple(
    sorted(
        set.intersection(
            *(set(mappings) for mappings in ARTIFACT_PLATFORM_MAPPINGS.values())
        )
    )
)


@dataclass(frozen=True)
class ArtifactContract:
    package: str
    package_version: str
    artifact_name: str
    browser_family: str
    revision: str
    browser_version: str | None
    platform: str
    cache_root: str
    cache_directory: str
    expected_archive_filename: str
    layout_kind: str
    expected_executable_relative_path: str

    @property
    def archive_root(self) -> str:
        return self.expected_executable_relative_path.split("/", 1)[0]

    @property
    def expected_cache_layout(self) -> str:
        return str(
            PurePosixPath(self.cache_root)
            / self.cache_directory
            / self.expected_executable_relative_path
        )


@dataclass(frozen=True)
class PlaywrightClosureContract:
    package: str
    package_version: str
    platform: str
    cache_root: str
    artifacts: tuple[ArtifactContract, ...]

    @property
    def artifact_names(self) -> tuple[str, ...]:
        return tuple(artifact.artifact_name for artifact in self.artifacts)

    def artifact(self, name: str) -> ArtifactContract:
        matches = [item for item in self.artifacts if item.artifact_name == name]
        if len(matches) != 1:
            _fail("ARTIFACT_METADATA_AMBIGUOUS")
        return matches[0]


@dataclass(frozen=True)
class LockedWheel:
    package: str
    package_version: str
    filename: str
    size: int
    sha256: str
    platform: str


@dataclass(frozen=True)
class VerifiedPlaywrightClosure:
    closure: PlaywrightClosureContract
    wheel: LockedWheel


def _fail(code: str) -> None:
    raise PlaywrightContractError(code)


def _required_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(code)
    return value


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _canonical_json(document: dict[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")



def contract_from_metadata(
    *, package_version: str, browsers_payload: object, platform: str
) -> PlaywrightClosureContract:
    if platform not in SUPPORTED_PLATFORMS:
        _fail("PLATFORM_UNSUPPORTED")
    if _VERSION_RE.fullmatch(package_version) is None:
        _fail("PACKAGE_VERSION_INVALID")
    if not isinstance(browsers_payload, dict):
        _fail("PACKAGE_METADATA_INVALID")
    browsers = browsers_payload.get("browsers")
    if not isinstance(browsers, list):
        _fail("PACKAGE_METADATA_INVALID")

    artifacts: list[ArtifactContract] = []
    for artifact_name in REQUIRED_ARTIFACT_NAMES:
        mapping = ARTIFACT_PLATFORM_MAPPINGS[artifact_name][platform]
        matches = [
            item
            for item in browsers
            if isinstance(item, dict) and item.get("name") == artifact_name
        ]
        if len(matches) != 1:
            _fail("ARTIFACT_METADATA_AMBIGUOUS")
        artifact_metadata = matches[0]
        if artifact_metadata.get("installByDefault") is not True:
            _fail("REQUIRED_ARTIFACT_NOT_INSTALL_BY_DEFAULT")
        base_revision = _required_string(
            artifact_metadata.get("revision"), "ARTIFACT_REVISION_INVALID"
        )
        if _REVISION_RE.fullmatch(base_revision) is None:
            _fail("ARTIFACT_REVISION_INVALID")
        overrides = artifact_metadata.get("revisionOverrides", {})
        if not isinstance(overrides, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in overrides.items()
        ):
            _fail("ARTIFACT_REVISION_OVERRIDES_INVALID")
        revision_override = overrides.get(mapping.playwright_host_platform)
        revision = revision_override or base_revision
        if _REVISION_RE.fullmatch(revision) is None:
            _fail("ARTIFACT_REVISION_INVALID")
        browser_version = artifact_metadata.get("browserVersion")
        if browser_version is not None and (
            not isinstance(browser_version, str) or not browser_version
        ):
            _fail("ARTIFACT_BROWSER_VERSION_INVALID")
        directory_prefix = artifact_name
        if revision_override is not None:
            directory_prefix = (
                f"{artifact_name}_{mapping.playwright_host_platform}_special"
            )
        cache_directory = f"{directory_prefix.replace('-', '_')}-{revision}"
        artifacts.append(
            ArtifactContract(
                package=PLAYWRIGHT_PACKAGE,
                package_version=package_version,
                artifact_name=artifact_name,
                browser_family=artifact_name,
                revision=revision,
                browser_version=browser_version,
                platform=platform,
                cache_root=CACHE_ROOT,
                cache_directory=cache_directory,
                expected_archive_filename=mapping.archive_filename,
                layout_kind=mapping.layout_kind,
                expected_executable_relative_path=(
                    mapping.executable_relative_path
                ),
            )
        )
    return PlaywrightClosureContract(
        package=PLAYWRIGHT_PACKAGE,
        package_version=package_version,
        platform=platform,
        cache_root=CACHE_ROOT,
        artifacts=tuple(artifacts),
    )


def _read_regular_file(path: Path, *, maximum: int, code: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        _fail(code)
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or not 0 < file_stat.st_size <= maximum
        ):
            _fail(code)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(maximum + 1)
        if len(data) != file_stat.st_size or len(data) > maximum:
            _fail(code)
        return data
    except PlaywrightContractError:
        raise
    except OSError:
        _fail(code)
    finally:
        os.close(descriptor)


def _wheel_filename(url: object) -> str:
    if not isinstance(url, str) or not url or "?" in url or "#" in url:
        _fail("LOCKFILE_WHEEL_URL_INVALID")
    filename = url.rsplit("/", 1)[-1]
    if not filename or "/" in filename or "\\" in filename:
        _fail("LOCKFILE_WHEEL_URL_INVALID")
    return filename


def load_locked_wheel(lockfile_path: Path, platform: str) -> LockedWheel:
    wheel_tag = _WHEEL_TAGS.get(platform)
    if wheel_tag is None:
        _fail("PLATFORM_UNSUPPORTED")
    data = _read_regular_file(
        lockfile_path,
        maximum=_MAX_LOCKFILE_BYTES,
        code="LOCKFILE_READ_FAILED",
    )
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError):
        _fail("LOCKFILE_INVALID")
    packages = document.get("package")
    if not isinstance(packages, list):
        _fail("LOCKFILE_INVALID")
    matches = [
        item
        for item in packages
        if isinstance(item, dict) and item.get("name") == PLAYWRIGHT_PACKAGE
    ]
    if len(matches) != 1:
        _fail("LOCKFILE_PACKAGE_AMBIGUOUS")
    package = matches[0]
    version = package.get("version")
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        _fail("LOCKFILE_PACKAGE_VERSION_INVALID")
    expected_filename = (
        f"{PLAYWRIGHT_PACKAGE}-{version}-py3-none-{wheel_tag}.whl"
    )
    wheels = package.get("wheels")
    if not isinstance(wheels, list):
        _fail("LOCKFILE_WHEEL_INDEX_INVALID")
    selected = [
        wheel
        for wheel in wheels
        if isinstance(wheel, dict)
        and _wheel_filename(wheel.get("url")) == expected_filename
    ]
    if len(selected) != 1:
        _fail("LOCKFILE_WHEEL_AMBIGUOUS")
    wheel = selected[0]
    raw_hash = wheel.get("hash")
    if (
        not isinstance(raw_hash, str)
        or not raw_hash.startswith("sha256:")
        or _SHA256_RE.fullmatch(raw_hash.removeprefix("sha256:")) is None
    ):
        _fail("LOCKFILE_WHEEL_HASH_INVALID")
    size = wheel.get("size")
    if type(size) is not int or not 0 < size <= _MAX_WHEEL_BYTES:
        _fail("LOCKFILE_WHEEL_SIZE_INVALID")
    return LockedWheel(
        package=PLAYWRIGHT_PACKAGE,
        package_version=version,
        filename=expected_filename,
        size=size,
        sha256=raw_hash.removeprefix("sha256:"),
        platform=platform,
    )


def _read_zip_entry(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum: int,
    code: str,
) -> bytes:
    if info.flag_bits & 0x1 or not 0 < info.file_size <= maximum:
        _fail(code)
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile):
        _fail(code)
    if len(data) != info.file_size:
        _fail(code)
    return data


def _metadata_from_wheel_bytes(
    wheel_bytes: bytes,
    wheel: LockedWheel,
) -> tuple[str, dict[str, Any]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(wheel_bytes))
    except (OSError, zipfile.BadZipFile):
        _fail("WHEEL_ARCHIVE_INVALID")
    with archive:
        try:
            entries = archive.infolist()
        except (OSError, zipfile.BadZipFile):
            _fail("WHEEL_ARCHIVE_INVALID")
        browser_entries = [
            entry for entry in entries if entry.filename == _BROWSER_METADATA_PATH
        ]
        if len(browser_entries) != 1:
            _fail("BROWSER_METADATA_ENTRY_AMBIGUOUS")
        package_metadata_path = (
            f"{PLAYWRIGHT_PACKAGE}-{wheel.package_version}.dist-info/METADATA"
        )
        package_entries = [
            entry for entry in entries if entry.filename == package_metadata_path
        ]
        if len(package_entries) != 1:
            _fail("PACKAGE_METADATA_ENTRY_AMBIGUOUS")
        browser_data = _read_zip_entry(
            archive,
            browser_entries[0],
            maximum=_MAX_METADATA_BYTES,
            code="BROWSER_METADATA_READ_FAILED",
        )
        package_data = _read_zip_entry(
            archive,
            package_entries[0],
            maximum=_MAX_PACKAGE_METADATA_BYTES,
            code="PACKAGE_METADATA_READ_FAILED",
        )

    try:
        message = BytesParser(policy=policy.default).parsebytes(package_data)
    except (TypeError, ValueError):
        _fail("PACKAGE_METADATA_INVALID")
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if names != [PLAYWRIGHT_PACKAGE] or versions != [wheel.package_version]:
        _fail("PACKAGE_VERSION_MISMATCH")
    try:
        payload = json.loads(
            browser_data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (_DuplicateJsonKey, UnicodeError, json.JSONDecodeError):
        _fail("BROWSER_METADATA_JSON_INVALID")
    if not isinstance(payload, dict):
        _fail("PACKAGE_METADATA_INVALID")
    return wheel.package_version, payload



def load_verified_wheel_closure(
    *,
    lockfile_path: Path,
    wheel_path: Path,
    platform: str,
) -> VerifiedPlaywrightClosure:
    wheel = load_locked_wheel(lockfile_path, platform)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(wheel_path, flags)
    except OSError:
        _fail("WHEEL_FILE_INVALID")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != wheel.size
        ):
            _fail("WHEEL_FILE_INVALID")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            wheel_bytes = handle.read(_MAX_WHEEL_BYTES + 1)
        if len(wheel_bytes) != before.st_size:
            _fail("WHEEL_SIZE_MISMATCH")
        if hashlib.sha256(wheel_bytes).hexdigest() != wheel.sha256:
            _fail("WHEEL_SHA256_MISMATCH")

        package_version, payload = _metadata_from_wheel_bytes(wheel_bytes, wheel)

        after = os.fstat(descriptor)
        try:
            path_stat = wheel_path.lstat()
        except OSError:
            _fail("WHEEL_CHANGED_DURING_READ")
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(before, field) != getattr(after, field)
            for field in stable_fields
        ):
            _fail("WHEEL_CHANGED_DURING_READ")
        if any(
            getattr(after, field) != getattr(path_stat, field)
            for field in stable_fields
        ):
            _fail("WHEEL_CHANGED_DURING_READ")
    except PlaywrightContractError:
        raise
    except OSError:
        _fail("WHEEL_READ_FAILED")
    finally:
        os.close(descriptor)

    closure = contract_from_metadata(
        package_version=package_version,
        browsers_payload=payload,
        platform=platform,
    )
    return VerifiedPlaywrightClosure(closure=closure, wheel=wheel)


def _load_installed_browsers_payload() -> tuple[str, dict[str, Any]]:
    try:
        distribution = metadata.distribution(PLAYWRIGHT_PACKAGE)
    except metadata.PackageNotFoundError:
        _fail("PLAYWRIGHT_PACKAGE_NOT_INSTALLED")
    files = distribution.files
    if files is None:
        _fail("PACKAGE_FILE_INDEX_MISSING")
    metadata_files = [
        file for file in files if str(file) == _BROWSER_METADATA_PATH
    ]
    if len(metadata_files) != 1:
        _fail("BROWSER_METADATA_MISSING_OR_AMBIGUOUS")
    path = Path(distribution.locate_file(metadata_files[0]))
    data = _read_regular_file(
        path,
        maximum=_MAX_METADATA_BYTES,
        code="BROWSER_METADATA_FILE_INVALID",
    )
    try:
        payload = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (_DuplicateJsonKey, UnicodeError, json.JSONDecodeError):
        _fail("BROWSER_METADATA_JSON_INVALID")
    if not isinstance(payload, dict):
        _fail("PACKAGE_METADATA_INVALID")
    return distribution.version, payload



def load_installed_closure(platform: str) -> PlaywrightClosureContract:
    package_version, payload = _load_installed_browsers_payload()
    return contract_from_metadata(
        package_version=package_version,
        browsers_payload=payload,
        platform=platform,
    )


def artifact_identity_document(artifact: ArtifactContract) -> dict[str, object]:
    return {
        "artifact_name": artifact.artifact_name,
        "browser_family": artifact.browser_family,
        "revision": artifact.revision,
        "browser_version": artifact.browser_version,
        "cache_directory": artifact.cache_directory,
        "layout_kind": artifact.layout_kind,
        "expected_executable_relative_path": (
            artifact.expected_executable_relative_path
        ),
    }


def installation_identity_document(
    verified: VerifiedPlaywrightClosure,
) -> dict[str, object]:
    closure = verified.closure
    wheel = verified.wheel
    return {
        "identity_version": 1,
        "playwright_package": closure.package,
        "playwright_package_version": closure.package_version,
        "playwright_wheel_filename": wheel.filename,
        "playwright_wheel_sha256": wheel.sha256,
        "playwright_wheel_size": wheel.size,
        "platform": closure.platform,
        "cache_root": closure.cache_root,
        "artifacts": [
            artifact_identity_document(artifact)
            for artifact in closure.artifacts
        ],
    }


def canonical_installation_identity(
    verified: VerifiedPlaywrightClosure,
) -> bytes:
    return _canonical_json(installation_identity_document(verified))


def _parse_installation_identity(data: bytes) -> dict[str, object]:
    if not data or len(data) > _MAX_IDENTITY_BYTES:
        _fail("CLOSURE_IDENTITY_INVALID")
    try:
        document = json.loads(
            data.decode("ascii"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (_DuplicateJsonKey, UnicodeError, json.JSONDecodeError):
        _fail("CLOSURE_IDENTITY_INVALID")
    if not isinstance(document, dict) or set(document) != _IDENTITY_FIELDS:
        _fail("CLOSURE_IDENTITY_INVALID")
    if _canonical_json(document) != data:
        _fail("CLOSURE_IDENTITY_INVALID")
    if document["identity_version"] != 1:
        _fail("CLOSURE_IDENTITY_INVALID")
    string_fields = {
        "playwright_package",
        "playwright_package_version",
        "playwright_wheel_filename",
        "playwright_wheel_sha256",
        "platform",
        "cache_root",
    }
    if any(not isinstance(document[field], str) for field in string_fields):
        _fail("CLOSURE_IDENTITY_INVALID")
    if type(document["playwright_wheel_size"]) is not int:
        _fail("CLOSURE_IDENTITY_INVALID")
    if _SHA256_RE.fullmatch(str(document["playwright_wheel_sha256"])) is None:
        _fail("CLOSURE_IDENTITY_INVALID")
    artifacts = document["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) != len(
        REQUIRED_ARTIFACT_NAMES
    ):
        _fail("CLOSURE_IDENTITY_INVALID")
    names: list[str] = []
    revisions: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != (
            _ARTIFACT_IDENTITY_FIELDS
        ):
            _fail("CLOSURE_IDENTITY_INVALID")
        required_strings = _ARTIFACT_IDENTITY_FIELDS - {"browser_version"}
        if any(not isinstance(artifact[field], str) for field in required_strings):
            _fail("CLOSURE_IDENTITY_INVALID")
        browser_version = artifact["browser_version"]
        if browser_version is not None and not isinstance(browser_version, str):
            _fail("CLOSURE_IDENTITY_INVALID")
        revision = str(artifact["revision"])
        if _REVISION_RE.fullmatch(revision) is None or revision in revisions:
            _fail("CLOSURE_IDENTITY_INVALID")
        revisions.add(revision)
        names.append(str(artifact["artifact_name"]))
    if tuple(names) != REQUIRED_ARTIFACT_NAMES:
        _fail("CLOSURE_IDENTITY_INVALID")
    return document


def current_runtime_platform() -> str:
    system = host_platform.system()
    machine = host_platform.machine().lower()
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux/amd64"
    if system == "Linux" and machine in {"aarch64", "arm64"}:
        return "linux/arm64"
    _fail("PLATFORM_UNSUPPORTED")



def _validate_packaged_executable(
    cache_root: Path, artifact: ArtifactContract
) -> None:
    destination = cache_root / artifact.cache_directory
    try:
        destination_metadata = destination.lstat()
    except OSError:
        _fail("PACKAGED_ARTIFACT_MISSING")
    if not stat.S_ISDIR(destination_metadata.st_mode) or destination.is_symlink():
        _fail("PACKAGED_ARTIFACT_INVALID")
    executable = destination.joinpath(
        *artifact.expected_executable_relative_path.split("/")
    )
    try:
        executable_stat = executable.lstat()
    except OSError:
        _fail("PACKAGED_ARTIFACT_MISSING")
    if (
        executable.is_symlink()
        or not stat.S_ISREG(executable_stat.st_mode)
        or stat.S_IMODE(executable_stat.st_mode) & 0o111 == 0
    ):
        _fail("PACKAGED_ARTIFACT_INVALID")


def verify_packaged_browser_readiness(
    platform: str | None = None,
) -> PlaywrightClosureContract:
    selected_platform = platform or current_runtime_platform()
    installed = load_installed_closure(selected_platform)
    cache_root = Path(installed.cache_root)
    marker = cache_root / INSTALLATION_MARKER
    marker_data = _read_regular_file(
        marker,
        maximum=_MAX_IDENTITY_BYTES,
        code="CLOSURE_IDENTITY_MISSING",
    )
    identity = _parse_installation_identity(marker_data)
    expected = {
        "playwright_package": installed.package,
        "playwright_package_version": installed.package_version,
        "platform": installed.platform,
        "cache_root": installed.cache_root,
        "artifacts": [
            artifact_identity_document(artifact)
            for artifact in installed.artifacts
        ],
    }
    if any(identity[field] != value for field, value in expected.items()):
        _fail("CLOSURE_IDENTITY_MISMATCH")
    try:
        actual_children = {child.name for child in cache_root.iterdir()}
    except OSError:
        _fail("PACKAGED_CACHE_INVALID")
    expected_children = {INSTALLATION_MARKER} | {
        artifact.cache_directory for artifact in installed.artifacts
    }
    if actual_children != expected_children:
        _fail("PACKAGED_CACHE_ENTRY_SET_MISMATCH")
    for artifact in installed.artifacts:
        _validate_packaged_executable(cache_root, artifact)
    return installed



def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report the lock-bound Playwright artifact closure."
    )
    parser.add_argument("--lockfile", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument(
        "--platform", required=True, choices=SUPPORTED_PLATFORMS
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
    except PlaywrightContractError as exc:
        print("PLAYWRIGHT_CONTRACT=FAIL")
        print(f"ERROR_CLASS={exc.code}")
        return 2
    except Exception:
        print("PLAYWRIGHT_CONTRACT=FAIL")
        print("ERROR_CLASS=INTERNAL_ERROR")
        return 2

    closure = verified.closure
    print(f"PLAYWRIGHT_PACKAGE={closure.package}")
    print(f"PLAYWRIGHT_PACKAGE_VERSION={closure.package_version}")
    print(f"PLAYWRIGHT_WHEEL_FILENAME={verified.wheel.filename}")
    print(f"PLAYWRIGHT_WHEEL_SHA256={verified.wheel.sha256}")
    print(f"PLATFORM={closure.platform}")
    print(f"REQUIRED_ARTIFACT_COUNT={len(closure.artifacts)}")
    print(f"REQUIRED_ARTIFACT_NAMES={','.join(closure.artifact_names)}")
    for index, artifact in enumerate(closure.artifacts, start=1):
        print(f"ARTIFACT_{index}_NAME={artifact.artifact_name}")
        print(f"ARTIFACT_{index}_REVISION={artifact.revision}")
        print(f"ARTIFACT_{index}_LAYOUT_KIND={artifact.layout_kind}")
        print(
            f"ARTIFACT_{index}_EXPECTED_CACHE_LAYOUT="
            f"{artifact.expected_cache_layout}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
