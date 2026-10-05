"""Backup restore and storage writers share the destination process lock."""

import json
import multiprocessing
from pathlib import Path

import pytest

from tests.cron.test_jobs_process import (
    _configure, _contending_writer, _receive, storage,
)


def _restore_worker(root, live_home, kind, pipe, release):
    from argparse import Namespace
    from hermes_cli import backup
    jobs = _configure(root)
    live_path = Path(live_home) / "cron" / "jobs.json"
    if kind == "if_empty":
        original_count = backup._count_cron_jobs
        def count_then_pause(path):
            count = original_count(path)
            if path == live_path:
                pipe.send(("snapshot", None))
                assert release.wait(20)
            return count
        backup._count_cron_jobs = count_then_pause
    else:
        original_replace = jobs.atomic_replace
        def replace_then_pause(source, target):
            if Path(target) == live_path:
                pipe.send(("snapshot", None))
                assert release.wait(20)
            return original_replace(source, target)
        jobs.atomic_replace = replace_then_pause
    try:
        if kind == "if_empty":
            result = backup.restore_cron_jobs_if_emptied("restore-test", hermes_home=Path(root))
            assert result and result["restored"]
        elif kind == "quick":
            result = backup.restore_quick_snapshot("restore-test", hermes_home=Path(root))
            assert result is True
        elif kind == "distribution":
            from hermes_cli.profile_distribution import _copy_dist_payload, DistributionManifest
            _copy_dist_payload(Path(root) / "dist-source", Path(live_home), DistributionManifest(name="worker"), preserve_config=True)
        else:
            backup.get_default_hermes_root = lambda: Path(root)
            backup.run_import(Namespace(zipfile=str(Path(root) / "restore.zip"), force=True))
        pipe.send(("completed", None))
    except BaseException as exc:
        pipe.send(("error", type(exc).__name__))
        raise


@pytest.mark.parametrize("kind", ["if_empty", "quick", "zip", "zip_profile", "distribution"])
def test_restore_vs_concurrent_creator(storage, monkeypatch, kind):
    import zipfile
    root, jobs = storage
    live_home = root / "profiles" / "worker" if kind == "zip_profile" else root
    if live_home != root:
        monkeypatch.setattr(jobs, "CRON_DIR", live_home / "cron")
        monkeypatch.setattr(jobs, "JOBS_FILE", live_home / "cron" / "jobs.json")
        monkeypatch.setattr(jobs, "OUTPUT_DIR", live_home / "cron" / "output")
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    payload = json.dumps({"jobs": [{**existing, "name": "restored"}], "backup_extra": "preserved"}).encode("utf-8")
    lock_path = jobs.JOBS_FILE.with_name("jobs.json.lock")
    lock_bytes = lock_path.read_bytes()
    if kind in {"if_empty", "quick"}:
        snapshot = root / "state-snapshots" / "restore-test"
        (snapshot / "cron").mkdir(parents=True)
        (snapshot / "cron" / "jobs.json").write_bytes(payload)
        (snapshot / "cron" / "jobs.json.lock").write_bytes(b"DO NOT RESTORE LOCK")
        (snapshot / "manifest.json").write_text(json.dumps({"files": {"cron/jobs.json": {}, "cron/jobs.json.lock": {}}}), encoding="utf-8")
        if kind == "if_empty":
            jobs.save_jobs([])
    elif kind == "distribution":
        source = root / "dist-source" / "cron"
        source.mkdir(parents=True)
        (source / "jobs.json").write_bytes(payload)
        (source / "jobs.json.lock").write_bytes(b"DO NOT RESTORE LOCK")
        (source / ".tick.lock").write_bytes(b"DO NOT RESTORE TICK LOCK")
        (source / "daily.json").write_text("{}", encoding="utf-8")
        (jobs.CRON_DIR / ".tick.lock").write_bytes(b"LIVE TICK LOCK")
        (jobs.CRON_DIR / "obsolete.json").write_text("{}", encoding="utf-8")
    else:
        root.mkdir(parents=True, exist_ok=True)
        prefix = "profiles/worker/" if kind == "zip_profile" else ""
        with zipfile.ZipFile(root / "restore.zip", "w") as archive:
            archive.writestr("config.yaml", "model: test-model")
            archive.writestr(prefix + "cron/jobs.json", payload)
            archive.writestr(prefix + "cron/jobs.json.lock", b"DO NOT RESTORE LOCK")
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Event()
    owner_parent, owner_child = ctx.Pipe(duplex=False)
    writer_parent, writer_child = ctx.Pipe(duplex=False)
    owner = ctx.Process(target=_restore_worker, args=(str(root), str(live_home), kind, owner_child, release))
    writer = ctx.Process(target=_contending_writer, args=(str(live_home), writer_child, "create"))
    try:
        owner.start()
        assert _receive(owner_parent)[0] == "snapshot"
        writer.start()
        assert _receive(writer_parent)[0] == "blocked"
        release.set()
        while True:
            stage, created = _receive(writer_parent)
            if stage != "blocked":
                assert stage == "completed"
                break
        assert _receive(owner_parent)[0] == "completed"
    finally:
        release.set()
        for worker in (owner, writer):
            worker.join(20)
            assert not worker.is_alive() and worker.exitcode == 0
            worker.close()
        for pipe in (owner_parent, owner_child, writer_parent, writer_child):
            pipe.close()
    persisted = {job["id"]: job for job in jobs.load_jobs()}
    assert persisted[existing["id"]]["name"] == "restored"
    assert created["id"] in persisted
    assert lock_path.read_bytes() == lock_bytes
    if kind == "distribution":
        assert (jobs.CRON_DIR / ".tick.lock").read_bytes() == b"LIVE TICK LOCK"
        assert (jobs.CRON_DIR / "daily.json").exists()
        assert not (jobs.CRON_DIR / "obsolete.json").exists()


def test_authoritative_restore_preserves_bytes_and_failed_replace(storage, monkeypatch):
    _, jobs = storage
    jobs.create_job(prompt="old", schedule="every 1h")
    original = jobs.JOBS_FILE.read_bytes()
    payload = b'{"jobs": [], "backup_extra": "keep me"}\n'
    with monkeypatch.context() as patcher:
        def broken(*args):
            raise OSError("restore replace failed")
        patcher.setattr(jobs, "atomic_replace", broken)
        with pytest.raises(OSError, match="restore replace"):
            jobs.replace_jobs_file(payload, jobs.JOBS_FILE)
    assert jobs.JOBS_FILE.read_bytes() == original
    assert list(jobs.CRON_DIR.glob(".jobs_*.tmp")) == []
    jobs.replace_jobs_file(payload, jobs.JOBS_FILE)
    assert jobs.JOBS_FILE.read_bytes() == payload



def test_distribution_without_jobs_remains_authoritative(storage):
    from hermes_cli.profile_distribution import _copy_dist_payload, DistributionManifest
    home, jobs = storage
    jobs.create_job(prompt="old", schedule="every 1h")
    lock_path = jobs.JOBS_FILE.with_name("jobs.json.lock")
    old_lock = lock_path.read_bytes()
    source = home / "distribution-without-jobs"
    (source / "cron").mkdir(parents=True)
    (source / "cron" / "daily.json").write_text("{}", encoding="utf-8")
    _copy_dist_payload(source, home, DistributionManifest(name="test"), preserve_config=True)
    assert not jobs.JOBS_FILE.exists()
    assert lock_path.read_bytes() == old_lock
    assert jobs.load_jobs() == []
    assert jobs.create_job(prompt="after distribution", schedule="every 1h")



def test_distribution_cleanup_preserves_inflight_atomic_temp(storage, monkeypatch):
    import threading
    from hermes_cli.profile_distribution import _copy_cron_payload
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    source = home / "dist-payload"
    source.mkdir()
    (source / "jobs.json").write_bytes(jobs.JOBS_FILE.read_bytes())
    cleanup_entered = threading.Event()
    temp_ready = threading.Event()
    cleanup_done = threading.Event()
    original_iterdir, original_replace = Path.iterdir, jobs.atomic_replace
    errors, created = [], []

    def observed_iterdir(path):
        if path == jobs.CRON_DIR and threading.current_thread().name == "distribution":
            cleanup_entered.set()
            assert temp_ready.wait(10)
        return original_iterdir(path)

    def paused_replace(source_path, target_path):
        if threading.current_thread().name == "creator":
            temp_ready.set()
            assert cleanup_done.wait(10)
        return original_replace(source_path, target_path)

    def distribute():
        try:
            _copy_cron_payload(source, jobs.CRON_DIR)
        except BaseException as exc:
            errors.append(exc)
        finally:
            cleanup_done.set()

    def create():
        try:
            created.append(jobs.create_job(prompt="concurrent", schedule="every 2h"))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(Path, "iterdir", observed_iterdir)
    monkeypatch.setattr(jobs, "atomic_replace", paused_replace)
    distribution = threading.Thread(name="distribution", target=distribute)
    creator = threading.Thread(name="creator", target=create)
    try:
        distribution.start()
        assert cleanup_entered.wait(10)
        creator.start()
    finally:
        for worker in (distribution, creator):
            if worker.ident is not None:
                worker.join(10)
                assert not worker.is_alive()
    assert not errors and len(created) == 1
    assert {job["id"] for job in jobs.load_jobs()} == {existing["id"], created[0]["id"]}
