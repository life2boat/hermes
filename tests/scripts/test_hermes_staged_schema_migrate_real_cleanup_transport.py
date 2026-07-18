from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts import healbite_schema_migrate
from scripts import hermes_staged_schema_migrate as staged


class _CloseFailingProxy:
    def __init__(self, wrapped: Any, attempts: list[str]) -> None:
        self._wrapped = wrapped
        self._attempts = attempts

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)

    def close(self) -> None:
        self._attempts.append("manifest_handle_close")
        self._wrapped.close()
        raise OSError("sensitive-real-manifest-release")


def _database(path: Path) -> Path:
    path.parent.mkdir(mode=0o700, parents=True)
    path.touch(mode=0o600)
    os.chmod(path, 0o600)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    return path


def _args(root: Path, source: Path) -> argparse.Namespace:
    backup = root / "backups"
    staging = root / "staging"
    backup.mkdir(mode=0o700)
    staging.mkdir(mode=0o700)
    return argparse.Namespace(
        source_db=str(source),
        backup_dir=str(backup),
        staging_root=str(staging),
        target_image_id="sha256:" + "2" * 64,
        previous_image_id="sha256:" + "3" * 64,
        expected_source_revision="1" * 40,
        synthetic_root=str(root),
    )


def _host_migration(_contract: staged.Contract, staging_dir: Path) -> None:
    result = healbite_schema_migrate.run_migration(
        db_path=str(staging_dir / "database.sqlite"),
        staged_copy=True,
    )
    assert result.exit_code == 0


def test_post_exchange_primary_preserves_real_manifest_handle_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _database(tmp_path / "source" / "database.sqlite")
    args = _args(tmp_path, source)
    uid_getter = getattr(os, "geteuid", None)
    gid_getter = getattr(os, "getegid", None)
    assert callable(uid_getter) and callable(gid_getter)
    monkeypatch.setattr(staged, "RUNTIME_UID", int(uid_getter()))
    monkeypatch.setattr(staged, "RUNTIME_GID", int(gid_getter()))
    monkeypatch.setattr(
        staged,
        "_inspect_image",
        lambda *_args, **_kwargs: "1" * 40,
    )

    state = {
        "post_exchange_primary": False,
        "manifest_handle_wrapped": False,
    }
    attempts: list[str] = []
    real_fdopen = os.fdopen

    def wrap_failure_manifest_handle(
        descriptor: int,
        *fdopen_args: Any,
        **fdopen_kwargs: Any,
    ) -> Any:
        handle = real_fdopen(descriptor, *fdopen_args, **fdopen_kwargs)
        if (
            state["post_exchange_primary"]
            and not state["manifest_handle_wrapped"]
        ):
            state["manifest_handle_wrapped"] = True
            attempts.append("manifest_handle_acquired")
            return _CloseFailingProxy(handle, attempts)
        return handle

    def inject_primary(phase: str, publish_state: str) -> None:
        if phase == "publish_exchange":
            state["post_exchange_primary"] = True
            raise staged.OrchestratorError(
                "PRIMARY_POST_EXCHANGE_FAILURE",
                publish_state=publish_state,
            )

    monkeypatch.setattr(os, "fdopen", wrap_failure_manifest_handle)

    assert staged.execute_synthetic(
        args,
        _failure_callback=inject_primary,
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 1
    payload = json.loads(capsys.readouterr().out)

    assert state["manifest_handle_wrapped"] is True
    assert attempts == ["manifest_handle_acquired", "manifest_handle_close"]
    assert payload["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert payload["failure_reason"] == "PRIMARY_POST_EXCHANGE_FAILURE"
    assert payload["target_may_have_changed"] is True
    assert payload["automatic_retry_allowed"] is False
    assert payload["manual_recovery_required"] is True
    assert payload.get("primary_exception_preserved") is True
    assert payload["primary_error_type"] == "OrchestratorError"
    assert payload["primary_error_code"] == "PRIMARY_POST_EXCHANGE_FAILURE"
    assert payload.get("cleanup_exception_recorded") is True
    assert payload.get("cleanup_exception_count") == 1
    assert payload.get("cleanup_failure_codes") == [
        "MANIFEST_TEMP_FILE_CLOSE_FAILED"
    ]
    assert payload["cleanup_failures"] == [
        {
            "resource_kind": "MANIFEST_TEMP_FILE_HANDLE",
            "cleanup_phase": "SCOPED_RESOURCE_RELEASE",
            "error_type": "OSError",
            "error_code": "MANIFEST_TEMP_FILE_CLOSE_FAILED",
        }
    ]
    assert payload["manifest_write_failed"] is True
    assert payload["durable_evidence_updated"] is False
    assert "sensitive" not in json.dumps(payload, ensure_ascii=True)


def _runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    uid_getter = getattr(os, "geteuid", None)
    gid_getter = getattr(os, "getegid", None)
    assert callable(uid_getter) and callable(gid_getter)
    monkeypatch.setattr(staged, "RUNTIME_UID", int(uid_getter()))
    monkeypatch.setattr(staged, "RUNTIME_GID", int(gid_getter()))
    monkeypatch.setattr(
        staged,
        "_inspect_image",
        lambda *_args, **_kwargs: "1" * 40,
    )


def _run_primary_with_manifest_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    primary_phase: str,
    fail_source_lease: bool,
) -> tuple[dict[str, Any], list[str]]:
    source = _database(tmp_path / "source" / "database.sqlite")
    args = _args(tmp_path, source)
    _runtime(monkeypatch)
    state = {"primary": False, "manifest_wrapped": False}
    attempts: list[str] = []
    real_fdopen = os.fdopen

    def wrap_failure_manifest_handle(
        descriptor: int,
        *fdopen_args: Any,
        **fdopen_kwargs: Any,
    ) -> Any:
        handle = real_fdopen(descriptor, *fdopen_args, **fdopen_kwargs)
        if state["primary"] and not state["manifest_wrapped"]:
            state["manifest_wrapped"] = True
            attempts.append("manifest_handle_acquired")
            return _CloseFailingProxy(handle, attempts)
        return handle

    def inject_primary(phase: str, publish_state: str) -> None:
        if phase == primary_phase:
            state["primary"] = True
            raise staged.OrchestratorError(
                "PRIMARY_INJECTED_FAILURE",
                publish_state=publish_state,
            )

    monkeypatch.setattr(os, "fdopen", wrap_failure_manifest_handle)
    if fail_source_lease:
        original_close = staged.SQLiteLease.close

        def close_source_lease(self: staged.SQLiteLease) -> None:
            original_close(self)
            if self.label == "SOURCE":
                attempts.append("source_lease_close")
                raise OSError("sensitive-source-lease-release")

        monkeypatch.setattr(staged.SQLiteLease, "close", close_source_lease)

    assert staged.execute_synthetic(
        args,
        _failure_callback=inject_primary,
        _migration_runner=_host_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    ) == 1
    payload = json.loads(capsys.readouterr().out)
    assert state["manifest_wrapped"] is True
    assert "sensitive" not in json.dumps(payload, ensure_ascii=True)
    return payload, attempts


def test_pre_exchange_primary_preserves_manifest_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload, attempts = _run_primary_with_manifest_close_failure(
        tmp_path,
        monkeypatch,
        capsys,
        primary_phase="integrity_validation",
        fail_source_lease=False,
    )

    assert attempts == ["manifest_handle_acquired", "manifest_handle_close"]
    assert payload["exit_classification"] == "PRIMARY_INJECTED_FAILURE"
    assert payload["publish_state"] == "BEFORE_EXCHANGE"
    assert payload["target_may_have_changed"] is False
    assert payload["automatic_retry_allowed"] is False
    assert payload["primary_exception_preserved"] is True
    assert payload["primary_error_type"] == "OrchestratorError"
    assert payload["primary_error_code"] == "PRIMARY_INJECTED_FAILURE"
    assert payload["cleanup_exception_recorded"] is True
    assert payload["cleanup_exception_count"] == 1
    assert payload["cleanup_failure_codes"] == [
        "MANIFEST_TEMP_FILE_CLOSE_FAILED"
    ]


def test_post_exchange_primary_aggregates_manifest_and_outer_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload, attempts = _run_primary_with_manifest_close_failure(
        tmp_path,
        monkeypatch,
        capsys,
        primary_phase="publish_exchange",
        fail_source_lease=True,
    )

    assert attempts == [
        "manifest_handle_acquired",
        "manifest_handle_close",
        "source_lease_close",
    ]
    assert payload["exit_classification"] == "PUBLISH_UNCERTAIN"
    assert payload["failure_reason"] == "PRIMARY_INJECTED_FAILURE"
    assert payload["target_may_have_changed"] is True
    assert payload["automatic_retry_allowed"] is False
    assert payload["manual_recovery_required"] is True
    assert payload["primary_exception_preserved"] is True
    assert payload["primary_error_type"] == "OrchestratorError"
    assert payload["primary_error_code"] == "PRIMARY_INJECTED_FAILURE"
    assert payload["cleanup_exception_recorded"] is True
    assert payload["cleanup_exception_count"] == 2
    assert payload["cleanup_failure_codes"] == [
        "MANIFEST_TEMP_FILE_CLOSE_FAILED",
        "SOURCE_SQLITE_LEASE_CLOSE_FAILED",
    ]


def test_successful_failed_manifest_write_records_handle_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = staged.DurableManifest(
        path=tmp_path / "manifest.json",
        payload={"STATE": "PLANNED", "PUBLISH_STATE": "BEFORE_EXCHANGE"},
    )
    attempts: list[str] = []
    real_fdopen = os.fdopen

    def wrap_manifest_handle(
        descriptor: int,
        *fdopen_args: Any,
        **fdopen_kwargs: Any,
    ) -> Any:
        handle = real_fdopen(descriptor, *fdopen_args, **fdopen_kwargs)
        attempts.append("manifest_handle_acquired")
        return _CloseFailingProxy(handle, attempts)

    monkeypatch.setattr(os, "fdopen", wrap_manifest_handle)
    with pytest.raises(staged._CleanupAggregateError) as captured:
        manifest.transition("FAILED")

    assert attempts == ["manifest_handle_acquired", "manifest_handle_close"]
    assert [record.resource_kind for record in captured.value.records] == [
        "MANIFEST_TEMP_FILE_HANDLE"
    ]
    assert [record.error_code for record in captured.value.records] == [
        "MANIFEST_TEMP_FILE_CLOSE_FAILED"
    ]
    assert "sensitive" not in str(captured.value)
