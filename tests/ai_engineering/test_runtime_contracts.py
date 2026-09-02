"""PR-13 runtime contract behavioral tests."""

from __future__ import annotations

import json

import pytest

from ai_engineering.execution.host_contracts import ExecutionState
from ai_engineering.runtime.runtime_contracts import (
    RUNTIME_SCHEMA_VERSION,
    AgentExecutionEvidence,
    AgentExecutionRequest,
    AgentProcessIdentity,
    AgentRuntimeError,
    RuntimeBlockingReason,
)
from tests.ai_engineering.runtime_fixture_helpers import make_request, make_local_fixture


class TestAgentExecutionRequestValidation:
    def test_valid_request_builds(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        assert request.execution_id == "exec-1"
        assert request.command_argv[0]

    def test_empty_argv_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, argv=())
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_COMMAND_NOT_AUTHORIZED.value

    def test_non_string_argv_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError):
            make_request(fx, argv=("python", 3))  # type: ignore[list-item]

    def test_absolute_working_directory_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, working_directory=str(tmp_path))
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value

    def test_traversal_working_directory_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, working_directory="../other")
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value

    def test_windows_drive_working_directory_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, working_directory="C:\\tmp")
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_WORKSPACE_ESCAPE.value

    def test_invalid_base_sha_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError):
            make_request(fx, base_sha="deadbeef")

    def test_invalid_authority_digest_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError) as exc:
            make_request(fx, authority_digest="not-a-digest")
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_EVIDENCE_INCOMPLETE.value

    def test_zero_timeout_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError):
            make_request(fx, timeout_seconds=0)

    def test_zero_output_limit_rejected(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        with pytest.raises(AgentRuntimeError):
            make_request(fx, max_stdout_bytes=0)

    def test_request_is_immutable(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        with pytest.raises(Exception):
            request.run_id = "other"  # type: ignore[misc]

    def test_request_json_roundtrip_is_deterministic(self, tmp_path):
        fx = make_local_fixture(tmp_path)
        request = make_request(fx)
        assert request.to_json() == json.dumps(request.to_dict(), sort_keys=True, separators=(",", ":"))
        assert json.loads(request.to_json())["schema_version"] == RUNTIME_SCHEMA_VERSION


class TestAgentProcessIdentity:
    def test_valid_identity(self):
        identity = AgentProcessIdentity(
            process_id="proc-1",
            run_id="run-1",
            workspace_id="ws-1",
            candidate_id="cand-1",
            execution_host_id="host-local",
            execution_epoch=1,
            pid=42,
            started_at="2026-01-01T00:00:00+00:00",
        )
        assert identity.pid == 42

    def test_pid_not_required(self):
        identity = AgentProcessIdentity(
            process_id="proc-1",
            run_id="run-1",
            workspace_id="ws-1",
            candidate_id="cand-1",
            execution_host_id="host-local",
            execution_epoch=1,
        )
        assert identity.pid is None

    def test_invalid_epoch_rejected(self):
        with pytest.raises(AgentRuntimeError):
            AgentProcessIdentity(
                process_id="proc-1",
                run_id="run-1",
                workspace_id="ws-1",
                candidate_id="cand-1",
                execution_host_id="host-local",
                execution_epoch=0,
            )

    def test_negative_pid_rejected(self):
        with pytest.raises(AgentRuntimeError):
            AgentProcessIdentity(
                process_id="proc-1",
                run_id="run-1",
                workspace_id="ws-1",
                candidate_id="cand-1",
                execution_host_id="host-local",
                execution_epoch=1,
                pid=-1,
            )

    def test_roundtrip_via_dict(self):
        identity = AgentProcessIdentity(
            process_id="proc-1",
            run_id="run-1",
            workspace_id="ws-1",
            candidate_id="cand-1",
            execution_host_id="host-local",
            execution_epoch=7,
            pid=99,
        )
        clone = AgentProcessIdentity.from_dict(identity.to_dict())
        assert clone == identity


def _evidence(**overrides):
    base = dict(
        execution_id="exec-1",
        run_id="run-1",
        task_id="task-1",
        node_id="node-1",
        cycle_id="cycle-1",
        workspace_id="ws-1",
        candidate_id="cand-1",
        repository_id="life2boat/hermes",
        base_sha="a" * 40,
        execution_epoch=1,
        execution_host_id="host-local",
        agent_capability="CANDIDATE_IMPLEMENTATION",
        working_directory=".",
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


class TestAgentExecutionEvidence:
    def test_success_requires_proven_zero_exit(self):
        evidence = _evidence()
        assert evidence.success is True

    def test_nonzero_exit_not_success(self):
        evidence = _evidence(exit_code=3)
        assert evidence.success is False

    def test_blockers_prevent_success(self):
        evidence = _evidence(blockers=(RuntimeBlockingReason.RUNTIME_OUTPUT_CAPTURE_FAILED.value,))
        assert evidence.success is False

    def test_exit_proven_requires_exit_code(self):
        with pytest.raises(AgentRuntimeError) as exc:
            _evidence(exit_code=None)
        assert exc.value.code == RuntimeBlockingReason.RUNTIME_PROCESS_UNVERIFIABLE.value

    def test_cancel_terminal_requires_proven_exit(self):
        with pytest.raises(AgentRuntimeError):
            _evidence(exit_code=None, exit_proven=False, cancel_terminal=True)

    def test_timeout_is_not_proven_exit(self):
        evidence = _evidence(
            state=ExecutionState.TIMED_OUT.value,
            exit_code=None,
            exit_proven=False,
            timed_out=True,
        )
        assert evidence.timed_out is True
        assert evidence.success is False

    def test_roundtrip_via_dict(self):
        evidence = _evidence(stdout="out", stderr="err", blockers=("B1",))
        clone = AgentExecutionEvidence.from_dict(evidence.to_dict())
        assert clone.stdout == "out"
        assert clone.blockers == ("B1",)
        assert clone.base_sha == evidence.base_sha

    def test_json_deterministic(self):
        evidence = _evidence()
        assert evidence.to_json() == evidence.to_json()
