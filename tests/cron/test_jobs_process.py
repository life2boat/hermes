"""Cross-process cron storage regressions using real public writers and spawn."""

import errno
import json
import multiprocessing
import os
from pathlib import Path

import pytest


def _configure(home):
    os.environ["HERMES_HOME"] = str(home)
    from cron import jobs
    jobs.HERMES_DIR = Path(home)
    jobs.CRON_DIR = Path(home) / "cron"
    jobs.JOBS_FILE = jobs.CRON_DIR / "jobs.json"
    jobs.OUTPUT_DIR = jobs.CRON_DIR / "output"
    return jobs


def _report_contention(pipe):
    """Observe real kernel lock contention; never bypass the production lock."""
    if os.name == "nt":
        import msvcrt
        original = msvcrt.locking
        def observed(fd, mode, length):
            try:
                return original(fd, mode, length)
            except OSError as exc:
                if mode == msvcrt.LK_NBLCK and exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    pipe.send(("blocked", None))
                raise
        msvcrt.locking = observed
    else:
        import fcntl
        original = fcntl.flock
        def observed(fd, mode):
            try:
                return original(fd, mode)
            except OSError as exc:
                if mode & fcntl.LOCK_NB and exc.errno in {errno.EACCES, errno.EAGAIN}:
                    pipe.send(("blocked", None))
                raise
        fcntl.flock = observed


def _paused_scheduler(home, job_id, pipe, release, mutation="mark"):
    jobs = _configure(home)
    original = jobs.load_jobs
    paused = False
    def load_then_pause():
        nonlocal paused
        snapshot = original()
        if not paused:
            paused = True
            pipe.send(("snapshot", None))
            if not release.wait(20):
                raise TimeoutError("scheduler snapshot was not released")
        return snapshot
    jobs.load_jobs = load_then_pause
    if mutation == "repair":
        original_save = jobs.save_jobs
        def save_then_pause(snapshot):
            pipe.send(("snapshot", None))
            if not release.wait(20):
                raise TimeoutError("repair was not released")
            original_save(snapshot)
        jobs.load_jobs = original
        jobs.save_jobs = save_then_pause
    try:
        if mutation == "mark":
            jobs.mark_job_run(job_id, success=True)
        elif mutation == "advance":
            assert jobs.advance_next_run(job_id)
        elif mutation == "due":
            jobs.get_due_jobs()
        elif mutation == "rewrite":
            assert jobs.rewrite_skill_refs(consolidated={"old": "new"}, pruned=[])["jobs_updated"] == 1
        elif mutation == "curator":
            from agent.curator_backup import _restore_cron_skill_links
            report = _restore_cron_skill_links(Path(home) / "curator-restore")
            assert report["error"] is None and report["restored"]
        elif mutation in {"cli", "cli_skills"}:
            from argparse import Namespace
            from hermes_cli.cron import cron_command
            args = Namespace(cron_command="edit", job_id=job_id, name="first-cli")
            if mutation == "cli_skills":
                args.add_skills = ["first"]
            assert cron_command(args) == 0
        else:
            jobs.load_jobs()
        pipe.send(("completed", None))
    except BaseException as exc:
        pipe.send(("error", type(exc).__name__))
        raise


def _contending_writer(home, pipe, action, job_id=None):
    jobs = _configure(home)
    _report_contention(pipe)
    try:
        if action == "create":
            result = jobs.create_job(prompt="new", schedule="every 2h")
        elif action == "delete":
            result = jobs.remove_job(job_id)
        elif action == "tool_update":
            from tools.cronjob_tools import cronjob
            payload = json.loads(cronjob(action="update", job_id=job_id, repeat=20))
            assert payload["success"]
            result = jobs.get_job(job_id)
        elif action == "cli_create":
            from argparse import Namespace
            from hermes_cli.cron import cron_command
            assert cron_command(Namespace(cron_command="create", prompt="cli-new", schedule="every 2h", name="cli-new")) == 0
            result = next(job for job in jobs.load_jobs() if job["name"] == "cli-new")
        elif action == "cli_skills":
            from argparse import Namespace
            from hermes_cli.cron import cron_command
            assert cron_command(Namespace(cron_command="edit", job_id=job_id, add_skills=["second"])) == 0
            result = jobs.get_job(job_id)
        elif action == "dashboard_create":
            from hermes_cli import web_server
            web_server._cron_profile_home = lambda _: ("test-profile", Path(home))
            result = web_server._call_cron_for_profile("test-profile", "create_job", prompt="web-new", schedule="every 2h")
        else:
            result = jobs.update_job(job_id, {"name": "edited", "prompt": "edited prompt"})
        pipe.send(("completed", result))
    except BaseException as exc:
        pipe.send(("error", type(exc).__name__))
        raise


def _receive(pipe):
    assert pipe.poll(20), "worker did not report progress"
    return pipe.recv()


def _run_interleaving(home, job_id, action, mutation="mark"):
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Event()
    first_parent, first_child = ctx.Pipe(duplex=False)
    second_parent, second_child = ctx.Pipe(duplex=False)
    first = ctx.Process(target=_paused_scheduler, args=(str(home), job_id, first_child, release, mutation))
    second = ctx.Process(target=_contending_writer, args=(str(home), second_child, action, job_id))
    try:
        first.start()
        assert _receive(first_parent)[0] == "snapshot"
        second.start()
        # V1: second public API saves while first still holds its old snapshot.
        # V2: real kernel contention is observed before first is released.
        stage, result = _receive(second_parent)
        assert stage in {"completed", "blocked"}, (stage, result)
        release.set()
        if stage == "blocked":
            while True:
                message, result = _receive(second_parent)
                if message != "blocked":
                    assert message == "completed", (message, result)
                    break
        assert _receive(first_parent)[0] == "completed"
        return stage, result
    finally:
        release.set()
        for worker in (first, second):
            if worker.pid is not None:
                worker.join(20)
                assert not worker.is_alive(), "worker deadlocked"
                assert worker.exitcode == 0
                worker.close()
        for pipe in (first_parent, first_child, second_parent, second_child):
            pipe.close()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    from cron import jobs
    monkeypatch.setattr(jobs, "HERMES_DIR", home)
    monkeypatch.setattr(jobs, "CRON_DIR", home / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", home / "cron" / "output")
    return home, jobs


def test_mp1_create_vs_stale_scheduler(storage):
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    stage, created = _run_interleaving(home, existing["id"], "create")
    persisted = {job["id"]: job for job in jobs.load_jobs()}
    assert created["id"] in persisted, f"new job lost after contender {stage}"
    assert persisted[existing["id"]]["repeat"]["completed"] == 1



def test_mp2_delete_vs_scheduler(storage):
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    stage, removed = _run_interleaving(home, existing["id"], "delete")
    assert stage == "blocked" and removed is True
    assert jobs.get_job(existing["id"]) is None


@pytest.mark.parametrize("action", ["update", "tool_update"])
def test_mp3_runtime_fields_survive_crud(storage, action):
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    stage, _ = _run_interleaving(home, existing["id"], action)
    assert stage == "blocked"
    persisted = jobs.get_job(existing["id"])
    assert persisted["repeat"]["completed"] == 1
    assert persisted["last_status"] == "ok"
    if action == "update":
        assert persisted["name"] == "edited" and persisted["prompt"] == "edited prompt"
    else:
        assert persisted["repeat"]["times"] == 20


@pytest.mark.parametrize("mutation", ["advance", "due", "rewrite", "curator", "repair"])
def test_remaining_rmw_writers_vs_create(storage, mutation):
    from datetime import timedelta
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h", skills=["old"])
    if mutation == "due":
        jobs.update_job(existing["id"], {"next_run_at": (jobs._hermes_now() - timedelta(hours=3)).isoformat()})
    if mutation == "curator":
        backup = home / "curator-restore"
        backup.mkdir()
        (backup / "cron-jobs.json").write_text(json.dumps({"jobs": [{"id": existing["id"], "skills": ["restored"], "skill": "restored"}]}), encoding="utf-8")
    if mutation == "repair":
        jobs.JOBS_FILE.write_text(json.dumps([existing]), encoding="utf-8")
    stage, created = _run_interleaving(home, existing["id"], "create", mutation)
    assert stage == "blocked"
    persisted = {job["id"]: job for job in jobs.load_jobs()}
    assert created["id"] in persisted and existing["id"] in persisted
    if mutation in {"rewrite", "curator"}:
        assert persisted[existing["id"]]["skills"] == (["new"] if mutation == "rewrite" else ["restored"])


@pytest.mark.parametrize("mutation,action", [("mark", "cli_create"), ("mark", "dashboard_create"), ("cli", "dashboard_create"), ("cli", "cli_create")])
def test_gateway_cli_dashboard_topology(storage, mutation, action):
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    stage, created = _run_interleaving(home, existing["id"], action, mutation)
    assert stage == "blocked"
    persisted = {job["id"]: job for job in jobs.load_jobs()}
    assert created["id"] in persisted
    if mutation == "mark":
        assert persisted[existing["id"]]["repeat"]["completed"] == 1
    else:
        assert persisted[existing["id"]]["name"] == "first-cli"


def _increment_runs(home, job_id, start, pipe):
    jobs = _configure(home)
    start.wait(timeout=20)
    for _ in range(4):
        jobs.mark_job_run(job_id, success=True)
    pipe.send(("completed", None))


def test_mp4_independent_public_api_processes(storage):
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Barrier(2)
    pipes = [ctx.Pipe(duplex=False) for _ in range(2)]
    workers = [ctx.Process(target=_increment_runs, args=(str(home), existing["id"], start, child)) for _, child in pipes]
    try:
        for worker in workers:
            worker.start()
        for parent, _ in pipes:
            assert _receive(parent)[0] == "completed"
    finally:
        for worker in workers:
            worker.join(20)
            assert not worker.is_alive() and worker.exitcode == 0
            worker.close()
        for pair in pipes:
            for pipe in pair:
                pipe.close()
    assert jobs.get_job(existing["id"])["repeat"]["completed"] == 8


def _hold_storage(home, pipe, release):
    jobs = _configure(home)
    with jobs.jobs_transaction():
        pipe.send(("held", None))
        assert release.wait(20)
    pipe.send(("completed", None))


@pytest.mark.parametrize("same_profile", [False, True])
def test_mp5_mp6_profile_lock_mapping(storage, tmp_path, same_profile):
    home, jobs = storage
    ctx = multiprocessing.get_context("spawn")
    release = ctx.Event()
    owner_pipe, owner_child = ctx.Pipe(duplex=False)
    writer_pipe, writer_child = ctx.Pipe(duplex=False)
    other_home = home if same_profile else tmp_path / "other-profile"
    owner = ctx.Process(target=_hold_storage, args=(str(home), owner_child, release))
    writer = ctx.Process(target=_contending_writer, args=(str(other_home), writer_child, "create"))
    try:
        owner.start()
        assert _receive(owner_pipe)[0] == "held"
        writer.start()
        stage, _ = _receive(writer_pipe)
        assert stage == ("blocked" if same_profile else "completed")
        # Another profile finishes while the first profile is still held.
        release.set()
        if same_profile:
            while _receive(writer_pipe)[0] == "blocked":
                pass
        assert _receive(owner_pipe)[0] == "completed"
    finally:
        release.set()
        for worker in (owner, writer):
            worker.join(20)
            assert not worker.is_alive() and worker.exitcode == 0
            worker.close()
        for pipe in (owner_pipe, owner_child, writer_pipe, writer_child):
            pipe.close()
    assert (other_home / "cron" / "jobs.json.lock").exists()
    assert bool(jobs.load_jobs()) == same_profile


def test_mp7_killed_owner_releases_kernel_lock(storage):
    home, jobs = storage
    ctx = multiprocessing.get_context("spawn")
    owner_pipe, owner_child = ctx.Pipe(duplex=False)
    writer_pipe, writer_child = ctx.Pipe(duplex=False)
    owner = ctx.Process(target=_hold_storage, args=(str(home), owner_child, ctx.Event()))
    writer = ctx.Process(target=_contending_writer, args=(str(home), writer_child, "create"))
    try:
        owner.start()
        assert _receive(owner_pipe)[0] == "held"
        writer.start()
        assert _receive(writer_pipe)[0] == "blocked"
        owner.terminate()  # Only this test-owned disposable child, never user processes.
        owner.join(20)
        assert not owner.is_alive() and owner.exitcode != 0
        while True:
            stage, created = _receive(writer_pipe)
            if stage != "blocked":
                assert stage == "completed"
                break
        assert jobs.get_job(created["id"]) is not None
    finally:
        for worker in (owner, writer):
            worker.join(20)
            assert not worker.is_alive()
            worker.close()
        for pipe in (owner_pipe, owner_child, writer_pipe, writer_child):
            pipe.close()


def test_nested_transaction_repair_and_path_inversion(storage, tmp_path):
    _, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    jobs.JOBS_FILE.write_text(json.dumps([existing]), encoding="utf-8")
    with jobs.jobs_transaction():
        with jobs.jobs_transaction(jobs.JOBS_FILE.parent / "." / "jobs.json"):
            assert jobs.load_jobs()[0]["id"] == existing["id"]
            jobs.update_job(existing["id"], {"name": "nested"})
        with pytest.raises(RuntimeError, match="different storage paths"):
            with jobs.jobs_transaction(tmp_path / "different" / "jobs.json"):
                pytest.fail("cross-path nesting must fail closed")
    assert jobs.get_job(existing["id"])["name"] == "nested"
    assert not (tmp_path / "different").exists()


def test_exception_releases_process_lock(storage, monkeypatch):
    home, jobs = storage
    def broken_replace(*args):
        raise OSError("simulated atomic replace error")
    with monkeypatch.context() as patcher:
        patcher.setattr(jobs, "atomic_replace", broken_replace)
        with pytest.raises(OSError, match="simulated"):
            jobs.create_job(prompt="fail", schedule="every 1h")
    assert list(jobs.CRON_DIR.glob(".jobs_*.tmp")) == []
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    worker = ctx.Process(target=_contending_writer, args=(str(home), child, "create"))
    worker.start()
    try:
        stage, created = _receive(parent)
        assert stage == "completed"
    finally:
        worker.join(20)
        assert not worker.is_alive() and worker.exitcode == 0
        worker.close()
        parent.close(); child.close()
    assert jobs.get_job(created["id"]) is not None


def test_tick_releases_jobs_transaction_before_execution(storage, monkeypatch):
    import threading
    from cron import scheduler
    home, jobs = storage
    existing = jobs.create_job(prompt="run", schedule="every 1h")
    jobs.trigger_job(existing["id"])
    created = []
    def run_job(job):
        assert getattr(jobs._jobs_transaction_state, "path", None) is None
        worker = threading.Thread(target=lambda: created.append(jobs.create_job(prompt="during inference", schedule="every 2h")))
        worker.start()
        worker.join(5)
        assert not worker.is_alive(), "tick held storage lock during agent execution"
        return True, "local output", "[SILENT]", None
    monkeypatch.setattr(scheduler, "run_job", run_job)
    import sys
    from types import ModuleType
    mcp_stub = ModuleType("tools.mcp_tool")
    mcp_stub._kill_orphaned_mcp_children = lambda: None
    monkeypatch.setitem(sys.modules, "tools.mcp_tool", mcp_stub)
    assert scheduler.tick(verbose=False, sync=True) == 1
    assert len(created) == 1 and jobs.get_job(created[0]["id"]) is not None



def test_lock_backend_error_fails_closed_and_can_retry(storage, monkeypatch):
    _, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    if os.name == "nt":
        import msvcrt as backend
        name = "locking"
    else:
        import fcntl as backend
        name = "flock"
    with monkeypatch.context() as patcher:
        def broken(*args):
            raise OSError(errno.EIO, "simulated unavailable lock backend")
        patcher.setattr(backend, name, broken)
        with pytest.raises(OSError, match="simulated unavailable"):
            jobs.update_job(existing["id"], {"name": "must not persist"})
    assert jobs.get_job(existing["id"])["name"] == existing["name"]
    assert jobs.update_job(existing["id"], {"name": "retry"})["name"] == "retry"


def test_posix_lock_branch_retries_and_reuses_nested_lock(storage, monkeypatch):
    import sys
    from types import SimpleNamespace
    _, jobs = storage
    calls = []
    def flock(fd, flags):
        calls.append(flags)
        if len(calls) == 1:
            raise BlockingIOError(errno.EAGAIN, "simulated contention")
    fake_fcntl = SimpleNamespace(flock=flock, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
    class PosixOS:
        name = "posix"
        def __getattr__(self, key):
            return getattr(os, key)
    monkeypatch.setattr(jobs, "os", PosixOS())
    monkeypatch.setitem(sys.modules, "fcntl", fake_fcntl)
    with jobs.jobs_transaction():
        with jobs.jobs_transaction():
            jobs.create_job(prompt="nested", schedule="every 1h")
    assert calls == [6, 6, 8]



def test_two_cli_derived_skill_updates_preserve_both(storage):
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h", skills=["original"])
    stage, _ = _run_interleaving(home, existing["id"], "cli_skills", "cli_skills")
    assert stage == "blocked"
    assert jobs.get_job(existing["id"])["skills"] == ["original", "first", "second"]


def test_public_writer_refreshes_snapshot_created_before_acquisition(storage):
    home, jobs = storage
    existing = jobs.create_job(prompt="base", schedule="every 1h")
    old_snapshot = jobs.load_jobs()
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    writer = ctx.Process(target=_contending_writer, args=(str(home), child, "create"))
    try:
        writer.start()
        stage, created = _receive(parent)
        assert stage == "completed"
        jobs.mark_job_run(old_snapshot[0]["id"], success=True)
    finally:
        writer.join(20)
        assert not writer.is_alive() and writer.exitcode == 0
        writer.close()
        parent.close()
        child.close()
    assert jobs.get_job(created["id"]) is not None
    assert jobs.get_job(existing["id"])["repeat"]["completed"] == 1


def test_dashboard_retarget_excludes_non_dashboard_threads(storage, tmp_path, monkeypatch):
    import threading
    from hermes_cli import web_server
    home, jobs = storage
    profile_home = tmp_path / "dashboard-profile"
    original_lock = jobs._jobs_file_lock
    retargeted = threading.Event()
    blocked = threading.Event()
    release = threading.Event()
    results, errors = {}, []

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread().name == "ordinary-cron":
                if not original_lock.acquire(blocking=False):
                    blocked.set()
                    original_lock.acquire()
            else:
                original_lock.acquire()
            return self
        def __exit__(self, *args):
            original_lock.release()

    def paused_create():
        assert jobs.JOBS_FILE == profile_home / "cron" / "jobs.json"
        retargeted.set()
        assert release.wait(10)
        return jobs.create_job(prompt="dashboard", schedule="every 1h")

    def run(name, function):
        try:
            results[name] = function()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(jobs, "_jobs_file_lock", ObservedLock())
    monkeypatch.setattr(jobs, "paused_create", paused_create, raising=False)
    monkeypatch.setattr(web_server, "_cron_profile_home", lambda _: ("worker", profile_home))
    dashboard = threading.Thread(target=run, args=("dashboard", lambda: web_server._call_cron_for_profile("worker", "paused_create")))
    ordinary = threading.Thread(name="ordinary-cron", target=run, args=("ordinary", lambda: jobs.create_job(prompt="ordinary", schedule="every 2h")))
    try:
        dashboard.start()
        assert retargeted.wait(10)
        ordinary.start()
        assert blocked.wait(10), "ordinary writer accessed temporarily retargeted globals"
    finally:
        release.set()
        for worker in (dashboard, ordinary):
            if worker.ident is not None:
                worker.join(10)
                assert not worker.is_alive()
    assert not errors
    assert jobs.JOBS_FILE == home / "cron" / "jobs.json"
    assert [job["id"] for job in jobs.load_jobs()] == [results["ordinary"]["id"]]
    profile_jobs = json.loads((profile_home / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert [job["id"] for job in profile_jobs] == [results["dashboard"]["id"]]


def test_tool_invalid_action_retains_json_error_contract(storage):
    from tools.cronjob_tools import cronjob
    payload = json.loads(cronjob(action=123))
    assert payload["success"] is False and "error" in payload
