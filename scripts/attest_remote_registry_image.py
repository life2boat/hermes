#!/usr/bin/env python3
"""Validate safe registry metadata for one exact-main image digest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from scripts.secret_scanner import (
    SecretScanError,
    scan_secret_bytes,
    scan_secret_text,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_MEDIA_TYPES = frozenset({
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
})


class RegistryAttestationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> None:
    raise RegistryAttestationError(code)


def _json(path: Path, maximum: int) -> tuple[bytes, dict[str, object]]:
    try:
        data = path.read_bytes()
        if not 0 < len(data) <= maximum:
            _fail("REGISTRY_METADATA_INVALID")
        document = json.loads(data.decode("utf-8"))
    except RegistryAttestationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        _fail("REGISTRY_METADATA_INVALID")
    if not isinstance(document, dict):
        _fail("REGISTRY_METADATA_INVALID")
    return data, document


def _secret_finding_count(value: object) -> int:
    if isinstance(value, str):
        return len(scan_secret_text(value))
    if isinstance(value, list):
        return sum(_secret_finding_count(item) for item in value)
    if isinstance(value, dict):
        return sum(
            _secret_finding_count(key) + _secret_finding_count(item)
            for key, item in value.items()
        )
    return 0


def attest(
    *,
    manifest_path: Path,
    config_path: Path,
    receipt_path: Path,
    expected_digest: str,
    expected_source_sha: str,
    expected_source_url: str,
    expected_platform: str,
) -> dict[str, object]:
    if (
        _DIGEST_RE.fullmatch(expected_digest) is None
        or _SHA_RE.fullmatch(expected_source_sha) is None
        or expected_platform != "linux/amd64"
    ):
        _fail("REGISTRY_EXPECTATION_INVALID")
    manifest_bytes, manifest = _json(manifest_path, 16 * 1024 * 1024)
    config_bytes, config = _json(config_path, 16 * 1024 * 1024)
    _, receipt = _json(receipt_path, 64 * 1024)
    actual_manifest_digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_digest != expected_digest:
        _fail("REGISTRY_DIGEST_MISMATCH")
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("mediaType") not in _MANIFEST_MEDIA_TYPES
        or not isinstance(manifest.get("config"), dict)
        or not isinstance(manifest.get("layers"), list)
    ):
        _fail("REGISTRY_MANIFEST_INVALID")
    descriptor = manifest["config"]
    config_digest = descriptor.get("digest")
    if not isinstance(config_digest, str) or _DIGEST_RE.fullmatch(config_digest) is None:
        _fail("REGISTRY_CONFIG_DIGEST_INVALID")
    layers = manifest["layers"]
    if not layers:
        _fail("REGISTRY_LAYER_SET_INVALID")
    layer_total = 0
    for layer in layers:
        if (
            not isinstance(layer, dict)
            or not isinstance(layer.get("digest"), str)
            or _DIGEST_RE.fullmatch(layer["digest"]) is None
            or type(layer.get("size")) is not int
            or layer["size"] <= 0
        ):
            _fail("REGISTRY_LAYER_SET_INVALID")
        layer_total += layer["size"]
    image_config = config.get("config")
    if not isinstance(image_config, dict):
        _fail("REGISTRY_CONFIG_INVALID")
    labels = image_config.get("Labels")
    if not isinstance(labels, dict):
        _fail("REGISTRY_LABELS_INVALID")
    revision = labels.get("org.opencontainers.image.revision")
    source = labels.get("org.opencontainers.image.source")
    if revision != expected_source_sha:
        _fail("REGISTRY_REVISION_MISMATCH")
    if source != expected_source_url:
        _fail("REGISTRY_SOURCE_LABEL_MISMATCH")
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        _fail("REGISTRY_PLATFORM_MISMATCH")
    if (
        receipt.get("source_sha") != expected_source_sha
        or receipt.get("registry_digest") != expected_digest
        or receipt.get("platform") != expected_platform
        or not isinstance(receipt.get("context_manifest_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", str(receipt["context_manifest_sha256"])
        ) is None
    ):
        _fail("REGISTRY_BUILD_RECEIPT_MISMATCH")
    try:
        findings = scan_secret_bytes(config_bytes)
        nested_findings = _secret_finding_count(config)
    except SecretScanError:
        _fail("REGISTRY_METADATA_SECRET_SCAN_FAILED")
    if findings or nested_findings:
        _fail("REGISTRY_METADATA_SECRET_FOUND")
    return {
        "config_digest": config_digest,
        "context_manifest_sha256": receipt["context_manifest_sha256"],
        "image_metadata_history_secret_findings": 0,
        "layer_count": len(layers),
        "manifest_digest": expected_digest,
        "manifest_platform": expected_platform,
        "oci_revision": revision,
        "registry_digest_immutable": True,
        "registry_digest_resolves": True,
        "registry_manifest_platform_match": True,
        "registry_oci_revision_match": True,
        "registry_source_label_match": True,
        "source_hash_match": True,
        "source_label": source,
        "target_compressed_layer_bytes": layer_total,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Attest one exact-main registry manifest and image config."
    )
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--expected-source-url", required=True)
    parser.add_argument("--expected-platform", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = attest(
            manifest_path=args.manifest,
            config_path=args.config,
            receipt_path=args.receipt,
            expected_digest=args.expected_digest,
            expected_source_sha=args.expected_source_sha,
            expected_source_url=args.expected_source_url,
            expected_platform=args.expected_platform,
        )
        args.output.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="ascii",
        )
        print("REMOTE_REGISTRY_ATTESTATION=PASS")
        print(f"REGISTRY_IMAGE_DIGEST={result['manifest_digest']}")
        print(f"REGISTRY_CONFIG_DIGEST={result['config_digest']}")
        print(
            "TARGET_COMPRESSED_LAYER_BYTES="
            f"{result['target_compressed_layer_bytes']}"
        )
        print("REGISTRY_OCI_REVISION_MATCH=true")
        print("REGISTRY_SOURCE_LABEL_MATCH=true")
        print("REGISTRY_MANIFEST_PLATFORM_MATCH=true")
        print("IMAGE_METADATA_HISTORY_SECRET_FINDINGS=0")
    except RegistryAttestationError as exc:
        print("REMOTE_REGISTRY_ATTESTATION=FAIL", file=sys.stderr)
        print(f"ERROR_CLASS={exc.code}", file=sys.stderr)
        return 1
    except Exception:
        print("REMOTE_REGISTRY_ATTESTATION=FAIL", file=sys.stderr)
        print("ERROR_CLASS=INTERNAL_ERROR", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
