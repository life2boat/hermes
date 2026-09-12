import argparse
import sys
import json
import logging
import asyncio
import datetime
import uuid
from pathlib import Path

from ai_engineering.supervisor.autonomous_run import AutonomousRunCoordinator, BudgetConfig
from ai_engineering.supervisor.router.transports import ConfiguredLocalAgentTransport
from ai_engineering.supervisor.router.adapters import CodexAdapter
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.router.router import CrossAgentRouter
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.task_intent import deserialize_intent

from ai_engineering.supervisor.router.registry import AgentRegistry, AgentDefinition
from ai_engineering.supervisor.router.envelope import MessageType
from ai_engineering.supervisor.state import SupervisorState
from ai_engineering.supervisor.autonomous_run import AstraNextActionProposal, AstraProposalProvider, CIStatusProvider
from ai_engineering.contracts import EffectClass

class LocalStaticAstraProvider(AstraProposalProvider):
    async def request_proposal(self, run_id: str, state: SupervisorState) -> AstraNextActionProposal:
        return AstraNextActionProposal(
            schema_version="hermes.astra-next-action.v1",
            proposal_id="prop-1",
            run_id=run_id,
            task_id=state.current_task_id,
            action_type="STOP_SUCCESS",
            objective="Default test stop",
            recommended_capability="none",
            recommended_worker="none",
            expected_effect_class="READ_ONLY",
            expected_stop_boundary="READ_ONLY",
            allowed_scope=(),
            required_validators=(),
            success_criteria="",
            reasoning_summary="Auto-stop in local CLI"
        )

# REAL_ASTRA_PROVIDER=NOT_IMPLEMENTED

class LocalCIProvider(CIStatusProvider):
    async def wait_for_ci(self, run_id: str, sha: str) -> bool:
        return True

# REAL_CI_PROVIDER=NOT_IMPLEMENTED

async def async_main():
    parser = argparse.ArgumentParser(description="Hermes Autonomous Run CLI (Task 7.6)")
    parser.add_argument("--intent", required=True, help="Path to TaskIntent JSON file")
    parser.add_argument("--state-dir", required=True, help="Path to persistent state directory")
    parser.add_argument("--run-id", required=True, help="Unique Run ID")
    parser.add_argument("--worker-cmd", nargs='+', required=True, help="Worker command array (e.g. codex)")
    args = parser.parse_args()

    intent_text = Path(args.intent).read_text(encoding="utf-8")
    intent = deserialize_intent(intent_text)

    store = FileSupervisorStateStore(Path(args.state_dir))
    loop = SupervisorLoop(store)

    registry = AgentRegistry()
    registry.register(
        AgentDefinition("codex", ["code", "test"], [MessageType.WORK_REQUEST], [EffectClass.REPOSITORY_WRITE], 300),
        CodexAdapter(ConfiguredLocalAgentTransport(args.worker_cmd))
    )

    from ai_engineering.supervisor.router.router import AuthorityResolver
    router = CrossAgentRouter(
        registry=registry,
        authority_resolver=AuthorityResolver(store),
        store=store
    )

    collector = ResultCollector()
    budget = BudgetConfig()

    # Initialize run if it does not exist
    try:
        store.load_state(args.run_id)
    except Exception:
        loop.initialize_run(
            intent=intent,
            lineage=None,
            root_goal=intent.desired_outcome,
            root_goal_id=args.run_id,
            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=router,
        result_collector=collector,
        budget=budget,
        run_id=args.run_id,
        astra_provider=LocalStaticAstraProvider(),
        ci_provider=LocalCIProvider()
    )

    receipt = await coord.run_until_terminal()
    print(f"Autonomous run terminal reason: {receipt.terminal_reason}")
    print(json.dumps(vars(receipt), indent=2))
    return 0 if receipt.terminal_reason == "GOAL_COMPLETE" else 1

def main():
    sys.exit(asyncio.run(async_main()))

if __name__ == '__main__':
    main()
