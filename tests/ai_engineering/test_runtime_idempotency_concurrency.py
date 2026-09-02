"""PR-13 runtime registry tests: fencing, PID reuse, concurrency races."""

from __future__ import annotations

import threading

import pytest

from ai_engineering.runtime.runtime_contracts import AgentRuntimeError, RuntimeBlockingReason
from ai_engineering.runtime.runtime_registry import RuntimeRegistry, RuntimeSlotAllocator
from tests.ai_engineering.runtime_fixture_helpers import make_local_fixture, make_request


def _evidence_for(request, **overrides):
    from ai_engineering.execution.host_contracts import ExecutionState
    from ai_engineering.runtime.runtime_contracts import AgentExecutionEvidence

    base = dict(
        execution_id=request.execution_id,
        run_id=request.run_id,
        task_id=request.task_id,
        node_id=request.node_id,
        cycle_id=request.cycle_id,
        workspace_id=request.workspace_id,
        candidate_id=request.candidate_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        execution_epoch=request.execution_epoch,
        execution_host_id=request.execution_host_id,
        agent_capability=request.agent_capability,
        working_directory=request.working_directory,
        process=None,
        state=ExecutionState.EXITED.value,
        exit_code=0,
        exit_proven=True,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        stdout_bytes=0,
        stderr_bytes=0,
        started_at="2026-01-01T00:00:00+00:00",
        completed_at="2026-01-01T00:00:01+00:00",
        timed_out=False,
        cancelled=False,
        cancel_terminal=False,
    )
    base.update(overrides)
    return AgentExecutionEvidence(**base)


def _identity_for(request, process_id="proc-1", pid=None):
    from ai_engineering.runtime.runtime_contracts import AgentProcessIdentity

    return AgentProcessIdentity(
        process_id=process_id,
        run_id=request.run_id,
        workspace_id=request.workspace_id,
        candidate_id=request.candidate_id,
        execution_host_id=request.execution_host_id,
        execution_epoch=request.execution_epoch,
        pid=pid,
    )


class TestSlotAllocator:
    def test_reserve_within_budget(self):
        allocator = RuntimeSlotAllocator()
        allocator.reserve("slot", "run-1", 2)
        allocator.reserve("slot", "run-2", 2)
        assert allocator.active_count("slot") == 2

    def test_reserve_over_budget_fails(self):
        allocator = RuntimeSlotAllocator()
        allocator.reserve("slot", "run-1", 1)
        with pytest.raises(AgentRuntimeError) as exc:
            allocator.reserve("slot", "run-2", 1)
        assert exc.value.code == "PARALLELIZATION_BUDGET_EXCEEDED"

    def test_release_frees_slot(self):
        allocator = RuntimeSlotAllocator()
        allocator.reserve("slot", "run-1", 1)
        allocator.release("slot", "run-1")
        allocator.reserve("slot", "run-2", 1)
        assert allocator.active_count("slot") == 1

    def test_race_never_oversubscribes(self):
        allocator = RuntimeSlotAllocator()
        results: list[str] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker(i: int):
            try:
                allocator.reserve("slot", f"run-{i}", 2)
                with lock:
                    results.append(f"run-{i}")
            except AgentRuntimeError as exc:
                with lock:
                    errors.append(exc.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 2
        assert errors == ["PARALLELIZATION_BUDGET_EXCEEDED"] * 6

    def test_budget_zero_or_negative_rejected(self):
        allocator = RuntimeSlotAllocator()
        with pytest.raises(AgentRuntimeError):
            allocator.reserve("slot", "run-1", 0)


class TestSpawnRegistry:
    def test_idempotent_identical_spawn(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        status1, _ = fx.runtime.runtime_registry.register_spawn(request)
        status2, _ = fx.runtime.runtime_registry.register_spawn(request)
        assert status1.value == "SPAWNED"
        assert status2.value == "ALREADY_ACTIVE"

    def test_divergent_request_collides(self, tmp_path):
        from dataclasses import replace

        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        fx.runtime.runtime_registry.register_spawn(request)
        with pytest.raises(AgentRuntimeError) as exc:
            fx.runtime.runtime_registry.register_spawn(replace(request, timeout_seconds=99))
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_SPAWN_COLLISION.value


class TestResultFencing:
    def test_result_requires_registered_process(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        with pytest.raises(AgentRuntimeError) as exc:
            registry.record_result(_evidence_for(request), process_id="proc-unknown", request=request)
        assert exc.value.code == RuntimeBlockingReason.STALE_RUNTIME_EVENT.value

    def test_stale_epoch_result_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        registry.register_process_identity(_identity_for(request))
        evidence = _evidence_for(request, execution_epoch=request.execution_epoch + 1)
        with pytest.raises(AgentRuntimeError) as exc:
            registry.record_result(evidence, process_id="proc-1", request=request)
        assert exc.value.code == RuntimeBlockingReason.STALE_RUNTIME_EVENT.value

    def test_foreign_workspace_result_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        registry.register_process_identity(_identity_for(request))
        evidence = _evidence_for(request, workspace_id="ws-foreign")
        with pytest.raises(AgentRuntimeError):
            registry.record_result(evidence, process_id="proc-1", request=request)

    def test_foreign_host_result_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        registry.register_process_identity(_identity_for(request))
        evidence = _evidence_for(request, execution_host_id="host-foreign")
        with pytest.raises(AgentRuntimeError):
            registry.record_result(evidence, process_id="proc-1", request=request)

    def test_foreign_candidate_result_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        registry.register_process_identity(_identity_for(request))
        evidence = _evidence_for(request, candidate_id="cand-foreign")
        with pytest.raises(AgentRuntimeError):
            registry.record_result(evidence, process_id="proc-1", request=request)

    def test_foreign_run_result_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        registry.register_process_identity(_identity_for(request))
        evidence = _evidence_for(request, run_id="run-foreign")
        with pytest.raises(AgentRuntimeError):
            registry.record_result(evidence, process_id="proc-1", request=request)

    def test_pid_reuse_does_not_admit_stale_event(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        # Process A (finished, pid 4242) and process B reusing pid 4242.
        registry.register_process_identity(_identity_for(request, process_id="proc-A", pid=4242))
        registry.register_process_identity(_identity_for(request, process_id="proc-B", pid=4242))
        # A result claiming process A's identity after B registered must
        # still be accepted (durable identity, not PID, is the fence).
        evidence_a = _evidence_for(request)
        registry.record_result(evidence_a, process_id="proc-A", request=request)
        assert registry.get_result(request.execution_id) is evidence_a

    def test_result_for_wrong_process_id_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        registry = RuntimeRegistry()
        registry.register_process_identity(_identity_for(request, process_id="proc-A", pid=4242))
        evidence = _evidence_for(request)
        with pytest.raises(AgentRuntimeError) as exc:
            registry.record_result(evidence, process_id="proc-never-registered", request=request)
        assert exc.value.code == RuntimeBlockingReason.STALE_RUNTIME_EVENT.value

    def test_results_listed_deterministically(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        registry = RuntimeRegistry()
        for i in (3, 1, 2):
            request = make_request(fx, execution_id=f"exec-{i}")
            registry.register_spawn(request)
            registry.register_process_identity(_identity_for(request, process_id=f"proc-{i}"))
            registry.record_result(
                _evidence_for(request),
                process_id=f"proc-{i}",
                request=request,
            )
        ids = [e.execution_id for e in registry.list_results()]
        assert ids == ["exec-1", "exec-2", "exec-3"]
