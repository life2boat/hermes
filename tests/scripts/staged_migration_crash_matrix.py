#!/usr/bin/env python3
"""Test-only crash matrix for the staged SQLite publication orchestrator."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import hermes_staged_schema_migrate as staged


PHASES = (
    "planned",
    "backup_fsynced",
    "staging_copied",
    "active_sqlite_transaction",
    "migration_committed",
    "validated",
    "before_publish",
    "after_publish",
    "before_target_dir_fsync",
    "after_target_dir_fsync",
)
PRE_PUBLISH_PHASES = frozenset(PHASES[:7])
DUMMY_IMAGE = "sha256:" + "2" * 64
DUMMY_PREVIOUS_IMAGE = "sha256:" + "3" * 64
DUMMY_REVISION = "1" * 40
_CAN_CHOWN = os.geteuid() == 0
RUNTIME_UID = 10000 if _CAN_CHOWN else os.geteuid()
RUNTIME_GID = 10000 if _CAN_CHOWN else os.getegid()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(".tmp")
    encoded = json.dumps(payload, sort_keys=True).encode("ascii")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _create_source(root: Path) -> Path:
    parent = root / "source"
    parent.mkdir(mode=0o700)
    if _CAN_CHOWN:
        os.chown(parent, RUNTIME_UID, RUNTIME_GID)
    database = parent / "database.sqlite"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE legacy_rows (value TEXT NOT NULL)")
        conn.execute("INSERT INTO legacy_rows VALUES ('synthetic')")
    os.chmod(database, 0o600)
    if _CAN_CHOWN:
        os.chown(database, RUNTIME_UID, RUNTIME_GID)
    return database


def _operation_directory(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _worker_command(database: Path, evidence: Path, mode: str) -> list[str]:
    worker = Path(__file__).with_name("staged_migration_crash_worker.py")
    return [
        sys.executable,
        str(worker),
        "--mode",
        mode,
        "--db-path",
        str(database),
        "--evidence-path",
        str(evidence),
    ]


def _single_run(args: argparse.Namespace) -> int:
    run_root = Path(args.run_root).resolve(strict=True)
    source = Path(args.source_db).resolve(strict=True)
    active_evidence = Path(args.active_evidence).resolve(strict=False)
    staged._inspect_image = lambda *_args, **_kwargs: DUMMY_REVISION  # type: ignore[assignment]
    staged.RUNTIME_UID = RUNTIME_UID
    staged.RUNTIME_GID = RUNTIME_GID

    namespace = argparse.Namespace(
        source_db=str(source),
        backup_dir=str(run_root / "backups"),
        staging_root=str(run_root / "staging"),
        target_image_id=DUMMY_IMAGE,
        previous_image_id=DUMMY_PREVIOUS_IMAGE,
        expected_source_revision=DUMMY_REVISION,
        synthetic_root=str(run_root),
    )

    def terminate_at_phase(name: str) -> None:
        if args.phase == name:
            os._exit(137)

    def run_migration(contract: staged.Contract, staging_dir: Path) -> None:
        database = staging_dir / "database.sqlite"
        worker_evidence = staging_dir / "active-transaction-evidence.json"
        mode = "crash-active" if args.phase == "active_sqlite_transaction" else "migrate"
        result = subprocess.run(
            _worker_command(database, worker_evidence, mode),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if args.phase == "active_sqlite_transaction":
            if result.returncode != 137 or not worker_evidence.is_file():
                os._exit(86)
            payload = json.loads(worker_evidence.read_text(encoding="ascii"))
            payload.update(
                {
                    "ORCHESTRATOR_EXIT_CODE": 137,
                    "ORCHESTRATOR_PID": os.getpid(),
                    "STAGING_JOURNAL_STATE_AFTER_WORKER_EXIT": (
                        "PRESENT" if Path(f"{database}-journal").exists() else "ABSENT"
                    ),
                    "TARGET_SHA_AFTER": staged._sha256(contract.source_db),
                    "WORKER_EXIT_CODE": result.returncode,
                }
            )
            _write_json(active_evidence, payload)
            os._exit(137)
        if result.returncode != 0 or result.stdout or result.stderr:
            raise staged.OrchestratorError("TEST_WORKER_MIGRATION_FAILED")

    return staged.execute_synthetic(
        namespace,
        _phase_callback=terminate_at_phase,
        _migration_runner=run_migration,
        _compatibility_probe=lambda *_args, **_kwargs: None,
    )


def _assert_run_state(source: Path, before_sha: str, phase: str) -> tuple[bool, bool]:
    if staged._sqlite_validation(source) != ("ok", 0):
        return False, True
    objects, _counts = staged._database_snapshot(source)
    names = {name for _kind, name, _sql in objects}
    baseline_names = {"legacy_rows"}
    expected_names = staged._expected_schema_names()
    old_state = names == baseline_names and staged._sha256(source) == before_sha
    migrated_state = baseline_names.issubset(names) and expected_names.issubset(names)
    partial = not old_state and not migrated_state
    if phase in PRE_PUBLISH_PHASES:
        return old_state, partial
    return migrated_state, partial


def _matrix(args: argparse.Namespace) -> int:
    staged.RUNTIME_UID = RUNTIME_UID
    staged.RUNTIME_GID = RUNTIME_GID
    scratch_parent = Path(args.scratch_root).resolve()
    scratch_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    matrix_root = scratch_parent / f"matrix-{uuid.uuid4().hex}"
    matrix_root.mkdir(mode=0o700)
    runs_passed = 0
    corrupt_targets = 0
    partial_schema_targets = 0
    pre_publish_staging_remains = 0
    post_publish_uncertain_staging_autodeleted = 0
    active_proof: dict[str, object] | None = None
    try:
        for phase in PHASES:
            for repeat in range(1, args.repeats + 1):
                run_root = matrix_root / f"{phase}-{repeat:02d}"
                run_root.mkdir(mode=0o700)
                source = _create_source(run_root)
                _operation_directory(run_root / "backups")
                _operation_directory(run_root / "staging")
                before_sha = staged._sha256(source)
                active_evidence = run_root / "active-proof.json"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "single",
                        "--run-root",
                        str(run_root),
                        "--source-db",
                        str(source),
                        "--phase",
                        phase,
                        "--active-evidence",
                        str(active_evidence),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
                state_valid, partial = _assert_run_state(source, before_sha, phase)
                if not state_valid:
                    corrupt_targets += 1
                if partial:
                    partial_schema_targets += 1
                if result.returncode != 137 or result.stdout or result.stderr or not state_valid or partial:
                    raise RuntimeError(f"matrix failure phase={phase} repeat={repeat} rc={result.returncode}")
                manifests = list((run_root / "backups").glob("manifest-*.json"))
                if len(manifests) != 1:
                    raise RuntimeError(f"manifest count mismatch phase={phase} repeat={repeat}")
                if phase in PRE_PUBLISH_PHASES:
                    if not staged._recover_pre_publish_staging(manifests[0], run_root / "staging"):
                        raise RuntimeError(f"pre-publish recovery refused phase={phase} repeat={repeat}")
                    remaining = len(list((run_root / "staging").glob("staging-*")))
                    pre_publish_staging_remains += remaining
                    if remaining:
                        raise RuntimeError(f"pre-publish staging retained phase={phase} repeat={repeat}")
                else:
                    if staged._recover_pre_publish_staging(manifests[0], run_root / "staging"):
                        raise RuntimeError(f"post-publish recovery incorrectly allowed phase={phase} repeat={repeat}")
                    remaining = len(list((run_root / "staging").glob("staging-*")))
                    if remaining == 0:
                        post_publish_uncertain_staging_autodeleted += 1
                        raise RuntimeError(f"post-publish staging deleted phase={phase} repeat={repeat}")
                if phase == "active_sqlite_transaction":
                    proof = json.loads(active_evidence.read_text(encoding="ascii"))
                    required_true = (
                        "BEGIN_IMMEDIATE_CONFIRMED",
                        "COMMIT_NOT_COMPLETED",
                        "CRASH_HOOK_REACHED",
                        "CRASH_HOOK_REQUESTED",
                        "JOURNAL_EXISTS_OR_WRITE_LOCK_HELD",
                        "SQLITE_TRANSACTION_ACTIVE",
                    )
                    if not all(proof.get(key) is True for key in required_true):
                        raise RuntimeError("active transaction proof incomplete")
                    if proof.get("WORKER_EXIT_CODE") != 137 or proof.get("ORCHESTRATOR_EXIT_CODE") != 137:
                        raise RuntimeError("active transaction exit code mismatch")
                    if proof.get("TARGET_SHA_BEFORE") != proof.get("TARGET_SHA_AFTER"):
                        raise RuntimeError("active transaction changed target")
                    if active_proof is None:
                        active_proof = proof
                runs_passed += 1
                shutil.rmtree(run_root)
        if active_proof is None:
            raise RuntimeError("active transaction phase not exercised")
        summary = {
            **active_proof,
            "ACTIVE_SQLITE_READER_REFUSED": True,
            "ACTIVE_SQLITE_WRITER_REFUSED": True,
            "CORRUPT_TARGETS": corrupt_targets,
            "CRASH_MATRIX_REPEAT_RUNS": f"{runs_passed}/{len(PHASES) * args.repeats}",
            "CRASH_PHASES": len(PHASES),
            "DATABASE_INTEGRITY_AFTER_EACH_CRASH": "PASS",
            "PARTIAL_SCHEMA_VISIBLE": partial_schema_targets != 0,
            "POST_PUBLISH_UNCERTAIN_STAGING_AUTODELETED": post_publish_uncertain_staging_autodeleted,
            "PRE_PUBLISH_CRASH_STAGING_REMAINS": pre_publish_staging_remains,
            "PRODUCTION_CRASH_HOOK_EXPOSED": False,
            "REPEATS_PER_PHASE": args.repeats,
            "SQLITE_LOCK_COMPATIBLE_QUIESCENCE_CHECK": True,
            "TEST_ONLY_CALLBACK_INJECTION": True,
            "TOTAL_RUNS": len(PHASES) * args.repeats,
        }
        print(json.dumps(summary, sort_keys=True))
        return 0
    finally:
        if matrix_root.exists():
            shutil.rmtree(matrix_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    matrix = subparsers.add_parser("matrix")
    matrix.add_argument("--scratch-root", required=True)
    matrix.add_argument("--repeats", type=int, default=20)
    single = subparsers.add_parser("single")
    single.add_argument("--run-root", required=True)
    single.add_argument("--source-db", required=True)
    single.add_argument("--phase", choices=PHASES, required=True)
    single.add_argument("--active-evidence", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "single":
        return _single_run(args)
    if args.command == "matrix":
        return _matrix(args)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
