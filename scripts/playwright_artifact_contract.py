from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any


PLAYWRIGHT_PACKAGE = "playwright"
BROWSER_FAMILY = "chromium-headless-shell"
CACHE_ROOT = "/opt/hermes/.playwright"
_METADATA_PATH = "playwright/driver/package/browsers.json"
_MAX_METADATA_BYTES = 128 * 1024
_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.-]+)?$")
_REVISION_RE = re.compile(r"^[0-9]+$")


class PlaywrightContractError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PlatformMapping:
    playwright_host_platform: str
    executable_relative_path: str


PLATFORM_MAPPINGS = {
    "linux/amd64": PlatformMapping(
        playwright_host_platform="debian13-x64",
        executable_relative_path=(
            "chrome-headless-shell-linux64/chrome-headless-shell"
        ),
    ),
    "linux/arm64": PlatformMapping(
        playwright_host_platform="debian13-arm64",
        executable_relative_path="chrome-linux/headless_shell",
    ),
}


@dataclass(frozen=True)
class BrowserContract:
    package: str
    package_version: str
    browser_family: str
    browser_revision: str
    platform: str
    cache_root: str
    cache_directory: str
    expected_executable_relative_path: str

    @property
    def expected_cache_layout(self) -> str:
        return str(
            PurePosixPath(self.cache_root)
            / self.cache_directory
            / self.expected_executable_relative_path
        )


def _fail(code: str) -> None:
    raise PlaywrightContractError(code)


def _required_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value:
        _fail(code)
    return value


def contract_from_metadata(
    *, package_version: str, browsers_payload: object, platform: str
) -> BrowserContract:
    mapping = PLATFORM_MAPPINGS.get(platform)
    if mapping is None:
        _fail("PLATFORM_UNSUPPORTED")
    if _VERSION_RE.fullmatch(package_version) is None:
        _fail("PACKAGE_VERSION_INVALID")
    if not isinstance(browsers_payload, dict):
        _fail("PACKAGE_METADATA_INVALID")
    browsers = browsers_payload.get("browsers")
    if not isinstance(browsers, list):
        _fail("PACKAGE_METADATA_INVALID")

    matches = [
        item
        for item in browsers
        if isinstance(item, dict) and item.get("name") == BROWSER_FAMILY
    ]
    if len(matches) != 1:
        _fail("BROWSER_METADATA_AMBIGUOUS")
    browser = matches[0]
    base_revision = _required_string(
        browser.get("revision"), "BROWSER_REVISION_INVALID"
    )
    if _REVISION_RE.fullmatch(base_revision) is None:
        _fail("BROWSER_REVISION_INVALID")

    overrides = browser.get("revisionOverrides", {})
    if not isinstance(overrides, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in overrides.items()
    ):
        _fail("BROWSER_REVISION_OVERRIDES_INVALID")
    revision_override = overrides.get(mapping.playwright_host_platform)
    revision = revision_override or base_revision
    if _REVISION_RE.fullmatch(revision) is None:
        _fail("BROWSER_REVISION_INVALID")

    directory_prefix = BROWSER_FAMILY
    if revision_override is not None:
        directory_prefix = (
            f"{BROWSER_FAMILY}_{mapping.playwright_host_platform}_special"
        )
    cache_directory = f"{directory_prefix.replace('-', '_')}-{revision}"

    return BrowserContract(
        package=PLAYWRIGHT_PACKAGE,
        package_version=package_version,
        browser_family=BROWSER_FAMILY,
        browser_revision=revision,
        platform=platform,
        cache_root=CACHE_ROOT,
        cache_directory=cache_directory,
        expected_executable_relative_path=mapping.executable_relative_path,
    )


def _load_installed_browsers_payload() -> tuple[str, dict[str, Any]]:
    try:
        distribution = metadata.distribution(PLAYWRIGHT_PACKAGE)
    except metadata.PackageNotFoundError:
        _fail("PLAYWRIGHT_PACKAGE_NOT_INSTALLED")
    files = distribution.files
    if files is None:
        _fail("PACKAGE_FILE_INDEX_MISSING")
    metadata_file = next(
        (file for file in files if str(file) == _METADATA_PATH), None
    )
    if metadata_file is None:
        _fail("BROWSER_METADATA_MISSING")
    path = Path(distribution.locate_file(metadata_file))
    try:
        if path.is_symlink() or not path.is_file():
            _fail("BROWSER_METADATA_FILE_INVALID")
        data = path.read_bytes()
    except OSError:
        _fail("BROWSER_METADATA_READ_FAILED")
    if not data or len(data) > _MAX_METADATA_BYTES:
        _fail("BROWSER_METADATA_SIZE_INVALID")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        _fail("BROWSER_METADATA_JSON_INVALID")
    if not isinstance(payload, dict):
        _fail("PACKAGE_METADATA_INVALID")
    return distribution.version, payload


def load_installed_contract(platform: str) -> BrowserContract:
    package_version, payload = _load_installed_browsers_payload()
    return contract_from_metadata(
        package_version=package_version,
        browsers_payload=payload,
        platform=platform,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report the installed pinned Playwright browser contract."
    )
    parser.add_argument(
        "--platform", required=True, choices=sorted(PLATFORM_MAPPINGS)
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        contract = load_installed_contract(args.platform)
    except PlaywrightContractError as exc:
        print("PLAYWRIGHT_CONTRACT=FAIL")
        print(f"ERROR_CLASS={exc.code}")
        return 2
    except Exception:
        print("PLAYWRIGHT_CONTRACT=FAIL")
        print("ERROR_CLASS=INTERNAL_ERROR")
        return 2

    print(f"PLAYWRIGHT_PACKAGE={contract.package}")
    print(f"PLAYWRIGHT_PACKAGE_VERSION={contract.package_version}")
    print(f"BROWSER_FAMILY={contract.browser_family}")
    print(f"BROWSER_REVISION={contract.browser_revision}")
    print(f"PLATFORM={contract.platform}")
    print(f"EXPECTED_CACHE_LAYOUT={contract.expected_cache_layout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
