#!/usr/bin/env python3
"""Fail-closed secret scan and structural validation for Docker image archives."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterable

sys.dont_write_bytecode = True

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts import hermes_production_deploy as deploy  # noqa: E402
from scripts.secret_scanner import SecretFinding, scan_secret_blob  # noqa: E402

SCAN_POLICY_VERSION = 1
IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
DIGEST_REFERENCE_RE = re.compile(r"[^@\s]+@sha256:[0-9a-f]{64}")
SHA256_RE = re.compile(r"sha256:([0-9a-f]{64})")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_FINDING_EVIDENCE = 1000
SCAN_CHUNK_BYTES = 1024 * 1024
SCAN_OVERLAP_BYTES = 4096


class ImageScanError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FindingEvidence:
    finding_class: str
    rule_id: str
    scope: str
    path_sha256: str
    layer_id: str | None


@dataclass
class ScanResult:
    image_id: str
    oci_revision: str
    config_digest: str
    layer_digests: tuple[str, ...]
    stored_layer_identities: tuple[str, ...]
    policy_sha256: str
    metadata_findings: int = 0
    layer_findings: int = 0
    final_filesystem_findings: int = 0
    layers_scanned: int = 0
    files_scanned: int = 0
    bytes_scanned: int = 0
    evidence: list[FindingEvidence] = field(default_factory=list)

    def contract(self) -> dict[str, Any]:
        total = self.metadata_findings + self.layer_findings
        return {
            "STATUS": "PASS" if total == 0 else "FAIL",
            "IMAGE_ID": self.image_id,
            "IMAGE_OCI_REVISION": self.oci_revision,
            "IMAGE_SECRET_FINDINGS": total,
            "IMAGE_METADATA_SECRET_FINDINGS": self.metadata_findings,
            "IMAGE_LAYER_SECRET_FINDINGS": self.layer_findings,
            "IMAGE_FINAL_FILESYSTEM_SECRET_FINDINGS": self.final_filesystem_findings,
            "LAYERS_SCANNED": self.layers_scanned,
            "FILES_SCANNED": self.files_scanned,
            "BYTES_SCANNED": self.bytes_scanned,
            "SCAN_POLICY_VERSION": SCAN_POLICY_VERSION,
            "SCAN_POLICY_SHA256": self.policy_sha256,
            "CONFIG_DIGEST": self.config_digest,
            "LAYER_DIGESTS": list(self.layer_digests),
            "STORED_LAYER_IDENTITIES": list(self.stored_layer_identities),
            "FINDINGS": [
                {
                    "finding_class": item.finding_class,
                    "rule_id": item.rule_id,
                    "scope": item.scope,
                    "path_sha256": item.path_sha256,
                    "layer_id": item.layer_id,
                }
                for item in self.evidence
            ],
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_path(value: str, *, rootfs: bool = False) -> str:
    if rootfs and value.startswith("./"):
        value = value[2:]
    if not value or "\\" in value or "\x00" in value:
        raise ImageScanError("IMAGE_ARCHIVE_PATH_UNSAFE")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ImageScanError("IMAGE_ARCHIVE_PATH_UNSAFE")
    return path.as_posix()


def _json_object(data: bytes, code: str) -> dict[str, Any]:
    if len(data) > MAX_JSON_BYTES:
        raise ImageScanError(code)
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ImageScanError(code) from exc
    if not isinstance(value, dict):
        raise ImageScanError(code)
    return value


def _copy_stream(source: BinaryIO, destination: Path) -> None:
    with destination.open("xb") as target:
        while chunk := source.read(SCAN_CHUNK_BYTES):
            target.write(chunk)
        target.flush()
        os.fsync(target.fileno())


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(SCAN_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _read_limited(path: Path, code: str) -> bytes:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ImageScanError(code)
    return path.read_bytes()


def _outer_members(archive: Path, staging: Path) -> dict[str, Path]:
    members: dict[str, Path] = {}
    try:
        with tarfile.open(archive, mode="r:*") as outer:
            for member in outer:
                name = _safe_path(member.name)
                if name in members:
                    raise ImageScanError("IMAGE_ARCHIVE_DUPLICATE_MEMBER")
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ImageScanError("IMAGE_ARCHIVE_MEMBER_TYPE_UNSUPPORTED")
                stream = outer.extractfile(member)
                if stream is None:
                    raise ImageScanError("IMAGE_ARCHIVE_MEMBER_MISSING")
                staged = staging / f"outer-{len(members):08d}"
                _copy_stream(stream, staged)
                if staged.stat().st_size != member.size:
                    raise ImageScanError("IMAGE_ARCHIVE_MEMBER_TRUNCATED")
                members[name] = staged
    except ImageScanError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise ImageScanError("IMAGE_ARCHIVE_MALFORMED") from exc
    return members


def _docker_layout(
    members: dict[str, Path],
) -> tuple[bytes, tuple[Path, ...], tuple[str, ...]]:
    manifest_path = members.get("manifest.json")
    if manifest_path is None:
        raise ImageScanError("IMAGE_ARCHIVE_MANIFEST_MISSING")
    raw = _read_limited(manifest_path, "IMAGE_ARCHIVE_MANIFEST_INVALID")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ImageScanError("IMAGE_ARCHIVE_MANIFEST_INVALID") from exc
    if (
        not isinstance(manifest, list)
        or len(manifest) != 1
        or not isinstance(manifest[0], dict)
    ):
        raise ImageScanError("IMAGE_ARCHIVE_IDENTITY_AMBIGUOUS")
    config_name = manifest[0].get("Config")
    layer_names = manifest[0].get("Layers")
    if (
        not isinstance(config_name, str)
        or not isinstance(layer_names, list)
        or not layer_names
    ):
        raise ImageScanError("IMAGE_ARCHIVE_MANIFEST_INVALID")
    config_name = _safe_path(config_name)
    if not all(isinstance(item, str) for item in layer_names):
        raise ImageScanError("IMAGE_ARCHIVE_MANIFEST_INVALID")
    safe_layers = tuple(_safe_path(item) for item in layer_names)
    if len(set(safe_layers)) != len(safe_layers):
        raise ImageScanError("IMAGE_ARCHIVE_LAYER_DUPLICATE")
    try:
        config_path = members[config_name]
        layers = tuple(members[name] for name in safe_layers)
    except KeyError as exc:
        raise ImageScanError("IMAGE_ARCHIVE_MEMBER_MISSING") from exc
    config = _read_limited(config_path, "IMAGE_CONFIG_INVALID")
    if PurePosixPath(config_name).name.removesuffix(".json") != _sha256(config):
        raise ImageScanError("IMAGE_CONFIG_DIGEST_MISMATCH")
    return config, layers, safe_layers


def _oci_blob(
    members: dict[str, Path], descriptor: Any, *, media_prefix: str
) -> tuple[Path, str]:
    if not isinstance(descriptor, dict):
        raise ImageScanError("OCI_DESCRIPTOR_INVALID")
    digest = descriptor.get("digest")
    size = descriptor.get("size")
    media_type = descriptor.get("mediaType")
    match = SHA256_RE.fullmatch(digest) if isinstance(digest, str) else None
    if (
        match is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(media_type, str)
        or media_prefix not in media_type
    ):
        raise ImageScanError("OCI_DESCRIPTOR_INVALID")
    try:
        path = members[f"blobs/sha256/{match.group(1)}"]
    except KeyError as exc:
        raise ImageScanError("IMAGE_ARCHIVE_MEMBER_MISSING") from exc
    if path.stat().st_size != size or _hash_file(path) != match.group(1):
        raise ImageScanError("OCI_BLOB_IDENTITY_MISMATCH")
    return path, media_type


def _oci_layout(
    members: dict[str, Path], staging: Path
) -> tuple[bytes, tuple[Path, ...], tuple[str, ...]]:
    if "oci-layout" not in members or "index.json" not in members:
        raise ImageScanError("IMAGE_ARCHIVE_FORMAT_UNSUPPORTED")
    index = _json_object(
        _read_limited(members["index.json"], "OCI_INDEX_INVALID"),
        "OCI_INDEX_INVALID",
    )
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ImageScanError("IMAGE_ARCHIVE_IDENTITY_AMBIGUOUS")
    manifest_path, _ = _oci_blob(
        members, manifests[0], media_prefix="application/vnd.oci.image.manifest"
    )
    manifest = _json_object(
        _read_limited(manifest_path, "OCI_MANIFEST_INVALID"),
        "OCI_MANIFEST_INVALID",
    )
    config_path, _ = _oci_blob(
        members, manifest.get("config"), media_prefix="application/vnd.oci.image.config"
    )
    config = _read_limited(config_path, "IMAGE_CONFIG_INVALID")
    descriptors = manifest.get("layers")
    if not isinstance(descriptors, list) or not descriptors:
        raise ImageScanError("OCI_MANIFEST_INVALID")
    layers: list[Path] = []
    identities: list[str] = []
    for descriptor in descriptors:
        path, media_type = _oci_blob(
            members, descriptor, media_prefix="application/vnd.oci.image.layer"
        )
        digest = str(descriptor["digest"])
        if digest in identities:
            raise ImageScanError("IMAGE_ARCHIVE_LAYER_DUPLICATE")
        identities.append(digest)
        if media_type.endswith("+gzip"):
            expanded = staging / f"layer-{len(layers):08d}.tar"
            try:
                with gzip.open(path, "rb") as source:
                    _copy_stream(source, expanded)
            except (EOFError, OSError) as exc:
                raise ImageScanError("IMAGE_LAYER_COMPRESSION_INVALID") from exc
            path = expanded
        elif media_type not in (
            "application/vnd.oci.image.layer.v1.tar",
            "application/vnd.oci.image.layer.nondistributable.v1.tar",
        ):
            raise ImageScanError("IMAGE_LAYER_MEDIA_TYPE_UNSUPPORTED")
        layers.append(path)
    return config, tuple(layers), tuple(identities)


def _layout(
    members: dict[str, Path], staging: Path
) -> tuple[bytes, tuple[Path, ...], tuple[str, ...]]:
    return (
        _docker_layout(members)
        if "manifest.json" in members
        else _oci_layout(members, staging)
    )


def _config_identity(
    config: dict[str, Any],
    config_bytes: bytes,
    expected_image_id: str,
    expected_revision: str | None,
) -> tuple[str, tuple[str, ...], str]:
    image_id = "sha256:" + _sha256(config_bytes)
    if (
        IMAGE_ID_RE.fullmatch(expected_image_id) is None
        or image_id != expected_image_id
    ):
        raise ImageScanError("IMAGE_IDENTITY_MISMATCH")
    rootfs = config.get("rootfs")
    diff_ids = rootfs.get("diff_ids") if isinstance(rootfs, dict) else None
    if (
        not isinstance(diff_ids, list)
        or not diff_ids
        or not all(
            isinstance(item, str) and SHA256_RE.fullmatch(item) for item in diff_ids
        )
    ):
        raise ImageScanError("IMAGE_DIFF_IDS_INVALID")
    image_config = config.get("config")
    labels = image_config.get("Labels") if isinstance(image_config, dict) else None
    revision = (
        labels.get("org.opencontainers.image.revision")
        if isinstance(labels, dict)
        else None
    )
    if expected_revision is None:
        revision = (
            revision
            if isinstance(revision, str) and REVISION_RE.fullmatch(revision)
            else "UNKNOWN"
        )
    elif revision != expected_revision:
        raise ImageScanError("IMAGE_OCI_REVISION_MISMATCH")
    return image_id, tuple(diff_ids), revision


def _policy_sha(repository_root: Path, protected_names: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for relative in (
        "scripts/secret_scanner.py",
        "scripts/hermes_image_secret_scan.py",
        "deploy/hermes-production.json",
    ):
        digest.update(relative.encode("ascii") + b"\0")
        digest.update((repository_root / relative).read_bytes() + b"\0")
    digest.update("\n".join(sorted(protected_names)).encode("ascii"))
    return digest.hexdigest()


def _evidence(
    findings: Iterable[SecretFinding], *, scope: str, path: str, layer_id: str | None
) -> list[FindingEvidence]:
    path_sha = _sha256(path.encode("utf-8", errors="surrogateescape"))
    return [
        FindingEvidence(item.match_class, item.rule_id, scope, path_sha, layer_id)
        for item in findings
    ]


def _scan_bytes(
    data: bytes,
    *,
    protected_names: Iterable[str],
    exact_values: Iterable[bytes],
) -> list[SecretFinding]:
    findings = list(scan_secret_blob(data, protected_names=protected_names))
    for value in exact_values:
        if value and value in data:
            findings.append(
                SecretFinding(
                    "exact-protected-fingerprint", "PROTECTED_SECRET_MATERIAL"
                )
            )
    return findings


def _scan_stream(
    stream: BinaryIO,
    *,
    protected_names: Iterable[str],
    exact_values: Iterable[bytes],
) -> tuple[list[SecretFinding], int]:
    values = tuple(value for value in exact_values if value)
    overlap = max(
        SCAN_OVERLAP_BYTES,
        max((len(value) - 1 for value in values), default=0),
    )
    tail = b""
    scanned = 0
    found: dict[tuple[str, str], SecretFinding] = {}
    while chunk := stream.read(SCAN_CHUNK_BYTES):
        scanned += len(chunk)
        window = tail + chunk
        for finding in scan_secret_blob(window, protected_names=protected_names):
            found[(finding.rule_id, finding.match_class)] = finding
        if b"-----BEGIN " in window and b"PRIVATE KEY-----" in window:
            finding = SecretFinding("private-key-block", "PRIVATE_KEY_MATERIAL")
            found[(finding.rule_id, finding.match_class)] = finding
        for value in values:
            if value in window:
                finding = SecretFinding(
                    "exact-protected-fingerprint",
                    "PROTECTED_SECRET_MATERIAL",
                )
                found[(finding.rule_id, finding.match_class)] = finding
        tail = window[-overlap:]
    return list(found.values()), scanned


def _remove(tree: dict[str, tuple[int, int]], target: str) -> None:
    prefix = target + "/"
    for current in tuple(tree):
        if current == target or current.startswith(prefix):
            del tree[current]


def _scan_layer(
    path: Path,
    *,
    layer_id: str,
    tree: dict[str, tuple[int, int]],
    protected_names: Iterable[str],
    exact_values: Iterable[bytes],
) -> tuple[int, int, int, list[FindingEvidence]]:
    count = files = scanned = 0
    evidence: list[FindingEvidence] = []
    seen: set[str] = set()
    try:
        with tarfile.open(path, mode="r:*") as layer:
            for member in layer:
                member_path = _safe_path(member.name, rootfs=True)
                if member_path in seen:
                    raise ImageScanError("IMAGE_LAYER_DUPLICATE_PATH")
                seen.add(member_path)
                pure = PurePosixPath(member_path)
                if pure.name == ".wh..wh..opq":
                    parent = pure.parent.as_posix()
                    if parent == ".":
                        tree.clear()
                    else:
                        prefix = parent + "/"
                        for current in tuple(tree):
                            if current.startswith(prefix):
                                del tree[current]
                    continue
                if pure.name.startswith(".wh."):
                    _remove(tree, (pure.parent / pure.name[4:]).as_posix())
                    continue
                if member.issym() or member.islnk():
                    _safe_path(member.linkname, rootfs=True)
                    tree[member_path] = (0, 0)
                    continue
                if member.isdir():
                    tree[member_path] = (0, 0)
                    continue
                if not member.isfile():
                    raise ImageScanError("IMAGE_LAYER_MEMBER_TYPE_UNSUPPORTED")
                source = layer.extractfile(member)
                if source is None:
                    raise ImageScanError("IMAGE_LAYER_MEMBER_MISSING")
                current, bytes_read = _scan_stream(
                    source,
                    protected_names=protected_names,
                    exact_values=exact_values,
                )
                if bytes_read != member.size:
                    raise ImageScanError("IMAGE_LAYER_MEMBER_TRUNCATED")
                tree[member_path] = (len(current), bytes_read)
                count += len(current)
                files += 1
                scanned += bytes_read
                evidence.extend(
                    _evidence(
                        current,
                        scope="layer",
                        path=member_path,
                        layer_id=layer_id,
                    )
                )
    except ImageScanError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise ImageScanError("IMAGE_LAYER_MALFORMED") from exc
    return count, files, scanned, evidence


def _analyze_image_archive_staged(
    archive: Path,
    staging: Path,
    *,
    expected_image_id: str,
    expected_revision: str | None,
    repository_root: Path = REPOSITORY_ROOT,
    protected_names: Iterable[str] = (),
    exact_secret_values: Iterable[bytes] = (),
) -> ScanResult:
    config_bytes, layers, stored_ids = _layout(
        _outer_members(archive, staging), staging
    )
    config = _json_object(config_bytes, "IMAGE_CONFIG_INVALID")
    image_id, diff_ids, revision = _config_identity(
        config, config_bytes, expected_image_id, expected_revision
    )
    if len(diff_ids) != len(layers):
        raise ImageScanError("IMAGE_LAYER_COUNT_MISMATCH")
    for layer_path, diff_id in zip(layers, diff_ids, strict=True):
        if "sha256:" + _hash_file(layer_path) != diff_id:
            raise ImageScanError("IMAGE_LAYER_IDENTITY_MISMATCH")
    names = tuple(sorted(set(protected_names)))
    values = tuple(exact_secret_values)
    result = ScanResult(
        image_id,
        revision,
        image_id,
        diff_ids,
        stored_ids,
        _policy_sha(repository_root, names),
    )
    metadata = _scan_bytes(config_bytes, protected_names=names, exact_values=values)
    result.metadata_findings = len(metadata)
    result.evidence.extend(
        _evidence(metadata, scope="metadata", path="<image-config>", layer_id=None)
    )
    tree: dict[str, tuple[int, int]] = {}
    for layer_path, diff_id, stored_id in zip(
        layers, diff_ids, stored_ids, strict=True
    ):
        layer_count, files, scanned, evidence = _scan_layer(
            layer_path,
            layer_id=diff_id if stored_id.endswith(".tar") else stored_id,
            tree=tree,
            protected_names=names,
            exact_values=values,
        )
        result.layer_findings += layer_count
        result.files_scanned += files
        result.bytes_scanned += scanned
        result.layers_scanned += 1
        result.evidence.extend(evidence)
    result.final_filesystem_findings = sum(item[0] for item in tree.values())
    result.evidence = result.evidence[:MAX_FINDING_EVIDENCE]
    return result


def analyze_image_archive(
    archive: Path,
    *,
    expected_image_id: str,
    expected_revision: str | None,
    repository_root: Path = REPOSITORY_ROOT,
    protected_names: Iterable[str] = (),
    exact_secret_values: Iterable[bytes] = (),
) -> ScanResult:
    with tempfile.TemporaryDirectory(prefix="hermes-image-archive-stage-") as temporary:
        return _analyze_image_archive_staged(
            archive,
            Path(temporary),
            expected_image_id=expected_image_id,
            expected_revision=expected_revision,
            repository_root=repository_root,
            protected_names=protected_names,
            exact_secret_values=exact_secret_values,
        )


def verify_rollback_archive(archive: Path, expected_image_id: str) -> dict[str, Any]:
    result = analyze_image_archive(
        archive,
        expected_image_id=expected_image_id,
        expected_revision=None,
    )
    return {
        "image_id": result.image_id,
        "config_digest": result.config_digest,
        "layer_digests": list(result.layer_digests),
        "layers_verified": result.layers_scanned,
        "archive_structure_valid": True,
    }


def _inspect_local_image(image: str, expected_revision: str) -> str:
    if (
        IMAGE_ID_RE.fullmatch(image) is None
        and DIGEST_REFERENCE_RE.fullmatch(image) is None
    ):
        raise ImageScanError("MUTABLE_IMAGE_REFERENCE_DENIED")
    completed = subprocess.run(
        ("docker", "image", "inspect", image),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ImageScanError("IMAGE_INSPECT_FAILED")
    try:
        records = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ImageScanError("IMAGE_INSPECT_INVALID") from exc
    if (
        not isinstance(records, list)
        or len(records) != 1
        or not isinstance(records[0], dict)
    ):
        raise ImageScanError("IMAGE_INSPECT_INVALID")
    image_id = records[0].get("Id")
    config = records[0].get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    revision = (
        labels.get("org.opencontainers.image.revision")
        if isinstance(labels, dict)
        else None
    )
    if not isinstance(image_id, str) or IMAGE_ID_RE.fullmatch(image_id) is None:
        raise ImageScanError("IMAGE_INSPECT_INVALID")
    if IMAGE_ID_RE.fullmatch(image) is not None and image_id != image:
        raise ImageScanError("IMAGE_IDENTITY_MISMATCH")
    if revision != expected_revision:
        raise ImageScanError("IMAGE_OCI_REVISION_MISMATCH")
    return image_id


def _write_new_json(path: Path, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "ascii"
    )
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def scan_local_image(args: argparse.Namespace) -> int:
    if REVISION_RE.fullmatch(args.expected_source_sha) is None:
        raise ImageScanError("EXPECTED_SOURCE_SHA_INVALID")
    repository_root = Path(args.repository_root).resolve()
    contract = deploy.load_contract(repository_root)
    image_id = _inspect_local_image(args.image, args.expected_source_sha)
    exact_values: tuple[bytes, ...] = ()
    if args.secret_source is not None:
        secrets = deploy.read_required_secrets(
            contract, Path(args.secret_source).resolve()
        )
        exact_values = tuple(value.encode("utf-8") for value in secrets.values())
    with tempfile.TemporaryDirectory(prefix="hermes-image-secret-scan-") as temporary:
        archive = Path(temporary) / "image.tar"
        completed = subprocess.run(
            ("docker", "image", "save", "--output", str(archive), image_id),
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if completed.returncode != 0:
            raise ImageScanError("IMAGE_EXPORT_FAILED")
        result = analyze_image_archive(
            archive,
            expected_image_id=image_id,
            expected_revision=args.expected_source_sha,
            repository_root=repository_root,
            protected_names=contract.protected_secret_names,
            exact_secret_values=exact_values,
        )
    payload = result.contract()
    _write_new_json(Path(args.output), payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload["IMAGE_SECRET_FINDINGS"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--secret-source")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return scan_local_image(build_parser().parse_args(argv))
    except (
        ImageScanError,
        OSError,
        subprocess.SubprocessError,
        deploy.DeploymentContractError,
    ) as exc:
        print(
            json.dumps(
                {"STATUS": "FAIL", "ERROR": getattr(exc, "code", "IMAGE_SCAN_FAILED")},
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
