from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts import healbite_schema_migrate
from scripts import hermes_staged_schema_migrate as staged


class _CloseFailingProxy:
    def __init__(self, wrapped: Any, attempts: list[str], label: str) -> None:
        self._wrapped = wrapped
        self._attempts = attempts
        self._label = label

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def close(self) -> None:
        self._attempts.append(self._label)
        self._wrapped.close()
        raise OSError("sensitive-real-release-failure")


def _database(path: Path) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    return path


def _migrated_database(path: Path) -> Path:
    database = _database(path)
    result = healbite_schema_migrate.run_migration(
        db_path=str(database),
        staged_copy=True,
    )
    assert result.exit_code == 0
    return database


def _assert_cleanup_records(
    error: Exception,
    *,
    resources: list[str],
    codes: list[str],
) -> Exception:
    primary, records = staged._split_primary_cleanup(error)
    assert [record.resource_kind for record in records] == resources
    assert [record.error_code for record in records] == codes
    assert len(records) == len(resources)
    serialized = json.dumps(
        [record.as_payload() for record in records],
        ensure_ascii=True,
    )
    assert "sensitive" not in serialized
    assert "sensitive" not in str(error)
    return primary


def _patch_path_open_close(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    attempts: list[str],
) -> None:
    real_open = Path.open

    def open_with_failing_close(
        path: Path,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        handle = real_open(path, *args, **kwargs)
        if path == target:
            attempts.append("acquired")
            return _CloseFailingProxy(handle, attempts, "close")
        return handle

    monkeypatch.setattr(Path, "open", open_with_failing_close)


def _patch_os_open_close(
    monkeypatch: pytest.MonkeyPatch,
    predicate: Callable[[object, dict[str, Any]], bool],
    attempts: list[str],
) -> set[int]:
    real_open = os.open
    real_close = os.close
    owned: set[int] = set()

    def tracking_open(path: object, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if predicate(path, kwargs):
            owned.add(descriptor)
            attempts.append("acquired")
        return descriptor

    def close_with_failure(descriptor: int) -> None:
        if descriptor not in owned:
            real_close(descriptor)
            return
        owned.remove(descriptor)
        attempts.append("close")
        real_close(descriptor)
        raise OSError("sensitive-real-descriptor-release")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "close", close_with_failure)
    return owned


def _patch_sqlite_close(
    monkeypatch: pytest.MonkeyPatch,
    attempts: list[str],
) -> None:
    real_connect = sqlite3.connect

    def connect_with_failing_close(*args: Any, **kwargs: Any) -> Any:
        connection = real_connect(*args, **kwargs)
        attempts.append("acquired")
        return _CloseFailingProxy(connection, attempts, "close")

    monkeypatch.setattr(sqlite3, "connect", connect_with_failing_close)


def test_hash_source_file_handle_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _database(tmp_path / "source.sqlite")
    attempts: list[str] = []
    _patch_path_open_close(monkeypatch, source, attempts)

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._sha256(source)

    primary = _assert_cleanup_records(
        captured.value,
        resources=["HASH_SOURCE_FILE_HANDLE"],
        codes=["HASH_SOURCE_FILE_CLOSE_FAILED"],
    )
    assert isinstance(primary, staged.OrchestratorError)
    assert primary.code == "CLEANUP_FAILED"
    assert attempts == ["acquired", "close"]


def test_fsync_file_handle_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "durable.bin"
    target.write_bytes(b"synthetic")
    attempts: list[str] = []
    _patch_path_open_close(monkeypatch, target, attempts)

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._fsync_file(target)

    _assert_cleanup_records(
        captured.value,
        resources=["FSYNC_FILE_HANDLE"],
        codes=["FSYNC_FILE_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "close"]


def test_fsync_directory_descriptor_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []
    owned = _patch_os_open_close(
        monkeypatch,
        lambda path, kwargs: Path(path) == tmp_path
        and kwargs.get("dir_fd") is None,
        attempts,
    )

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._fsync_directory(tmp_path)

    _assert_cleanup_records(
        captured.value,
        resources=["FSYNC_DIRECTORY_DESCRIPTOR"],
        codes=["FSYNC_DIRECTORY_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "close"]
    assert owned == set()


def test_copy_destination_descriptor_real_release_failure_continues_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _database(tmp_path / "source.sqlite")
    destination = tmp_path / "copy.sqlite"
    source_fd = os.open(source, os.O_RDONLY)
    real_open = os.open
    real_close = os.close
    real_fdopen = os.fdopen
    real_unlink = staged._unlink_path_if_exists
    destination_fds: set[int] = set()
    attempts: list[str] = []

    def tracking_open(path: object, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if Path(path) == destination:
            destination_fds.add(descriptor)
            attempts.append("acquired")
        return descriptor

    def fail_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        if descriptor in destination_fds:
            attempts.append("fdopen")
            raise OSError("sensitive-primary-fdopen")
        return real_fdopen(descriptor, *args, **kwargs)

    def close_then_fail(descriptor: int) -> None:
        if descriptor not in destination_fds:
            real_close(descriptor)
            return
        destination_fds.remove(descriptor)
        attempts.append("descriptor_close")
        real_close(descriptor)
        raise OSError("sensitive-destination-close")

    def unlink_then_fail(path: Path) -> None:
        attempts.append("file_unlink")
        real_unlink(path)
        raise OSError("sensitive-destination-unlink")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    monkeypatch.setattr(os, "close", close_then_fail)
    monkeypatch.setattr(staged, "_unlink_path_if_exists", unlink_then_fail)
    try:
        with pytest.raises(staged._PrimaryAndCleanupError) as captured:
            staged._copy_fd_durable(
                source_fd,
                destination,
                uid=os.getuid(),
                gid=os.getgid(),
                phase_prefix="real-boundary",
            )
    finally:
        real_close(source_fd)

    primary = _assert_cleanup_records(
        captured.value,
        resources=["COPY_DESTINATION_DESCRIPTOR", "COPY_DESTINATION_FILE"],
        codes=[
            "COPY_DESTINATION_CLOSE_FAILED",
            "COPY_DESTINATION_UNLINK_FAILED",
        ],
    )
    assert isinstance(primary, OSError)
    assert attempts == [
        "acquired",
        "fdopen",
        "descriptor_close",
        "file_unlink",
    ]
    assert not destination.exists()
    assert destination_fds == set()


def test_copy_destination_file_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _database(tmp_path / "source.sqlite")
    destination = tmp_path / "copy.sqlite"
    source_fd = os.open(source, os.O_RDONLY)
    real_open = os.open
    real_close = os.close
    real_fdopen = os.fdopen
    real_unlink = staged._unlink_path_if_exists
    destination_fds: set[int] = set()
    attempts: list[str] = []

    def tracking_open(path: object, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if Path(path) == destination:
            destination_fds.add(descriptor)
            attempts.append("acquired")
        return descriptor

    def fail_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        if descriptor in destination_fds:
            attempts.append("fdopen")
            raise OSError("sensitive-primary-fdopen")
        return real_fdopen(descriptor, *args, **kwargs)

    def tracking_close(descriptor: int) -> None:
        if descriptor in destination_fds:
            destination_fds.remove(descriptor)
            attempts.append("descriptor_close")
        real_close(descriptor)

    def unlink_then_fail(path: Path) -> None:
        attempts.append("file_unlink")
        real_unlink(path)
        raise OSError("sensitive-destination-unlink")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(staged, "_unlink_path_if_exists", unlink_then_fail)
    try:
        with pytest.raises(staged._PrimaryAndCleanupError) as captured:
            staged._copy_fd_durable(
                source_fd,
                destination,
                uid=os.getuid(),
                gid=os.getgid(),
                phase_prefix="real-boundary",
            )
    finally:
        real_close(source_fd)

    _assert_cleanup_records(
        captured.value,
        resources=["COPY_DESTINATION_FILE"],
        codes=["COPY_DESTINATION_UNLINK_FAILED"],
    )
    assert attempts == [
        "acquired",
        "fdopen",
        "descriptor_close",
        "file_unlink",
    ]
    assert not destination.exists()


def test_copy_destination_file_handle_real_release_failure_continues_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _database(tmp_path / "source.sqlite")
    destination = tmp_path / "copy.sqlite"
    source_fd = os.open(source, os.O_RDONLY)
    real_open = os.open
    real_close = os.close
    real_fdopen = os.fdopen
    destination_fds: set[int] = set()
    attempts: list[str] = []

    def tracking_open(path: object, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if Path(path) == destination:
            destination_fds.add(descriptor)
            attempts.append("acquired")
        return descriptor

    def wrap_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        handle = real_fdopen(descriptor, *args, **kwargs)
        if descriptor in destination_fds:
            destination_fds.remove(descriptor)
            attempts.append("fdopen")
            return _CloseFailingProxy(handle, attempts, "handle_close")
        return handle

    real_unlink = staged._unlink_path_if_exists

    def tracking_unlink(path: Path) -> None:
        attempts.append("file_unlink")
        real_unlink(path)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", wrap_fdopen)
    monkeypatch.setattr(staged, "_unlink_path_if_exists", tracking_unlink)
    try:
        with pytest.raises(staged._CleanupAggregateError) as captured:
            staged._copy_fd_durable(
                source_fd,
                destination,
                uid=os.getuid(),
                gid=os.getgid(),
                phase_prefix="real-boundary",
            )
    finally:
        real_close(source_fd)

    _assert_cleanup_records(
        captured.value,
        resources=["COPY_DESTINATION_FILE_HANDLE"],
        codes=["COPY_DESTINATION_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "fdopen", "handle_close", "file_unlink"]
    assert not destination.exists()


def test_copy_source_descriptor_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _database(tmp_path / "source.sqlite")
    destination = tmp_path / "copy.sqlite"
    attempts: list[str] = []
    owned = _patch_os_open_close(
        monkeypatch,
        lambda path, _kwargs: Path(path) == source,
        attempts,
    )

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._copy_durable(
            source,
            destination,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    _assert_cleanup_records(
        captured.value,
        resources=["COPY_SOURCE_DESCRIPTOR"],
        codes=["COPY_SOURCE_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "close"]
    assert owned == set()
    assert destination.exists()


def _manifest_parent_fd(path: Path) -> int:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))


def test_manifest_temp_file_descriptor_real_release_failure_continues_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "manifest"
    parent_fd = _manifest_parent_fd(parent)
    real_open = os.open
    real_close = os.close
    real_fdopen = os.fdopen
    real_unlink = staged._unlink_at_if_exists
    owned: set[int] = set()
    attempts: list[str] = []

    def tracking_open(path: object, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if kwargs.get("dir_fd") == parent_fd:
            owned.add(descriptor)
            attempts.append("acquired")
        return descriptor

    def fail_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        if descriptor in owned:
            attempts.append("fdopen")
            raise OSError("sensitive-primary-fdopen")
        return real_fdopen(descriptor, *args, **kwargs)

    def close_then_fail(descriptor: int) -> None:
        if descriptor not in owned:
            real_close(descriptor)
            return
        owned.remove(descriptor)
        attempts.append("descriptor_close")
        real_close(descriptor)
        raise OSError("sensitive-manifest-close")

    def unlink_then_fail(descriptor: int, name: str) -> None:
        attempts.append("file_unlink")
        real_unlink(descriptor, name)
        raise OSError("sensitive-manifest-unlink")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    monkeypatch.setattr(os, "close", close_then_fail)
    monkeypatch.setattr(staged, "_unlink_at_if_exists", unlink_then_fail)
    try:
        with pytest.raises(staged._PrimaryAndCleanupError) as captured:
            staged._write_json_durable_at(parent_fd, "manifest.json", {"ok": True})
    finally:
        real_close(parent_fd)

    _assert_cleanup_records(
        captured.value,
        resources=["MANIFEST_TEMP_FILE_DESCRIPTOR", "MANIFEST_TEMP_FILE"],
        codes=[
            "MANIFEST_TEMP_FILE_CLOSE_FAILED",
            "MANIFEST_TEMP_FILE_UNLINK_FAILED",
        ],
    )
    assert attempts == [
        "acquired",
        "fdopen",
        "descriptor_close",
        "file_unlink",
    ]
    assert list(parent.iterdir()) == []
    assert owned == set()


def test_manifest_temp_file_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "manifest"
    parent_fd = _manifest_parent_fd(parent)
    real_open = os.open
    real_close = os.close
    real_fdopen = os.fdopen
    real_unlink = staged._unlink_at_if_exists
    owned: set[int] = set()
    attempts: list[str] = []

    def tracking_open(path: object, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if kwargs.get("dir_fd") == parent_fd:
            owned.add(descriptor)
            attempts.append("acquired")
        return descriptor

    def fail_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        if descriptor in owned:
            attempts.append("fdopen")
            raise OSError("sensitive-primary-fdopen")
        return real_fdopen(descriptor, *args, **kwargs)

    def tracking_close(descriptor: int) -> None:
        if descriptor in owned:
            owned.remove(descriptor)
            attempts.append("descriptor_close")
        real_close(descriptor)

    def unlink_then_fail(descriptor: int, name: str) -> None:
        attempts.append("file_unlink")
        real_unlink(descriptor, name)
        raise OSError("sensitive-manifest-unlink")

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    monkeypatch.setattr(os, "close", tracking_close)
    monkeypatch.setattr(staged, "_unlink_at_if_exists", unlink_then_fail)
    try:
        with pytest.raises(staged._PrimaryAndCleanupError) as captured:
            staged._write_json_durable_at(parent_fd, "manifest.json", {"ok": True})
    finally:
        real_close(parent_fd)

    _assert_cleanup_records(
        captured.value,
        resources=["MANIFEST_TEMP_FILE"],
        codes=["MANIFEST_TEMP_FILE_UNLINK_FAILED"],
    )
    assert attempts == [
        "acquired",
        "fdopen",
        "descriptor_close",
        "file_unlink",
    ]
    assert list(parent.iterdir()) == []


def test_manifest_temp_file_handle_real_release_failure_continues_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "manifest"
    parent_fd = _manifest_parent_fd(parent)
    real_open = os.open
    real_close = os.close
    real_fdopen = os.fdopen
    real_unlink = staged._unlink_at_if_exists
    owned: set[int] = set()
    attempts: list[str] = []

    def tracking_open(path: object, *args: Any, **kwargs: Any) -> int:
        descriptor = real_open(path, *args, **kwargs)
        if kwargs.get("dir_fd") == parent_fd:
            owned.add(descriptor)
            attempts.append("acquired")
        return descriptor

    def wrap_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        handle = real_fdopen(descriptor, *args, **kwargs)
        if descriptor in owned:
            owned.remove(descriptor)
            attempts.append("fdopen")
            return _CloseFailingProxy(handle, attempts, "handle_close")
        return handle

    def tracking_unlink(descriptor: int, name: str) -> None:
        attempts.append("file_unlink")
        real_unlink(descriptor, name)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "fdopen", wrap_fdopen)
    monkeypatch.setattr(staged, "_unlink_at_if_exists", tracking_unlink)
    try:
        with pytest.raises(staged._CleanupAggregateError) as captured:
            staged._write_json_durable_at(parent_fd, "manifest.json", {"ok": True})
    finally:
        real_close(parent_fd)

    _assert_cleanup_records(
        captured.value,
        resources=["MANIFEST_TEMP_FILE_HANDLE"],
        codes=["MANIFEST_TEMP_FILE_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "fdopen", "handle_close", "file_unlink"]
    assert list(parent.iterdir()) == []


def test_recovery_manifest_file_handle_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"PUBLISH_STATE": "EXCHANGE_COMPLETED_NOT_VERIFIED"}),
        encoding="ascii",
    )
    staging_root = tmp_path / "staging"
    staging_root.mkdir(mode=0o700)
    attempts: list[str] = []
    _patch_path_open_close(monkeypatch, manifest, attempts)

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._recover_pre_publish_staging(manifest, staging_root)

    _assert_cleanup_records(
        captured.value,
        resources=["RECOVERY_MANIFEST_FILE_HANDLE"],
        codes=["RECOVERY_MANIFEST_FILE_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "close"]


def test_sqlite_snapshot_connection_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _database(tmp_path / "source.sqlite")
    attempts: list[str] = []
    _patch_sqlite_close(monkeypatch, attempts)

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._database_snapshot(source)

    _assert_cleanup_records(
        captured.value,
        resources=["SQLITE_SNAPSHOT_CONNECTION"],
        codes=["SQLITE_SNAPSHOT_CONNECTION_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "close"]


def test_sqlite_validation_connection_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _database(tmp_path / "source.sqlite")
    attempts: list[str] = []
    _patch_sqlite_close(monkeypatch, attempts)

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._sqlite_validation(source)

    _assert_cleanup_records(
        captured.value,
        resources=["SQLITE_VALIDATION_CONNECTION"],
        codes=["SQLITE_VALIDATION_CONNECTION_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "close"]


def test_target_fingerprint_connection_real_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _migrated_database(tmp_path / "source.sqlite")
    attempts: list[str] = []
    _patch_sqlite_close(monkeypatch, attempts)

    with pytest.raises(staged._CleanupAggregateError) as captured:
        staged._target_schema_fingerprint(source)

    _assert_cleanup_records(
        captured.value,
        resources=["TARGET_FINGERPRINT_CONNECTION"],
        codes=["TARGET_FINGERPRINT_CONNECTION_CLOSE_FAILED"],
    )
    assert attempts == ["acquired", "close"]
