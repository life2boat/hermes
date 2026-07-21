from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts import install_pinned_playwright_artifact as installer
from scripts import playwright_installed_closure as installed
from scripts.playwright_artifact_contract import VerifiedPlaywrightClosure
from tests.playwright_supply_chain_support import (
    closure_manifest_document,
    write_artifact_archive,
    write_closure_archives,
    write_closure_context,
    write_manifest,
    verified_closure,
)


@dataclass
class ClosureFixture:
    verified: VerifiedPlaywrightClosure
    context: Path
    artifacts_root: Path
    manifest: Path
    document: dict[str, object]
    manifest_sha256: str
    cache_root: Path


def _fixture(tmp_path: Path) -> ClosureFixture:
    cache_parent = tmp_path / "cache-parent"
    cache_parent.mkdir(mode=0o700)
    cache_root = cache_parent / "ms-playwright"
    verified, _lockfile, wheel = verified_closure(
        tmp_path,
        cache_root=cache_root,
    )
    archives = write_closure_archives(tmp_path / "archives", verified)
    document = closure_manifest_document(verified, archives)
    context = tmp_path / "artifact-context"
    manifest_sha256 = write_closure_context(
        context,
        verified,
        wheel,
        archives,
        document=document,
    )
    return ClosureFixture(
        verified=verified,
        context=context,
        artifacts_root=context / "artifacts",
        manifest=context / "closure.json",
        document=document,
        manifest_sha256=manifest_sha256,
        cache_root=cache_root,
    )


def _artifact_item(env: ClosureFixture, name: str) -> dict[str, object]:
    artifacts = env.document["artifacts"]
    assert isinstance(artifacts, list)
    return next(
        item
        for item in artifacts
        if isinstance(item, dict) and item["artifact_name"] == name
    )


def _rewrite_manifest(env: ClosureFixture) -> str:
    env.manifest_sha256 = write_manifest(env.manifest, env.document)
    return env.manifest_sha256


def _refresh_archive_identity(env: ClosureFixture, name: str) -> str:
    archive = env.artifacts_root / name / "archive"
    item = _artifact_item(env, name)
    item["archive_size"] = archive.stat().st_size
    item["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    return _rewrite_manifest(env)


def _install(env: ClosureFixture) -> Path:
    return installer.install_closure(
        manifest_path=env.manifest,
        artifacts_root=env.artifacts_root,
        expected_manifest_sha256=env.manifest_sha256,
        verified_closure=env.verified,
    )


def _assert_no_published_cache(env: ClosureFixture) -> None:
    assert not env.cache_root.exists()
    assert not env.cache_root.is_symlink()
    identity = installed.expected_identity_path(env.cache_root)
    assert not identity.exists()
    assert not identity.is_symlink()


def _write_zip(
    path: Path,
    entries: list[tuple[str, bytes, int, int]],
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data, mode, file_type in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (file_type | mode) << 16
            archive.writestr(info, data)



def test_owner_identity_unavailable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    monkeypatch.setattr(installer.os, "geteuid", None)
    with pytest.raises(
        installer.ArtifactContractError,
        match="OWNER_IDENTITY_UNSUPPORTED",
    ):
        _install(env)
    _assert_no_published_cache(env)


def test_valid_complete_closure_is_published_once_and_revalidated(
    tmp_path: Path,
) -> None:
    env = _fixture(tmp_path)
    result = _install(env)
    assert result == env.cache_root
    assert {child.name for child in result.iterdir()} == {
        "INSTALLATION_COMPLETE",
        *(artifact.cache_directory for artifact in env.verified.closure.artifacts),
    }
    for artifact in env.verified.closure.artifacts:
        executable = (
            result
            / artifact.cache_directory
            / artifact.expected_executable_relative_path
        )
        assert executable.is_file()
        assert executable.stat().st_mode & 0o111
    assert stat.S_IMODE(result.stat().st_mode) == 0o555
    identity = installed.expected_identity_path(result)
    assert stat.S_IMODE(identity.stat().st_mode) == 0o444
    marker = result / installed.INSTALLATION_MARKER
    assert json.loads(marker.read_text(encoding="ascii"))["marker_version"] == 2
    assert _install(env) == result


def test_missing_expected_closure_manifest_sha_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    with pytest.raises(
        installer.ArtifactContractError,
        match="EXPECTED_CLOSURE_MANIFEST_SHA256_INVALID",
    ):
        installer.install_closure(
            manifest_path=env.manifest,
            artifacts_root=env.artifacts_root,
            expected_manifest_sha256="",
            verified_closure=env.verified,
        )
    _assert_no_published_cache(env)


def test_closure_manifest_sha_mismatch_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    env.manifest_sha256 = "a" * 64
    with pytest.raises(
        installer.ArtifactContractError,
        match="CLOSURE_MANIFEST_SHA256_MISMATCH",
    ):
        _install(env)
    _assert_no_published_cache(env)


def test_legacy_single_artifact_manifest_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    env.manifest.write_text('{"manifest_version":1}\n', encoding="ascii")
    env.manifest_sha256 = hashlib.sha256(env.manifest.read_bytes()).hexdigest()
    with pytest.raises(installer.ArtifactContractError, match="MANIFEST_FIELDS_INVALID"):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize("missing_name", ["chromium-headless-shell", "ffmpeg"])
def test_missing_required_manifest_artifact_is_denied(
    tmp_path: Path, missing_name: str
) -> None:
    env = _fixture(tmp_path)
    artifacts = env.document["artifacts"]
    assert isinstance(artifacts, list)
    env.document["artifacts"] = [
        item for item in artifacts if item["artifact_name"] != missing_name
    ]
    env.document["artifact_count"] = 1
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match="ARTIFACT_SET_MISMATCH"):
        _install(env)
    _assert_no_published_cache(env)


def test_unexpected_third_manifest_artifact_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    extra = dict(_artifact_item(env, "ffmpeg"))
    extra.update(
        artifact_name="webkit",
        browser_family="webkit",
        revision="9999",
        archive_filename="webkit.zip",
    )
    artifacts = env.document["artifacts"]
    assert isinstance(artifacts, list)
    artifacts.append(extra)
    artifacts.sort(key=lambda item: item["artifact_name"])
    env.document["artifact_count"] = 3
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match="ARTIFACT_SET_MISMATCH"):
        _install(env)
    _assert_no_published_cache(env)


def test_duplicate_manifest_artifact_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    artifacts = env.document["artifacts"]
    assert isinstance(artifacts, list)
    artifacts.insert(1, dict(artifacts[0]))
    env.document["artifact_count"] = 3
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match="DUPLICATE_ARTIFACT_NAME"):
        _install(env)
    _assert_no_published_cache(env)


def test_duplicate_revision_policy_is_fail_closed(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    _artifact_item(env, "ffmpeg")["revision"] = "1228"
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match="DUPLICATE_ARTIFACT_REVISION"):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("artifact_count", 1, "ARTIFACT_COUNT_INVALID"),
        ("playwright_package_version", "1.60.0", "CLOSURE_IDENTITY_MISMATCH"),
        ("platform", "linux/arm64", "CLOSURE_IDENTITY_MISMATCH"),
    ],
)
def test_wrong_closure_identity_is_denied(
    tmp_path: Path, field: str, value: object, error: str
) -> None:
    env = _fixture(tmp_path)
    env.document[field] = value
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match=error):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize(
    ("name", "field", "value"),
    [
        ("chromium-headless-shell", "revision", "9999"),
        ("ffmpeg", "revision", "9998"),
        ("ffmpeg", "platform", "linux/arm64"),
        ("ffmpeg", "layout_kind", "DIRECTORY_TREE"),
    ],
)
def test_wrong_artifact_identity_is_denied(
    tmp_path: Path, name: str, field: str, value: object
) -> None:
    env = _fixture(tmp_path)
    _artifact_item(env, name)[field] = value
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match="ARTIFACT_IDENTITY_MISMATCH"):
        _install(env)
    _assert_no_published_cache(env)


def test_unknown_manifest_field_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    env.document["optional_artifact"] = True
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match="MANIFEST_FIELDS_INVALID"):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize("missing_name", ["chromium-headless-shell", "ffmpeg"])
def test_missing_required_context_archive_is_denied(
    tmp_path: Path, missing_name: str
) -> None:
    env = _fixture(tmp_path)
    shutil.rmtree(env.artifacts_root / missing_name)
    with pytest.raises(installer.ArtifactContractError, match="ARTIFACT_CONTEXT_SET_MISMATCH"):
        _install(env)
    _assert_no_published_cache(env)


def test_unreferenced_context_file_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    (env.artifacts_root / "ffmpeg" / "extra").write_bytes(b"extra")
    with pytest.raises(installer.ArtifactContractError, match="ARTIFACT_CONTEXT_ENTRY_INVALID"):
        _install(env)
    _assert_no_published_cache(env)


def test_chromium_single_file_layout_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    archive = env.artifacts_root / "chromium-headless-shell" / "archive"
    _write_zip(
        archive,
        [("chrome-headless-shell", b"synthetic", 0o755, stat.S_IFREG)],
    )
    _refresh_archive_identity(env, "chromium-headless-shell")
    with pytest.raises(installer.ArtifactContractError, match="EXPECTED_EXECUTABLE_MISSING"):
        _install(env)
    _assert_no_published_cache(env)


def test_ffmpeg_directory_layout_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    artifact = env.verified.closure.artifact("ffmpeg")
    archive = env.artifacts_root / "ffmpeg" / "archive"
    write_artifact_archive(
        archive,
        artifact,
        force_layout="DIRECTORY_TREE",
    )
    _refresh_archive_identity(env, "ffmpeg")
    with pytest.raises(installer.ArtifactContractError, match="ARCHIVE_PATH_CONFLICT"):
        _install(env)
    _assert_no_published_cache(env)


def test_ffmpeg_second_top_level_entry_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    artifact = env.verified.closure.artifact("ffmpeg")
    archive = env.artifacts_root / "ffmpeg" / "archive"
    write_artifact_archive(
        archive,
        artifact,
        extra_entries=[("second", b"extra", 0o644, stat.S_IFREG)],
    )
    _refresh_archive_identity(env, "ffmpeg")
    with pytest.raises(installer.ArtifactContractError, match="SINGLE_EXECUTABLE_LAYOUT_INVALID"):
        _install(env)
    _assert_no_published_cache(env)


def test_ffmpeg_symlink_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    archive = env.artifacts_root / "ffmpeg" / "archive"
    _write_zip(
        archive,
        [("ffmpeg-linux", b"target", 0o777, stat.S_IFLNK)],
    )
    _refresh_archive_identity(env, "ffmpeg")
    with pytest.raises(installer.ArtifactContractError, match="ARCHIVE_SYMLINK_DENIED"):
        _install(env)
    _assert_no_published_cache(env)


def test_chromium_sibling_top_level_file_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    artifact = env.verified.closure.artifact("chromium-headless-shell")
    archive = env.artifacts_root / artifact.artifact_name / "archive"
    write_artifact_archive(
        archive,
        artifact,
        extra_entries=[("sibling", b"extra", 0o644, stat.S_IFREG)],
    )
    _refresh_archive_identity(env, artifact.artifact_name)
    with pytest.raises(installer.ArtifactContractError, match="ARCHIVE_UNEXPECTED_TOP_LEVEL"):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize(
    "unsafe_name",
    ["../escape", "/absolute", "C:/drive", "root/../escape"],
)
def test_unsafe_archive_paths_are_denied(
    tmp_path: Path, unsafe_name: str
) -> None:
    env = _fixture(tmp_path)
    artifact = env.verified.closure.artifact("chromium-headless-shell")
    archive = env.artifacts_root / artifact.artifact_name / "archive"
    write_artifact_archive(
        archive,
        artifact,
        extra_entries=[(unsafe_name, b"bad", 0o644, stat.S_IFREG)],
    )
    _refresh_archive_identity(env, artifact.artifact_name)
    with pytest.raises(installer.ArtifactContractError, match="ARCHIVE_"):
        _install(env)
    _assert_no_published_cache(env)


def test_case_collision_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    artifact = env.verified.closure.artifact("chromium-headless-shell")
    archive = env.artifacts_root / artifact.artifact_name / "archive"
    write_artifact_archive(
        archive,
        artifact,
        extra_entries=[
            (f"{artifact.archive_root}/RESOURCES.PAK", b"collision", 0o644, stat.S_IFREG)
        ],
    )
    _refresh_archive_identity(env, artifact.artifact_name)
    with pytest.raises(installer.ArtifactContractError, match="ARCHIVE_CASE_COLLISION"):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize("name", ["chromium-headless-shell", "ffmpeg"])
def test_non_executable_required_artifact_is_denied(
    tmp_path: Path, name: str
) -> None:
    env = _fixture(tmp_path)
    artifact = env.verified.closure.artifact(name)
    archive = env.artifacts_root / name / "archive"
    write_artifact_archive(archive, artifact, executable=False)
    _refresh_archive_identity(env, name)
    with pytest.raises(installer.ArtifactContractError, match="EXPECTED_EXECUTABLE_INVALID"):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize("field", ["archive_size", "archive_sha256"])
def test_archive_identity_mismatch_is_denied(tmp_path: Path, field: str) -> None:
    env = _fixture(tmp_path)
    item = _artifact_item(env, "ffmpeg")
    item[field] = item[field] + 1 if field == "archive_size" else "a" * 64
    _rewrite_manifest(env)
    with pytest.raises(installer.ArtifactContractError, match="ARCHIVE_(SIZE|SHA256)_MISMATCH"):
        _install(env)
    _assert_no_published_cache(env)


def test_all_archives_validate_before_first_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    artifact = env.verified.closure.artifact("ffmpeg")
    archive = env.artifacts_root / "ffmpeg" / "archive"
    write_artifact_archive(archive, artifact, executable=False)
    _refresh_archive_identity(env, "ffmpeg")
    extraction_calls = 0

    def unexpected_extract(*_args: object, **_kwargs: object) -> None:
        nonlocal extraction_calls
        extraction_calls += 1

    monkeypatch.setattr(installer, "_extract_archive", unexpected_extract)
    with pytest.raises(installer.ArtifactContractError, match="EXPECTED_EXECUTABLE_INVALID"):
        _install(env)
    assert extraction_calls == 0
    _assert_no_published_cache(env)


def test_second_extraction_failure_leaves_no_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original = installer._extract_archive
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise installer.ArtifactContractError("SECOND_EXTRACTION_FAILED")
        original(*args, **kwargs)

    monkeypatch.setattr(installer, "_extract_archive", fail_second)
    with pytest.raises(installer.ArtifactContractError, match="SECOND_EXTRACTION_FAILED"):
        _install(env)
    assert calls == 2
    _assert_no_published_cache(env)


def test_file_fsync_failure_leaves_no_published_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    monkeypatch.setattr(
        installer,
        "_fsync_regular_file",
        lambda _path: (_ for _ in ()).throw(
            installer.ArtifactContractError("FILE_FSYNC_FAILED")
        ),
    )
    with pytest.raises(installer.ArtifactContractError, match="FILE_FSYNC_FAILED"):
        _install(env)
    _assert_no_published_cache(env)


def test_directory_fsync_failure_leaves_no_published_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original = installer._fsync_directory

    def fail_staging(path: Path, code: str = "DIRECTORY_FSYNC_FAILED") -> None:
        if path.name.startswith(".playwright-closure."):
            raise installer.ArtifactContractError(code)
        original(path, code)

    monkeypatch.setattr(installer, "_fsync_directory", fail_staging)
    with pytest.raises(installer.ArtifactContractError, match="DIRECTORY_FSYNC_FAILED"):
        _install(env)
    _assert_no_published_cache(env)


def test_atomic_rename_failure_leaves_no_published_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original = installer.os.replace

    def fail_cache_publish(source: object, destination: object) -> None:
        if Path(destination) == env.cache_root:
            raise OSError("synthetic")
        original(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_cache_publish)
    with pytest.raises(installer.ArtifactContractError, match="ATOMIC_PUBLISH_FAILED"):
        _install(env)
    _assert_no_published_cache(env)


def test_final_parent_fsync_failure_discards_complete_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original = installer._fsync_directory

    def fail_final(path: Path, code: str = "DIRECTORY_FSYNC_FAILED") -> None:
        if code == "FINAL_PARENT_FSYNC_FAILED":
            raise installer.ArtifactContractError(code)
        original(path, code)

    monkeypatch.setattr(installer, "_fsync_directory", fail_final)
    with pytest.raises(installer.ArtifactContractError, match="FINAL_PARENT_FSYNC_FAILED"):
        _install(env)
    _assert_no_published_cache(env)


@pytest.mark.parametrize("name", ["chromium-headless-shell", "ffmpeg"])
def test_post_publish_artifact_mismatch_discards_complete_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    env = _fixture(tmp_path)
    original = installer._revalidate_complete_cache

    def tamper_then_validate(
        cache_root: Path,
        independent_identity: Path,
        closure: object,
        *,
        owner_uid: int,
        owner_gid: int,
    ) -> None:
        artifact = env.verified.closure.artifact(name)
        executable = (
            cache_root
            / artifact.cache_directory
            / artifact.expected_executable_relative_path
        )
        executable.chmod(0o755)
        executable.write_bytes(b"post-publish tamper")
        executable.chmod(0o555)
        original(
            cache_root,
            independent_identity,
            closure,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    monkeypatch.setattr(installer, "_revalidate_complete_cache", tamper_then_validate)
    with pytest.raises(
        installer.ArtifactContractError,
        match="INSTALLED_CLOSURE_DIGEST_MISMATCH",
    ):
        _install(env)
    _assert_no_published_cache(env)




def test_quarantine_rename_failure_uses_direct_safe_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original_revalidate = installer._revalidate_complete_cache
    original_replace = installer.os.replace

    def tamper_then_validate(
        cache_root: Path,
        independent_identity: Path,
        closure: object,
        *,
        owner_uid: int,
        owner_gid: int,
    ) -> None:
        artifact = env.verified.closure.artifact("ffmpeg")
        executable = (
            cache_root
            / artifact.cache_directory
            / artifact.expected_executable_relative_path
        )
        executable.chmod(0o755)
        executable.write_bytes(b"tampered")
        executable.chmod(0o555)
        original_revalidate(
            cache_root,
            independent_identity,
            closure,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    def fail_quarantine_rename(source: object, destination: object) -> None:
        if Path(destination).parent.name.startswith(".failed-publish."):
            raise OSError("synthetic quarantine failure")
        original_replace(source, destination)

    monkeypatch.setattr(installer, "_revalidate_complete_cache", tamper_then_validate)
    monkeypatch.setattr(installer.os, "replace", fail_quarantine_rename)
    with pytest.raises(
        installer.ArtifactContractError,
        match="INSTALLED_CLOSURE_DIGEST_MISMATCH",
    ):
        _install(env)
    _assert_no_published_cache(env)



def test_marker_write_failure_leaves_no_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original = installer._write_identity_marker

    def fail_marker(path: Path, data: bytes) -> None:
        if path.name == installed.INSTALLATION_MARKER:
            raise installer.ArtifactContractError("MARKER_WRITE_FAILURE")
        original(path, data)

    monkeypatch.setattr(installer, "_write_identity_marker", fail_marker)
    with pytest.raises(installer.ArtifactContractError, match="MARKER_WRITE_FAILURE"):
        _install(env)
    _assert_no_published_cache(env)


def test_marker_fsync_failure_leaves_no_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original = installer._fsync_regular_file

    def fail_marker_fsync(path: Path) -> None:
        if path.name == installed.INSTALLATION_MARKER:
            raise installer.ArtifactContractError("MARKER_FSYNC_FAILURE")
        original(path)

    monkeypatch.setattr(installer, "_fsync_regular_file", fail_marker_fsync)
    with pytest.raises(installer.ArtifactContractError, match="MARKER_FSYNC_FAILURE"):
        _install(env)
    _assert_no_published_cache(env)


def test_tree_digest_failure_leaves_no_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    monkeypatch.setattr(
        installer,
        "build_installed_marker_document",
        lambda **_kwargs: (_ for _ in ()).throw(
            installed.InstalledClosureError("TREE_DIGEST_FAILURE")
        ),
    )
    with pytest.raises(installer.ArtifactContractError, match="TREE_DIGEST_FAILURE"):
        _install(env)
    _assert_no_published_cache(env)


def test_independent_identity_read_failure_reverses_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    original = installer._revalidate_complete_cache

    def deny_identity_read(
        cache_root: Path,
        independent_identity: Path,
        closure: object,
        *,
        owner_uid: int,
        owner_gid: int,
    ) -> None:
        independent_identity.chmod(0o000)
        original(
            cache_root,
            independent_identity,
            closure,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )

    monkeypatch.setattr(installer, "_revalidate_complete_cache", deny_identity_read)
    with pytest.raises(
        installer.ArtifactContractError,
        match="EXPECTED_IDENTITY_FILE_INVALID",
    ):
        _install(env)
    _assert_no_published_cache(env)



def test_cleanup_failure_does_not_mask_primary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _fixture(tmp_path)
    monkeypatch.setattr(
        installer,
        "_extract_archive",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            installer.ArtifactContractError("PRIMARY_FAILURE")
        ),
    )
    monkeypatch.setattr(
        installer.shutil,
        "rmtree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup")),
    )
    with pytest.raises(installer.ArtifactContractError, match="PRIMARY_FAILURE"):
        _install(env)
    _assert_no_published_cache(env)


def test_preexisting_partial_cache_is_preserved_and_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    env.cache_root.mkdir()
    sentinel = env.cache_root / "sentinel"
    sentinel.write_text("preserve", encoding="utf-8")
    with pytest.raises(
        installer.ArtifactContractError,
        match="EXISTING_CACHE_INCOMPLETE_OR_MISMATCH",
    ):
        _install(env)
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_preexisting_mixed_revision_cache_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    _install(env)
    marker = env.cache_root / "INSTALLATION_COMPLETE"
    document = json.loads(marker.read_text(encoding="ascii"))
    document["artifacts"][1]["revision"] = "9999"
    marker.chmod(0o644)
    marker.write_bytes(installer.canonical_json(document))
    marker.chmod(0o444)
    with pytest.raises(
        installer.ArtifactContractError,
        match="EXISTING_CACHE_INCOMPLETE_OR_MISMATCH",
    ):
        _install(env)


def test_preexisting_extra_cache_entry_is_denied(tmp_path: Path) -> None:
    env = _fixture(tmp_path)
    _install(env)
    env.cache_root.chmod(0o755)
    (env.cache_root / "unapproved-9999").mkdir()
    env.cache_root.chmod(0o555)
    with pytest.raises(
        installer.ArtifactContractError,
        match="EXISTING_CACHE_INCOMPLETE_OR_MISMATCH",
    ):
        _install(env)


def test_cli_error_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _fixture(tmp_path)
    monkeypatch.setattr(
        installer,
        "load_verified_wheel_closure",
        lambda **_kwargs: env.verified,
    )
    secret = "SENSITIVE_ARCHIVE_DETAIL"
    env.manifest.write_text(secret, encoding="utf-8")
    rc = installer.main(
        [
            "--closure-manifest",
            str(env.manifest),
            "--artifacts-root",
            str(env.artifacts_root),
            "--lockfile",
            str(tmp_path / "uv.lock"),
            "--wheel",
            str(tmp_path / "wheel"),
            "--expected-closure-manifest-sha256",
            hashlib.sha256(secret.encode()).hexdigest(),
            "--platform",
            "linux/amd64",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 2
    assert "ERROR_CLASS=" in captured.err
    assert secret not in captured.err


def test_installer_has_no_network_client_or_public_skip_path() -> None:
    source = Path(installer.__file__).read_text(encoding="utf-8")
    assert "requests" not in source
    assert "urllib" not in source
    assert "subprocess" not in source
    assert "--skip" not in source
    assert "legacy" not in source.lower()
