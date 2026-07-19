from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import pytest

from scripts import playwright_artifact_contract as contract_module
from tests.playwright_supply_chain_support import (
    verified_contract,
    write_lockfile,
    write_wheel,
)


def _payload(*, revision: str = "9876") -> dict[str, object]:
    return {
        "browsers": [
            {
                "name": "chromium-headless-shell",
                "revision": revision,
                "revisionOverrides": {},
            }
        ]
    }


def _wheel_fixture(
    tmp_path: Path,
    **wheel_options: object,
) -> tuple[Path, Path, bytes]:
    wheel = tmp_path / "playwright-wheel"
    wheel_bytes = write_wheel(wheel, **wheel_options)
    lockfile = tmp_path / "uv.lock"
    write_lockfile(lockfile, wheel_bytes)
    return lockfile, wheel, wheel_bytes


def test_contract_derives_identity_without_installing_or_using_network() -> None:
    contract = contract_module.contract_from_metadata(
        package_version="1.61.0",
        browsers_payload=_payload(),
        platform="linux/amd64",
    )

    assert contract.package == "playwright"
    assert contract.package_version == "1.61.0"
    assert contract.browser_family == "chromium-headless-shell"
    assert contract.browser_revision == "9876"
    assert contract.cache_directory == "chromium_headless_shell-9876"
    assert contract.archive_root == "chrome-headless-shell-linux64"
    assert contract.expected_cache_layout == (
        "/opt/hermes/.playwright/chromium_headless_shell-9876/"
        "chrome-headless-shell-linux64/chrome-headless-shell"
    )


def test_platform_mapping_is_explicit_for_supported_architectures() -> None:
    amd64 = contract_module.contract_from_metadata(
        package_version="1.61.0",
        browsers_payload=_payload(),
        platform="linux/amd64",
    )
    arm64 = contract_module.contract_from_metadata(
        package_version="1.61.0",
        browsers_payload=_payload(),
        platform="linux/arm64",
    )

    assert amd64.expected_executable_relative_path == (
        "chrome-headless-shell-linux64/chrome-headless-shell"
    )
    assert arm64.expected_executable_relative_path == "chrome-linux/headless_shell"


def test_revision_override_controls_revision_and_cache_directory() -> None:
    payload = _payload()
    browser = payload["browsers"][0]
    assert isinstance(browser, dict)
    browser["revisionOverrides"] = {"debian13-x64": "9877"}

    contract = contract_module.contract_from_metadata(
        package_version="1.61.0",
        browsers_payload=payload,
        platform="linux/amd64",
    )

    assert contract.browser_revision == "9877"
    assert contract.cache_directory == (
        "chromium_headless_shell_debian13_x64_special-9877"
    )


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"browsers": []}, "BROWSER_METADATA_AMBIGUOUS"),
        (
            {
                "browsers": [
                    {"name": "chromium-headless-shell", "revision": "1"},
                    {"name": "chromium-headless-shell", "revision": "2"},
                ]
            },
            "BROWSER_METADATA_AMBIGUOUS",
        ),
        (_payload(revision="not-numeric"), "BROWSER_REVISION_INVALID"),
    ],
)
def test_ambiguous_or_invalid_package_metadata_is_denied(
    payload: dict[str, object], code: str
) -> None:
    with pytest.raises(contract_module.PlaywrightContractError, match=f"^{code}$"):
        contract_module.contract_from_metadata(
            package_version="1.61.0",
            browsers_payload=payload,
            platform="linux/amd64",
        )


def test_verified_locked_wheel_is_the_browser_metadata_authority(
    tmp_path: Path,
) -> None:
    lockfile, wheel, wheel_bytes = _wheel_fixture(tmp_path)

    verified = contract_module.load_verified_wheel_contract(
        lockfile_path=lockfile,
        wheel_path=wheel,
        platform="linux/amd64",
    )

    assert verified.wheel.sha256 == hashlib.sha256(wheel_bytes).hexdigest()
    assert verified.wheel.filename == (
        "playwright-1.61.0-py3-none-manylinux1_x86_64.whl"
    )
    assert verified.browser.package_version == "1.61.0"
    assert verified.browser.browser_revision == "9876"


def test_wheel_sha_not_authorized_by_lock_is_denied(tmp_path: Path) -> None:
    lockfile, wheel, wheel_bytes = _wheel_fixture(tmp_path)
    write_lockfile(lockfile, wheel_bytes, sha256="0" * 64)

    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^WHEEL_SHA256_MISMATCH$",
    ):
        contract_module.load_verified_wheel_contract(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_wheel_sha_mismatch_is_denied_before_metadata_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile, wheel, wheel_bytes = _wheel_fixture(tmp_path)
    tampered = bytearray(wheel_bytes)
    tampered[-1] ^= 1
    wheel.write_bytes(tampered)

    def metadata_must_not_be_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("metadata read before wheel SHA verification")

    monkeypatch.setattr(
        contract_module,
        "_metadata_from_wheel_bytes",
        metadata_must_not_be_read,
    )
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^WHEEL_SHA256_MISMATCH$",
    ):
        contract_module.load_verified_wheel_contract(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_wheel_package_version_mismatch_is_denied(tmp_path: Path) -> None:
    lockfile, wheel, _ = _wheel_fixture(
        tmp_path,
        metadata_version="1.60.0",
    )

    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^PACKAGE_VERSION_MISMATCH$",
    ):
        contract_module.load_verified_wheel_contract(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_wheel_changed_during_metadata_read_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile, wheel, wheel_bytes = _wheel_fixture(tmp_path)
    original = contract_module._metadata_from_wheel_bytes

    def mutate_after_verified_read(
        data: bytes,
        locked: contract_module.LockedWheel,
    ) -> tuple[str, dict[str, object]]:
        result = original(data, locked)
        replacement = wheel.with_name("replacement-wheel")
        replacement.write_bytes(wheel_bytes)
        os.replace(replacement, wheel)
        return result

    monkeypatch.setattr(
        contract_module,
        "_metadata_from_wheel_bytes",
        mutate_after_verified_read,
    )
    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^WHEEL_CHANGED_DURING_READ$",
    ):
        contract_module.load_verified_wheel_contract(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_duplicate_browser_metadata_wheel_entry_is_denied(
    tmp_path: Path,
) -> None:
    lockfile, wheel, _ = _wheel_fixture(
        tmp_path,
        duplicate_browser_entry=True,
    )

    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^BROWSER_METADATA_ENTRY_AMBIGUOUS$",
    ):
        contract_module.load_verified_wheel_contract(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_duplicate_browser_matches_in_verified_metadata_are_denied(
    tmp_path: Path,
) -> None:
    lockfile, wheel, _ = _wheel_fixture(tmp_path, browser_matches=2)

    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^BROWSER_METADATA_AMBIGUOUS$",
    ):
        contract_module.load_verified_wheel_contract(
            lockfile_path=lockfile,
            wheel_path=wheel,
            platform="linux/amd64",
        )


def test_installed_site_packages_tampering_does_not_change_wheel_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lockfile, wheel, _ = _wheel_fixture(tmp_path)

    class TamperedDistribution:
        version = "9.9.9"
        files = [Path("playwright/driver/package/browsers.json")]

        @staticmethod
        def locate_file(_file: object) -> Path:
            return tmp_path / "tampered-installed-metadata.json"

    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: TamperedDistribution(),
    )
    verified = contract_module.load_verified_wheel_contract(
        lockfile_path=lockfile,
        wheel_path=wheel,
        platform="linux/amd64",
    )

    assert verified.browser.package_version == "1.61.0"
    assert verified.browser.browser_revision == "9876"


def test_packaged_browser_readiness_accepts_matching_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, _, _ = verified_contract(tmp_path, cache_root=tmp_path / "cache")
    contract = verified.browser
    destination = Path(contract.cache_root) / contract.cache_directory
    executable = destination.joinpath(
        *contract.expected_executable_relative_path.split("/")
    )
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"binary")
    executable.chmod(0o755)
    (destination / contract_module.INSTALLATION_MARKER).write_bytes(
        contract_module.canonical_installation_identity(verified)
    )
    monkeypatch.setattr(
        contract_module,
        "load_installed_contract",
        lambda _platform: contract,
    )

    actual = contract_module.verify_packaged_browser_readiness("linux/amd64")

    assert actual == contract


def test_missing_packaged_browser_identity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, _, _ = verified_contract(tmp_path, cache_root=tmp_path / "cache")
    monkeypatch.setattr(
        contract_module,
        "load_installed_contract",
        lambda _platform: verified.browser,
    )

    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^BROWSER_IDENTITY_MISSING$",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_wrong_packaged_browser_revision_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, _, _ = verified_contract(tmp_path, cache_root=tmp_path / "cache")
    contract = verified.browser
    destination = Path(contract.cache_root) / contract.cache_directory
    destination.mkdir(parents=True)
    identity = contract_module.installation_identity_document(verified)
    identity["browser_revision"] = "9999"
    (destination / contract_module.INSTALLATION_MARKER).write_bytes(
        (json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "ascii"
        )
    )
    monkeypatch.setattr(
        contract_module,
        "load_installed_contract",
        lambda _platform: contract,
    )

    with pytest.raises(
        contract_module.PlaywrightContractError,
        match="^BROWSER_IDENTITY_MISMATCH$",
    ):
        contract_module.verify_packaged_browser_readiness("linux/amd64")


def test_contract_reporter_has_no_network_or_install_path() -> None:
    source = Path(contract_module.__file__).read_text(encoding="utf-8")
    forbidden = (
        "requests",
        "httpx",
        "socket",
        "subprocess",
        "playwright install",
    )
    for needle in forbidden:
        assert needle not in source
