import pytest
import subprocess
import json
import uuid
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import datetime

import ai_engineering.supervisor.autonomous_run as ar
import ai_engineering.supervisor.router.transports as trans
import ai_engineering.supervisor.router.adapters as adapt
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.state import SupervisorPhase
from ai_engineering.supervisor.router.router import CrossAgentRouter, AuthorityResolver
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.router.registry import AgentRegistry, AgentDefinition
from ai_engineering.supervisor.router.envelope import MessageType
from ai_engineering.contracts import EffectClass

import os

def test_cli_smoke():
    res = subprocess.run([sys.executable, "scripts/autonomous_run.py", "--help"], capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(Path.cwd())))
    assert res.returncode == 0
    assert "Hermes Autonomous Run CLI" in res.stdout

def test_cli_isolated_e2e(tmp_path: Path):
    intent_path = tmp_path / "intent.json"
    intent_path.write_text(json.dumps({
        "schema_version": 1,
        "task_id": "test-task",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "test",
        "source_repository": "repo",
        "source_main_ref": "refs/heads/main",
        "source_base_sha": "a"*40,
        "constraints": [],
        "allowed_mutations": ["code"],
        "forbidden_mutations": [],
        "stop_boundary": "LOCAL_DIFF",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None
    }))
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    worker_script = tmp_path / "worker.py"
    worker_script.write_text("""
import sys, json, uuid
req = json.loads(sys.stdin.read())
print(json.dumps({
    "schema_version": "hermes.worker-result.v1",
    "result_id": str(uuid.uuid4()),
    "task_id": req.get("task_id", ""),
    "attempt_id": req.get("attempt_id", ""),
    "worker_id": req.get("worker_id", "codex"),
    "base_sha": req.get("base_sha", ""),
    "head_sha": req.get("base_sha", ""),
    "canonical_remote": req.get("canonical_remote", ""),
    "repository": req.get("repository", ""),
    "intent_digest": req.get("task_intent_digest", ""),
    "produced_at_utc": "2026-01-01T00:00:00Z",
    "artifacts": [],
    "gate_claims": [{"claim_id":"1", "validator_id":"test", "claimed_status":"PASS", "evidence_summary":"", "produced_at_utc": "2026-01-01T00:00:00Z"}]
}))
""")

    res = subprocess.run([
        sys.executable, "scripts/autonomous_run.py",
        "--intent", str(intent_path),
        "--state-dir", str(state_dir),
        "--run-id", "test-run",
        "--worker-cmd", sys.executable, str(worker_script)
    ], capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(Path.cwd())))

    assert "GOAL_COMPLETE" in res.stdout or "STOP_SUCCESS_NOT_EVIDENCED" in res.stdout

def test_transport_layering():
    t = trans.ConfiguredLocalAgentTransport(["echo"])
    a = adapt.CodexAdapter(t)
    assert a.health()

def test_subprocess_timeout(tmp_path: Path):
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("import time; time.sleep(10)")
    t = trans.ConfiguredLocalAgentTransport([sys.executable, str(worker_script)])
    with pytest.raises(TimeoutError, match="WORK_TIMEOUT"):
        t.dispatch({"operation_id": "op-1"}, timeout=0.1)
    assert not t.is_running("op-1")

def test_subprocess_invalid_json(tmp_path: Path):
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("print('not json')")
    t = trans.ConfiguredLocalAgentTransport([sys.executable, str(worker_script)])
    with pytest.raises(RuntimeError, match="Worker did not output valid JSON"):
        t.dispatch({"operation_id": "op-1"}, timeout=2)

def test_subprocess_nonzero_exit(tmp_path: Path):
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("import sys; sys.stderr.write('err'); sys.exit(1)")
    t = trans.ConfiguredLocalAgentTransport([sys.executable, str(worker_script)])
    with pytest.raises(RuntimeError, match="Worker failed: err"):
        t.dispatch({"operation_id": "op-1"}, timeout=2)

def test_subprocess_explicit_cancellation(tmp_path: Path):
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("import time; time.sleep(10)")
    t = trans.ConfiguredLocalAgentTransport([sys.executable, str(worker_script)])
    import threading

    def run_dispatch():
        try:
            t.dispatch({"operation_id": "op-cancel"}, timeout=10)
        except Exception:
            pass

    th = threading.Thread(target=run_dispatch)
    th.start()

    import time
    time.sleep(0.5) # Wait for it to start
    assert t.is_running("op-cancel")
    assert t.cancel("op-cancel")
    assert not t.is_running("op-cancel")
    th.join()

def test_provider_unavailable():
    from ai_engineering.supervisor.router.adapters import AgentTransportUnavailableError
    t = trans.ConfiguredLocalAgentTransport(None)
    with pytest.raises(AgentTransportUnavailableError):
        t.dispatch({"operation_id": "op-1"}, timeout=2)

@pytest.mark.asyncio
async def test_budget_restart_persistence(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    from ai_engineering.task_intent import TaskIntent
    from ai_engineering.contracts import StopBoundary, TaskClass
    from ai_engineering.task_intent import IntentStatus
    intent = TaskIntent(
        schema_version=1,
        task_id="t1",
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="x",
        source_repository="repo",
        source_main_ref="refs/heads/main",
        source_base_sha="a"*40,
        constraints=(),
        allowed_mutations=("code",),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.LOCAL_DIFF,
        acceptance_criteria=(),
        unknowns=(),
        applicable_invariants=(),
        required_gates=(),
        parent_intent_digest=None
    )
    run_id = "run-1"
    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal="x",
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )

    # Fake some events
    from ai_engineering.supervisor.events import create_event, SupervisorEventType
    events = store.load_events(run_id)
    ev1 = create_event(
        run_id=run_id,
        sequence=len(events)+1,
        previous_event_digest=events[-1].event_digest if events else None,
        event_type=SupervisorEventType.ASTRA_PROPOSAL_CREATED,
        state_revision=1,
        task_id="t1",
        attempt_id="a1",
        intent_digest="a"*64,
        payload={"proposal_id": "p1"},
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    store.save_event(ev1)

    events = store.load_events(run_id)
    ev2 = create_event(
        run_id=run_id,
        sequence=len(events)+1,
        previous_event_digest=events[-1].event_digest,
        event_type=SupervisorEventType.DISPATCH_SENT,
        state_revision=1,
        task_id="t1",
        attempt_id="a1",
        intent_digest="a"*64,
        payload={"operation_id": "op1"},
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    store.save_event(ev2)

    events = store.load_events(run_id)
    ev3 = create_event(
        run_id=run_id,
        sequence=len(events)+1,
        previous_event_digest=events[-1].event_digest,
        event_type=SupervisorEventType.DECISION_ACCEPTED,
        state_revision=1,
        task_id="t1",
        attempt_id="a1",
        intent_digest="a"*64,
        payload={"action": "RETRY"},
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    store.save_event(ev3)

    # Initialize coordinator and verify budget load
    coord = ar.AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=MagicMock(),
        result_collector=MagicMock(),
        budget=ar.BudgetConfig(),
        run_id=run_id,
        astra_provider=MagicMock(),
        ci_provider=MagicMock()
    )

    # We call a dummy method that relies on budget loading or run_until_terminal directly?
    # run_until_terminal will immediately block and load events.
    # To test we can mock astra_provider to return STOP_SUCCESS
    from ai_engineering.supervisor.autonomous_run import AstraNextActionProposal, NextActionType
    class MockAstra(ar.AstraProposalProvider):
        async def request_proposal(self, run_id, state):
            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id="prop-1",
                run_id=run_id,
                task_id=state.current_task_id,
                action_type=NextActionType.STOP_SUCCESS,
                objective="x",
                recommended_capability="code",
                recommended_worker="codex",
                expected_effect_class="READ_ONLY",
                expected_stop_boundary="READ_ONLY",
                allowed_scope=(),
                required_validators=(),
                success_criteria="",
                reasoning_summary=""
            )
    coord.astra_provider = MockAstra()
    receipt = await coord.run_until_terminal()

    # It loaded previous budget
    assert coord.provider_calls == 2 # 1 from fake events, 1 from the MockAstra call
    assert coord.child_tasks == 1
    pass
    assert coord.retries == 1

