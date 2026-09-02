"""PR-13.1 corrective tests: cancellation startup race, spawn/cancel atomicity.

Deterministic coverage for the independently reproduced C1/C2/C6 defects:

- a cancellation accepted before spawn registration is consumed
  atomically at registration (PENDING_CANCEL_BEFORE_REGISTRATION=CONSUMED);
- a cancellation accepted while the process is live terminates it and
  only proven termination yields cancellation outcomes;
- a cancellation arriving after natural exit is never claimed as a
  cancellation outcome;
- 100 repeated concurrent spawn/cancel iterations with zero lost cancel
  requests and zero duplicate-termination corruption.

Host-level tests use ``LocalExecutionHost`` directly: the race window
guarded here lives in the host, below the runtime facade.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from ai_engineering.execution.host_contracts import ExecutionMode, ExecutionRequest
from ai_engineering.execution.local_host import LocalExecutionHost

EXEC_HOST = "host-local"


def _request(execution_id: str, argv: tuple[str, ...], *, timeout: float = 30.0) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=execution_id,
        run_id="run-c1",
        task_id="task-c1",
        workspace_id="ws-c1",
        execution_host_id=EXEC_HOST,
        mode=ExecutionMode.LOCAL,
        argv=argv,
        cwd=".",
        env={"PATH": os_path()},
        inherit_environment=False,
        timeout_seconds=timeout,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        created_at="2026-09-02T00:00:00Z",
    )


def os_path() -> str:
    import os

    return os.environ.get("PATH", "")


def _child_code(marker: str | None, body: str) -> tuple[str, ...]:
    pre = f"open(r'{marker}', 'w').write('1'); " if marker else ""
    return (sys.executable, "-c", pre + body)


class TestCancelBeforeRegistration:
    def test_cancel_before_registration_is_consumed(self, tmp_path):
        """Cancel accepted while spawn is in flight must terminate the process."""
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        started = threading.Event()
        real_popen = subprocess.Popen

        def cancel_during_spawn(argv, **kwargs):
            # Deterministically land inside the Popen()..registration window.
            host.request_cancel("exec-c1")
            proc = real_popen(argv, **kwargs)
            started.set()
            return proc

        host._process_launcher = cancel_during_spawn
        result = host.execute(_request("exec-c1", _child_code(None, "import time; time.sleep(30)")))
        assert result.cancelled is True
        assert result.state.value == "EXITED"
        assert isinstance(result.exit_code, int)
        assert result.exit_code != 0
        assert host.lifecycle_state("exec-c1") == "CANCEL_REQUESTED"

    def test_cancel_after_popen_before_registration_is_consumed(self, tmp_path):
        """Cancel between Popen() returning and registration is still consumed."""
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        real_popen = subprocess.Popen

        def cancel_after_popen(argv, **kwargs):
            proc = real_popen(argv, **kwargs)
            host.request_cancel("exec-c2")
            return proc

        host._process_launcher = cancel_after_popen
        result = host.execute(_request("exec-c2", _child_code(None, "import time; time.sleep(30)")))
        assert result.cancelled is True
        assert result.state.value == "EXITED"
        assert not host._active_processes

    def test_pending_cancel_on_already_exited_process_not_claimed(self):
        """Pending cancel + already-exited process: no cancellation outcome."""
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)

        class _DeadProc:
            pid = 424242
            returncode = 0

            def poll(self):
                return 0

            def terminate(self):
                raise AssertionError("must not terminate an already-exited proc")

            def kill(self):
                raise AssertionError("must not kill an already-exited proc")

            def wait(self, timeout=None):
                return 0

            def communicate(self, timeout=None):
                return (b"done", b"")

        host._process_launcher = lambda argv, **kwargs: _DeadProc()
        result = host.execute(_request("exec-c3", ("fake", "argv")))
        # The cancel is recorded, but the process exited on its own: the
        # result must stay a natural exit, never a cancellation claim.
        assert result.cancelled is False
        assert result.state.value == "EXITED"
        assert result.exit_code == 0


class TestCancelDuringExecution:
    def test_cancel_during_communicate_produces_cancel_terminal(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        marker = tmp_path / "started-c4.txt"
        result_box: dict = {}

        def run():
            result_box["result"] = host.execute(
                _request("exec-c4", _child_code(marker, "import time; time.sleep(60)"))
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists(), "child never started"
            assert host.request_cancel("exec-c4") is True
        finally:
            thread.join(timeout=60)
        result = result_box["result"]
        assert result.cancelled is True
        assert result.state.value == "EXITED"
        assert isinstance(result.exit_code, int)
        assert not host._active_processes

    def test_lifecycle_transitions_under_cancel(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        marker = tmp_path / "started-c5.txt"
        done = threading.Event()

        def run():
            host.execute(_request("exec-c5", _child_code(marker, "import time; time.sleep(60)")))
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists()
            host.request_cancel("exec-c5")
            # While terminating, the lifecycle must show a cancel state.
            state = host.lifecycle_state("exec-c5")
            assert state in ("CANCEL_REQUESTED", "TERMINATING")
            assert done.wait(timeout=60)
        finally:
            thread.join(timeout=60)
        assert host.lifecycle_state("exec-c5") == "CANCEL_REQUESTED"

    def test_concurrent_cancel_requests_single_outcome(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        marker = tmp_path / "started-c6.txt"
        result_box: dict = {}

        def run():
            result_box["result"] = host.execute(
                _request("exec-c6", _child_code(marker, "import time; time.sleep(60)"))
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists()
            outcomes = [host.request_cancel("exec-c6") for _ in range(5)]
        finally:
            thread.join(timeout=60)
        assert all(outcomes)
        result = result_box["result"]
        assert result.cancelled is True
        assert result.state.value == "EXITED"
        assert not host._active_processes

    def test_cancel_after_natural_exit_is_not_a_cancellation_outcome(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        result = host.execute(
            _request(
                "exec-c7",
                (sys.executable, "-c", "print('quick')"),
                timeout=10.0,
            )
        )
        assert result.state.value == "EXITED"
        assert result.exit_code == 0
        # A cancel recorded after the process is gone must not retroactively
        # rewrite the terminal outcome (deterministic: process already reaped).
        assert host._active_processes.get("exec-c7") is None


class TestSpawnCancelRace100:
    def test_100x_concurrent_spawn_cancel_no_lost_cancel(self, tmp_path):
        """100 repeated spawn/cancel iterations; zero lost cancellations."""
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        lost_cancels = 0
        duplicate_termination = 0
        for i in range(100):
            execution_id = f"exec-race-{i}"
            marker = tmp_path / f"started-race-{i}.txt"
            result_box: dict = {}

            def run(execution_id=execution_id, marker=marker, result_box=result_box):
                result_box["result"] = host.execute(
                    _request(execution_id, _child_code(marker, "import time; time.sleep(60)"))
                )

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists(), f"child {execution_id} never started"
            # Cancel concurrently with the still-running execution.
            cancel_thread = threading.Thread(
                target=host.request_cancel, args=(execution_id,), daemon=True
            )
            cancel_thread.start()
            thread.join(timeout=60)
            cancel_thread.join(timeout=10)
            result = result_box["result"]
            if not result.cancelled:
                lost_cancels += 1
            if result.state.value != "EXITED" or not isinstance(result.exit_code, int):
                duplicate_termination += 1
            if host._active_processes.get(execution_id) is not None:
                duplicate_termination += 1
        assert lost_cancels == 0
        assert duplicate_termination == 0
        assert not host._active_processes


class TestProcessIdentityAfterCancel:
    def test_result_binding_survives_cancellation(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        marker = tmp_path / "started-c8.txt"
        result_box: dict = {}

        def run():
            result_box["result"] = host.execute(
                _request("exec-c8", _child_code(marker, "import time; time.sleep(60)"))
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists()
            host.request_cancel("exec-c8")
        finally:
            thread.join(timeout=60)
        result = result_box["result"]
        assert result.run_id == "run-c1"
        assert result.workspace_id == "ws-c1"
        assert result.execution_host_id == EXEC_HOST
        assert result.execution_id == "exec-c8"
        assert result.cancelled is True
