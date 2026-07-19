from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
SCHEMA = REPO_ROOT / "schemas" / "playwright-artifact-manifest.schema.json"
CONTRACT_SCRIPT = REPO_ROOT / "scripts" / "playwright_artifact_contract.py"
INSTALLER_SCRIPT = REPO_ROOT / "scripts" / "install_pinned_playwright_artifact.py"
BUILD_HELPER = REPO_ROOT / "scripts" / "build_verified_playwright_image.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dockerfile_uses_only_verified_named_artifact_context() -> None:
    text = _text(DOCKERFILE)

    assert "ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in text
    assert "ARG PLAYWRIGHT_ARTIFACT_MANIFEST_SHA256\n" in text
    assert "ARG PLAYWRIGHT_ARTIFACT_MANIFEST_SHA256=" not in text
    assert (
        "RUN --mount=type=bind,from=playwright_artifact,source=/,"
        "target=/tmp/playwright-artifact,ro" in text
    )
    assert "--manifest /tmp/playwright-artifact/manifest.json" in text
    assert "--archive /tmp/playwright-artifact/browser-archive" in text
    assert "--expected-manifest-sha256" in text
    assert "COPY --from=playwright_artifact" not in text
    assert "rm -rf /tmp/playwright-artifact" not in text


def test_dockerfile_uses_pinned_python_runtime_without_cdn_fallback() -> None:
    text = _text(DOCKERFILE)
    verified_block = text.split(
        "# ---------- Verified Playwright browser artifact ----------", 1
    )[1].split("# ---------- Frontend build", 1)[0]

    assert "--extra google-meet" in text
    assert ".venv/bin/python -m playwright install-deps chromium" in verified_block
    assert ".venv/bin/python -m scripts.install_pinned_playwright_artifact" in (
        verified_block
    )
    assert "npx playwright" not in text
    assert "playwright install --with-deps" not in text
    assert "http://" not in verified_block
    assert "https://" not in verified_block
    assert "cdn" not in verified_block.lower()
    assert "latest" not in verified_block.lower()


def test_playwright_runtime_is_exactly_pinned_and_locked_with_hashes() -> None:
    pyproject = _text(PYPROJECT)
    lock = _text(UV_LOCK)

    assert 'google-meet = ["playwright==1.61.0", "websockets==15.0.1"]' in (
        pyproject
    )
    assert re.search(
        r'\[\[package\]\]\nname = "playwright"\nversion = "1\.61\.0"',
        lock,
    )
    playwright_block = lock.split(
        '[[package]]\nname = "playwright"\nversion = "1.61.0"', 1
    )[1].split("[[package]]", 1)[0]
    assert "sha256:" in playwright_block
    assert "wheels = [" in playwright_block


def test_manifest_schema_is_strict_and_requires_every_identity_field() -> None:
    schema = json.loads(_text(SCHEMA))
    required = {
        "manifest_version",
        "playwright_package",
        "playwright_package_version",
        "browser_family",
        "browser_revision",
        "platform",
        "archive_filename",
        "archive_size",
        "archive_sha256",
        "archive_format",
        "cache_root",
        "expected_executable_relative_path",
        "source_kind",
        "source_reference",
    }

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == required
    assert schema["properties"]["manifest_version"]["const"] == 1
    assert schema["properties"]["playwright_package"]["const"] == "playwright"
    assert schema["properties"]["browser_family"]["const"] == (
        "chromium-headless-shell"
    )
    assert schema["properties"]["archive_filename"]["const"] == (
        "browser-archive"
    )
    assert schema["properties"]["archive_sha256"]["pattern"] == (
        "^[0-9a-f]{64}$"
    )
    assert schema["properties"]["source_reference"]["not"]["pattern"] == (
        "[Ll][Aa][Tt][Ee][Ss][Tt]"
    )


def test_browser_revision_is_derived_and_not_encoded_as_previous_observation() -> None:
    source_contract = _text(CONTRACT_SCRIPT)
    source_installer = _text(INSTALLER_SCRIPT)
    source_build = _text(BUILD_HELPER)
    dockerfile = _text(DOCKERFILE)
    schema = _text(SCHEMA)

    assert "browsers.json" in source_contract
    assert "revisionOverrides" in source_contract
    assert "1228" not in "\n".join(
        (source_contract, source_installer, source_build, dockerfile, schema)
    )


def test_no_browser_archive_or_manifest_instance_is_committed() -> None:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    tracked = set(completed.stdout.splitlines())

    assert "browser-archive" not in tracked
    assert not any(path.endswith("/browser-archive") for path in tracked)
    assert not any(
        path.endswith("/manifest.json") and "playwright-artifact" in path
        for path in tracked
    )


def test_canonical_build_helper_exposes_check_and_explicit_build_only() -> None:
    source = _text(BUILD_HELPER)

    assert 'choices=("check", "build")' in source
    assert 'if args.mode == "build":' in source
    assert "--artifact-context" in source
    assert "--expected-manifest-sha256" in source
    assert "--expected-source-sha" in source
    assert "--image-tag" in source
    assert "--skip" not in source
    assert "--force" not in source
