from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "hermes-production-deployment.md"


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _normalized() -> str:
    return " ".join(_text().split())


def test_runbook_names_verified_locked_wheel_as_metadata_authority() -> None:
    text = _normalized()

    required = (
        "## Verified Playwright image build prerequisite",
        "single authoritative Playwright runtime source",
        "wheel filename, size, and SHA-256",
        "directly from the verified wheel bytes",
        "never the sole browser identity authority",
        "scripts/playwright_artifact_contract.py",
        "without installing a browser or making a network request",
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
        "playwright-wheel",
        "schemas/playwright-artifact-manifest.schema.json",
        "opaque approval reference, not a URL",
        "no Playwright CDN fallback",
    )
    for needle in required:
        assert needle in text


def test_runbook_contract_report_requires_lockfile_and_verified_wheel() -> None:
    text = _text()
    command = text.split(
        ".venv/bin/python scripts/playwright_artifact_contract.py", 1
    )[1].split("```", 1)[0]

    assert "--lockfile uv.lock" in command
    assert '--wheel "$ARTIFACT_DIR/playwright-wheel"' in command
    assert "--platform linux/amd64" in command


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


def test_runbook_requires_exact_git_tree_context_and_independent_inspection() -> None:
    text = _normalized()
    required = (
        "export the exact requested Git tree",
        "verify every path, mode, and blob identity",
        "create and re-read a context manifest",
        "Ignored, untracked, and other raw-worktree content never enters",
        "reject submodules, Git LFS pointers, secrets, databases, patch files",
    )
    for needle in required:
        assert needle in text


def test_runbook_documents_shared_context_aware_secret_policy() -> None:
    text = _normalized()
    required = (
        "Secret classification is deterministic and shared",
        "Credential variable names without assigned values",
        "redaction-pattern definitions",
        "marker-only test fixtures are not secret material",
        "Complete private-key blocks",
        "credential-bearing URLs with secret-shaped values",
        "high-entropy credential assignments are denied regardless of path",
        "No filename or directory allowlist",
        "scanner failures remain fail closed",
    )
    for needle in required:
        assert needle in text


def test_runbook_requires_durable_publication_and_fail_closed_readiness() -> None:
    text = _normalized()
    required = (
        "fsyncs regular files and created directories",
        "atomically renames the complete cache",
        "fsyncs the final parent",
        "re-opens the published identity",
        "fails `hermes meet setup` closed",
        "never downloads a browser at setup or runtime",
    )
    for needle in required:
        assert needle in text


def test_runbook_keeps_build_and_deployment_as_separate_gates() -> None:
    text = _normalized()
    required = (
        "Only a separately authorized image-build task",
        "one read-only BuildKit named context",
        "Artifact acquisition, image build, image validation, deployment, and",
        "activation remain separate approval gates.",
    )
    for needle in required:
        assert needle in text


def test_runbook_does_not_embed_real_artifact_url_revision_or_download_command() -> None:
    section = _text().split(
        "## Verified Playwright image build prerequisite", 1
    )[1].split("## Repository validation", 1)[0]

    assert "https://" not in section
    assert "http://" not in section
    assert "1228" not in section
    assert "latest" not in section.lower()
    assert "npx playwright" not in section
    assert "python -m playwright install" not in section
