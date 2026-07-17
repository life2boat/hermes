from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts import healbite_schema_migrate
from scripts import hermes_production_staged_migrate as production
from scripts import hermes_staged_schema_migrate as staged


REVISION = "1" * 40
IMAGE_ID = "sha256:" + "2" * 64
PREVIOUS_IMAGE_ID = "sha256:" + "3" * 64
NEGATIVE_CASES = (
    "missing_explicit_db_path",
    "environment_only_db_path",
    "plan_sha_mismatch",
    "operation_id_mismatch",
    "expired_plan",
    "plan_symlink_substitution",
    "plan_path_substitution",
    "source_path_substitution",
    "source_inode_drift",
    "source_sha_drift",
    "source_schema_drift",
    "source_mode_drift",
    "hostname_drift",
    "filesystem_drift",
    "insufficient_free_space",
    "migration_image_missing",
    "image_revision_mismatch",
    "previous_image_missing",
    "feature_flag_enabled",
    "active_sqlite_reader",
    "active_sqlite_writer",
    "unsupported_sidecar",
    "cross_filesystem_staging",
    "compatibility_failure",
    "pre_publish_crash",
    "exchange_uncertainty",
    "final_verification_failure",
    "cleanup_failure",
    "manifest_fsync_failure",
)


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _source(root: Path) -> Path:
    parent = _private_directory(root / "source")
    source = parent / "database.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    os.chmod(source, 0o600)
    return source


def _deployment_contract(root: Path) -> Path:
    parent = _private_directory(root / "contract")
    path = parent / "deployment.json"
    path.write_text(
        json.dumps(
            {
                "feature_gates": {
                    "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
                    "HEALBITE_SHOPPING_LIST_ENABLED": "false",
                }
            }
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    return path


def _plan_args(root: Path, source: Path, contract: Path) -> argparse.Namespace:
    metadata = source.stat()
    return argparse.Namespace(
        command="plan",
        db_path=str(source),
        backup_parent=str(_private_directory(root / "backups")),
        staging_parent=str(_private_directory(root / "staging")),
        evidence_parent=str(_private_directory(root / "evidence")),
        deployment_contract=str(contract),
        migration_image_id=IMAGE_ID,
        migration_image_revision=REVISION,
        previous_image_id=PREVIOUS_IMAGE_ID,
        expected_hostname=socket.gethostname(),
        expected_source_device=metadata.st_dev,
        expected_source_inode=metadata.st_ino,
        expected_source_size=metadata.st_size,
        expected_source_sha256=production._sha256(source),
        expected_free_bytes=1,
        target_schema_version="household-weekly-shopping-v1",
        expires_in_seconds=3600,
    )


def _create_plan(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    source = _source(root)
    contract = _deployment_contract(root)
    monkeypatch.setattr(production, "_inspect_image", lambda image, expected: expected or REVISION)
    args = _plan_args(root, source, contract)
    assert production.create_plan(args) == 0
    output = json.loads(capsys.readouterr().out)
    plan_path = Path(output["plan_path"])
    payload = json.loads(plan_path.read_text(encoding="ascii"))
    return plan_path, payload, {"source": source, "contract": contract, "output": output}


def _rewrite_plan(plan_path: Path, payload: dict[str, Any]) -> str:
    production._write_json_durable(plan_path, payload)
    return production._sha256(plan_path)


def _execute_argv(
    plan_path: Path,
    payload: dict[str, Any],
    plan_sha: str,
    **overrides: str,
) -> list[str]:
    values = {
        "expected_plan_sha256": plan_sha,
        "confirm_operation_id": str(payload["OPERATION_ID"]),
        "confirm_source_sha256": str(payload["SOURCE_SHA256"]),
        "confirm_image_revision": str(payload["MIGRATION_IMAGE_REVISION"]),
    }
    values.update(overrides)
    return [
        "execute",
        "--plan",
        str(plan_path),
        "--expected-plan-sha256",
        values["expected_plan_sha256"],
        "--confirm-operation-id",
        values["confirm_operation_id"],
        "--confirm-source-sha256",
        values["confirm_source_sha256"],
        "--confirm-image-revision",
        values["confirm_image_revision"],
    ]


def _host_migration(_contract: staged.Contract, staging_dir: Path) -> None:
    result = healbite_schema_migrate.run_migration(
        db_path=str(staging_dir / "database.sqlite"),
        staged_copy=True,
    )
    assert result.exit_code == 0


def _prepare_staged_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    uid = os.geteuid()
    gid = os.getegid()
    monkeypatch.setattr(staged, "RUNTIME_UID", uid)
    monkeypatch.setattr(staged, "RUNTIME_GID", gid)
    monkeypatch.setattr(staged, "_inspect_image", lambda *_args, **_kwargs: REVISION)


def _staged_test_executor(
    monkeypatch: pytest.MonkeyPatch,
    *,
    failure_callback: Callable[[str, str], None] | None = None,
    compatibility_probe: Callable[[staged.Contract, Path], Any] | None = None,
) -> None:
    _prepare_staged_runtime(monkeypatch)

    def execute(
        args: argparse.Namespace,
        *,
        operation_id: str,
        expected_source_identity: staged.SourceIdentity,
        phase_callback: Callable[[str], None] | None = None,
    ) -> int:
        return staged._execute_staged(
            args,
            synthetic=False,
            operation_id=operation_id,
            expected_source_identity=expected_source_identity,
            _phase_callback=phase_callback,
            _failure_callback=failure_callback,
            _migration_runner=_host_migration,
            _compatibility_probe=compatibility_probe or (lambda *_args, **_kwargs: {}),
        )

    monkeypatch.setattr(production, "execute_production_staged", execute)


def _failure_at(selected: str) -> Callable[[str, str], None]:
    def callback(phase: str, publish_state: str) -> None:
        if phase == selected:
            raise staged.OrchestratorError(f"INJECTED_{phase.upper()}", publish_state=publish_state)

    return callback


def test_public_contract_has_only_explicit_plan_and_execute() -> None:
    parser = production.build_parser()
    subparsers = parser._subparsers._group_actions[0].choices
    assert set(subparsers) == {"plan", "execute"}
    plan_help = subparsers["plan"].format_help()
    execute_help = subparsers["execute"].format_help()
    for required in (
        "--db-path",
        "--backup-parent",
        "--staging-parent",
        "--evidence-parent",
        "--migration-image-id",
        "--migration-image-revision",
        "--previous-image-id",
        "--expected-hostname",
        "--expected-source-device",
        "--expected-source-inode",
        "--expected-source-size",
        "--expected-source-sha256",
    ):
        assert required in plan_help
    for required in (
        "--plan",
        "--expected-plan-sha256",
        "--confirm-operation-id",
        "--confirm-source-sha256",
        "--confirm-image-revision",
    ):
        assert required in execute_help
    combined = f"{plan_help}\n{execute_help}"
    assert "--yes" not in combined
    assert "--force" not in combined
    assert "--test-" not in combined


def test_production_entrypoint_has_no_environment_or_test_hook_bypass() -> None:
    source = Path(production.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv(" not in source
    assert "_failure_callback" not in source
    assert "_before_exchange_callback" not in source
    assert "_migration_runner" not in source
    assert "_compatibility_probe" not in source
    assert not hasattr(production, "_execute_staged")


def test_plan_is_read_only_atomic_private_and_hash_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, payload, context = _create_plan(tmp_path, monkeypatch, capsys)
    source = context["source"]
    output = context["output"]
    assert output["plan_sha256"] == production._sha256(plan_path)
    assert production._canonical_json(payload) == plan_path.read_bytes()
    assert stat.S_IMODE(plan_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(plan_path.parent.stat().st_mode) == 0o700
    assert payload["PLAN_READ_ONLY"] is True
    assert payload["PLAN_DATABASE_MUTATION"] is False
    assert payload["PLAN_BACKUP_CREATED"] is False
    assert payload["PLAN_STAGING_CREATED"] is False
    assert list((tmp_path / "backups").iterdir()) == []
    assert list((tmp_path / "staging").iterdir()) == []
    assert payload["SOURCE_SHA256"] == production._sha256(source)


def test_synthetic_plan_execute_workflow_reuses_staged_implementation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, payload, context = _create_plan(tmp_path, monkeypatch, capsys)
    _staged_test_executor(monkeypatch)
    before_hash = payload["SOURCE_SHA256"]

    assert production.main(_execute_argv(plan_path, payload, production._sha256(plan_path))) == 0

    output = json.loads(capsys.readouterr().out)
    evidence = json.loads((plan_path.parent / "execution.json").read_text(encoding="ascii"))
    backup = Path(payload["BACKUP_PARENT"]) / f"backup-{payload['OPERATION_ID']}.sqlite"
    assert output["manifest_state"] == "COMPLETED"
    assert evidence["STATE_HISTORY"] == list(production.SUCCESS_STATES)
    assert evidence["BACKUP_SHA256"] == before_hash
    assert production._sha256(backup) == before_hash
    assert evidence["FINAL_TARGET_SHA256"] == production._sha256(context["source"])
    assert evidence["FINAL_TARGET_SHA256"] != before_hash
    assert evidence["MANUAL_RECOVERY_REQUIRED"] is False
    assert evidence["AUTOMATIC_RETRY_ALLOWED"] is False
    assert evidence["BACKUP_SOURCE_IDENTITY_MATCH"] is True
    assert staged._sqlite_validation(context["source"]) == ("ok", 0)


def test_late_source_drift_between_gate_and_lease_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan_path, payload, context = _create_plan(tmp_path, monkeypatch, capsys)
    source = context["source"]
    _prepare_staged_runtime(monkeypatch)
    mutated_hash: str | None = None

    def mutate_before_lease(
        args: argparse.Namespace,
        *,
        operation_id: str,
        expected_source_identity: staged.SourceIdentity,
        phase_callback: Callable[[str], None] | None = None,
    ) -> int:
        nonlocal mutated_hash
        with sqlite3.connect(args.source_db) as connection:
            connection.execute("INSERT INTO legacy_rows VALUES ('late-drift')")
        mutated_hash = production._sha256(Path(args.source_db))
        return staged._execute_staged(
            args,
            synthetic=False,
            operation_id=operation_id,
            expected_source_identity=expected_source_identity,
            _phase_callback=phase_callback,
            _migration_runner=_host_migration,
            _compatibility_probe=lambda *_args, **_kwargs: {},
        )

    monkeypatch.setattr(production, "execute_production_staged", mutate_before_lease)
    assert production.main(
        _execute_argv(plan_path, payload, production._sha256(plan_path))
    ) == 1

    output = json.loads(capsys.readouterr().out)
    evidence = json.loads((plan_path.parent / "execution.json").read_text(encoding="ascii"))
    assert output["error_type"] == "SOURCE_IDENTITY_CHANGED"
    assert evidence["STATE"] == "PRE_PUBLISH_FAILED"
    assert list(Path(payload["BACKUP_PARENT"]).iterdir()) == []
    assert mutated_hash is not None
    assert production._sha256(source) == mutated_hash


@pytest.mark.parametrize("case", NEGATIVE_CASES)
def test_negative_contract_matrix_fails_closed_before_or_with_accurate_publish_state(
    case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert len(NEGATIVE_CASES) == 29
    parser = production.build_parser()
    if case in {"missing_explicit_db_path", "environment_only_db_path"}:
        if case == "environment_only_db_path":
            monkeypatch.setenv("HEALBITE_DB_PATH", str(tmp_path / "ignored.sqlite"))
        with pytest.raises(SystemExit):
            parser.parse_args(["plan"])
        return

    plan_path, payload, context = _create_plan(tmp_path, monkeypatch, capsys)
    source = context["source"]
    plan_sha = production._sha256(plan_path)
    before_execute = production._sha256(source)
    argv_overrides: dict[str, str] = {}
    reader: sqlite3.Connection | None = None
    writer: sqlite3.Connection | None = None

    def execution_must_not_start(*_args: Any, **_kwargs: Any) -> int:
        raise AssertionError("staged execution reached after a preflight denial")

    monkeypatch.setattr(production, "execute_production_staged", execution_must_not_start)

    try:
        if case == "plan_sha_mismatch":
            argv_overrides["expected_plan_sha256"] = "f" * 64
        elif case == "operation_id_mismatch":
            argv_overrides["confirm_operation_id"] = "f" * 32
        elif case == "expired_plan":
            payload["EXPIRES_AT"] = production._timestamp(production._now() - timedelta(seconds=1))
            plan_sha = _rewrite_plan(plan_path, payload)
        elif case == "plan_symlink_substitution":
            link = plan_path.parent / "plan-link.json"
            link.symlink_to(plan_path)
            plan_path = link
        elif case == "plan_path_substitution":
            alias = plan_path.parent / "plan-alias.json"
            os.link(plan_path, alias)
            plan_path = alias
        elif case in {"source_path_substitution", "source_inode_drift"}:
            replacement = source.with_name("replacement.sqlite")
            replacement.write_bytes(source.read_bytes())
            os.chmod(replacement, 0o600)
            os.replace(replacement, source)
        elif case == "source_sha_drift":
            with sqlite3.connect(source) as connection:
                connection.execute("INSERT INTO legacy_rows VALUES ('drift')")
        elif case == "source_schema_drift":
            old_schema = payload["SOURCE_SCHEMA_FINGERPRINT"]
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE schema_drift (value TEXT)")
            metadata = source.stat()
            payload.update(
                SOURCE_SIZE=metadata.st_size,
                SOURCE_SHA256=production._sha256(source),
                SOURCE_SCHEMA_FINGERPRINT=old_schema,
            )
            plan_sha = _rewrite_plan(plan_path, payload)
            argv_overrides["confirm_source_sha256"] = payload["SOURCE_SHA256"]
        elif case == "source_mode_drift":
            os.chmod(source, 0o640)
        elif case == "hostname_drift":
            payload["HOSTNAME"] = "not-the-current-host"
            plan_sha = _rewrite_plan(plan_path, payload)
        elif case == "filesystem_drift":
            payload["EXPECTED_FILESYSTEM_DEVICE"] = int(payload["EXPECTED_FILESYSTEM_DEVICE"]) + 1
            plan_sha = _rewrite_plan(plan_path, payload)
        elif case == "insufficient_free_space":
            payload["EXPECTED_FREE_BYTES"] = 2**63
            plan_sha = _rewrite_plan(plan_path, payload)
        elif case == "migration_image_missing":
            monkeypatch.setattr(
                production,
                "_inspect_image",
                lambda image, _revision: (_ for _ in ()).throw(production.ProductionGateError("IMAGE_NOT_AVAILABLE"))
                if image == IMAGE_ID
                else REVISION,
            )
        elif case == "image_revision_mismatch":
            monkeypatch.setattr(
                production,
                "_inspect_image",
                lambda image, expected: (_ for _ in ()).throw(
                    production.ProductionGateError("IMAGE_REVISION_MISMATCH")
                )
                if image == IMAGE_ID and expected is not None
                else REVISION,
            )
        elif case == "previous_image_missing":
            monkeypatch.setattr(
                production,
                "_inspect_image",
                lambda image, _revision: (_ for _ in ()).throw(production.ProductionGateError("IMAGE_NOT_AVAILABLE"))
                if image == PREVIOUS_IMAGE_ID
                else REVISION,
            )
        elif case == "feature_flag_enabled":
            context["contract"].write_text(
                json.dumps(
                    {
                        "feature_gates": {
                            "HEALBITE_SHOPPING_LIST_ALLOWLIST": "",
                            "HEALBITE_SHOPPING_LIST_ENABLED": "true",
                        }
                    }
                ),
                encoding="utf-8",
            )
        elif case == "active_sqlite_reader":
            reader = sqlite3.connect(source)
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM legacy_rows").fetchall()
            _staged_test_executor(monkeypatch)
        elif case == "active_sqlite_writer":
            writer = sqlite3.connect(source)
            writer.execute("BEGIN IMMEDIATE")
            _staged_test_executor(monkeypatch)
        elif case == "unsupported_sidecar":
            Path(f"{source}-journal").touch()
        elif case == "cross_filesystem_staging":
            payload["STAGING_PARENT_IDENTITY"]["DEVICE"] = int(
                payload["STAGING_PARENT_IDENTITY"]["DEVICE"]
            ) + 1
            plan_sha = _rewrite_plan(plan_path, payload)
        elif case == "compatibility_failure":
            def incompatible(*_args: Any, **_kwargs: Any) -> None:
                raise staged.OrchestratorError("PREVIOUS_IMAGE_INCOMPATIBLE")

            _staged_test_executor(monkeypatch, compatibility_probe=incompatible)
        elif case == "pre_publish_crash":
            _staged_test_executor(monkeypatch, failure_callback=_failure_at("pre_publish_cleanup"))
        elif case == "exchange_uncertainty":
            _staged_test_executor(monkeypatch, failure_callback=_failure_at("target_parent_fsync"))
        elif case == "final_verification_failure":
            _staged_test_executor(monkeypatch, failure_callback=_failure_at("final_verification"))
        elif case == "cleanup_failure":
            def cleanup_failure(phase: str, publish_state: str) -> None:
                if phase == "pre_publish_cleanup":
                    raise staged.OrchestratorError("PRIMARY_FAILURE", publish_state=publish_state)
                if phase == "staging_cleanup":
                    raise staged.OrchestratorError("CLEANUP_FAILED", publish_state=publish_state)

            _staged_test_executor(monkeypatch, failure_callback=cleanup_failure)
        elif case == "manifest_fsync_failure":
            _staged_test_executor(monkeypatch, failure_callback=_failure_at("manifest_fsync"))
        else:
            raise AssertionError(case)

        pre_call_hash = production._sha256(source)
        result = production.main(_execute_argv(plan_path, payload, plan_sha, **argv_overrides))
        assert result == 1
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "FAILED"
        assert output.get("automatic_retry_allowed", False) is False
        if case in {"active_sqlite_reader", "active_sqlite_writer"}:
            assert output["exit_classification"] == "QUIESCENCE_FAILED"
            assert output["database_mutated"] is False
            assert output["backup_created"] is False
        if case == "exchange_uncertainty":
            evidence = json.loads((Path(payload["EVIDENCE_PARENT"]) / payload["OPERATION_ID"] / "execution.json").read_text())
            assert evidence["TARGET_MAY_HAVE_CHANGED"] is True
            assert evidence["MANUAL_RECOVERY_REQUIRED"] is True
        elif case not in {"final_verification_failure"}:
            assert production._sha256(source) == pre_call_hash
    finally:
        if reader is not None:
            reader.rollback()
            reader.close()
        if writer is not None:
            writer.rollback()
            writer.close()


def test_failure_states_are_monotonic_and_rollback_is_never_blind(tmp_path: Path) -> None:
    evidence = production.ExecutionEvidence(
        path=tmp_path / "execution.json",
        payload={"STATE": "PLANNED", "STATE_HISTORY": ["PLANNED"]},
    )
    production._write_json_durable(evidence.path, evidence.payload)
    evidence.transition("PREFLIGHT_VERIFIED")
    evidence.transition("PRE_PUBLISH_FAILED", AUTOMATIC_RETRY_ALLOWED=False)
    with pytest.raises(production.ProductionGateError, match="DUPLICATE_FAILURE_STATE"):
        evidence.transition("PRE_PUBLISH_FAILED")
    assert "automatic" not in production.__doc__.lower()
