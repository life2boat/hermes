from __future__ import annotations

from importlib import metadata
from pathlib import Path

import pytest

from scripts import playwright_artifact_contract as contract_module


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


def test_installed_metadata_loader_uses_distribution_file_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_path = tmp_path / "playwright" / "driver" / "package" / "browsers.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_text(
        '{"browsers":[{"name":"chromium-headless-shell",'
        '"revision":"9876","revisionOverrides":{}}]}',
        encoding="utf-8",
    )

    class DistributionFixture:
        version = "1.61.0"
        files = [Path("playwright/driver/package/browsers.json")]

        @staticmethod
        def locate_file(_file: object) -> Path:
            return metadata_path

    monkeypatch.setattr(
        metadata,
        "distribution",
        lambda _name: DistributionFixture(),
    )

    contract = contract_module.load_installed_contract("linux/amd64")

    assert contract.package_version == "1.61.0"
    assert contract.browser_revision == "9876"


def test_reporter_has_no_network_or_install_path() -> None:
    source = Path(contract_module.__file__).read_text(encoding="utf-8")
    forbidden = (
        "requests",
        "urllib",
        "httpx",
        "socket",
        "subprocess",
        "playwright install",
    )
    for needle in forbidden:
        assert needle not in source
