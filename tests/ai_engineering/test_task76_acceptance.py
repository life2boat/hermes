import pytest
import asyncio
from pathlib import Path
from ai_engineering.supervisor.autonomous_run import AutonomousRunCoordinator, BudgetConfig
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.router.router import CrossAgentRouter, AuthorityResolver
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.router.registry import AgentRegistry, AgentDefinition
from ai_engineering.supervisor.router.envelope import MessageType
from ai_engineering.supervisor.router.transports import FakeAgentTransport
from ai_engineering.supervisor.router.adapters import CodexAdapter
from ai_engineering.task_intent import TaskIntent, deserialize_intent
from ai_engineering.supervisor.autonomous_run import AstraNextActionProposal, NextActionType
from ai_engineering.supervisor.policy.contracts import WorkProfile, AutonomyLevel, BudgetLimits, PromotionThresholds, ExecutionTarget, EffectClass
from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus
import json
from ai_engineering.task_intent import intent_digest


class MockAstra:
    def __init__(self, actions):
        self.actions = actions
        self.calls = 0
    async def request_proposal(self, run_id, state):
        act = self.actions[self.calls]
        self.calls += 1
        return AstraNextActionProposal(
            schema_version="hermes.astra-next-action.v1",
            proposal_id=f"prop-{self.calls}",
            run_id=run_id,
            task_id=state.current_task_id,
            action_type=act,
            objective="test",
            recommended_capability="READ_ONLY",
            recommended_worker="codex",
            expected_effect_class="READ_ONLY",
            expected_stop_boundary="READ_ONLY",
            allowed_scope=(),
            required_validators=(),
            success_criteria="",
            reasoning_summary=""
        )

class MockCI:
    async def wait_for_ci(self, run_id, sha):
        return True

@pytest.mark.asyncio
async def test_restart_recovery(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent_json = {
        "schema_version": 1,
        "task_id": "t1",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Implement autonomous supervisor pipeline",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "constraints": [],
        "allowed_mutations": ["READ_ONLY"],
        "forbidden_mutations": [],
        "stop_boundary": "MERGE",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None
    }
    intent = deserialize_intent(json.dumps(intent_json))
    wp = WorkProfile(
        schema_version="hermes.work-profile.v1",
        profile_id="wp1",
        profile_version=1,
        profile_digest="wp1",
        preferred_worker_model=None,
        preferred_verifier_model=None,
        preferred_supervisor_model=None,
        escalation_model=None,
        allowed_task_classes=("BOUNDED_IMPLEMENTATION",),
        maximum_autonomy_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION,
        allowed_effect_classes=(EffectClass.READ_ONLY,),
        forbidden_effect_classes=(),
        allowed_targets=(ExecutionTarget.DEV,),
        required_validators=(),
        promotion_thresholds=PromotionThresholds(1, 0, False),
        budget_limits=BudgetLimits(20, 10, 1, 1, 1, 1, 1),
        production_execution_allowed=True,
        vector_mutation_allowed=True,
        secret_mutation_allowed=True,
        external_send_allowed=True
    )

    from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus, TaskPolicyAttribution
    ep = EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="ep1",
        task_id="t1",
        intent_digest=intent_digest(intent),
        intent_revision=1,
        source_base_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        subject_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        status=EffectivePolicyStatus.COMPLETE,
        policy_sources=(),
        task_policy=TaskPolicyAttribution(task_id="t1", intent_revision=1, intent_digest=intent_digest(intent), source_base_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", constraints=(), allowed_mutations=(), forbidden_mutations=(), stop_boundary="MERGE", source_id="s1"),
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="s1",
        authority_expansion=False
    )

    loop.initialize_run(intent, None, "ok", "r1", "2026-01-01T00:00:00Z")
    loop.bind_profile("r1", wp, initial_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION, effective_policy=ep)

    reg = AgentRegistry()
    reg.register(
        AgentDefinition(agent_id="codex", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True))
    )
    from ai_engineering.supervisor.router.router import PersistentStore
    router_store = PersistentStore(str(tmp_path))
    router = CrossAgentRouter(reg, AuthorityResolver(store), router_store)

    coord1 = AutonomousRunCoordinator(evidence_root=tmp_path,
        loop=loop, store=store, router=router, result_collector=ResultCollector(),
        budget=BudgetConfig(), run_id="r1",
        astra_provider=MockAstra([NextActionType.IMPLEMENT, NextActionType.STOP_BLOCKED]),
        ci_provider=MockCI()
    )
    receipt1 = await coord1.run_until_terminal()
    print("RECEIPT1:", receipt1)
    assert receipt1.terminal_reason == "POLICY_BLOCKED"

    store2 = FileSupervisorStateStore(tmp_path)
    loop2 = SupervisorLoop(store2)
    router_store2 = PersistentStore(str(tmp_path))
    router2 = CrossAgentRouter(reg, AuthorityResolver(store2), router_store2)

    coord2 = AutonomousRunCoordinator(evidence_root=tmp_path,
        loop=loop2, store=store2, router=router2, result_collector=ResultCollector(),
        budget=BudgetConfig(), run_id="r1",
        astra_provider=MockAstra([NextActionType.STOP_SUCCESS]),
        ci_provider=MockCI()
    )
    receipt2 = await coord2.run_until_terminal()
    assert receipt2.terminal_reason == "GOAL_COMPLETE"





@pytest.mark.asyncio
async def test_worker_fallback(tmp_path: Path):
    from ai_engineering.supervisor.router.envelope import AgentEnvelope, MessageType
    from ai_engineering.supervisor.policy.contracts import StopBoundary, EffectClass
    from ai_engineering.supervisor.router.router import PersistentStore
    import uuid
    import datetime

    store = FileSupervisorStateStore(tmp_path)
    router_store = PersistentStore(str(tmp_path))
    reg = AgentRegistry()
    reg.register(
        AgentDefinition(agent_id="primary", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=False))
    )
    reg.register(
        AgentDefinition(agent_id="fallback", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True, fake_result={"status": "PASS"}))
    )

    from unittest.mock import MagicMock
    from ai_engineering.supervisor.policy.contracts import PolicyVerdict, PolicyReceipt
    auth_mock = MagicMock()
    fake_intent = MagicMock()
    fake_intent.allowed_mutations = ["READ_ONLY"]
    auth_mock.resolve_task_intent.return_value = fake_intent

    fake_policy = PolicyReceipt(
        schema_version="1",
        receipt_id="pr1",
        request_id="r1",
        task_intent_digest="t1",
        decision_id="d1",
        decision_receipt_id="dr1",
        effective_policy_id="ep1",
        work_profile_id="wp1",
        work_profile_digest="wp1",
        autonomy_state_digest="a",
        budget_state_digest="b",
        verdict=PolicyVerdict.ALLOW,
        reason_codes=(),
        created_at_utc="2026-01-01T00:00:00Z"
    )

    auth_mock.resolve_policy_receipt.return_value = fake_policy
    auth_mock.evaluate_fresh_policy.return_value = fake_policy
    auth_mock.get_provenance.return_value = {"repository": "github", "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "canonical_remote": "github"}
    auth_mock.get_permitted_capabilities.return_value = ["READ_ONLY"]

    router = CrossAgentRouter(reg, auth_mock, router_store)

    env = AgentEnvelope(
        message_id=str(uuid.uuid4()),
        correlation_id="corr1",
        causation_id="caus1",
        run_id="r1",
        task_id="t1",
        attempt_id="a1",
        sender_agent="supervisor",
        recipient_agent="primary",
        recipient_capability="READ_ONLY",
        message_type=MessageType.WORK_REQUEST,
        payload={},
        payload_digest=__import__("ai_engineering.supervisor.router.envelope", fromlist=["compute_payload_digest"]).compute_payload_digest({}),
        task_intent_id="t1",
        task_intent_digest="abcd",
        policy_receipt_id="pr1",
        policy_receipt_digest="abcd",
        effect_class=EffectClass.READ_ONLY,
        stop_boundary=StopBoundary.MERGE,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        expires_at_utc="",
        max_retries=1
    )

    bundle = await router.dispatch(env, fallback_candidate="fallback")
    assert bundle.worker_id == "fallback"


@pytest.mark.asyncio
async def test_policy_denial(tmp_path: Path):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent_json = {
        "schema_version": 1,
        "task_id": "t1",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Implement autonomous supervisor pipeline",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "constraints": [],
        "allowed_mutations": ["READ_ONLY"],
        "forbidden_mutations": [],
        "stop_boundary": "MERGE",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None
    }
    intent = deserialize_intent(json.dumps(intent_json))
    wp = WorkProfile(
        schema_version="hermes.work-profile.v1",
        profile_id="wp1",
        profile_version=1,
        profile_digest="wp1",
        preferred_worker_model=None,
        preferred_verifier_model=None,
        preferred_supervisor_model=None,
        escalation_model=None,
        allowed_task_classes=("BOUNDED_IMPLEMENTATION",),
        maximum_autonomy_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION,
        allowed_effect_classes=(EffectClass.READ_ONLY,),
        forbidden_effect_classes=(),
        allowed_targets=(ExecutionTarget.DEV,),
        required_validators=(),
        promotion_thresholds=PromotionThresholds(1, 0, False),
        budget_limits=BudgetLimits(20, 10, 1, 1, 1, 1, 1),
        production_execution_allowed=True,
        vector_mutation_allowed=True,
        secret_mutation_allowed=True,
        external_send_allowed=True
    )

    from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus, TaskPolicyAttribution
    ep = EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="ep1",
        task_id="t1",
        intent_digest=intent_digest(intent),
        intent_revision=1,
        source_base_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        subject_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        status=EffectivePolicyStatus.COMPLETE,
        policy_sources=(),
        task_policy=TaskPolicyAttribution(task_id="t1", intent_revision=1, intent_digest=intent_digest(intent), source_base_sha="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", constraints=(), allowed_mutations=["READ_ONLY"], forbidden_mutations=(), stop_boundary="MERGE", source_id="s1"),
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="s1",
        authority_expansion=False
    )

    loop.initialize_run(intent, None, "ok", "r1", "2026-01-01T00:00:00Z")
    loop.bind_profile("r1", wp, initial_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION, effective_policy=ep)

    reg = AgentRegistry()
    reg.register(
        AgentDefinition(agent_id="codex", capabilities=["WRITE_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True))
    )
    from ai_engineering.supervisor.router.router import PersistentStore
    router_store = PersistentStore(str(tmp_path))
    router = CrossAgentRouter(reg, AuthorityResolver(store), router_store)

    coord = AutonomousRunCoordinator(evidence_root=tmp_path,
        loop=loop, store=store, router=router, result_collector=ResultCollector(),
        budget=BudgetConfig(), run_id="r1",
        # Request codex, which needs WRITE_ONLY, but intent only allowed READ_ONLY!
        astra_provider=MockAstra([NextActionType.IMPLEMENT]),
        ci_provider=MockCI()
    )
    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "ROUTER_ERROR_CapabilityNotAuthorizedError"


def _create_test_env(tmp_path: Path, run_id: str = "r1"):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    intent_json = {
        "schema_version": 1,
        "task_id": "t1",
        "intent_revision": 1,
        "status": "READY",
        "task_class": "BOUNDED_IMPLEMENTATION",
        "desired_outcome": "Implement autonomous supervisor pipeline",
        "source_repository": "github",
        "source_main_ref": "refs/remotes/github/main",
        "source_base_sha": "a" * 40,
        "constraints": [],
        "allowed_mutations": ["READ_ONLY"],
        "forbidden_mutations": [],
        "stop_boundary": "MERGE",
        "acceptance_criteria": [],
        "unknowns": [],
        "applicable_invariants": [],
        "required_gates": [],
        "parent_intent_digest": None
    }
    intent = deserialize_intent(json.dumps(intent_json))
    wp = WorkProfile(
        schema_version="hermes.work-profile.v1",
        profile_id="wp1",
        profile_version=1,
        profile_digest="wp1",
        preferred_worker_model=None,
        preferred_verifier_model=None,
        preferred_supervisor_model=None,
        escalation_model=None,
        allowed_task_classes=("BOUNDED_IMPLEMENTATION",),
        maximum_autonomy_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION,
        allowed_effect_classes=(EffectClass.READ_ONLY,),
        forbidden_effect_classes=(),
        allowed_targets=(ExecutionTarget.DEV, ExecutionTarget.LOCAL),
        required_validators=(),
        promotion_thresholds=PromotionThresholds(1, 0, False),
        budget_limits=BudgetLimits(20, 10, 1, 1, 1, 1, 1),
        production_execution_allowed=True,
        vector_mutation_allowed=True,
        secret_mutation_allowed=True,
        external_send_allowed=True
    )

    from ai_engineering.effective_policy import EffectivePolicyReport, EffectivePolicyStatus, TaskPolicyAttribution
    ep = EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="0" * 64,
        task_id="t1",
        intent_digest=intent_digest(intent),
        intent_revision=1,
        source_base_sha="a" * 40,
        subject_sha="a" * 40,
        status=EffectivePolicyStatus.COMPLETE,
        policy_sources=(),
        task_policy=TaskPolicyAttribution(
            task_id="t1",
            intent_revision=1,
            intent_digest=intent_digest(intent),
            source_base_sha="a" * 40,
            constraints=(),
            allowed_mutations=("READ_ONLY",),
            forbidden_mutations=(),
            stop_boundary="MERGE",
            source_id="0" * 64
        ),
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="0" * 64,
        authority_expansion=False
    )

    loop.initialize_run(intent, None, "ok", run_id, "2026-01-01T00:00:00Z")
    loop.bind_profile(run_id, wp, initial_level=AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION, effective_policy=ep)

    reg = AgentRegistry()
    from ai_engineering.supervisor.router.router import PersistentStore
    router_store = PersistentStore(str(tmp_path))
    router = CrossAgentRouter(reg, AuthorityResolver(store, loop), router_store)

    return store, loop, router, reg, router_store, intent, wp, ep


@pytest.mark.asyncio
async def test_budget_real_runtime_restart(tmp_path: Path):
    store, loop, router, reg, router_store, intent, wp, ep = _create_test_env(tmp_path, "r1")
    reg.register(
        AgentDefinition(agent_id="codex", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True))
    )
    coord1 = AutonomousRunCoordinator(
        evidence_root=tmp_path, loop=loop, store=store, router=router,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id="r1",
        astra_provider=MockAstra([NextActionType.IMPLEMENT, NextActionType.STOP_BLOCKED]),
        ci_provider=MockCI()
    )
    receipt1 = await coord1.run_until_terminal()
    assert receipt1.terminal_reason == "POLICY_BLOCKED"
    assert coord1.child_tasks == 1
    assert coord1.iterations == 2
    assert coord1.provider_calls == 2

    # Destroy and recreate runtime from disk
    del coord1, loop, store, router
    store2 = FileSupervisorStateStore(tmp_path)
    loop2 = SupervisorLoop(store2)
    router2 = CrossAgentRouter(reg, AuthorityResolver(store2, loop2), router_store)
    coord2 = AutonomousRunCoordinator(
        evidence_root=tmp_path, loop=loop2, store=store2, router=router2,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id="r1",
        astra_provider=MockAstra([NextActionType.STOP_SUCCESS]),
        ci_provider=MockCI()
    )
    # Assert reconstructed counters match prior execution state
    assert coord2.child_tasks == 1
    assert coord2.provider_calls == 2
    assert coord2.iterations == 2

    receipt2 = await coord2.run_until_terminal()
    assert receipt2.terminal_reason == "GOAL_COMPLETE"
    assert coord2.provider_calls == 3
    assert coord2.iterations == 3
    assert coord2.child_tasks == 1


@pytest.mark.asyncio
async def test_verified_result_restart_rehydration(tmp_path: Path):
    store, loop, router, reg, router_store, intent, wp, ep = _create_test_env(tmp_path, "r1")
    reg.register(
        AgentDefinition(agent_id="codex", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True))
    )
    coord1 = AutonomousRunCoordinator(
        evidence_root=tmp_path, loop=loop, store=store, router=router,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id="r1",
        astra_provider=MockAstra([NextActionType.IMPLEMENT, NextActionType.STOP_BLOCKED]),
        ci_provider=MockCI()
    )
    receipt1 = await coord1.run_until_terminal()
    assert receipt1.terminal_reason == "POLICY_BLOCKED"
    assert len(receipt1.verified_results) == 1
    vr_id = receipt1.verified_results[0]

    # Wipe in-memory caches and destroy runtime
    loop._vr_cache.clear()
    del coord1, loop, store, router

    # Fresh store and loop with empty in-memory cache
    store2 = FileSupervisorStateStore(tmp_path)
    loop2 = SupervisorLoop(store2)
    assert len(loop2._vr_cache) == 0

    router2 = CrossAgentRouter(reg, AuthorityResolver(store2, loop2), router_store)
    coord2 = AutonomousRunCoordinator(
        evidence_root=tmp_path, loop=loop2, store=store2, router=router2,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id="r1",
        astra_provider=MockAstra([NextActionType.STOP_SUCCESS]),
        ci_provider=MockCI()
    )
    receipt2 = await coord2.run_until_terminal()
    assert receipt2.terminal_reason == "GOAL_COMPLETE"
    assert vr_id in receipt2.verified_results


@pytest.mark.asyncio
async def test_routing_receipt_is_real_id(tmp_path: Path):
    import uuid
    store, loop, router, reg, router_store, intent, wp, ep = _create_test_env(tmp_path, "r1")
    reg.register(
        AgentDefinition(agent_id="codex", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True))
    )
    coord = AutonomousRunCoordinator(
        evidence_root=tmp_path, loop=loop, store=store, router=router,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id="r1",
        astra_provider=MockAstra([NextActionType.IMPLEMENT, NextActionType.STOP_SUCCESS]),
        ci_provider=MockCI()
    )
    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert len(receipt.routing_receipts) >= 1
    r_id = receipt.routing_receipts[0]
    parsed_uuid = uuid.UUID(r_id)
    assert str(parsed_uuid) == r_id

    # If routing receipt is missing in store, records ROUTING_RECEIPT_MISSING -> BLOCKED
    run_id_missing = "r-missing"
    store_m, loop_m, router_m, reg_m, router_store_m, _, _, _ = _create_test_env(tmp_path / "missing", run_id_missing)

    import os
    from ai_engineering.supervisor.router.router import PersistentStore
    class NoReceiptStore(PersistentStore):
        def save(self, envelope, state, result=None, operation_id=None, failure_class=None, routing_receipt=None):
            super().save(envelope, state, result=result, operation_id=operation_id, failure_class=failure_class, routing_receipt=None)
            p = self._safe_path(envelope.message_id)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                d.pop("routing_receipt", None)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(d, f, indent=2)

    reg_m.register(
        AgentDefinition(agent_id="codex", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True))
    )
    router_no_rcpt = CrossAgentRouter(reg_m, AuthorityResolver(store_m, loop_m), NoReceiptStore(str(tmp_path / "missing")))
    coord_m = AutonomousRunCoordinator(
        evidence_root=tmp_path / "missing", loop=loop_m, store=store_m, router=router_no_rcpt,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id=run_id_missing,
        astra_provider=MockAstra([NextActionType.IMPLEMENT]),
        ci_provider=MockCI()
    )
    receipt_m = await coord_m.run_until_terminal()
    assert receipt_m.terminal_reason == "BLOCKED"
    events = store_m.load_events(run_id_missing)
    assert any(e.payload.get("blocker") == "ROUTING_RECEIPT_MISSING" for e in events)


@pytest.mark.asyncio
async def test_router_configured_transport_timeout(tmp_path: Path):
    import sys
    import uuid
    import datetime
    from ai_engineering.supervisor.router.transports import ConfiguredLocalAgentTransport
    from ai_engineering.supervisor.router.envelope import AgentEnvelope, compute_payload_digest
    from ai_engineering.supervisor.policy.contracts import StopBoundary, PolicyReceipt, PolicyVerdict
    from ai_engineering.supervisor.router.router import compute_policy_receipt_digest
    from ai_engineering.supervisor.decision import AstraDecision, SupervisorAction

    store, loop, _, reg, router_store, intent, wp, ep = _create_test_env(tmp_path, "r-timeout")
    worker_script = tmp_path / "sleep_worker.py"
    worker_script.write_text("import time; time.sleep(10)")

    transport = ConfiguredLocalAgentTransport([sys.executable, str(worker_script)])
    adapter = CodexAdapter(transport)
    reg.register(
        AgentDefinition(agent_id="codex", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=1),
        adapter
    )
    router = CrossAgentRouter(reg, AuthorityResolver(store, loop), router_store)

    proposal = AstraNextActionProposal(
        schema_version="hermes.astra-next-action.v1",
        proposal_id="prop-1",
        run_id="r-timeout",
        task_id=intent.task_id,
        action_type=NextActionType.IMPLEMENT,
        objective="test",
        recommended_capability="READ_ONLY",
        recommended_worker="codex",
        expected_effect_class="READ_ONLY",
        expected_stop_boundary="LOCAL_DIFF",
        allowed_scope=(),
        required_validators=(),
        success_criteria="",
        reasoning_summary="test"
    )
    coord = AutonomousRunCoordinator(
        evidence_root=tmp_path, loop=loop, store=store, router=router,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id="r-timeout",
        astra_provider=MockAstra([]),
        ci_provider=MockCI()
    )
    state = store.load_state("r-timeout")
    decision = coord._convert_proposal_to_decision(proposal, state)
    d_receipt, next_st = loop.accept_decision(
        run_id="r-timeout",
        decision=decision,
        context_pack_digest=decision.context_pack_digest,
        verified_result_status="PASS",
        validated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    events = store.load_events("r-timeout")
    policy_ev = [e for e in events if getattr(e.event_type, "value", str(e.event_type)) == "POLICY_EVALUATED"][-1]
    pr_data = policy_ev.payload["policy_receipt"]
    p_receipt = PolicyReceipt(
        schema_version=pr_data.get("schema_version", "hermes.supervisor-policy-receipt.v1"),
        receipt_id=pr_data["receipt_id"],
        request_id=pr_data.get("request_id", ""),
        task_intent_digest=pr_data.get("task_intent_digest", ""),
        decision_id=pr_data.get("decision_id", ""),
        decision_receipt_id=pr_data.get("decision_receipt_id", ""),
        effective_policy_id=pr_data.get("effective_policy_id", ""),
        work_profile_id=pr_data.get("work_profile_id", ""),
        work_profile_digest=pr_data.get("work_profile_digest", ""),
        autonomy_state_digest=pr_data.get("autonomy_state_digest", ""),
        budget_state_digest=pr_data.get("budget_state_digest", ""),
        verdict=PolicyVerdict(pr_data["verdict"]),
        reason_codes=tuple(pr_data["reason_codes"]),
        created_at_utc=pr_data["created_at_utc"],
    )

    env = AgentEnvelope(
        message_id=str(uuid.uuid4()),
        correlation_id="c1",
        causation_id="c1",
        run_id="r-timeout",
        task_id=next_st.current_task_id,
        attempt_id=next_st.current_attempt_id,
        sender_agent="supervisor",
        recipient_agent="codex",
        recipient_capability="READ_ONLY",
        message_type=MessageType.WORK_REQUEST,
        payload={"task_id": next_st.current_task_id},
        payload_digest=compute_payload_digest({"task_id": next_st.current_task_id}),
        task_intent_id=next_st.current_task_id,
        task_intent_digest=next_st.current_intent_digest,
        policy_receipt_id=p_receipt.receipt_id,
        policy_receipt_digest=compute_policy_receipt_digest(p_receipt),
        effect_class=EffectClass.READ_ONLY,
        stop_boundary=StopBoundary.LOCAL_DIFF,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        expires_at_utc="",
        max_retries=0
    )

    with pytest.raises(asyncio.TimeoutError, match="WORK_TIMEOUT"):
        await router.dispatch(env)

    assert router_store.load_state(env.message_id) == "TIMED_OUT"
    file_path = router_store._safe_path(env.message_id)
    with open(file_path, "r", encoding="utf-8") as f:
        stored_data = json.load(f)
    assert stored_data["failure_class"] == "TIMEOUT"


@pytest.mark.asyncio
async def test_real_authority_fallback(tmp_path: Path):
    import uuid
    import datetime
    from ai_engineering.supervisor.router.envelope import AgentEnvelope, compute_payload_digest
    from ai_engineering.supervisor.policy.contracts import StopBoundary, PolicyReceipt, PolicyVerdict
    from ai_engineering.supervisor.router.router import compute_policy_receipt_digest
    from ai_engineering.supervisor.decision import AstraDecision, SupervisorAction

    store, loop, _, reg, router_store, intent, wp, ep = _create_test_env(tmp_path, "r-fallback")
    reg.register(
        AgentDefinition(agent_id="primary", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=False))
    )
    reg.register(
        AgentDefinition(agent_id="fallback", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        CodexAdapter(FakeAgentTransport(healthy=True))
    )
    router = CrossAgentRouter(reg, AuthorityResolver(store, loop), router_store)

    proposal = AstraNextActionProposal(
        schema_version="hermes.astra-next-action.v1",
        proposal_id="prop-1",
        run_id="r-fallback",
        task_id=intent.task_id,
        action_type=NextActionType.IMPLEMENT,
        objective="test",
        recommended_capability="READ_ONLY",
        recommended_worker="primary",
        expected_effect_class="READ_ONLY",
        expected_stop_boundary="LOCAL_DIFF",
        allowed_scope=(),
        required_validators=(),
        success_criteria="",
        reasoning_summary="test"
    )
    coord = AutonomousRunCoordinator(
        evidence_root=tmp_path, loop=loop, store=store, router=router,
        result_collector=ResultCollector(), budget=BudgetConfig(), run_id="r-fallback",
        astra_provider=MockAstra([]),
        ci_provider=MockCI()
    )
    state = store.load_state("r-fallback")
    decision = coord._convert_proposal_to_decision(proposal, state)
    d_receipt, next_st = loop.accept_decision(
        run_id="r-fallback",
        decision=decision,
        context_pack_digest=decision.context_pack_digest,
        verified_result_status="PASS",
        validated_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    events = store.load_events("r-fallback")
    policy_ev = [e for e in events if getattr(e.event_type, "value", str(e.event_type)) == "POLICY_EVALUATED"][-1]
    pr_data = policy_ev.payload["policy_receipt"]
    p_receipt = PolicyReceipt(
        schema_version=pr_data.get("schema_version", "hermes.supervisor-policy-receipt.v1"),
        receipt_id=pr_data["receipt_id"],
        request_id=pr_data.get("request_id", ""),
        task_intent_digest=pr_data.get("task_intent_digest", ""),
        decision_id=pr_data.get("decision_id", ""),
        decision_receipt_id=pr_data.get("decision_receipt_id", ""),
        effective_policy_id=pr_data.get("effective_policy_id", ""),
        work_profile_id=pr_data.get("work_profile_id", ""),
        work_profile_digest=pr_data.get("work_profile_digest", ""),
        autonomy_state_digest=pr_data.get("autonomy_state_digest", ""),
        budget_state_digest=pr_data.get("budget_state_digest", ""),
        verdict=PolicyVerdict(pr_data["verdict"]),
        reason_codes=tuple(pr_data["reason_codes"]),
        created_at_utc=pr_data["created_at_utc"],
    )

    env = AgentEnvelope(
        message_id=str(uuid.uuid4()),
        correlation_id="c1",
        causation_id="c1",
        run_id="r-fallback",
        task_id=next_st.current_task_id,
        attempt_id=next_st.current_attempt_id,
        sender_agent="supervisor",
        recipient_agent="primary",
        recipient_capability="READ_ONLY",
        message_type=MessageType.WORK_REQUEST,
        payload={"task_id": next_st.current_task_id},
        payload_digest=compute_payload_digest({"task_id": next_st.current_task_id}),
        task_intent_id=next_st.current_task_id,
        task_intent_digest=next_st.current_intent_digest,
        policy_receipt_id=p_receipt.receipt_id,
        policy_receipt_digest=compute_policy_receipt_digest(p_receipt),
        effect_class=EffectClass.READ_ONLY,
        stop_boundary=StopBoundary.LOCAL_DIFF,
        created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        expires_at_utc="",
        max_retries=1
    )

    bundle = await router.dispatch(env, fallback_candidate="fallback")
    assert bundle.worker_id == "fallback"
    assert bundle.task_id == next_st.current_task_id

