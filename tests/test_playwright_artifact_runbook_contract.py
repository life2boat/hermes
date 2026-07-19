from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "hermes-production-deployment.md"


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _normalized() -> str:
    return " ".join(_text().split())


def test_runbook_names_pinned_python_package_metadata_as_authority() -> None:
    text = _normalized()

    required = (
        "## Verified Playwright image build prerequisite",
        "single authoritative Playwright runtime",
        "bundled `browsers.json` metadata",
        "scripts/playwright_artifact_contract.py",
        "This command is read-only.",
        "neither installs a browser nor makes a network request",
    )
    for needle in required:
        assert needle in text


def test_runbook_requires_separately_approved_external_artifact() -> None:
    text = _normalized()

    required = (
        "acquired and approved in a separate task",
        "known and reviewed before a canonical build starts",
        "absolute path outside the repository",
        "manifest.json",
        "browser-archive",
        "schemas/playwright-artifact-manifest.schema.json",
        "opaque approval reference, not a URL",
        "no Playwright CDN fallback",
    )
    for needle in required:
        assert needle in text


def test_runbook_check_command_requires_exact_source_manifest_and_context() -> None:
    text = _text()

    command = text.split(
        ".venv/bin/python scripts/build_verified_playwright_image.py check", 1
    )[1].split("```", 1)[0]
    assert '--expected-source-sha "$EXACT_SHA"' in command
    assert '--artifact-context "$ARTIFACT_DIR"' in command
    assert '--expected-manifest-sha256 "$MANIFEST_SHA256"' in command
    assert '--image-tag "$IMAGE_REF"' in command
    assert "--platform linux/amd64" in command
    assert "--skip" not in command
    assert "--force" not in command


def test_runbook_keeps_build_and_deployment_as_separate_gates() -> None:
    text = _normalized()

    required = (
        "Only a separately authorized image-build task",
        "read-only BuildKit named context",
        "Artifact acquisition, image build, image validation, and",
        "deployment remain separate approval gates.",
        "It never falls back to an external browser download.",
    )
    for needle in required:
        assert needle in text


def test_runbook_does_not_embed_real_artifact_url_or_checksum() -> None:
    section = _text().split(
        "## Verified Playwright image build prerequisite", 1
    )[1].split("## Repository validation", 1)[0]

    assert "https://" not in section
    assert "http://" not in section
    assert "1228" not in section
    assert "latest" not in section.lower()
    assert "npx playwright" not in section
