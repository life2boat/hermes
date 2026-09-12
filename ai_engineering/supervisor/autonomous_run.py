import json
import hashlib
import uuid
import datetime
import asyncio
from enum import Enum
from dataclasses import dataclass, replace
from typing import Any, Mapping

from ai_engineering.supervisor.state import SupervisorState, SupervisorPhase, _fail
from ai_engineering.supervisor.loop import SupervisorLoop
from ai_engineering.supervisor.store import FileSupervisorStateStore
from ai_engineering.supervisor.decision import AstraDecision, SupervisorAction
from ai_engineering.supervisor.context_pack import ContextPack
from ai_engineering.supervisor.router.router import CrossAgentRouter, compute_policy_receipt_digest, AuthorityResolver
from ai_engineering.supervisor.router.envelope import AgentEnvelope, MessageType
from ai_engineering.supervisor.collector import ResultCollector
from ai_engineering.supervisor.worker_result import canonical_serialize_worker_result
from ai_engineering.contracts import GateResult, Status, EffectClass, StopBoundary
from ai_engineering.task_intent import TaskIntent

class NextActionType(str, Enum):
    INSPECT = "INSPECT"
    IMPLEMENT = "IMPLEMENT"
    VALIDATE = "VALIDATE"
    FIX = "FIX"
    RETRY = "RETRY"
    CREATE_PR = "CREATE_PR"
    WAIT_FOR_CI = "WAIT_FOR_CI"
    MERGE_IF_GREEN = "MERGE_IF_GREEN"
    STOP_SUCCESS = "STOP_SUCCESS"
    STOP_BLOCKED = "STOP_BLOCKED"

@dataclass(frozen=True, slots=True)
class AstraNextActionProposal:
    schema_version: str
    proposal_id: str
    run_id: str
    task_id: str
    action_type: NextActionType
    objective: str
    recommended_capability: str
    recommended_worker: str
    expected_effect_class: str
    expected_stop_boundary: str
    allowed_scope: tuple[str, ...]
    required_validators: tuple[str, ...]
    success_criteria: str
    reasoning_summary: str

@dataclass(frozen=True, slots=True)
class BudgetConfig:
    max_supervisor_decisions: int = 20
    max_child_tasks: int = 10
    max_retries: int = 3
    max_fix_cycles: int = 3
    max_consecutive_failures: int = 2
    max_provider_calls: int = 50

@dataclass(frozen=True, slots=True)
class AutonomousRunReceipt:
    schema_version: str
    run_id: str
    root_task_intent_digest: str
    initial_source_sha: str
    final_source_sha: str
    iterations: int
    child_tasks: int
    budgets: dict[str, int]
    policy_receipts: list[str]
    routing_receipts: list[str]
    verified_results: list[str]
    terminal_reason: str
    started_at: str
    completed_at: str
    receipt_digest: str = ""


class AstraProposalProvider:
    async def request_proposal(self, run_id: str, state: SupervisorState) -> AstraNextActionProposal:
        raise NotImplementedError

class CIStatusProvider:
    async def wait_for_ci(self, run_id: str, sha: str) -> bool:
        raise NotImplementedError

class AutonomousRunCoordinator:
    def __init__(
        self,
        loop: SupervisorLoop,
        store: FileSupervisorStateStore,
        router: CrossAgentRouter,
        result_collector: ResultCollector,
        budget: BudgetConfig,
        run_id: str,
        astra_provider: AstraProposalProvider,
        ci_provider: CIStatusProvider,
        evidence_root: str = "/tmp"
    ) -> None:
        self.loop = loop
        self.store = store
        self.router = router
        self.result_collector = result_collector
        self.budget = budget
        self.run_id = run_id
        self.evidence_root = evidence_root
        self.astra_provider = astra_provider
        self.ci_provider = ci_provider

        self.iterations = 0
        self.child_tasks = 0
        self.policy_receipts: list[str] = []
        self.routing_receipts: list[str] = []
        self.verified_results: list[str] = []

        # Test mock hook
        self._mock_astra_proposal: AstraNextActionProposal | None = None

    async def run_until_terminal(self) -> AutonomousRunReceipt:
        started_at = datetime.datetime.now(datetime.UTC).isoformat()
        state = self.store.load_state(self.run_id)

        if not state:
            from ai_engineering.supervisor.state import _fail
            _fail("RUN_NOT_INITIALIZED")

        initial_sha = state.current_base_sha
        terminal_reason = "UNKNOWN"

        from ai_engineering.supervisor.events import SupervisorEvent, SupervisorEventType, create_event
        events = self.store.load_events(self.run_id)
        ev = create_event(
            run_id=self.run_id,
            sequence=len(events) + 1,
            previous_event_digest=events[-1].event_digest if events else None,
            event_type=SupervisorEventType.PHASE_TRANSITIONED,
            state_revision=1,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            payload={"phase": "STARTED"},
            created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
        )
        self.store.save_event(ev)

        while True:
            if self.iterations >= self.budget.max_supervisor_decisions:
                terminal_reason = "BUDGET_EXHAUSTED"
                break
            if self.child_tasks >= self.budget.max_child_tasks:
                terminal_reason = "BUDGET_EXHAUSTED"
                break

            state = self.store.load_state(self.run_id)

            if state.phase in (SupervisorPhase.DONE, SupervisorPhase.CANCELLED, SupervisorPhase.FAILED, SupervisorPhase.BLOCKED):
                terminal_reason = state.phase.value
                break

            self.iterations += 1

            proposal = await self._request_proposal(state)

            if proposal.action_type == NextActionType.STOP_SUCCESS:
                if not state.latest_verified_result_id:
                    terminal_reason = "EVIDENCE_MISSING"
                    break
                terminal_reason = "GOAL_COMPLETE"
                break
            elif proposal.action_type == NextActionType.STOP_BLOCKED:
                terminal_reason = "POLICY_BLOCKED"
                break

            if proposal.action_type == NextActionType.WAIT_FOR_CI:
                success = await self.ci_provider.wait_for_ci(self.run_id, state.current_base_sha)
                if not success:
                    terminal_reason = "CI_FAILED"
                    break
                continue
            
            if proposal.action_type in (NextActionType.CREATE_PR, NextActionType.MERGE_IF_GREEN):
                events = self.store.load_events(self.run_id)
                ev = create_event(
                    run_id=self.run_id,
                    sequence=len(events) + 1,
                    previous_event_digest=events[-1].event_digest if events else None,
                    event_type=SupervisorEventType.PHASE_TRANSITIONED,
                    state_revision=1,
                    task_id=state.current_task_id,
                    attempt_id=state.current_attempt_id,
                    intent_digest=state.current_intent_digest,
                    payload={"action": proposal.action_type.value},
                    created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
                )
                self.store.save_event(ev)
                continue

            decision = self._convert_proposal_to_decision(proposal, state)

            try:
                decision_receipt, next_state = self.loop.accept_decision(
                    run_id=self.run_id,
                    decision=decision,
                    context_pack_digest=decision.context_pack_digest,
                    verified_result_status="FAIL" if proposal.action_type in (NextActionType.FIX, NextActionType.RETRY) else "PASS",
                    validated_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
                )
                events = self.store.load_events(self.run_id)
                print("DEBUG EVENTS:", [getattr(e.event_type, "value", str(e.event_type)) for e in events])
                policy_event = next(e for e in reversed(events) if getattr(e.event_type, "value", str(e.event_type)) == "POLICY_EVALUATED")
                pr_data = policy_event.payload["policy_receipt"]
                from ai_engineering.supervisor.policy.contracts import PolicyReceipt, PolicyVerdict
                pr_data_copy = dict(pr_data)
                pr_data_copy["verdict"] = PolicyVerdict(pr_data["verdict"])
                # Extract reason codes to tuple
                pr_data_copy["reason_codes"] = tuple(pr_data["reason_codes"])
                receipt = PolicyReceipt(**pr_data_copy)
                
                print("REASON CODES:", receipt.reason_codes)
                self.policy_receipts.append(receipt.receipt_id)
            except Exception as e:
                import traceback
                traceback.print_exc()
                terminal_reason = f"POLICY_DENIED: {e}"
                break

            envelope = AgentEnvelope(
                message_id=str(uuid.uuid4()),
                correlation_id=str(uuid.uuid4()),
                causation_id=str(uuid.uuid4()),
                run_id=self.run_id,
                task_id=next_state.current_task_id,
                attempt_id=next_state.current_attempt_id,
                sender_agent="supervisor",
                recipient_agent=proposal.recommended_worker,
                recipient_capability=proposal.recommended_capability,
                message_type=MessageType.WORK_REQUEST,
                payload={
                    "objective": proposal.objective,
                    "allowed_scope": proposal.allowed_scope,
                    "required_validators": proposal.required_validators,
                },
                payload_digest=__import__("ai_engineering.supervisor.router.envelope", fromlist=["compute_payload_digest"]).compute_payload_digest({
                    "objective": proposal.objective,
                    "allowed_scope": proposal.allowed_scope,
                    "required_validators": proposal.required_validators,
                }),
                task_intent_id=next_state.current_task_id,
                task_intent_digest=next_state.current_intent_digest,
                policy_receipt_id=receipt.receipt_id,
                policy_receipt_digest=compute_policy_receipt_digest(receipt),
                effect_class=EffectClass(proposal.expected_effect_class),
                stop_boundary=StopBoundary(proposal.expected_stop_boundary),
                created_at_utc=datetime.datetime.now(datetime.UTC).isoformat(),
                expires_at_utc=""
            )

            try:
                worker_bundle = await self.router.dispatch(envelope)
                self.routing_receipts.append(envelope.message_id)
                self.child_tasks += 1

                intent = self.loop._intents.get(next_state.current_task_id) or self.loop._intents[self.run_id]
                worker_bundle_json = canonical_serialize_worker_result(worker_bundle)
                normalized_evidence = self.result_collector.collect(worker_bundle_json, self.evidence_root, intent)

                from ai_engineering.supervisor.validator import validate_normalized_evidence, canonical_serialize_verified_result
                import hashlib
                vr = validate_normalized_evidence(normalized_evidence, intent)
                vr_digest = hashlib.sha256(canonical_serialize_verified_result(vr).encode("utf-8")).hexdigest()

                self.verified_results.append(vr.result_id)
                self.loop.ingest_verified_result(self.run_id, vr, vr_digest)

            except Exception as e:
                import traceback
                traceback.print_exc()
                terminal_reason = f"ROUTER_ERROR_{type(e).__name__}"
                break

        completed_at = datetime.datetime.now(datetime.UTC).isoformat()
        final_state = self.store.load_state(self.run_id)

        import json
        budget_dict = {
            "max_supervisor_decisions": self.budget.max_supervisor_decisions,
            "max_child_tasks": self.budget.max_child_tasks
        }
        
        receipt_obj = AutonomousRunReceipt(
            schema_version="hermes.autonomous-run-receipt.v1",
            run_id=self.run_id,
            root_task_intent_digest=final_state.current_intent_digest,
            initial_source_sha=initial_sha,
            final_source_sha=final_state.current_base_sha,
            iterations=self.iterations,
            child_tasks=self.child_tasks,
            budgets=budget_dict,
            policy_receipts=self.policy_receipts,
            routing_receipts=self.routing_receipts,
            verified_results=self.verified_results,
            terminal_reason=terminal_reason,
            started_at=started_at,
            completed_at=completed_at,
            receipt_digest=""
        )
        
        digest_str = json.dumps({
            "run_id": receipt_obj.run_id,
            "root_task_intent_digest": receipt_obj.root_task_intent_digest,
            "final_source_sha": receipt_obj.final_source_sha,
            "iterations": receipt_obj.iterations,
            "terminal_reason": receipt_obj.terminal_reason
        }, sort_keys=True)
        import hashlib
        h = hashlib.sha256(digest_str.encode()).hexdigest()
        
        import dataclasses
        receipt_obj = dataclasses.replace(receipt_obj, receipt_digest=h)
        return receipt_obj


    async def _request_proposal(self, state: SupervisorState) -> AstraNextActionProposal:
        if self._mock_astra_proposal:
            return self._mock_astra_proposal
        return await self.astra_provider.request_proposal(self.run_id, state)

    def _convert_proposal_to_decision(self, proposal: AstraNextActionProposal, state: SupervisorState) -> AstraDecision:
        action_map = {
            NextActionType.INSPECT: SupervisorAction.CONTINUE,
            NextActionType.IMPLEMENT: SupervisorAction.CONTINUE,
            NextActionType.VALIDATE: SupervisorAction.CONTINUE,
            NextActionType.FIX: SupervisorAction.FIX,
            NextActionType.RETRY: SupervisorAction.RETRY,
            NextActionType.CREATE_PR: SupervisorAction.CONTINUE,
            NextActionType.WAIT_FOR_CI: SupervisorAction.CONTINUE,
            NextActionType.MERGE_IF_GREEN: SupervisorAction.COMPLETE,
            NextActionType.STOP_SUCCESS: SupervisorAction.COMPLETE,
            NextActionType.STOP_BLOCKED: SupervisorAction.BLOCK,
        }

        intent = self.loop._intents.get(state.current_task_id) or self.loop._intents[self.run_id]
        pack, _ = self.loop.build_context_pack(self.run_id, intent)
        from ai_engineering.supervisor.context_pack import context_pack_digest
        pack_dg = context_pack_digest(pack)

        vr_digest = state.latest_verified_result_digest or ""
        if not vr_digest and state.latest_verified_result_id:
            vr_digest = self.loop._vr_cache.get(f"digest:{state.latest_verified_result_id}", "")

        return AstraDecision(
            schema_version="hermes.astra-decision.v1",
            decision_id=proposal.proposal_id,
            run_id=self.run_id,
            task_id=state.current_task_id,
            attempt_id=state.current_attempt_id,
            intent_digest=state.current_intent_digest,
            verified_result_id=state.latest_verified_result_id,
            verified_result_digest=vr_digest,
            context_pack_digest=pack_dg,
            action=action_map[proposal.action_type],
            rationale_summary=proposal.reasoning_summary,
            next_objective=proposal.objective,
            acceptance_delta=None,
            requested_required_gates=(),
            created_at_utc=datetime.datetime.now(datetime.UTC).isoformat()
        )
