"""PR-13 real local execution activation tests (SHADOW_LOCAL end-to-end)."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace

import pytest

from ai_engineering.execution.run_contracts import RunState
from ai_engineering.runtime.runtime_contracts import (
    AgentRuntimeError,
    RuntimeBlockingReason,
    RuntimeMode,
)
from tests.ai_engineering.runtime_fixture_helpers import (
    RUN_ID,
    make_local_fixture,
    make_request,
)


class TestRuntimeDisabled:
    def test_disabled_runtime_rejects_spawn(self, tmp_path):
        fx = make_local_fixture(tmp_path, mode=RuntimeMode.DISABLED)
        request = make_request(fx)
        with pytest.raises(AgentRuntimeError) as exc:
            fx.runtime.execute_agent_process(
                request,
                intent=fx.intent,
                authority=fx.authority,
                run_identity=fx.run_identity,
                candidate=fx.candidate,
            )
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_ACTIVATION_DISABLED.value


class TestLocalSpawnPass:
    def test_real_python_process_executes(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(fx, argv=(sys.executable, "-c", "print('hello')")),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.exit_proven is True
        assert evidence.exit_code == 0
        assert "hello" in evidence.stdout
        assert evidence.blockers == ()
        assert evidence.process is not None

    def test_stderr_captured(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(fx, argv=(sys.executable, "-c", "import sys; sys.stderr.write('boom')")),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.exit_code == 0
        assert "boom" in evidence.stderr

    def test_nonzero_exit_is_not_success(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(fx, argv=(sys.executable, "-c", "import sys; sys.exit(3)")),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.exit_proven is True
        assert evidence.exit_code == 3
        assert evidence.success is False

    def test_launch_failure_reports_failed_not_success(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(fx, argv=("definitely-not-a-real-binary-pr13",)),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.exit_proven is False
        assert evidence.state == "FAILED"

    def test_run_record_reaches_exited(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(fx),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        record = fx.run_registry.get_run(RUN_ID)
        assert record is not None
        assert record.state == RunState.EXITED


class TestTimeoutSemantics:
    def test_timeout_is_not_proven_exit(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(
                fx,
                argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                timeout_seconds=1.0,
            ),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.timed_out is True
        assert evidence.exit_proven is False
        assert evidence.exit_code is None
        assert evidence.success is False

    def test_timeout_marks_run_failed(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(
                fx,
                argv=(sys.executable, "-c", "import time; time.sleep(30)"),
                timeout_seconds=1.0,
                execution_id="exec-timeout",
            ),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        record = fx.run_registry.get_run(RUN_ID)
        assert record is not None
        assert record.state == RunState.FAILED


class TestCancellationSemantics:
    def test_cancel_request_is_not_terminal(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        marker = fx.workspace_manager.canonical_root.parent / "workspaces" / "ws-task-1" / "started.txt"
        result: dict = {}

        def run():
            result["evidence"] = fx.runtime.execute_agent_process(
                make_request(
                    fx,
                    argv=(
                        sys.executable,
                        "-c",
                        "open('started.txt', 'w').write('1'); import time; time.sleep(60)",
                    ),
                    timeout_seconds=30.0,
                    execution_id="exec-cancel",
                ),
                intent=fx.intent,
                authority=fx.authority,
                run_identity=fx.run_identity,
                candidate=fx.candidate,
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            # Wait until the child process is demonstrably running (marker
            # file written inside the workspace) so the cancel lands while
            # the runtime is inside host execution, past spawn.
            for _ in range(200):
                if marker.exists():
                    break
                time.sleep(0.05)
            assert marker.exists(), "child process never started"
            cancelled_record = fx.runtime.request_cancel("exec-cancel", reason="test")
            assert cancelled_record.state == RunState.CANCEL_REQUESTED
        finally:
            thread.join(timeout=60)
        evidence = result["evidence"]
        assert evidence.cancelled is True
        assert evidence.cancel_terminal is True
        assert evidence.exit_proven is True

    def test_cancel_unknown_execution_fails_closed(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            fx.runtime.request_cancel("exec-never-spawned")
        assert exc.value.code == RuntimeBlockingReason.STALE_RUNTIME_EVENT.value


class TestIdempotentSpawn:
    def test_duplicate_spawn_replays_evidence(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, execution_id="exec-idem")
        first = fx.runtime.execute_agent_process(
            request,
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        second = fx.runtime.execute_agent_process(
            request,
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert first.to_json() == second.to_json()

    def test_in_flight_duplicate_collides(self, tmp_path):
        from ai_engineering.runtime.runtime_registry import RuntimeRegistry

        fx = make_local_fixture(tmp_path)
        request = make_request(fx, execution_id="exec-inflight")
        fx.runtime.runtime_registry.register_spawn(request)
        with pytest.raises(AgentRuntimeError) as exc:
            fx.runtime.execute_agent_process(
                request,
                intent=fx.intent,
                authority=fx.authority,
                run_identity=fx.run_identity,
                candidate=fx.candidate,
            )
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_SPAWN_COLLISION.value

    def test_divergent_request_same_identity_collides(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx, execution_id="exec-div")
        fx.runtime.execute_agent_process(
            request,
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        divergent = replace(request, command_argv=(sys.executable, "-c", "print('other')"))
        with pytest.raises(AgentRuntimeError) as exc:
            fx.runtime.execute_agent_process(
                divergent,
                intent=fx.intent,
                authority=fx.authority,
                run_identity=fx.run_identity,
                candidate=fx.candidate,
            )
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_SPAWN_COLLISION.value


class TestOutputBounds:
    def test_stdout_truncation_disclosed(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(
                fx,
                argv=(sys.executable, "-c", "print('x' * 100000)"),
                max_stdout_bytes=1024,
                execution_id="exec-bounds",
            ),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.stdout_truncated is True
        assert evidence.stdout_bytes <= 1024
        assert evidence.exit_proven is True

    def test_stderr_truncation_disclosed(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        evidence = fx.runtime.execute_agent_process(
            make_request(
                fx,
                argv=(sys.executable, "-c", "import sys; sys.stderr.write('y' * 100000)"),
                max_stderr_bytes=1024,
                execution_id="exec-bounds-err",
            ),
            intent=fx.intent,
            authority=fx.authority,
            run_identity=fx.run_identity,
            candidate=fx.candidate,
        )
        assert evidence.stderr_truncated is True
        assert evidence.stderr_bytes <= 1024
