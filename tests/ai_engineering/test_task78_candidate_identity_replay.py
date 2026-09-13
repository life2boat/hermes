import os
import uuid
import json
import pytest
import datetime
import hashlib
from pathlib import Path

from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.task_intent import TaskIntent, intent_digest, IntentStatus
from ai_engineering.supervisor.events import create_event, SupervisorEventType
from ai_engineering.contracts import StopBoundary, TaskClass, EffectClass
from ai_engineering.supervisor.pr_provider import compute_candidate_head_identity_digest, CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION, CandidateHeadIdentity
from ai_engineering.supervisor.autonomous_run import AutonomousRunCoordinator, ScriptedAstraProposalProvider, BudgetConfig

def _make_intent():
    return TaskIntent(
        task_id="task-123",
        schema_version=1,
        source_repository="life2boat/hermes",
        source_base_sha="4da55cbf3722f7964dd10d56725b45a5456f8c02",
        source_main_ref="main",
        desired_outcome="Test outcome",
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        stop_boundary=StopBoundary.DRAFT_PR,
        allowed_mutations=(),
        forbidden_mutations=(),
        required_gates=(),
        parent_intent_digest="0" * 64,
        status=IntentStatus.READY,
        constraints=(),
        acceptance_criteria=(),
        unknowns=(),
        applicable_invariants=(),
        intent_revision=1
    )

def _setup_run(tmp_path):
    run_id = "run-test-" + uuid.uuid4().hex[:8]
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent = _make_intent()
    state = loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal="test",
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    return store, run_id, state

def _valid_payload():
    payload = {
        "schema_version": CANDIDATE_HEAD_IDENTITY_SCHEMA_VERSION,
        "candidate_id": "cid-123",
        "run_id": "run-test",
        "task_id": "task-123",
        "repository": "life2boat/hermes",
        "remote_name": "github",
        "head_ref": "feat/test",
        "head_sha": "a" * 40,
        "base_ref": "main",
        "base_sha": "b" * 40,
        "source_base_sha": "c" * 40,
        "published_at_utc": "2026-09-13T00:00:00+00:00"
    }
    payload["digest"] = compute_candidate_head_identity_digest(payload)
    return payload

def _inject_cid_event(store, run_id, payload):
    events = store.load_events(run_id)
    prev_digest = events[-1].event_digest if events else None
    seq = len(events) + 1
    ev = create_event(
        run_id=run_id,
        sequence=seq,
        previous_event_digest=prev_digest,
        event_type=SupervisorEventType.CANDIDATE_HEAD_IDENTITY_REGISTERED,
        state_revision=1,
        task_id="task-123",
        attempt_id="att-1",
        intent_digest="0" * 64,
        payload=payload,
        created_at_utc="2026-09-13T00:00:00+00:00"
    )
    store.save_event(ev)

def _create_coordinator(store, run_id, provider_mode="test", pr_provider=None, candidate_head_ref=None, candidate_head_identity=None):
    from ai_engineering.supervisor.router.router import CrossAgentRouter
    from ai_engineering.supervisor.collector import ResultCollector
    loop = SupervisorLoop(store)
    class MockRouter:
        def route_result(self, r): pass
        def send_to_task(self, t, m): pass
        def terminate(self): pass
    router = MockRouter()
    class MockResultCollector:
        def write(self, c): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    return AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=router,
        result_collector=MockResultCollector(),
        budget=BudgetConfig(),
        run_id=run_id,
        astra_provider=ScriptedAstraProposalProvider([]),
        ci_provider=None,
        evidence_root="/tmp",
        provider_mode=provider_mode,
        pr_provider=pr_provider,
        allow_pr_create=False,
        allow_pr_merge=False,
        candidate_head_ref=candidate_head_ref,
        candidate_head_identity=candidate_head_identity,
    )

@pytest.mark.asyncio
async def test_candidate_identity_replay_missing_digest(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    payload = _valid_payload()
    del payload["digest"]
    _inject_cid_event(store, run_id, payload)
    
    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()
    
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"
    assert coordinator._candidate_head_identity_blocked is True
    
    events = store.load_events(run_id)
    assert any(e.event_type == SupervisorEventType.CANDIDATE_HEAD_IDENTITY_INVALID for e in events)

@pytest.mark.asyncio
async def test_candidate_identity_replay_empty_head_sha(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["head_sha"] = ""
    payload["digest"] = compute_candidate_head_identity_digest(payload)
    _inject_cid_event(store, run_id, payload)
    
    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()
    
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_missing_repo(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    payload = _valid_payload()
    del payload["repository"]
    # Intentionally do not recompute digest to trigger schema failure before digest failure
    _inject_cid_event(store, run_id, payload)
    
    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()
    
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_wrong_schema(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["schema_version"] = "hermes.candidate-head-identity.v999"
    payload["digest"] = compute_candidate_head_identity_digest(payload)
    _inject_cid_event(store, run_id, payload)
    
    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()
    
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_malformed_payload(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    _inject_cid_event(store, run_id, {"foo": "bar"})
    
    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()
    
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_replay_digest_mismatch(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["digest"] = "bad" * 21 + "b"
    _inject_cid_event(store, run_id, payload)
    
    coordinator = _create_coordinator(store, run_id)
    receipt = await coordinator.run_until_terminal()
    
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"
    assert coordinator._candidate_head_identity_blocked is True

@pytest.mark.asyncio
async def test_candidate_identity_replay_corrupt_with_cli_ref(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    payload = _valid_payload()
    payload["digest"] = "bad" * 21 + "b"
    _inject_cid_event(store, run_id, payload)
    
    coordinator = _create_coordinator(store, run_id, candidate_head_ref="valid/ref")
    receipt = await coordinator.run_until_terminal()
    
    assert receipt.terminal_reason == "CANDIDATE_HEAD_IDENTITY_INVALID"

@pytest.mark.asyncio
async def test_candidate_identity_injected_real_mode(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    
    cid_payload = _valid_payload()
    cid = CandidateHeadIdentity(**cid_payload)
    
    coordinator = _create_coordinator(store, run_id, provider_mode="real", candidate_head_identity=cid)
    
    assert coordinator._injected_candidate_head_identity is cid
    assert getattr(coordinator, "candidate_head_identity", None) is None

@pytest.mark.asyncio
async def test_candidate_identity_valid_persisted(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    payload = _valid_payload()
    _inject_cid_event(store, run_id, payload)
    
    coordinator = _create_coordinator(store, run_id)
    await coordinator.run_until_terminal()

    assert getattr(coordinator, "candidate_head_identity", None) is not None
    assert getattr(coordinator, "candidate_head_identity").digest == payload["digest"]

@pytest.mark.asyncio
async def test_candidate_identity_valid_cli_ref(tmp_path):
    store, run_id, state = _setup_run(tmp_path)
    coordinator = _create_coordinator(store, run_id, candidate_head_ref="feat/some-branch")
    
    assert getattr(coordinator, "candidate_head_identity", None) is None
    assert getattr(coordinator, "candidate_head_ref", None) == "feat/some-branch"
