from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
SCHEMA = REPO_ROOT / "schemas" / "playwright-artifact-manifest.schema.json"
CONTRACT_SCRIPT = REPO_ROOT / "scripts" / "playwright_artifact_contract.py"
INSTALLED_CLOSURE_SCRIPT = (
    REPO_ROOT / "scripts" / "playwright_installed_closure.py"
)
INSTALLER_SCRIPT = REPO_ROOT / "scripts" / "install_pinned_playwright_artifact.py"
BUILD_HELPER = REPO_ROOT / "scripts" / "build_verified_playwright_image.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dockerfile_uses_one_verified_closure_named_context() -> None:
    text = _text(DOCKERFILE)
    assert "ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in text
    assert "ARG PLAYWRIGHT_ARTIFACT_CLOSURE_SHA256\n" in text
    assert "ARG PLAYWRIGHT_ARTIFACT_CLOSURE_SHA256=" not in text
    assert (
        "RUN --mount=type=bind,from=playwright_artifacts,source=/,"
        "target=/tmp/playwright-artifacts,ro" in text
    )
    assert "--closure-manifest /tmp/playwright-artifacts/closure.json" in text
    assert "--artifacts-root /tmp/playwright-artifacts/artifacts" in text
    assert "--lockfile ./uv.lock" in text
    assert "--wheel /tmp/playwright-artifacts/playwright-wheel" in text
    assert "--expected-closure-manifest-sha256" in text
    assert "from=playwright_artifact," not in text
    assert "browser-archive" not in text
    assert "--expected-manifest-sha256" not in text


def test_dockerfile_uses_locked_python_runtime_without_browser_cdn_fallback() -> None:
    text = _text(DOCKERFILE)
    verified_block = text.split(
        "# ---------- Verified Playwright artifact closure ----------", 1
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



def test_dockerfile_packages_root_owned_immutable_installed_closure() -> None:
    text = _text(DOCKERFILE)
    assert (
        "COPY scripts/playwright_artifact_contract.py "
        "scripts/playwright_installed_closure.py "
        "scripts/install_pinned_playwright_artifact.py scripts/"
    ) in text
    assert (
        "chown root:root /opt/hermes /opt/hermes/.playwright "
        "/opt/hermes/.playwright.expected-closure.json"
    ) in text
    assert "chmod 0755 /opt/hermes" in text
    assert "chmod 0555 /opt/hermes/.playwright" in text
    assert (
        "chmod 0444 /opt/hermes/.playwright.expected-closure.json "
        "/opt/hermes/.playwright/INSTALLATION_COMPLETE"
    ) in text


def test_playwright_runtime_is_exactly_pinned_and_locked_with_hashes() -> None:
    pyproject = _text(PYPROJECT)
    lock = _text(UV_LOCK)
    assert 'google-meet = ["playwright==1.61.0", "websockets==15.0.1"]' in pyproject
    assert re.search(
        r'\[\[package\]\]\nname = "playwright"\nversion = "1\.61\.0"',
        lock,
    )
    playwright_block = lock.split(
        '[[package]]\nname = "playwright"\nversion = "1.61.0"', 1
    )[1].split("[[package]]", 1)[0]
    assert "sha256:" in playwright_block
    assert "wheels = [" in playwright_block
    assert "manylinux1_x86_64.whl" in playwright_block
    assert "manylinux2014_aarch64.whl" in playwright_block


def test_aggregate_manifest_schema_is_strict_for_closure_and_artifacts() -> None:
    schema = json.loads(_text(SCHEMA))
    top_required = {
        "manifest_version",
        "manifest_kind",
        "playwright_package",
        "playwright_package_version",
        "playwright_wheel_filename",
        "playwright_wheel_size",
        "playwright_wheel_sha256",
        "platform",
        "cache_root",
        "artifact_count",
        "artifacts",
    }
    artifact_required = {
        "artifact_name",
        "browser_family",
        "revision",
        "browser_version",
        "platform",
        "archive_filename",
        "archive_size",
        "archive_sha256",
        "archive_format",
        "layout_kind",
        "archive_root",
        "expected_executable_relative_path",
        "executable_mode_required",
        "source_kind",
        "source_reference_sha256",
    }
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == top_required
    assert schema["properties"]["manifest_version"]["const"] == 2
    assert schema["properties"]["manifest_kind"]["const"] == (
        "PLAYWRIGHT_ARTIFACT_CLOSURE"
    )
    artifact = schema["properties"]["artifacts"]["items"]
    assert artifact["additionalProperties"] is False
    assert set(artifact["required"]) == artifact_required
    assert artifact["properties"]["layout_kind"]["enum"] == [
        "DIRECTORY_TREE",
        "SINGLE_EXECUTABLE_FILE",
    ]
    assert artifact["properties"]["archive_sha256"]["pattern"] == (
        "^[0-9a-f]{64}$"
    )
    assert artifact["properties"]["source_reference_sha256"]["pattern"] == (
        "^[0-9a-f]{64}$"
    )


def test_artifact_revisions_are_derived_from_verified_wheel_not_hard_coded() -> None:
    sources = "\n".join(
        _text(path)
        for path in (
            CONTRACT_SCRIPT,
            INSTALLED_CLOSURE_SCRIPT,
            INSTALLER_SCRIPT,
            BUILD_HELPER,
            DOCKERFILE,
            SCHEMA,
        )
    )
    assert "browsers.json" in _text(CONTRACT_SCRIPT)
    assert "load_locked_wheel" in _text(CONTRACT_SCRIPT)
    assert "_metadata_from_wheel_bytes" in _text(CONTRACT_SCRIPT)
    assert "revisionOverrides" in _text(CONTRACT_SCRIPT)
    assert "load_installed_closure(args.platform)" not in _text(INSTALLER_SCRIPT)
    assert "1228" not in sources
    assert "1011" not in sources


def test_no_archive_manifest_or_wheel_instance_is_committed() -> None:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    tracked = set(completed.stdout.splitlines())
    for fixed_name in (
        "archive",
        "browser-archive",
        "closure.json",
        "playwright-wheel",
    ):
        assert fixed_name not in tracked
        assert not any(path.endswith(f"/{fixed_name}") for path in tracked)
    assert not any("playwright-artifact" in path and path.endswith(".zip") for path in tracked)


def test_canonical_build_helper_exports_exact_git_tree_and_binds_closure() -> None:
    source = _text(BUILD_HELPER)
    assert 'choices=("check", "build")' in source
    assert 'if args.mode == "build":' in source
    assert "git-tree-context" in source
    assert "inspect_exported_context" in source
    assert "inputs.build_context" in source
    command_block = source.split("def docker_build_command", 1)[1].split(
        "def _parser", 1
    )[0]
    assert "str(inputs.repository_root)" not in command_block
    assert "playwright_artifacts=" in command_block
    assert "PLAYWRIGHT_ARTIFACT_CLOSURE_SHA256=" in command_block
    assert "playwright_artifact=" not in command_block
    assert "--artifact-context" in source
    assert "--expected-closure-manifest-sha256" in source
    assert "--expected-manifest-sha256" not in source
    assert "--skip" not in source
    assert "--force" not in source


def test_dockerignore_contains_defense_in_depth_local_artifact_exclusions() -> None:
    text = _text(DOCKERIGNORE)
    required = (
        "__pycache__/",
        "*.pyc",
        "*.pyo",
        ".pytest_cache/",
        ".pytest-cache/",
        ".ruff_cache/",
        "*.egg-info/",
        "*.patch",
        "*.diff",
        ".codex-remote-edit/",
        "evidence/",
        "review-mirrors/",
        "deploy/reviews/",
        "Thumbs.db",
        "$RECYCLE.BIN/",
    )
    for pattern in required:
        assert pattern in text
