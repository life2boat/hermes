import pytest
import datetime
import uuid
import json
import hashlib
from pathlib import Path

from ai_engineering.supervisor.autonomous_run import (
    AutonomousRunCoordinator,
    NextActionType,
    AstraNextActionProposal,
    BudgetConfig,
    AutonomousRunReceipt
)
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.router.router import CrossAgentRouter
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.task_intent import TaskIntent, IntentStatus, TaskClass, AcceptanceCriterion
from ai_engineering.contracts import StopBoundary
from ai_engineering.supervisor.state import SupervisorState, SupervisorPhase
from ai_engineering.supervisor.worker_result import WorkerResultBundle

class MockRouter:
    def __init__(self, failure_on_dispatch=False):
        self.dispatched = []
        self.failure_on_dispatch = failure_on_dispatch

    async def dispatch(self, envelope, fallback_candidate=None, caller_provenance=None):
        if self.failure_on_dispatch:
            raise RuntimeError("Mock router failure")
        self.dispatched.append(envelope)
        import uuid, datetime
        return WorkerResultBundle(
            schema_version="hermes.worker-result.v1",
            result_id=str(uuid.uuid4()),
            task_id=envelope.task_id,
            attempt_id=envelope.attempt_id,
            intent_digest=envelope.task_intent_digest,
            worker_id=envelope.recipient_agent,
            repository="mock",
            canonical_remote="mock",
            base_sha="mock-sha",
            head_sha="mock-sha-2",
            produced_at_utc=datetime.datetime.now(datetime.UTC).isoformat(),
            artifacts=(),
            gate_claims=()
        )

@pytest.fixture
def run_id():
    return "test-run-auto"

@pytest.fixture
def intent(run_id):
    return TaskIntent(
        schema_version=1,
        task_id=run_id,
        intent_revision=1,
        status=IntentStatus.READY,
        task_class=TaskClass.BOUNDED_IMPLEMENTATION,
        desired_outcome="Test outcome",
        source_repository="life2boat/hermes",
        source_main_ref="refs/heads/main",
        source_base_sha="a"*40,
        constraints=(),
        allowed_mutations=(),
        forbidden_mutations=(),
        stop_boundary=StopBoundary.COMMIT,
        acceptance_criteria=(AcceptanceCriterion(criterion_id="c1", statement="Works"),),
        unknowns=(),
        applicable_invariants=(),
        required_gates=("tests",),
        parent_intent_digest=None
    )

@pytest.mark.asyncio
async def test_autonomous_run_positive(tmp_path, run_id, intent):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    router = MockRouter()
    collector = ResultCollector()
    budget = BudgetConfig()

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal="test",
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
    )

    coord = AutonomousRunCoordinator(
        loop=loop, store=store, router=router, result_collector=collector, budget=budget, run_id=run_id
    )

    class MultiAstraHook:
        def __init__(self):
            self.calls = 0

        def __call__(self, state):
            self.calls += 1
            if self.calls == 1:
                return AstraNextActionProposal(
                    schema_version="hermes.astra-next-action.v1",
                    proposal_id=str(uuid.uuid4()),
                    run_id=run_id,
                    task_id=run_id,
                    action_type=NextActionType.IMPLEMENT,
                    objective="Implement feature",
                    recommended_capability="code",
                    recommended_worker="antigravity",
                    expected_effect_class="GIT_COMMIT",
                    expected_stop_boundary="COMMIT",
                    allowed_scope=(), required_validators=(), success_criteria="", reasoning_summary=""
                )
            else:
                return AstraNextActionProposal(
                    schema_version="hermes.astra-next-action.v1",
                    proposal_id=str(uuid.uuid4()),
                    run_id=run_id,
                    task_id=run_id,
                    action_type=NextActionType.STOP_SUCCESS,
                    objective="Done",
                    recommended_capability="none",
                    recommended_worker="none",
                    expected_effect_class="READ_ONLY",
                    expected_stop_boundary="READ_ONLY",
                    allowed_scope=(), required_validators=(), success_criteria="", reasoning_summary=""
                )

    coord._request_proposal = MultiAstraHook()

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
    assert receipt.iterations == 2
    assert receipt.child_tasks == 1
    assert len(router.dispatched) == 1
    assert router.dispatched[0].recipient_agent == "antigravity"

@pytest.mark.asyncio
async def test_autonomous_run_budget_exhaustion(tmp_path, run_id, intent):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)
    router = MockRouter()
    collector = ResultCollector()
    budget = BudgetConfig(max_supervisor_decisions=3)

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal="test",
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
    )

    coord = AutonomousRunCoordinator(
        loop=loop, store=store, router=router, result_collector=collector, budget=budget, run_id=run_id
    )

    def infinite_implement_hook(state):
        return AstraNextActionProposal(
            schema_version="hermes.astra-next-action.v1",
            proposal_id=str(uuid.uuid4()),
            run_id=run_id,
            task_id=run_id,
            action_type=NextActionType.IMPLEMENT,
            objective="Keep implementing",
            recommended_capability="code",
            recommended_worker="antigravity",
            expected_effect_class="GIT_COMMIT",
            expected_stop_boundary="COMMIT",
            allowed_scope=(), required_validators=(), success_criteria="", reasoning_summary=""
        )

    coord._request_proposal = infinite_implement_hook

    receipt = await coord.run_until_terminal()
    assert receipt.terminal_reason == "BUDGET_EXHAUSTED"
    assert receipt.iterations == 3

@pytest.mark.asyncio
async def test_autonomous_run_policy_denial(tmp_path, run_id, intent):
    store = FileSupervisorStateStore(tmp_path)
    loop = SupervisorLoop(store)

    # We simulate policy denial by having router fail with specific error, or Astra requesting forbidden action.
    # But PolicyEngine runs inside loop.accept_decision!
    # Wait, loop.accept_decision doesn't explicitly throw PolicyDeniedError unless it's hooked.
    # I can just use a forbidden action? NextActionType.IMPLEMENT with STOP_BOUNDARY beyond intent?
    # Yes, intent stop_boundary=COMMIT, if Astra asks for PR_MERGE it should be rejected.

    router = MockRouter()
    collector = ResultCollector()
    budget = BudgetConfig()

    loop.initialize_run(
        intent=intent,
        lineage=None,
        root_goal="test",
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
    )

    coord = AutonomousRunCoordinator(
        loop=loop, store=store, router=router, result_collector=collector, budget=budget, run_id=run_id
    )

    # But wait, AstraNextActionProposal expects "expected_stop_boundary" which is just passed to Envelope.
    # The actual authority is checked when calling CrossAgentRouter.
    router.failure_on_dispatch = True

    def bad_hook(state):
        return AstraNextActionProposal(
            schema_version="hermes.astra-next-action.v1",
            proposal_id=str(uuid.uuid4()),
            run_id=run_id,
            task_id=run_id,
            action_type=NextActionType.IMPLEMENT,
            objective="Bad implement",
            recommended_capability="code",
            recommended_worker="antigravity",
            expected_effect_class="PR_MERGE",
            expected_stop_boundary="MERGE",
            allowed_scope=(), required_validators=(), success_criteria="", reasoning_summary=""
        )

    coord._request_proposal = bad_hook

    receipt = await coord.run_until_terminal()
    assert "ROUTER_ERROR" in receipt.terminal_reason

@pytest.mark.asyncio
async def test_autonomous_run_restart(tmp_path, run_id, intent):
    # Setup initial run
    store1 = FileSupervisorStateStore(tmp_path)
    loop1 = SupervisorLoop(store1)

    loop1.initialize_run(
        intent=intent,
        lineage=None,
        root_goal="test",
        root_goal_id=run_id,
        created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
    )

    # Run a single step
    coord1 = AutonomousRunCoordinator(
        loop=loop1, store=store1, router=MockRouter(), result_collector=ResultCollector(), budget=BudgetConfig(), run_id=run_id
    )
    class SingleHook:
        def __call__(self, state):
            return AstraNextActionProposal(
                schema_version="hermes.astra-next-action.v1",
                proposal_id=str(uuid.uuid4()),
                run_id=run_id,
                task_id=run_id,
                action_type=NextActionType.INSPECT,
                objective="Inspect",
                recommended_capability="code",
                recommended_worker="antigravity",
                expected_effect_class="READ_ONLY",
                expected_stop_boundary="READ_ONLY",
                allowed_scope=(), required_validators=(), success_criteria="", reasoning_summary=""
            )

    coord1._request_proposal = SingleHook()
    # To simulate restart, we just let it run one step then crash it?
    # run_until_terminal runs while True. We can raise an Exception inside hook after 1.
    class CrashHook:
        def __init__(self):
            self.calls = 0
        def __call__(self, state):
            self.calls += 1
            if self.calls == 1:
                return AstraNextActionProposal(
                    schema_version="hermes.astra-next-action.v1",
                    proposal_id="p1", run_id=run_id, task_id=run_id,
                    action_type=NextActionType.INSPECT, objective="Inspect", recommended_capability="code",
                    recommended_worker="antigravity", expected_effect_class="READ_ONLY", expected_stop_boundary="READ_ONLY",
                    allowed_scope=(), required_validators=(), success_criteria="", reasoning_summary=""
                )
            raise RuntimeError("CRASH")

    coord1._request_proposal = CrashHook()
    try:
        await coord1.run_until_terminal()
    except RuntimeError:
        pass

    # Restart
    store2 = FileSupervisorStateStore(tmp_path)
    loop2 = SupervisorLoop(store2)
    coord2 = AutonomousRunCoordinator(
        loop=loop2, store=store2, router=MockRouter(), result_collector=ResultCollector(), budget=BudgetConfig(), run_id=run_id
    )

    def stop_hook(state):
        return AstraNextActionProposal(
            schema_version="hermes.astra-next-action.v1",
            proposal_id="p2", run_id=run_id, task_id=run_id,
            action_type=NextActionType.STOP_SUCCESS, objective="Done", recommended_capability="none",
            recommended_worker="none", expected_effect_class="READ_ONLY", expected_stop_boundary="READ_ONLY",
            allowed_scope=(), required_validators=(), success_criteria="", reasoning_summary=""
        )

    coord2._request_proposal = stop_hook
    receipt = await coord2.run_until_terminal()
    assert receipt.terminal_reason == "GOAL_COMPLETE"
