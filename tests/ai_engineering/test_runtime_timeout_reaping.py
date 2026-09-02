"""PR-13.1 corrective tests: timeout process-tree termination & deterministic reaping.

Covers C3/C4/C5:

- timeout: terminate -> bounded graceful wait -> forced kill -> bounded
  reap; terminal evidence only when exit is proven (TIMEOUT != VERIFIED_EXIT);
- unreprovable termination yields UNVERIFIABLE + RUNTIME_PROCESS_UNVERIFIABLE;
- no orphaned direct children after cancel/timeout/normal exit;
- POSIX process-group termination covers descendants
  (PROCESS_TREE_CONTAINMENT=PLATFORM_DEPENDENT, enforced on POSIX).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from ai_engineering.execution.host_contracts import ExecutionMode, ExecutionRequest
from ai_engineering.execution.local_host import LocalExecutionHost

EXEC_HOST = "host-local"


def _request(execution_id: str, argv: tuple[str, ...], *, timeout: float = 2.0) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=execution_id,
        run_id="run-t1",
        task_id="task-t1",
        workspace_id="ws-t1",
        execution_host_id=EXEC_HOST,
        mode=ExecutionMode.LOCAL,
        argv=argv,
        cwd=".",
        env={"PATH": os.environ.get("PATH", "")},
        inherit_environment=False,
        timeout_seconds=timeout,
        max_stdout_bytes=65536,
        max_stderr_bytes=65536,
        created_at="2026-09-02T00:00:00Z",
    )


class TestTimeoutReaping:
    def test_timeout_terminates_and_reaps_sleep_child(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        result = host.execute(
            _request("exec-t1", (sys.executable, "-c", "import time; time.sleep(30)"))
        )
        assert result.timed_out is True
        assert result.state.value == "TIMED_OUT"
        assert result.exit_code is None
        assert host.lifecycle_state("exec-t1") == "TIMED_OUT"
        assert not host._active_processes

    def test_timeout_is_never_verified_exit(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        result = host.execute(
            _request("exec-t2", (sys.executable, "-c", "import time; time.sleep(30)"))
        )
        assert result.state.value != "EXITED"
        assert result.exit_code is None

    def test_timeout_reaping_is_bounded(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        start = time.monotonic()
        host.execute(
            _request("exec-t3", (sys.executable, "-c", "import time; time.sleep(30)"))
        )
        elapsed = time.monotonic() - start
        # timeout(2) + graceful(5) + kill(5) + capture(10) + slack; must be
        # far below an unbounded hang.
        assert elapsed < 45.0

    @pytest.mark.skipif(os.name != "posix", reason="SIGTERM-ignorable children only on POSIX")
    def test_sigterm_ignoring_child_is_escalated_and_reaped(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        child = (
            sys.executable,
            "-c",
            "import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "print('ready', flush=True)\n"
            "time.sleep(30)\n",
        )
        result = host.execute(_request("exec-t4", child, timeout=1.0))
        # SIGTERM ignored -> escalation to SIGKILL still proves death, but
        # the outcome remains TIMED_OUT, never a verified exit.
        assert result.timed_out is True
        assert result.exit_code is None
        assert "ready" in result.stdout
        assert not host._active_processes

    def test_unprovable_termination_is_unverifiable(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)

        class _ImmortalProc:
            pid = 123456

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

        host._process_launcher = lambda argv, **kwargs: _ImmortalProc()
        result = host.execute(_request("exec-t5", ("fake", "child")))
        assert result.state.value == "UNVERIFIABLE"
        assert "RUNTIME_PROCESS_UNVERIFIABLE" in result.blockers
        assert result.exit_code is None
        assert host.lifecycle_state("exec-t5") == "UNVERIFIABLE"

    def test_fast_exit_near_boundary_completes(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        result = host.execute(
            _request(
                "exec-t6",
                (sys.executable, "-c", "print('fast')"),
                timeout=5.0,
            )
        )
        assert result.state.value == "EXITED"
        assert result.exit_code == 0
        assert result.timed_out is False

    def test_natural_exit_reaped(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        result = host.execute(
            _request("exec-t7", (sys.executable, "-c", "print('bye')"), timeout=10.0)
        )
        assert result.state.value == "EXITED"
        assert result.exit_code == 0
        assert host.lifecycle_state("exec-t7") == "EXITED"
        assert not host._active_processes

    def test_cancel_reaps_process(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        marker = tmp_path / "started-t8.txt"
        result_box: dict = {}

        def run():
            result_box["result"] = host.execute(
                _request(
                    "exec-t8",
                    (
                        sys.executable,
                        "-c",
                        f"open(r'{marker}', 'w').write('1'); import time; time.sleep(60)",
                    ),
                )
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists()
            host.request_cancel("exec-t8")
        finally:
            thread.join(timeout=60)
        assert result_box["result"].cancelled is True
        assert not host._active_processes

    def test_launch_partial_failure_leaves_no_orphan(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        result = host.execute(_request("exec-t9", ("definitely-not-a-real-binary-xyz",)))
        assert result.state.value == "FAILED"
        assert host.lifecycle_state("exec-t9") == "FAILED"
        assert not host._active_processes

    @pytest.mark.skipif(os.name != "posix", reason="process-group termination is POSIX-only")
    def test_posix_process_group_termination_covers_descendants(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        grandchild_pid_file = tmp_path / "grandchild-pid.txt"
        child = (
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            "subprocess.Popen([\n"
            "    sys.executable, '-c',\n"
            f"    \"open(r'{grandchild_pid_file}', 'w').write(str(__import__('os').getpid())); \"\n"
            "    \"import time; time.sleep(30)\"\n"
            "])\n"
            "time.sleep(30)\n",
        )
        result = host.execute(_request("exec-t10", child, timeout=1.0))
        assert result.timed_out is True
        for _ in range(100):
            if grandchild_pid_file.exists():
                break
            time.sleep(0.05)
        assert grandchild_pid_file.exists(), "grandchild never started"
        grandchild_pid = int(grandchild_pid_file.read_text().strip())
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild_pid, 0)


class TestLifecycleStates:
    def test_lifecycle_records_all_terminal_states(self):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        # EXITED
        host.execute(_request("exec-l1", (sys.executable, "-c", "pass"), timeout=10.0))
        assert host.lifecycle_state("exec-l1") == "EXITED"
        # FAILED (launch failure)
        host.execute(_request("exec-l2", ("no-such-binary-abc",)))
        assert host.lifecycle_state("exec-l2") == "FAILED"
        # TIMED_OUT
        host.execute(
            _request("exec-l3", (sys.executable, "-c", "import time; time.sleep(30)"))
        )
        assert host.lifecycle_state("exec-l3") == "TIMED_OUT"

    def test_no_lingering_children_after_full_cycle(self, tmp_path):
        host = LocalExecutionHost(execution_host_id=EXEC_HOST)
        # Normal, timeout, cancel, and failure paths all reaped.
        host.execute(_request("exec-x1", (sys.executable, "-c", "pass"), timeout=10.0))
        host.execute(_request("exec-x2", (sys.executable, "-c", "import time; time.sleep(30)")))
        marker = tmp_path / "started-x3.txt"
        box: dict = {}

        def run():
            box["result"] = host.execute(
                _request(
                    "exec-x3",
                    (
                        sys.executable,
                        "-c",
                        f"open(r'{marker}', 'w').write('1'); import time; time.sleep(60)",
                    ),
                )
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            host.request_cancel("exec-x3")
        finally:
            thread.join(timeout=60)
        host.execute(_request("exec-x4", ("no-such-binary-abc",)))
        assert not host._active_processes
        assert box["result"].cancelled is True
