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
        FakeAgentTransport(healthy=True)
    )
    from ai_engineering.supervisor.router.router import PersistentStore
    router_store = PersistentStore(str(tmp_path))
    router = CrossAgentRouter(reg, AuthorityResolver(store), router_store)
    
    coord1 = AutonomousRunCoordinator(
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
    
    coord2 = AutonomousRunCoordinator(
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
        FakeAgentTransport(healthy=False)
    )
    reg.register(
        AgentDefinition(agent_id="fallback", capabilities=["READ_ONLY"], supported_message_types=[MessageType.WORK_REQUEST], allowed_effect_classes=[], timeout_seconds=300),
        FakeAgentTransport(healthy=True, fake_result={"status": "PASS"})
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
    auth_mock.get_provenance.return_value = {"repository": "github", "base_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
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
        created_at_utc=datetime.datetime.now(datetime.UTC).isoformat(),
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
        FakeAgentTransport(healthy=True)
    )
    from ai_engineering.supervisor.router.router import PersistentStore
    router_store = PersistentStore(str(tmp_path))
    router = CrossAgentRouter(reg, AuthorityResolver(store), router_store)
    
    coord = AutonomousRunCoordinator(
        loop=loop, store=store, router=router, result_collector=ResultCollector(),
        budget=BudgetConfig(), run_id="r1",
        # Request codex, which needs WRITE_ONLY, but intent only allowed READ_ONLY!
        astra_provider=MockAstra([NextActionType.IMPLEMENT]),
        ci_provider=MockCI()
    )
    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "ROUTER_ERROR_CapabilityNotAuthorizedError"
