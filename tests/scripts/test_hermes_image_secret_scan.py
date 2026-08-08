from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts import hermes_image_secret_scan as scanner


REVISION = "a" * 40
TOKEN = "90753184:" + "Qw9Zx7Cv5Bn3Mk8Jh6Gf2Ds4Pa"


def _tar_bytes(entries: list[tuple[str, bytes]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, payload in entries:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(payload))
    return stream.getvalue()


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    info.mode = 0o600
    archive.addfile(info, io.BytesIO(payload))


def _docker_archive(
    tmp_path: Path,
    layers: list[bytes],
    *,
    env: list[str] | None = None,
    labels: dict[str, str] | None = None,
    history: list[dict[str, str]] | None = None,
    revision: str = REVISION,
    manifest_records: int = 1,
    omit_layer: int | None = None,
    outer_extra: tuple[str, bytes] | None = None,
    config_name_override: str | None = None,
) -> tuple[Path, str]:
    image_labels = {"org.opencontainers.image.revision": revision}
    image_labels.update(labels or {})
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": env or ["PATH=/usr/bin"], "Labels": image_labels},
        "history": history or [{"created_by": "COPY safe /opt/app"}],
        "rootfs": {
            "type": "layers",
            "diff_ids": [
                "sha256:" + hashlib.sha256(layer).hexdigest() for layer in layers
            ],
        },
    }
    config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    layer_names = [f"layer-{index}/layer.tar" for index in range(len(layers))]
    config_name = config_name_override or f"{config_hash}.json"
    record = {"Config": config_name, "RepoTags": [], "Layers": layer_names}
    manifest = [record for _ in range(manifest_records)]
    path = tmp_path / "image.tar"
    with tarfile.open(path, mode="w") as outer:
        _add_bytes(outer, config_name, config_bytes)
        _add_bytes(outer, "manifest.json", json.dumps(manifest).encode())
        for index, (name, layer) in enumerate(zip(layer_names, layers, strict=True)):
            if index != omit_layer:
                _add_bytes(outer, name, layer)
        if outer_extra is not None:
            _add_bytes(outer, *outer_extra)
    return path, "sha256:" + config_hash


def _scan(path: Path, image_id: str, *, revision: str | None = REVISION):
    return scanner.analyze_image_archive(
        path,
        expected_image_id=image_id,
        expected_revision=revision,
        protected_names=("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY"),
    )


def test_clean_image_passes(tmp_path: Path) -> None:
    path, image_id = _docker_archive(
        tmp_path, [_tar_bytes([("opt/app/readme.txt", b"safe content")])]
    )
    contract = _scan(path, image_id).contract()
    assert contract["STATUS"] == "PASS"
    assert contract["IMAGE_SECRET_FINDINGS"] == 0
    assert contract["LAYERS_SCANNED"] == 1


def test_secret_in_final_filesystem_fails(tmp_path: Path) -> None:
    layer = _tar_bytes([("run/config.env", f"TELEGRAM_BOT_TOKEN={TOKEN}".encode())])
    path, image_id = _docker_archive(tmp_path, [layer])
    result = _scan(path, image_id)
    assert result.layer_findings > 0
    assert result.final_filesystem_findings > 0


def test_deleted_old_layer_secret_is_still_a_finding(tmp_path: Path) -> None:
    old = _tar_bytes([("run/config.env", f"TELEGRAM_BOT_TOKEN={TOKEN}".encode())])
    delete = _tar_bytes([("run/.wh.config.env", b"")])
    path, image_id = _docker_archive(tmp_path, [old, delete])
    result = _scan(path, image_id)
    assert result.layer_findings > 0
    assert result.final_filesystem_findings == 0


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"env": [f"TELEGRAM_BOT_TOKEN={TOKEN}"]}, "Env"),
        ({"labels": {"OPENAI_API_KEY": TOKEN}}, "label"),
        ({"history": [{"created_by": f"RUN token={TOKEN}"}]}, "history"),
    ],
)
def test_metadata_secret_fails(
    tmp_path: Path, kwargs: dict[str, object], field: str
) -> None:
    path, image_id = _docker_archive(
        tmp_path, [_tar_bytes([("safe", b"safe")])], **kwargs
    )
    result = _scan(path, image_id)
    assert result.metadata_findings > 0, field


def test_private_key_material_fails(tmp_path: Path) -> None:
    private_key = (
        b"-----BEGIN PRIVATE KEY-----\n" + b"Q" * 64 + b"\n-----END PRIVATE KEY-----"
    )
    path, image_id = _docker_archive(
        tmp_path, [_tar_bytes([("root/id.key", private_key)])]
    )
    assert _scan(path, image_id).layer_findings > 0


def test_secret_crossing_stream_chunk_boundary_fails(tmp_path: Path) -> None:
    assignment = f"TELEGRAM_BOT_TOKEN={TOKEN}".encode()
    payload = b"x" * (scanner.SCAN_CHUNK_BYTES - 12) + assignment
    path, image_id = _docker_archive(
        tmp_path,
        [_tar_bytes([("run/large-config.env", payload)])],
    )

    contract = _scan(path, image_id).contract()

    assert contract["STATUS"] == "FAIL"
    assert contract["IMAGE_LAYER_SECRET_FINDINGS"] > 0
    assert contract["BYTES_SCANNED"] == len(payload)
    assert contract["STORED_LAYER_IDENTITIES"] == ["layer-0/layer.tar"]


def test_malformed_archive_fails(tmp_path: Path) -> None:
    path = tmp_path / "bad.tar"
    path.write_bytes(b"not a tar archive")
    with pytest.raises(scanner.ImageScanError, match="IMAGE_ARCHIVE_MALFORMED"):
        _scan(path, "sha256:" + "1" * 64)


def test_missing_layer_fails(tmp_path: Path) -> None:
    path, image_id = _docker_archive(
        tmp_path, [_tar_bytes([("safe", b"safe")])], omit_layer=0
    )
    with pytest.raises(scanner.ImageScanError, match="IMAGE_ARCHIVE_MEMBER_MISSING"):
        _scan(path, image_id)


def test_outer_archive_traversal_fails(tmp_path: Path) -> None:
    path, image_id = _docker_archive(
        tmp_path,
        [_tar_bytes([("safe", b"safe")])],
        outer_extra=("../escape", b"unsafe"),
    )
    with pytest.raises(scanner.ImageScanError, match="IMAGE_ARCHIVE_PATH_UNSAFE"):
        _scan(path, image_id)


def test_layer_path_traversal_fails(tmp_path: Path) -> None:
    path, image_id = _docker_archive(tmp_path, [_tar_bytes([("../escape", b"unsafe")])])
    with pytest.raises(scanner.ImageScanError, match="IMAGE_ARCHIVE_PATH_UNSAFE"):
        _scan(path, image_id)


def test_duplicate_artifact_identity_fails(tmp_path: Path) -> None:
    path, image_id = _docker_archive(
        tmp_path, [_tar_bytes([("safe", b"safe")])], manifest_records=2
    )
    with pytest.raises(
        scanner.ImageScanError, match="IMAGE_ARCHIVE_IDENTITY_AMBIGUOUS"
    ):
        _scan(path, image_id)


def test_config_digest_identity_mismatch_fails(tmp_path: Path) -> None:
    path, image_id = _docker_archive(
        tmp_path,
        [_tar_bytes([("safe", b"safe")])],
        config_name_override=f"{'f' * 64}.json",
    )

    with pytest.raises(scanner.ImageScanError, match="IMAGE_CONFIG_DIGEST_MISMATCH"):
        _scan(path, image_id)


def test_contract_never_contains_secret_value(tmp_path: Path) -> None:
    path, image_id = _docker_archive(
        tmp_path,
        [_tar_bytes([("run/config.env", f"TELEGRAM_BOT_TOKEN={TOKEN}".encode())])],
    )
    serialized = json.dumps(_scan(path, image_id).contract(), sort_keys=True)
    assert TOKEN not in serialized
    assert "config.env" not in serialized


def test_mutable_tag_cannot_satisfy_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("docker must not be invoked"),
    )
    with pytest.raises(scanner.ImageScanError, match="MUTABLE_IMAGE_REFERENCE_DENIED"):
        scanner._inspect_local_image("healbite-hermes:latest", REVISION)


def test_wrong_expected_revision_fails(tmp_path: Path) -> None:
    path, image_id = _docker_archive(tmp_path, [_tar_bytes([("safe", b"safe")])])
    with pytest.raises(scanner.ImageScanError, match="IMAGE_OCI_REVISION_MISMATCH"):
        _scan(path, image_id, revision="b" * 40)


def test_wrong_expected_image_identity_fails(tmp_path: Path) -> None:
    path, _image_id = _docker_archive(tmp_path, [_tar_bytes([("safe", b"safe")])])
    with pytest.raises(scanner.ImageScanError, match="IMAGE_IDENTITY_MISMATCH"):
        _scan(path, "sha256:" + "f" * 64)
