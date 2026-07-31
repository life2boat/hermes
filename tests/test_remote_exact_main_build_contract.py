from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts import build_verified_playwright_image as build_helper


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/healbite-exact-main-ghcr.yml"
POLICY = ROOT / "deploy/playwright-remote-build-artifacts.json"
RUNBOOK = ROOT / "docs/runbooks/hermes-remote-exact-main-build.md"


def test_workflow_is_manual_main_only_with_minimal_permissions() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "pull_request:" not in text
    assert "push:" not in text
    assert "contents: read" in text
    assert "packages: write" in text
    assert "github.repository == 'life2boat/hermes'" in text
    assert "github.ref == 'refs/heads/main'" in text
    assert "ref: ${{ github.sha }}" in text
    assert "fetch-depth: 0" in text
    assert "refs/remotes/origin/main" in text
    assert text.count("-m scripts.build_verified_playwright_image build-push") == 1
    assert "ghcr.io/life2boat/hermes:sha-" not in text
    assert "$REGISTRY_IMAGE:sha-$GITHUB_SHA" in text
    assert ":latest" not in text
    assert "DOCKERHUB" not in text


def test_workflow_uses_repository_module_entrypoints() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python3 -m scripts.prepare_remote_playwright_artifacts" in text
    assert "python3 -m scripts.build_verified_playwright_image" in text
    assert "python3 -m scripts.attest_remote_registry_image" in text
    assert "python3 scripts/" not in text


def test_workflow_actions_are_commit_pinned() -> None:
    for line in WORKFLOW.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("uses:") or stripped.startswith("uses: ./"):
            continue
        reference = stripped.split("@", 1)[1].split()[0]
        assert len(reference) == 40
        assert all(character in "0123456789abcdef" for character in reference)


def test_approved_policy_binds_reviewed_closure() -> None:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    assert policy["policy_version"] == 1
    assert policy["platform"] == "linux/amd64"
    assert policy["approved_base_sha"] == (
        "8b44bb146b31902dc99c53d976e7b20964eb4caa"
    )
    assert policy["closure_manifest_sha256"] == (
        "a685b3d071968d5140ce42f8346246b0857167c90eefdde71b6ff011eb97340f"
    )
    assert {item["artifact_name"] for item in policy["artifacts"]} == {
        "chromium-headless-shell",
        "ffmpeg",
    }
    assert all(item["url"].startswith("https://") for item in policy["artifacts"])
    assert all(len(item["archive_sha256"]) == 64 for item in policy["artifacts"])


def test_buildx_push_command_has_only_non_secret_build_args(tmp_path: Path) -> None:
    inputs = build_helper.BuildInputs(
        repository_root=tmp_path,
        source_sha="a" * 40,
        source_tree_sha="b" * 40,
        approved_base_sha="c" * 40,
        approved_base_tree_sha="d" * 40,
        build_context=tmp_path / "context",
        context_manifest=tmp_path / "context.json",
        context_manifest_sha256="e" * 64,
        context_file_count=1,
        artifact_context=tmp_path / "artifacts",
        closure_manifest_sha256="f" * 64,
        image_tag=f"ghcr.io/life2boat/hermes:sha-{'a' * 40}",
        platform="linux/amd64",
    )
    command = build_helper.docker_buildx_push_command(
        inputs, tmp_path / "metadata.json"
    )
    assert command[:3] == ["docker", "buildx", "build"]
    assert "--push" in command
    assert "--load" not in command
    assert "--provenance=false" in command
    assert "--sbom=false" in command
    args = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--build-arg"
    ]
    assert args == [
        f"HERMES_GIT_SHA={'a' * 40}",
        f"PLAYWRIGHT_ARTIFACT_CLOSURE_SHA256={'f' * 64}",
    ]
    assert "org.opencontainers.image.source=https://github.com/life2boat/hermes" in command
    assert not any("TOKEN" in argument or "API_KEY" in argument for argument in command)


def test_runbook_has_explicit_stop_boundary() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "never pulls an image to the production VPS" in text
    assert "one `docker buildx build --push`" in text
    assert "Pull and deployment require a separate" in text
    assert "latest` is never" in text


def test_policy_contains_no_credential_material() -> None:
    data = POLICY.read_bytes()
    text = data.decode("utf-8")
    assert "token" not in text.lower()
    assert "password" not in text.lower()
    assert "secret" not in text.lower()
    assert hashlib.sha256(data).hexdigest()
