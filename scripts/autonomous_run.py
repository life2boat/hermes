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
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.router.router import CrossAgentRouter
from ai_engineering.supervisor.worker_result import ResultCollector
from ai_engineering.task_intent import deserialize_intent

# Mock or actual registry/authority for router
class MockRegistry:
    def get_adapter(self, agent_id):
        # We assume local command passed in args
        return ConfiguredLocalAgentTransport(["echo", "mock_worker"])
    def get_definition(self, agent_id):
        class MockDef:
            capabilities = ["code"]
            timeout_seconds = 300
        return MockDef()

class MockAuthority:
    def get_provenance(self, run_id, task_id):
        return {}
    def resolve_task_intent(self, *args):
        pass
    def resolve_policy_receipt(self, *args):
        pass

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

    registry = MockRegistry()
    # Override the adapter dynamically
    registry.get_adapter = lambda x: ConfiguredLocalAgentTransport(args.worker_cmd)

    router = CrossAgentRouter(
        registry=registry,
        authority_resolver=MockAuthority(),
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
            created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
        )

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=router,
        result_collector=collector,
        budget=budget,
        run_id=args.run_id
    )

    receipt = await coord.run_until_terminal()
    print(f"Autonomous run terminal reason: {receipt.terminal_reason}")
    print(json.dumps(vars(receipt), indent=2))
    return 0 if receipt.terminal_reason == "GOAL_COMPLETE" else 1

def main():
    sys.exit(asyncio.run(async_main()))

if __name__ == '__main__':
    main()
