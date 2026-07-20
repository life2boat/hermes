from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "hermes-production-deployment.md"


def _text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _normalized() -> str:
    return " ".join(_text().split())


def test_runbook_names_verified_locked_wheel_as_closure_authority() -> None:
    text = _normalized()
    required = (
        "## Verified Playwright image build prerequisite",
        "package authority for the complete runtime closure",
        "wheel, verifies its filename, size, and SHA-256",
        "directly from those verified wheel bytes",
        "cannot replace the verified wheel as build authority",
        "scripts/playwright_artifact_contract.py",
        "without installing artifacts or making a network request",
    )
    for needle in required:
        assert needle in text


def test_runbook_requires_exact_two_artifact_closure_and_strict_layouts() -> None:
    text = _normalized()
    required = (
        "exactly `chromium-headless-shell` and `ffmpeg`",
        "FFmpeg is required, never optional",
        "strict `DIRECTORY_TREE` layout",
        "strict `SINGLE_EXECUTABLE_FILE` layout",
        "No generic permissive archive layout",
        "legacy single-artifact manifest",
    )
    for needle in required:
        assert needle in text


def test_runbook_documents_exact_complete_named_context_shape() -> None:
    text = _text()
    required = (
        "closure.json",
        "playwright-wheel",
        "artifacts/",
        "chromium-headless-shell/",
        "ffmpeg/",
        "playwright_artifacts=<approved complete closure directory>",
    )
    for needle in required:
        assert needle in text


def test_runbook_check_command_requires_exact_source_and_closure_sha() -> None:
    text = _text()
    command = text.split(
        ".venv/bin/python scripts/build_verified_playwright_image.py check", 1
    )[1].split("```", 1)[0]
    assert '--expected-source-sha "$EXACT_SHA"' in command
    assert '--approved-base-sha "$APPROVED_BASE_SHA"' in command
    assert '--artifact-context "$ARTIFACT_DIR"' in command
    assert (
        '--expected-closure-manifest-sha256 "$CLOSURE_MANIFEST_SHA256"'
        in command
    )
    assert '--image-tag "$IMAGE_REF"' in command
    assert "--platform linux/amd64" in command
    assert "--expected-manifest-sha256" not in command
    assert "--skip" not in command
    assert "--force" not in command


def test_runbook_requires_exact_git_tree_context_and_shared_policy() -> None:
    text = _normalized()
    required = (
        "export the exact requested Git tree",
        "verify every path, mode, and blob identity",
        "Ignored, untracked, and other raw-worktree content never enters",
        "approved base SHA is mandatory",
        "Worktree bytes are never the authority",
        "Secret classification is deterministic and shared",
        "No filename or directory allowlist",
        "scanner failures remain fail closed",
        "same Git-object policy",
        "reads staged candidates from the index",
    )
    for needle in required:
        assert needle in text


def test_runbook_requires_all_archives_before_atomic_publication() -> None:
    text = _normalized()
    required = (
        "Before extraction, the installer verifies",
        "every archive size/hash",
        "extracts all artifacts into one same-filesystem staging cache",
        "fsyncs regular files and directories bottom-up",
        "atomically publishes the complete cache root with one rename",
        "both published artifacts are reopened and revalidated",
        "No artifact is published before the full closure validates",
    )
    for needle in required:
        assert needle in text


def test_runbook_defines_fail_closed_existing_cache_policy() -> None:
    text = _normalized()
    required = (
        "complete matching existing cache is accepted after full revalidation",
        "incomplete cache, mixed revision, package mismatch",
        "unexpected cache entry is denied",
        "never merged with staged content",
        "Chromium-only or FFmpeg-only cache is not Ready",
        "requires both exact artifacts",
        "performs no download attempt",
    )
    for needle in required:
        assert needle in text


def test_runbook_keeps_acquisition_build_and_deployment_as_separate_gates() -> None:
    text = _normalized()
    required = (
        "Acquisition and approval of every archive happen in a separate task",
        "Only a separately authorized image-build task",
        "one read-only BuildKit named context",
        "Artifact acquisition, image build, image validation, deployment, and",
        "activation remain separate approval gates",
    )
    for needle in required:
        assert needle in text


def test_runbook_does_not_embed_real_artifact_url_revision_or_download_command() -> None:
    section = (
        _text()
        .split("## Verified Playwright image build prerequisite", 1)[1]
        .split("## Repository validation", 1)[0]
    )
    assert "https://" not in section
    assert "http://" not in section
    assert "1228" not in section
    assert "1011" not in section
    assert "latest" not in section.lower()
    assert "npx playwright" not in section
    assert "python -m playwright install" not in section
