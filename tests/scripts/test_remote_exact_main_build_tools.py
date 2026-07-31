from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from scripts import attest_remote_registry_image as registry_attestation
from scripts import install_pinned_playwright_artifact as installer
from scripts import prepare_remote_playwright_artifacts as acquisition
from tests.playwright_supply_chain_support import (
    SOURCE_REFERENCE_SHA256,
    closure_manifest_document,
    verified_closure,
    write_closure_archives,
)
from tests.secret_scanner_support import synthetic_assignment


def _write_json(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )


def test_remote_acquisition_reconstructs_reviewed_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, lockfile, wheel = verified_closure(tmp_path)
    archives = write_closure_archives(tmp_path / "source-archives", verified)
    expected_document = closure_manifest_document(verified, archives)
    expected_bytes = installer.canonical_json(expected_document)
    expected_sha = hashlib.sha256(expected_bytes).hexdigest()
    wheel_url = (
        f"https://packages.invalid/{verified.wheel.filename}"
    )
    sources = {wheel_url: wheel}
    artifacts = []
    for artifact in verified.closure.artifacts:
        url = f"https://artifacts.invalid/{artifact.artifact_name}.zip"
        archive = archives[artifact.artifact_name]
        sources[url] = archive
        artifacts.append({
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "archive_size": archive.stat().st_size,
            "artifact_name": artifact.artifact_name,
            "source_kind": "operator-approved-offline-artifact",
            "source_reference_sha256": SOURCE_REFERENCE_SHA256,
            "url": url,
        })
    policy = {
        "approved_base_sha": "a" * 40,
        "artifacts": artifacts,
        "closure_manifest_sha256": expected_sha,
        "platform": "linux/amd64",
        "policy_version": 1,
        "wheel_url": wheel_url,
    }
    policy_path = tmp_path / "policy.json"
    _write_json(policy_path, policy)
    output = tmp_path / "operation" / "closure"
    output.parent.mkdir()

    monkeypatch.setattr(
        acquisition,
        "_ALLOWED_DOWNLOAD_HOSTS",
        frozenset({"artifacts.invalid", "packages.invalid"}),
    )

    def copy_download(
        *,
        url: str,
        destination: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        source = sources[url]
        assert source.stat().st_size == expected_size
        assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_sha256
        shutil.copyfile(source, destination)
        destination.chmod(0o600)

    monkeypatch.setattr(acquisition, "_download", copy_download)

    assert acquisition.prepare(
        policy_path=policy_path,
        lockfile=lockfile,
        output=output,
    ) == expected_sha
    assert (output / "closure.json").read_bytes() == expected_bytes
    assert (output / "playwright-wheel").read_bytes() == wheel.read_bytes()


def test_remote_acquisition_rejects_unknown_policy_field(tmp_path: Path) -> None:
    policy = {
        "approved_base_sha": "a" * 40,
        "artifacts": [],
        "closure_manifest_sha256": "b" * 64,
        "platform": "linux/amd64",
        "policy_version": 1,
        "unexpected": True,
        "wheel_url": "https://files.pythonhosted.org/example.whl",
    }
    path = tmp_path / "policy.json"
    _write_json(path, policy)
    with pytest.raises(
        acquisition.RemoteArtifactError,
        match="^REMOTE_ARTIFACT_POLICY_INVALID$",
    ):
        acquisition._read_policy(path)


def _registry_fixture(tmp_path: Path) -> dict[str, Path | str]:
    source_sha = "a" * 40
    source_url = "https://github.com/life2boat/hermes"
    config = {
        "architecture": "amd64",
        "config": {
            "Env": ["PATH=/usr/bin"],
            "Labels": {
                "org.opencontainers.image.revision": source_sha,
                "org.opencontainers.image.source": source_url,
            },
        },
        "history": [{"created_by": "COPY exact reviewed context"}],
        "os": "linux",
    }
    config_path = tmp_path / "config.json"
    _write_json(config_path, config)
    manifest = {
        "config": {
            "digest": "sha256:" + "b" * 64,
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "size": config_path.stat().st_size,
        },
        "layers": [
            {
                "digest": "sha256:" + "c" * 64,
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 101,
            },
            {
                "digest": "sha256:" + "d" * 64,
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 202,
            },
        ],
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "schemaVersion": 2,
    }
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, manifest)
    digest = "sha256:" + hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    receipt_path = tmp_path / "receipt.json"
    _write_json(receipt_path, {
        "context_manifest_sha256": "e" * 64,
        "platform": "linux/amd64",
        "registry_digest": digest,
        "source_sha": source_sha,
    })
    return {
        "config": config_path,
        "digest": digest,
        "manifest": manifest_path,
        "receipt": receipt_path,
        "source_sha": source_sha,
        "source_url": source_url,
    }


def test_registry_attestation_binds_digest_source_platform_and_layers(
    tmp_path: Path,
) -> None:
    fixture = _registry_fixture(tmp_path)
    result = registry_attestation.attest(
        manifest_path=fixture["manifest"],
        config_path=fixture["config"],
        receipt_path=fixture["receipt"],
        expected_digest=fixture["digest"],
        expected_source_sha=fixture["source_sha"],
        expected_source_url=fixture["source_url"],
        expected_platform="linux/amd64",
    )
    assert result["target_compressed_layer_bytes"] == 303
    assert result["registry_oci_revision_match"] is True
    assert result["registry_source_label_match"] is True
    assert result["image_metadata_history_secret_findings"] == 0


def test_registry_attestation_denies_secret_shaped_history(tmp_path: Path) -> None:
    fixture = _registry_fixture(tmp_path)
    config_path = fixture["config"]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["history"] = [{"created_by": synthetic_assignment()}]
    _write_json(config_path, config)
    with pytest.raises(
        registry_attestation.RegistryAttestationError,
        match="^REGISTRY_METADATA_SECRET_FOUND$",
    ):
        registry_attestation.attest(
            manifest_path=fixture["manifest"],
            config_path=config_path,
            receipt_path=fixture["receipt"],
            expected_digest=fixture["digest"],
            expected_source_sha=fixture["source_sha"],
            expected_source_url=fixture["source_url"],
            expected_platform="linux/amd64",
        )
