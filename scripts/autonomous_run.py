import argparse
import sys
import json
import logging
import asyncio
import datetime
import uuid
from pathlib import Path

from dataclasses import asdict
from ai_engineering.supervisor.autonomous_run import (
    AutonomousRunCoordinator,
    BudgetConfig,
    ScriptedAstraProposalProvider,
    AstraNextActionProposal,
    AstraProposalProvider,
    CIStatusProvider,
)
from ai_engineering.supervisor.router.transports import ConfiguredLocalAgentTransport
from ai_engineering.supervisor.router.adapters import CodexAdapter
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.router.router import CrossAgentRouter, AuthorityResolver, PersistentStore
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.task_intent import deserialize_intent, intent_digest

from ai_engineering.supervisor.router.registry import AgentRegistry, AgentDefinition
from ai_engineering.supervisor.router.envelope import MessageType
from ai_engineering.supervisor.state import SupervisorState
from ai_engineering.contracts import EffectClass, StopBoundary
from ai_engineering.supervisor.policy.contracts import (
    WorkProfile,
    AutonomyLevel,
    ExecutionTarget,
    PromotionThresholds,
    BudgetLimits,
)
from ai_engineering.effective_policy import (
    EffectivePolicyReport,
    EffectivePolicyStatus,
    TaskPolicyAttribution,
)

from ai_engineering.supervisor.astra_provider import ConfiguredAstraProposalProvider
from ai_engineering.supervisor.ci_provider import (
    GitHubCIStatusProvider,
    REQUIRED_TECHNICAL_CHECKS,
)

class LocalCIProvider(CIStatusProvider):
    async def wait_for_ci(self, run_id: str, sha: str) -> bool:
        return True

async def async_main():
    parser = argparse.ArgumentParser(description="Hermes Autonomous Run CLI (Task 7.7)")
    parser.add_argument("--intent", required=True, help="Path to TaskIntent JSON file")
    parser.add_argument("--state-dir", required=True, help="Path to persistent state directory")
    parser.add_argument("--run-id", required=True, help="Unique Run ID")
    parser.add_argument("--worker-cmd", nargs='+', required=True, help="Worker command array (e.g. codex)")
    parser.add_argument("--astra-cmd", nargs='+', default=None, help="Command array for Astra proposal provider")
    parser.add_argument("--github-repository", default="life2boat/hermes", help="GitHub repository (owner/repo)")
    parser.add_argument("--provider-mode", choices=["real", "test"], default="real", help="Provider execution mode (real or test)")
    parser.add_argument("--ci-mode", choices=["local", "github"], default="local", help="CI provider mode")
    parser.add_argument("--ci-required-check", nargs='*', default=None, help="Required CI check names")
    parser.add_argument("--ci-timeout", type=float, default=600.0, help="CI wait timeout in seconds")
    parser.add_argument("--ci-poll-interval", type=float, default=10.0, help="CI poll interval in seconds")
    args = parser.parse_args()

    intent_text = Path(args.intent).read_text(encoding="utf-8")
    intent = deserialize_intent(intent_text)

    store = FileSupervisorStateStore(Path(args.state_dir))
    loop = SupervisorLoop(store)

    registry = AgentRegistry()
    registry.register(
        AgentDefinition("codex", ["code", "test"], [MessageType.WORK_REQUEST], [EffectClass.REPOSITORY_WRITE, EffectClass.READ_ONLY], 300),
        CodexAdapter(ConfiguredLocalAgentTransport(args.worker_cmd))
    )

    router_store = PersistentStore(args.state_dir)
    router = CrossAgentRouter(
        registry=registry,
        authority_resolver=AuthorityResolver(supervisor_store=store, supervisor_loop=loop),
        store=router_store
    )

    collector = ResultCollector()
    budget = BudgetConfig()

    from ai_engineering.supervisor.policy.work_profile import validate_work_profile
    raw_profile = {
        "schema_version": "hermes.work-profile.v1",
        "profile_id": "canonical-non-production",
        "profile_version": 1,
        "preferred_worker_model": None,
        "preferred_verifier_model": None,
        "preferred_supervisor_model": None,
        "escalation_model": None,
        "allowed_task_classes": [intent.task_class.value if hasattr(intent.task_class, "value") else str(intent.task_class)],
        "maximum_autonomy_level": AutonomyLevel.LEVEL_6_AUTHORIZED_PRODUCTION.value,
        "allowed_effect_classes": [EffectClass.READ_ONLY.value, EffectClass.REPOSITORY_WRITE.value],
        "forbidden_effect_classes": [],
        "allowed_targets": [ExecutionTarget.DEV.value],
        "required_validators": [],
        "promotion_thresholds": {
            "required_successful_runs": 1,
            "allowed_critical_failures": 0,
            "require_rollback_verified": False,
        },
        "budget_limits": {
            "max_supervisor_decisions": 20,
            "max_child_tasks": 10,
            "max_retries_per_task": 3,
            "max_fix_cycles_per_task": 3,
            "max_consecutive_failures": 2,
            "max_provider_calls": 50,
            "max_policy_denials": 10,
        },
        "production_execution_allowed": False,
        "vector_mutation_allowed": False,
        "secret_mutation_allowed": False,
        "external_send_allowed": False,
    }
    work_profile = validate_work_profile(raw_profile)

    tpa = TaskPolicyAttribution(
        task_id=intent.task_id,
        intent_revision=intent.intent_revision,
        intent_digest=intent_digest(intent),
        source_base_sha=intent.source_base_sha,
        constraints=intent.constraints,
        allowed_mutations=intent.allowed_mutations,
        forbidden_mutations=intent.forbidden_mutations,
        stop_boundary=intent.stop_boundary.value if hasattr(intent.stop_boundary, "value") else str(intent.stop_boundary),
        source_id="0" * 64,
    )
    effective_policy = EffectivePolicyReport(
        schema_version=1,
        effective_policy_id="0" * 64,
        task_id=intent.task_id,
        intent_digest=intent_digest(intent),
        intent_revision=intent.intent_revision,
        source_base_sha=intent.source_base_sha,
        subject_sha=intent.source_base_sha,
        status=EffectivePolicyStatus.COMPLETE,
        policy_sources=(),
        task_policy=tpa,
        invariant_resolutions=(),
        required_gate_resolutions=(),
        unresolved_references=(),
        precedence_source_id="0" * 64,
        authority_expansion=False,
    )

    # 1. Initialize run if it does not exist
    try:
        state = store.load_state(args.run_id)
    except Exception:
        state = loop.initialize_run(
            intent=intent,
            lineage=None,
            root_goal=intent.desired_outcome,
            root_goal_id=args.run_id,
            created_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )

    # 2. Bind canonical profile and policy before IMPLEMENT dispatch
    if state.autonomy_state is None:
        loop.bind_profile(args.run_id, work_profile, effective_policy)

    cap = intent.allowed_mutations[0] if intent.allowed_mutations else "code"
    eff_class = (
        EffectClass.REPOSITORY_WRITE.value
        if (
            EffectClass.REPOSITORY_WRITE.value in intent.allowed_mutations
            or EffectClass.REPOSITORY_WRITE in intent.allowed_mutations
            or "REPOSITORY_WRITE" in intent.allowed_mutations
        )
        else EffectClass.READ_ONLY.value
    )
    stop_b = (
        intent.stop_boundary.value
        if hasattr(intent.stop_boundary, "value")
        else str(intent.stop_boundary)
    )

    if args.provider_mode == "real":
        if not args.astra_cmd:
            sys.stderr.write("Error: ASTRA_PROVIDER_UNAVAILABLE: --astra-cmd is required in real provider mode\n")
            return 1
        if args.ci_mode != "github":
            sys.stderr.write("Error: CI_PROVIDER_UNAVAILABLE: --ci-mode github is required in real provider mode\n")
            return 1

    if args.astra_cmd:
        astra_provider: AstraProposalProvider = ConfiguredAstraProposalProvider(args.astra_cmd)
    else:
        astra_provider = ScriptedAstraProposalProvider(
            target_worker="codex",
            target_capability=cap,
            expected_effect_class=eff_class,
            expected_stop_boundary=stop_b,
        )

    if args.ci_mode == "github":
        req_checks = tuple(args.ci_required_check) if args.ci_required_check else REQUIRED_TECHNICAL_CHECKS
        ci_provider: CIStatusProvider = GitHubCIStatusProvider(
            repository=args.github_repository,
            required_checks=req_checks,
            timeout_seconds=args.ci_timeout,
            poll_interval_seconds=args.ci_poll_interval,
        )
    else:
        ci_provider = LocalCIProvider()

    coord = AutonomousRunCoordinator(
        loop=loop,
        store=store,
        router=router,
        result_collector=collector,
        budget=budget,
        run_id=args.run_id,
        astra_provider=astra_provider,
        ci_provider=ci_provider,
        evidence_root=args.state_dir,
    )

    receipt = await coord.run_until_terminal()
    print(f"Autonomous run terminal reason: {receipt.terminal_reason}")
    print(json.dumps(asdict(receipt), indent=2))
    return 0 if receipt.terminal_reason == "GOAL_COMPLETE" else 1

def main():
    sys.exit(asyncio.run(async_main()))

if __name__ == '__main__':
    main()
